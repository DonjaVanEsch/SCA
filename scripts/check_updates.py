"""
Periodic update-availability scanner.

For every framework/library tracked across all 7 language registries,
queries the real upstream package registry (npm / PyPI / Packagist /
Maven Central / NuGet / crates.io / RubyGems) for its full release history and flags any major
version that exists upstream but isn't yet tracked as a registry.json
bucket ("nr"). Purely a detection/notification layer -- it never builds,
tests, or edits a registry itself; a human reviews the result and decides
whether/when to add a bucket and verify it works (the same standard this
project's registries already hold every other entry to).

Scope: framework/library version buckets only, not new *language* major
releases (those follow known, separate release calendars per language and
would need a different data source than a package registry).

Usage:
    python scripts/check_updates.py                 # check all languages, save to DB
    python scripts/check_updates.py --lang node      # check one language only
    python scripts/check_updates.py --dry-run        # print only, don't touch the DB
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

import db  # noqa: E402

_REGISTRY_FILES = {
    "python": "registry python.json",
    "php":    "registry php.json",
    "node":   "registry node.json",
    "java":   "registry java.json",
    "dotnet": "registry dotnet.json",
    "rust":   "registry rust.json",
    "ruby":   "registry ruby.json",
}

# How a framework/library's own registry.json "module" value marks "no real
# package to check" -- Node spells this "built-in", every other language
# uses JSON null.
_BUILTIN_MARKERS = {None, "", "built-in"}


def _major_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))
    except ValueError:
        return (0,)


def _dominant_depth(nrs: list) -> int:
    """Most common dot-depth among already-tracked 'nr' strings (e.g. '1.72'
    has depth 2, '18' has depth 1) -- used so upstream versions get grouped
    into "majors" the same way this specific bucket already does, rather
    than assuming every framework/library uses single-integer majors."""
    depths = [len(re.findall(r"\d+", nr)) for nr in nrs if re.match(r"^[\d.]+$", nr)]
    if not depths:
        return 1
    return max(set(depths), key=depths.count)


def _extract_major(version: str, depth: int) -> str:
    parts = re.findall(r"\d+", version.split("-")[0])
    return ".".join(parts[:depth]) if parts else version


def _tracked_majors(entry: dict | None) -> list:
    if not entry:
        return []
    versions = entry.get("version")
    if not isinstance(versions, list):
        return []
    return [v["nr"] for v in versions if isinstance(v, dict) and "nr" in v]


# ── Per-language name/package-id enumeration ─────────────────────────────────
# Each returns a list of (kind, name, (fetch_kind, package_id)) tuples.

def _enumerate_registry_module(lang_data: dict, fetch_kind: str) -> list:
    """Shared enumerator for languages whose registry.json 'module' field is
    directly the package identifier to query (Node/PHP/Java/.NET) -- Java's
    module is 'group:artifact', split apart in _fetch() below."""
    items = []
    for section, kind in ((lang_data.get("frameworks", []), "framework"),
                          (lang_data.get("cryptography_libs", []), "library")):
        for entry in section:
            mod = entry.get("module")
            if mod in _BUILTIN_MARKERS:
                continue
            items.append((kind, entry["name"], (fetch_kind, mod)))
    return items


def _enumerate_python(lang_data: dict) -> list:
    """Python is the one language whose framework package names live in a
    code-level dict (FW_PIP), not registry.json's own 'module' field -- it
    pins an exact version RANGE string per (name, major) like
    'Flask>=1.0,<2.0' rather than resolving 'latest patch' live like every
    other language, so the package name has to be parsed out of that pin."""
    import lang_python

    items = []
    seen_fw = set()
    for (fw_name, _major), pin in lang_python.FW_PIP.items():
        if fw_name in seen_fw:
            continue
        seen_fw.add(fw_name)
        m = re.match(r"^([A-Za-z0-9_.\-\[\]]+?)\s*[><=~!]", pin)
        if m:
            items.append(("framework", fw_name, ("pypi", m.group(1))))

    for lib in lang_data.get("cryptography_libs", []):
        meta = lang_python.LIB_META.get(lib["name"])
        pip = meta.get("pip") if meta else None
        if pip:
            items.append(("library", lib["name"], ("pypi", pip)))
    return items


_ENUMERATORS = {
    "node":   lambda d: _enumerate_registry_module(d, "npm"),
    "php":    lambda d: _enumerate_registry_module(d, "packagist"),
    "java":   lambda d: _enumerate_registry_module(d, "maven"),
    "dotnet": lambda d: _enumerate_registry_module(d, "nuget"),
    "rust":   lambda d: _enumerate_registry_module(d, "crates"),
    "ruby":   lambda d: _enumerate_registry_module(d, "rubygems"),
    "python": _enumerate_python,
}


# ── Fetchers (thin wrappers reusing each language module's own HTTP calls,
#    the exact same functions generate_images.py itself relies on to resolve
#    a bucket to "latest patch" -- no separate/duplicate registry client) ───

# A brief real-network blip used to silently sink an entire scheduled scan:
# confirmed live that 2 consecutive weekly cron runs on the actual server
# failed EVERY single package lookup with "Temporary failure in name
# resolution" (DNS), while the identical script run interactively minutes
# later succeeded cleanly -- a one-off local-network hiccup, not a cron-
# environment/DNS-config bug (resolv.conf, nsswitch, PATH all checked and
# fine). Each lookup was a single attempt with no retry, so one bad moment
# for one package -> logged as a [WARN] and skipped -- and the whole run
# still exits 0 reporting "0 new version(s) found", indistinguishable from
# a real "nothing new" result. Retrying a few times with a short delay is
# cheap insurance against exactly that; it does NOT touch the per-language
# modules' own resolution functions (used far more heavily by
# generate_images.py's real build-time resolution), only this scanner's use
# of them.
_FETCH_RETRIES = 3
_FETCH_RETRY_DELAY = 5  # seconds between attempts

# How often check_language() prints a "still working" progress line -- see
# its own comment. Not per-package (too noisy for a healthy, fast run) and
# not silent for minutes at a time either (the original complaint: a slow
# or stalled-looking run gives no indication anything is happening).
_PROGRESS_INTERVAL = 8  # seconds

_KNOWN_FETCH_KINDS = {"npm", "pypi", "packagist", "maven", "nuget", "crates", "rubygems"}


def _fetch_once(fetch_kind: str, package_id: str) -> list:
    if fetch_kind == "npm":
        import lang_node
        return lang_node._fetch_releases(package_id)
    if fetch_kind == "pypi":
        import lang_python
        return lang_python._fetch_releases(package_id)
    if fetch_kind == "packagist":
        import lang_php
        return lang_php._fetch_packagist_versions(package_id)
    if fetch_kind == "maven":
        import lang_java
        group, artifact = package_id.split(":", 1)
        return lang_java._fetch_maven_versions(group, artifact)
    if fetch_kind == "nuget":
        import lang_dotnet
        return lang_dotnet._fetch_nuget_versions(package_id)
    if fetch_kind == "crates":
        import lang_rust
        return lang_rust._fetch_crates_versions(package_id)
    if fetch_kind == "rubygems":
        import lang_ruby
        return lang_ruby._fetch_gem_versions(package_id)
    raise ValueError(f"unknown fetch kind: {fetch_kind}")


def _fetch(fetch_kind: str, package_id: str) -> list:
    """Retries a transient failure up to _FETCH_RETRIES times (see the note
    above) before re-raising for check_language()'s own per-package
    [WARN]+skip handling. An unknown fetch_kind is a programming error, not
    a network blip -- raised immediately, never retried."""
    if fetch_kind not in _KNOWN_FETCH_KINDS:
        raise ValueError(f"unknown fetch kind: {fetch_kind}")
    last_exc = None
    for attempt in range(_FETCH_RETRIES):
        try:
            return _fetch_once(fetch_kind, package_id)
        except Exception as exc:
            last_exc = exc
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_RETRY_DELAY)
    raise last_exc


def _fetch_date_once(fetch_kind: str, package_id: str, version: str) -> str | None:
    if fetch_kind == "npm":
        import lang_node
        return lang_node._release_date(package_id, version)
    if fetch_kind == "pypi":
        import lang_python
        return lang_python._release_date(package_id, version)
    if fetch_kind == "packagist":
        import lang_php
        return lang_php._release_date(package_id, version)
    if fetch_kind == "maven":
        import lang_java
        group, artifact = package_id.split(":", 1)
        return lang_java._release_date(group, artifact, version)
    if fetch_kind == "nuget":
        import lang_dotnet
        return lang_dotnet._release_date(package_id, version)
    if fetch_kind == "crates":
        import lang_rust
        return lang_rust._release_date(package_id, version)
    if fetch_kind == "rubygems":
        import lang_ruby
        return lang_ruby._release_date(package_id, version)
    return None


def _fetch_date(fetch_kind: str, package_id: str, version: str) -> str | None:
    """release_date for one already-resolved version -- a newly detected
    major's winning release, not the whole history. Best-effort: PyPI/npm/
    Packagist read it straight out of the same response _fetch() already
    cached; Maven/NuGet need one small supplementary request (see each
    module's own _release_date). Retries transient failures the same way
    _fetch() does; any failure still standing after that just means the
    bucket gets written with release_date=None, same as before this
    existed."""
    last_exc = None
    for attempt in range(_FETCH_RETRIES):
        try:
            return _fetch_date_once(fetch_kind, package_id, version)
        except Exception as exc:
            last_exc = exc
            if attempt < _FETCH_RETRIES - 1:
                time.sleep(_FETCH_RETRY_DELAY)
    print(f"  [WARN] release-date lookup failed for {package_id} {version} "
          f"after {_FETCH_RETRIES} attempts: {last_exc}", flush=True)
    return None


def check_language(lang_id: str) -> list:
    """Returns a list of update dicts for one language -- one dict per
    (framework/library, newly-found major) pair not yet in registry.json."""
    registry_path = SCRIPT_DIR / _REGISTRY_FILES[lang_id]
    with open(registry_path, encoding="utf-8") as f:
        data = json.load(f)
    lang_data = data["languages"][0]

    entries_by_name = {}
    for section in (lang_data.get("frameworks", []), lang_data.get("cryptography_libs", [])):
        for entry in section:
            entries_by_name[entry["name"]] = entry

    items = _ENUMERATORS[lang_id](lang_data)
    total = len(items)
    print(f"Checking {lang_id} ({total} frameworks/libraries) ...", flush=True)

    results = []
    last_progress = time.monotonic()
    for i, (kind, name, (fetch_kind, package_id)) in enumerate(items, 1):
        tracked = _tracked_majors(entries_by_name.get(name))
        depth = _dominant_depth(tracked)

        try:
            releases = _fetch(fetch_kind, package_id)
        except Exception as exc:
            print(f"  [WARN] {lang_id}/{name}: fetch failed after {_FETCH_RETRIES} attempts ({exc})", flush=True)
            releases = None

        # A periodic "still working" heartbeat, not a per-package line --
        # only prints if _PROGRESS_INTERVAL seconds have actually passed
        # since the last one, so a fast/healthy run (a handful of seconds
        # per language) stays silent, while a slow one (retries kicking in
        # under a real network blip) doesn't look hung for minutes at a
        # stretch with zero output.
        now = time.monotonic()
        if now - last_progress >= _PROGRESS_INTERVAL:
            print(f"  ... {lang_id}: {i}/{total} checked so far", flush=True)
            last_progress = now

        if not releases:
            continue

        # releases is sorted ascending -- last write per major wins, giving
        # the highest real version within that major group.
        upstream_majors = {}
        for v in releases:
            upstream_majors[_extract_major(v, depth)] = v

        # Only majors *beyond* the current ceiling count as "new" -- an
        # untracked major that falls BELOW or BETWEEN what's already tracked
        # is a deliberate historical gap (e.g. this project's density-
        # expansion passes often skip intermediate minors on purpose), not a
        # future release to flag. Comparing against every tracked major
        # individually (`not in tracked_set`) would surface those gaps as
        # false "updates" -- confirmed by a real dry-run that did exactly
        # that for e.g. @noble/curves's already-known 0.2-0.9 gap.
        if tracked:
            ceiling = max((_major_key(t) for t in tracked), default=(0,))
            new_majors = sorted(
                (m for m in upstream_majors if _major_key(m) > ceiling),
                key=_major_key,
            )
        else:
            # Nothing tracked yet at all -- every real release is "new".
            new_majors = sorted(upstream_majors, key=_major_key)
        for maj in new_majors:
            latest_version = upstream_majors[maj]
            results.append({
                "language": lang_id,
                "kind": kind,
                "name": name,
                "package_id": package_id,
                "new_major": maj,
                "latest_version": latest_version,
                "tracked_majors": sorted(tracked, key=_major_key),
                "release_date": _fetch_date(fetch_kind, package_id, latest_version),
            })
    return results


def check_all(languages: list | None = None) -> list:
    languages = languages or list(_REGISTRY_FILES)
    all_results = []
    for lang_id in languages:
        results = check_language(lang_id)
        print(f"  {len(results)} new version(s) found", flush=True)
        all_results.extend(results)
    return all_results


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=list(_REGISTRY_FILES),
                        help="Check only this language (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results, don't write to the database")
    args = parser.parse_args()

    languages = [args.lang] if args.lang else None
    results = check_all(languages)

    for r in results:
        print(f"  NEW: {r['language']}/{r['kind']} {r['name']} -> major {r['new_major']} "
              f"(latest {r['latest_version']}, currently tracking {r['tracked_majors']})")

    if args.dry_run:
        print(f"\n[dry-run] {len(results)} update(s) found, not saved.")
        return

    db.init_db()
    for r in results:
        db.save_pending_update(**r)
    print(f"\nSaved {len(results)} pending update(s) to the database "
          f"({db.count_pending_updates()} total not yet dismissed).")


if __name__ == "__main__":
    main()
