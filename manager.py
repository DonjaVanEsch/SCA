#!/usr/bin/env python3
"""
PQC Image Manager

Manage and inspect generated Docker image contexts.

Usage:
  python manager.py --list                  [filters]
  python manager.py --build                 [filters]
  python manager.py --build --test --remove [filters]   # build → test → prune
  python manager.py --run                   [filters]

Actions:
  -l, --list                List matching image contexts
  -b, --build               Build Docker images for matching contexts
  -r, --run                 Run containers for matching contexts
  -T, --test                Test built images: start container, check / and /version endpoints,
                            stop container. Skips images that failed to build or are not present.
  -D, --remove              Remove Docker images after building/testing (docker rmi).
                            Also removes any stopped containers that reference matching images.
                            Use together with -b and -T for a build → test → prune workflow.
  -y, --yes                 Skip confirmation prompt for large sets

Filters:
  -L, --language LANG       Filter by language            (e.g. python)
  -v, --version VER         Filter by language version    (e.g. 3.9, 3.x, 3.1x)
  -f, --framework FW        Filter by framework name      (e.g. Flask, Django, FastAPI)
  -F, --framework-version V Filter by framework major     (e.g. 2, 3)
  -c, --library LIB         Filter by crypto library      (e.g. cryptography, PyNaCl)
  -C, --library-version V   Filter by library version     (e.g. 47.0, 1.x)

Version wildcard:
  Use 'x' in place of a version part to match all values at that position.
    3.x   -> any Python 3.x (3.9, 3.10, 3.11 ...)
    3.1x  -> any Python 3.1x (3.10, 3.11, 3.12 ...)
    1.x   -> any library 1.x (1.0, 1.5.0, 1.6.2 ...)
"""

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
IMAGES_BASE  = PROJECT_ROOT / "images"

# Ask for confirmation when the matched set exceeds this count.
_CONFIRM_AT = 5

# Column definitions: (header, dict-key, min-width)
_COLUMNS = [
    ("Language",     "language",  8),
    ("Version",      "lang_ver",  7),
    ("Framework",    "framework", 9),
    ("FW Ver",       "fw_ver",    6),
    ("Library",      "library",   12),
    ("Lib Ver",      "lib_ver",   7),
    ("Context path", "path",      12),
]


# ── Collection & filtering ────────────────────────────────────────────────────

def _version_matches(actual, pattern):
    """Return True if actual matches pattern.

    'x' acts as a trailing prefix wildcard:
      '3.x'  matches '3.9', '3.10' ...
      '3.1x' matches '3.10', '3.11' ...
      '1.x'  matches '1.0', '1.5.0' ...
    No 'x' means exact match.
    """
    if "x" in pattern:
        return actual.startswith(pattern[: pattern.index("x")])
    return actual == pattern


def _collect(base):
    """Walk the images tree; return one dict per Dockerfile found.

    The framework folder may contain a path separator (e.g. net/http stored as
    nested directories), so framework is reconstructed by joining all parts
    between lang_ver and fw_ver (the last three parts are always fw_ver,
    library, lib_ver).
    """
    entries = []
    for dockerfile in sorted(base.rglob("Dockerfile")):
        parts = dockerfile.parent.relative_to(base).parts
        if len(parts) < 6:
            continue
        language  = parts[0]
        lang_ver  = parts[1]
        lib_ver   = parts[-1]
        library   = parts[-2]
        fw_ver    = parts[-3]
        framework = "/".join(parts[2:-3])
        entries.append({
            "language":  language,
            "lang_ver":  lang_ver,
            "framework": framework,
            "fw_ver":    fw_ver,
            "library":   library,
            "lib_ver":   lib_ver,
            "path":      str(dockerfile.parent.relative_to(PROJECT_ROOT)),
        })
    return entries


def _filter(entries, args):
    def keep(e):
        if args.language         and e["language"].lower()  != args.language.lower():   return False
        if args.version          and not _version_matches(e["lang_ver"], args.version): return False
        if args.framework:
            fw_norm   = lambda s: s.lower().replace("/", "_")
            fw_filter = fw_norm(args.framework)
            fw_actual = fw_norm(e["framework"])
            if fw_actual != fw_filter and not fw_actual.startswith(fw_filter + "_"):
                return False
        if args.framework_version and not _version_matches(e["fw_ver"], args.framework_version): return False
        if args.library          and e["library"].lower()   != args.library.lower():    return False
        if args.library_version  and not _version_matches(e["lib_ver"], args.library_version): return False
        return True
    return [e for e in entries if keep(e)]


# ── Output helpers ────────────────────────────────────────────────────────────

def _print_table(entries):
    headers = [c[0] for c in _COLUMNS]
    keys    = [c[1] for c in _COLUMNS]
    widths  = [c[2] for c in _COLUMNS]

    for e in entries:
        for i, k in enumerate(keys):
            widths[i] = max(widths[i], len(e[k]))

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    hdr = "|" + "|".join(f" {h:<{widths[i]}} " for i, h in enumerate(headers)) + "|"

    print(sep)
    print(hdr)
    print(sep)
    for e in entries:
        print("|" + "|".join(f" {e[k]:<{widths[i]}} " for i, k in enumerate(keys)) + "|")
    print(sep)
    print(f"\n{len(entries):,} image context(s) listed.")


def _print_summary(entries):
    """Grouped overview — used when --list is given without filters."""
    from collections import defaultdict
    by_lang = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    for e in entries:
        by_lang[e["language"]][e["lang_ver"]][e["framework"]].add(e["library"])

    for lang, versions in sorted(by_lang.items()):
        print(f"\n{lang.upper()}")
        for ver, frameworks in sorted(versions.items()):
            print(f"  {ver}")
            for fw, libs in sorted(frameworks.items()):
                print(f"    {fw:10s}  libraries: {', '.join(sorted(libs))}")

    print(f"\nTotal: {len(entries):,} image contexts.")
    print("Add filters (--version, --framework, --library ...) to list individual contexts.")


# ── Docker helpers ────────────────────────────────────────────────────────────

def _image_tag(e):
    """Deterministic, Docker-legal image name derived from a context entry."""
    fw = e["framework"].lower().replace("/", "_")
    return (
        f"pqc-{e['language']}-{e['lang_ver']}"
        f"-{fw}-{e['fw_ver']}"
        f"-{e['library'].lower()}-{e['lib_ver']}"
    )


def _require_docker():
    if shutil.which("docker") is None:
        print("Error: 'docker' not found on PATH. Install Docker Desktop and try again.")
        sys.exit(1)


def _image_exists(tag):
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True,
    ).returncode == 0


def _get_host_port(container):
    """Return the host port Docker assigned to container's internal port 8000."""
    r = subprocess.run(
        ["docker", "port", container, "8000"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            if ":" in line:
                return line.rsplit(":", 1)[-1].strip()
    return "?"


def _confirm(verb, n, yes):
    """Ask for confirmation when n > _CONFIRM_AT (unless yes=True)."""
    if yes or n <= _CONFIRM_AT:
        return True
    try:
        answer = input(f"About to {verb} {n:,} image(s). Continue? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes")


# ── Build ─────────────────────────────────────────────────────────────────────

def _do_build(entries, no_cache=False, skip_existing=False):
    """Build a Docker image for every entry. Returns {tag: bool} results."""
    n = len(entries)
    pad = len(str(n))
    note = "  (--no-cache)" if no_cache else ""
    print(f"\nBuilding {n:,} image(s){note} ...\n")
    results = {}

    for i, e in enumerate(entries, 1):
        tag     = _image_tag(e)
        context = str(PROJECT_ROOT / e["path"])
        print(f"[{i:{pad}}/{n}] {tag}", flush=True)

        if skip_existing and _image_exists(tag):
            print(f"         SKIPPED  (image already exists)")
            results[tag] = True
            continue

        cmd = ["docker", "build", "-t", tag]
        if no_cache:
            cmd.append("--no-cache")
        cmd.append(context)

        t0   = time.monotonic()
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.monotonic() - t0

        if proc.returncode == 0:
            print(f"         OK  ({elapsed:.1f}s)")
            results[tag] = True
        else:
            print(f"         FAILED  ({elapsed:.1f}s)")
            output = (proc.stderr or proc.stdout or "").strip()
            for line in output.splitlines()[-15:]:
                print(f"         | {line}")
            results[tag] = False

    ok   = sum(1 for v in results.values() if v)
    fail = n - ok
    print(f"\nBuild complete: {ok} succeeded, {fail} failed.")
    return results


# ── Run ───────────────────────────────────────────────────────────────────────

def _do_run(entries, build_results=None):
    """Start a detached container for every entry.

    build_results: dict {tag: bool} from _do_build; entries whose build
    failed are automatically skipped.  Pass None to run without a preceding
    build step (images must already exist in that case).
    """
    n   = len(entries)
    pad = len(str(n))
    print(f"\nStarting {n:,} container(s) ...\n")

    started = []   # [(name, url)]
    failed  = []

    for i, e in enumerate(entries, 1):
        tag  = _image_tag(e)
        name = tag   # container name == image tag

        # Skip entries whose build step failed
        if build_results is not None and not build_results.get(tag):
            print(f"[{i:{pad}}/{n}] {name}")
            print(f"         SKIPPED  (build failed)")
            continue

        # Without a preceding build, verify the image exists
        if build_results is None and not _image_exists(tag):
            print(f"[{i:{pad}}/{n}] {name}")
            print(f"         NOT FOUND  (run --build first)")
            failed.append(name)
            continue

        # Remove any existing container with this name (stopped or running)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", "0:8000", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

        print(f"[{i:{pad}}/{n}] {name}", flush=True)

        if proc.returncode != 0:
            print(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-5:]:
                print(f"         | {line}")
            failed.append(name)
            continue

        port = _get_host_port(name)
        url  = f"http://localhost:{port}"
        print(f"         {url}")
        started.append((name, url))

    # ── Summary table ─────────────────────────────────────────────────────────
    if started:
        name_w = max(len(nm) for nm, _ in started)
        print(f"\nRunning ({len(started)}):")
        print(f"  {'Container':<{name_w}}  URL")
        print(f"  {'-' * name_w}  ---")
        for nm, url in started:
            print(f"  {nm:<{name_w}}  {url}")

    if failed:
        print(f"\nFailed to start ({len(failed)}): {', '.join(failed)}")


# ── Stop ──────────────────────────────────────────────────────────────────────

def _do_stop(entries):
    """Stop and remove the container for every entry."""
    n   = len(entries)
    pad = len(str(n))
    print(f"\nStopping {n:,} container(s) ...\n")

    stopped = []
    not_running = []
    failed  = []

    for i, e in enumerate(entries, 1):
        name = _image_tag(e)
        print(f"[{i:{pad}}/{n}] {name}", flush=True)

        proc = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

        if proc.returncode == 0:
            print(f"         STOPPED")
            stopped.append(name)
        else:
            err = proc.stderr.strip()
            if "No such container" in err:
                print(f"         NOT RUNNING")
                not_running.append(name)
            else:
                print(f"         FAILED")
                for line in err.splitlines()[-3:]:
                    print(f"         | {line}")
                failed.append(name)

    parts = []
    if stopped:
        parts.append(f"{len(stopped)} stopped")
    if not_running:
        parts.append(f"{len(not_running)} not running")
    if failed:
        parts.append(f"{len(failed)} failed")
    print(f"\nDone: {', '.join(parts)}.")


def _do_stop_all():
    """Stop and remove every container whose name starts with 'pqc-'."""
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=pqc-", "--format", "{{.Names}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    names = [n.strip() for n in proc.stdout.splitlines()
             if n.strip().startswith("pqc-")]

    if not names:
        print("No PQC containers are running.")
        return

    n   = len(names)
    pad = len(str(n))
    print(f"\nStopping all {n:,} PQC container(s) ...\n")

    stopped = 0
    for i, name in enumerate(names, 1):
        print(f"[{i:{pad}}/{n}] {name}", flush=True)
        r = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            print(f"         STOPPED")
            stopped += 1
        else:
            print(f"         FAILED")
            for line in r.stderr.strip().splitlines()[-3:]:
                print(f"         | {line}")

    print(f"\nDone: {stopped}/{n} stopped.")


# ── Test ─────────────────────────────────────────────────────────────────────

def _do_test(entries, build_results=None):
    """Start each container, test / and /version, stop it. Returns {tag: bool}."""
    n   = len(entries)
    pad = len(str(n))
    print(f"\nTesting {n:,} image(s) ...\n")
    results = {}

    for i, e in enumerate(entries, 1):
        tag  = _image_tag(e)
        name = tag
        print(f"[{i:{pad}}/{n}] {tag}", flush=True)

        if build_results is not None and not build_results.get(tag):
            print(f"         SKIP  (build failed)")
            results[tag] = False
            continue

        if not _image_exists(tag):
            print(f"         SKIP  (not built)")
            results[tag] = False
            continue

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", "0:8000", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            print(f"         FAIL  (container did not start)")
            for line in proc.stderr.strip().splitlines()[-3:]:
                print(f"         | {line}")
            results[tag] = False
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            continue

        port = _get_host_port(name)
        ok   = True
        fail_reason = ""

        for path, check in [("/", lambda d: d.get("message") == "Hello World"),
                             ("/version", lambda d: isinstance(d, dict) and len(d) > 0)]:
            passed = False
            for _ in range(20):
                try:
                    with urllib.request.urlopen(
                        f"http://localhost:{port}{path}", timeout=2
                    ) as r:
                        data = json.loads(r.read().decode())
                        if check(data):
                            passed = True
                    break
                except Exception:
                    time.sleep(0.5)
            if not passed:
                ok = False
                fail_reason = path
                break

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        if ok:
            print(f"         PASS")
        else:
            print(f"         FAIL  ({fail_reason} did not respond correctly)")
        results[tag] = ok

    passed = sum(1 for v in results.values() if v)
    failed = len(results) - passed
    print(f"\nTest complete: {passed} passed, {failed} failed.")
    return results


# ── Remove images ─────────────────────────────────────────────────────────────

def _remove_stopped_containers(tag: str) -> int:
    """Remove all stopped containers that reference *tag*. Returns count removed."""
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"ancestor={tag}",
         "--filter", "status=exited", "--filter", "status=created",
         "--format", "{{.ID}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    ids = proc.stdout.strip().splitlines()
    if not ids:
        return 0
    subprocess.run(["docker", "rm"] + ids, capture_output=True)
    return len(ids)


def _do_remove(entries):
    """Remove stopped containers and the Docker image for every entry (docker rmi)."""
    n   = len(entries)
    pad = len(str(n))
    print(f"\nRemoving {n:,} image(s) ...\n")

    removed = 0
    for i, e in enumerate(entries, 1):
        tag = _image_tag(e)
        print(f"[{i:{pad}}/{n}] {tag}", flush=True)

        if not _image_exists(tag):
            print(f"         NOT FOUND")
            continue

        n_containers = _remove_stopped_containers(tag)
        if n_containers:
            print(f"         CONTAINERS  ({n_containers} stopped container(s) removed)")

        proc = subprocess.run(
            ["docker", "rmi", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            print(f"         REMOVED")
            removed += 1
        else:
            print(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-3:]:
                print(f"         | {line}")

    print(f"\nDone: {removed}/{n} removed.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser():
    p = argparse.ArgumentParser(
        prog="manager.py",
        description="PQC Image Manager — list, build, and run Docker image contexts.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python manager.py --list
  python manager.py --list --language python --version 3.x
  python manager.py -l -v 3.11 -f Flask -F 3

  python manager.py --build -v 3.11 -f Flask -F 3 -c cryptography -C 47.0
  python manager.py --run   -v 3.11 -f Flask -F 3 -c cryptography -C 47.0
  python manager.py --build --run -v 3.11 -c cryptography -C 47.0

  python manager.py -b -r -v 3.11 -c PyNaCl -C 1.6.2 --yes
  python manager.py -b -r -v 3.9 -f FastAPI -c cryptography -C 4x
        """,
    )

    # ── Actions ───────────────────────────────────────────────────────────────
    actions = p.add_argument_group("actions")
    actions.add_argument(
        "-l", "--list",
        action="store_true",
        help="List matching image contexts",
    )
    actions.add_argument(
        "-b", "--build",
        action="store_true",
        help="Build Docker images for matching contexts",
    )
    actions.add_argument(
        "-r", "--run",
        action="store_true",
        help="Start containers for matching contexts (images must be built first, "
             "or combine with --build)",
    )
    actions.add_argument(
        "-n", "--no-cache",
        action="store_true",
        dest="no_cache",
        help="Pass --no-cache to docker build, ignoring all cached layers",
    )
    actions.add_argument(
        "-s", "--skip-existing",
        action="store_true",
        dest="skip_existing",
        help="Skip build if the Docker image already exists",
    )
    actions.add_argument(
        "-T", "--test",
        action="store_true",
        help="Start each container, test / and /version endpoints, then stop it",
    )
    actions.add_argument(
        "-D", "--remove",
        action="store_true",
        dest="remove",
        help="Remove Docker images for matching contexts (docker rmi)",
    )
    actions.add_argument(
        "-S", "--stop",
        action="store_true",
        help="Stop and remove containers for matching contexts",
    )
    actions.add_argument(
        "-A", "--stop-all",
        action="store_true",
        dest="stop_all",
        help="Stop and remove ALL pqc-* containers (ignores filters)",
    )
    actions.add_argument(
        "-y", "--yes",
        action="store_true",
        help=f"Skip confirmation when matched set exceeds {_CONFIRM_AT} contexts",
    )

    # ── Filters ───────────────────────────────────────────────────────────────
    filters = p.add_argument_group("filters")
    filters.add_argument("-L", "--language",          metavar="LANG",  help="Language name          (e.g. python)")
    filters.add_argument("-v", "--version",           metavar="VER",   help="Language version       (e.g. 3.9, 3.x, 3.1x)")
    filters.add_argument("-f", "--framework",         metavar="FW",    help="Framework name         (e.g. Flask, Django, FastAPI)")
    filters.add_argument("-F", "--framework-version", metavar="FWVER", dest="framework_version",
                         help="Framework major version (e.g. 2, 3)")
    filters.add_argument("-c", "--library",           metavar="LIB",   help="Crypto library name    (e.g. cryptography, PyNaCl, M2Crypto)")
    filters.add_argument("-C", "--library-version",   metavar="LIBVER",dest="library_version",
                         help="Library version         (e.g. 47.0, 1.x, 3.2x)")

    return p


def main():
    # Reconfigure stdout/stderr to replace unencodable characters (e.g. Docker
    # progress symbols on Windows cp1252 terminals) instead of raising an error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = _build_parser()
    args   = parser.parse_args()

    if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all):
        parser.print_help()
        sys.exit(0)

    # ── Stop-all is independent of the images tree ────────────────────────────
    if args.stop_all:
        _require_docker()
        _do_stop_all()
        if not (args.list or args.build or args.run or args.stop):
            sys.exit(0)

    # ── Load contexts ─────────────────────────────────────────────────────────
    if not IMAGES_BASE.exists():
        print(f"Error: images directory not found at '{IMAGES_BASE}'.")
        print("Run 'python scripts/generate_images.py' first.")
        sys.exit(1)

    all_entries = _collect(IMAGES_BASE)
    if not all_entries:
        print("No image contexts found. Run 'python scripts/generate_images.py' first.")
        sys.exit(1)

    has_filter = any([
        args.language, args.version, args.framework,
        args.framework_version, args.library, args.library_version,
    ])
    entries = _filter(all_entries, args)

    if not entries:
        print("No image contexts match the given filters.")
        sys.exit(0)

    # ── List ──────────────────────────────────────────────────────────────────
    if args.list:
        if has_filter:
            _print_table(entries)
        else:
            _print_summary(entries)

    # ── Build ─────────────────────────────────────────────────────────────────
    build_results = None
    if args.build:
        _require_docker()
        if not _confirm("build", len(entries), args.yes):
            print("Aborted.")
            sys.exit(0)
        build_results = _do_build(entries, no_cache=args.no_cache, skip_existing=args.skip_existing)

    # ── Run ───────────────────────────────────────────────────────────────────
    if args.run:
        _require_docker()
        # Only ask to confirm again when --run is used standalone (not after --build)
        if build_results is None and not _confirm("run", len(entries), args.yes):
            print("Aborted.")
            sys.exit(0)
        _do_run(entries, build_results)

    # ── Test ──────────────────────────────────────────────────────────────────
    if args.test:
        _require_docker()
        _do_test(entries, build_results)

    # ── Remove images ─────────────────────────────────────────────────────────
    if args.remove:
        _require_docker()
        if not _confirm("remove", len(entries), args.yes):
            print("Aborted.")
            sys.exit(0)
        _do_remove(entries)

    # ── Stop ──────────────────────────────────────────────────────────────────
    if args.stop:
        _require_docker()
        if not _confirm("stop", len(entries), args.yes):
            print("Aborted.")
            sys.exit(0)
        _do_stop(entries)


if __name__ == "__main__":
    main()
