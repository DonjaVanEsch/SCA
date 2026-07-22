#!/usr/bin/env python3
"""
PQC Image Manager

Manage and inspect generated Docker image contexts.

Usage:
  python manager.py --list                  [filters]
  python manager.py --build                 [filters]
  python manager.py --build --test --remove [filters]   # build → test → prune
  python manager.py --run                   [filters]
  python manager.py --cleanup
  python manager.py --cleanup-full
  python manager.py --cleanup-dry-run

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

Docker cleanup (independent of image filters):
      --cleanup             Remove stopped containers, dangling images, and unused
                            networks. Never touches build cache -- see
                            --prune-build-cache below.
      --cleanup-full        Same as --cleanup, plus all unused images and volumes.
      --cleanup-dry-run     Preview what would be removed (no changes made).
      --prune-build-cache   Remove Docker's ENTIRE build cache, including the shared
                            Maven/npm/pip/Composer/NuGet package caches. Kept
                            separate from --cleanup on purpose: this brings back
                            registry rate-limiting risk (e.g. Maven Central 429s).
                            Only use for disk pressure or a suspected corrupt entry.
      --prune-build-cache-dry-run
                            Preview current build cache disk usage (no changes made).

Filters:
  -L, --language LANG       Filter by language            (e.g. python)
  -v, --version VER         Filter by language version    (e.g. 3.9, 3.x, 3.1x)
  -f, --framework FW        Filter by framework name      (e.g. Flask, Django, FastAPI)
  -F, --framework-version V Filter by framework major     (e.g. 2, 3)
  -c, --library LIB         Filter by crypto library      (e.g. cryptography, PyNaCl)
  -C, --library-version V   Filter by library version     (e.g. 47.0, 1.x)

List / ignore files:
  --image-list FILE         Path to a text file with one context path per line.
                            Only images whose path appears in the file are included.
  --ignore-list FILE        Path to a text file with context paths or image tags to skip.

Version wildcard:
  Use 'x' in place of a version part to match all values at that position.
    3.x   -> any Python 3.x (3.9, 3.10, 3.11 ...)
    3.1x  -> any Python 3.1x (3.10, 3.11, 3.12 ...)
    1.x   -> any library 1.x (1.0, 1.5.0, 1.6.2 ...)
"""

import argparse
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import net_signal

PROJECT_ROOT       = Path(__file__).parent
IMAGES_BASE        = PROJECT_ROOT / "images"
CLIENT_IMAGES_BASE = PROJECT_ROOT / "images_clients"

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


def _load_path_set(filepath: str) -> set:
    """Read a text file; return a set of normalised path strings (forward slashes)."""
    p = Path(filepath)
    if not p.exists():
        print(f"Error: file not found: {filepath}")
        sys.exit(1)
    lines = p.read_text(encoding="utf-8").splitlines()
    return {line.strip().replace("\\", "/") for line in lines if line.strip()}


def _filter_by_list(entries: list, path_set: set) -> list:
    """Keep only entries whose relative context path appears in path_set."""
    return [e for e in entries if e["path"].replace("\\", "/") in path_set]


def _filter_by_ignore(entries: list, ignore_set: set) -> list:
    """Remove entries whose path or image tag appears in ignore_set."""
    return [
        e for e in entries
        if e["path"].replace("\\", "/") not in ignore_set
        and _image_tag(e) not in ignore_set
    ]


def _collect(base):
    """Walk the images tree; return one dict per Dockerfile found.

    The framework folder may contain a path separator (e.g. net/http stored as
    nested directories), so framework is reconstructed by joining all parts
    between lang_ver and fw_ver (the last three parts are normally fw_ver,
    library, lib_ver) -- UNLESS the library itself is a scoped npm package
    name (e.g. "@noble/curves"), which write_context() passes straight into
    a Path() join, and pathlib silently splits its embedded "/" into an
    EXTRA real directory level on disk. Detected here by the segment that
    would otherwise be fw_ver starting with "@" -- a real fw_ver is always a
    plain version string and never starts with "@" -- in which case the
    library spans that segment plus the next one, and everything shifts
    left by one. Found via a false "orphaned image" report for every
    @noble/curves and @noble/post-quantum combo (a real, valid, built image
    whose re-derived tag never matched anything in expected_tags because of
    this exact mis-slice).
    """
    entries = []
    for dockerfile in sorted(base.rglob("Dockerfile")):
        parts = dockerfile.parent.relative_to(base).parts
        if len(parts) < 6:
            continue
        language  = parts[0]
        lang_ver  = parts[1]
        lib_ver   = parts[-1]
        if len(parts) >= 7 and parts[-3].startswith("@"):
            library   = f"{parts[-3]}/{parts[-2]}"
            fw_ver    = parts[-4]
            framework = "/".join(parts[2:-4])
        else:
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


# ── Per-language worker caps ──────────────────────────────────────────────────
# Some languages' own build step hits a shared upstream package registry much
# harder per-image than others (a `mvn package` inside every Java build does
# its own full dependency resolution against Maven Central, independent of
# every other concurrent build -- there's no shared cache between builds).
# Confirmed live (2026-07-20): a batch with the configured default_workers
# (8) triggered a sustained, IP-wide Maven Central rate-limit (HTTP 429 on
# EVERY coordinate) that was still in effect over an hour later. Capping
# applies regardless of what default_workers is set to -- the configured
# value can stay high for other languages, Java just never exceeds this.
_LANGUAGE_WORKER_CAP = {
    "java": 4,
}


def _capped_workers(entries: list, requested: int) -> tuple[int, str]:
    """Returns (effective_workers, note) -- note is "" when no cap applied,
    otherwise a short explanation to fold into the existing parallel-count
    log line so the cap is visible, not silent."""
    langs = {e.get("language") for e in entries if e.get("language")}
    caps = [_LANGUAGE_WORKER_CAP[l] for l in langs if l in _LANGUAGE_WORKER_CAP]
    if not caps:
        return requested, ""
    cap = min(caps)
    if requested <= cap:
        return requested, ""
    capped_langs = ", ".join(sorted(l for l in langs if l in _LANGUAGE_WORKER_CAP))
    return cap, f"capped to {cap} for {capped_langs} (requested {requested})"


# ── Docker helpers ────────────────────────────────────────────────────────────

def _image_tag(e):
    """Deterministic, Docker-legal image name derived from a context entry."""
    fw  = e["framework"].lower().replace("/", "_").replace("@", "").replace(" ", "")
    lib = e["library"].lower().replace("/", "_").replace("@", "").replace(" ", "")
    return (
        f"pqc-{e['language']}-{e['lang_ver']}"
        f"-{fw}-{e['fw_ver']}"
        f"-{lib}-{e['lib_ver']}"
    )


def _docker_target_host() -> str:
    """The address a published container port is actually reachable on.

    Published ports bind on whichever machine dockerd runs on -- for a local
    engine that's this machine (localhost); for a remote engine (DOCKER_HOST=
    ssh://user@host) it's that remote host, never localhost.
    """
    docker_host = os.environ.get("DOCKER_HOST", "")
    if not docker_host:
        return "localhost"
    parsed = urllib.parse.urlparse(docker_host)
    return parsed.hostname or "localhost"


def _require_docker():
    if shutil.which("docker") is None:
        print("Error: 'docker' not found on PATH. Install Docker Desktop and try again.")
        sys.exit(1)


def test_connection(docker_host=None, timeout=15):
    """Run `docker version` against a Docker host without mutating the
    process-wide environment.

    docker_host: "ssh://user@host" to test a specific remote engine, "" to
    force a test against the local engine, or None to use whatever DOCKER_HOST
    is already set in the environment (or local, if unset).
    Returns (ok: bool, output: str).
    """
    env = dict(os.environ)
    if docker_host:
        env["DOCKER_HOST"] = docker_host
    elif docker_host == "":
        env.pop("DOCKER_HOST", None)

    try:
        proc = subprocess.run(
            ["docker", "version"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s connecting to Docker host."
    except FileNotFoundError:
        return False, "'docker' not found on PATH."
    output = "\n".join(s for s in (proc.stdout, proc.stderr) if s).strip()
    return proc.returncode == 0, output


def _image_exists(tag):
    return subprocess.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", tag],
        capture_output=True,
    ).returncode == 0


def list_existing_image_repos(timeout=10):
    """Return the set of pqc-* repository names currently present on the
    active Docker engine, or None if the engine couldn't be reached.

    One bulk `docker images` call instead of one `docker image inspect` per
    row -- the dashboard's image list annotates hundreds of rows at once.
    """
    try:
        proc = subprocess.run(
            ["docker", "images", "--filter", "reference=pqc-*", "--format", "{{.Repository}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


def list_running_containers(timeout=10):
    """Return {container_name: host_port} for every running pqc-* container
    (host_port is the published port mapped to the container's internal
    8000, or None if it couldn't be parsed out of `docker ps`'s output), or
    None if the engine couldn't be reached.

    One bulk `docker ps` call instead of one lookup per row -- same rationale
    as list_existing_image_repos().
    """
    try:
        proc = subprocess.run(
            ["docker", "ps", "--filter", "name=pqc-", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    result = {}
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        name, _, ports = line.partition("\t")
        port = None
        m = re.search(r":(\d+)->8000/tcp", ports)
        if m:
            port = m.group(1)
        result[name] = port
    return result


def _get_host_port(container, retries=8, delay=0.4):
    """Return the host port Docker assigned to container's internal port 8000.

    Retries a few times to handle the Docker Desktop (WSL2) race condition
    where `docker port` returns empty immediately after `docker run -d`.
    """
    for attempt in range(retries):
        r = subprocess.run(
            ["docker", "port", container, "8000"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.splitlines():
                if ":" in line:
                    return line.rsplit(":", 1)[-1].strip()
        if attempt < retries - 1:
            time.sleep(delay)
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


# ── Build error classification ────────────────────────────────────────────────

_REGISTRY_PATTERNS = [
    (re.compile(r"unexpected status from \w+ request[^\n]*?(\d{3})", re.I),
     lambda m: f"Docker registry HTTP {m.group(1)}"),
    (re.compile(r"failed to resolve source metadata[^\n]*?(\d{3})", re.I),
     lambda m: f"Docker registry HTTP {m.group(1)}"),
    (re.compile(r"failed to do request[^\n]*?(\d{3})", re.I),
     lambda m: f"Docker registry HTTP {m.group(1)}"),
    (re.compile(r"dial tcp[^\n]*?i/o timeout", re.I),
     lambda _: "network timeout reaching Docker registry"),
    (re.compile(r"no such host", re.I),
     lambda _: "DNS failure — cannot reach Docker registry"),
    (re.compile(r"net/http: TLS handshake timeout", re.I),
     lambda _: "TLS handshake timeout to Docker registry"),
    (re.compile(r"connection refused", re.I),
     lambda _: "connection refused to Docker registry"),
]


def _registry_error(output):
    """Return a short description when a build failed due to a Docker registry /
    network problem.  Returns None for ordinary image build failures."""
    for pattern, msg_fn in _REGISTRY_PATTERNS:
        m = pattern.search(output)
        if m:
            return msg_fn(m)
    return None


# ── Stale build-cache detection ────────────────────────────────────────────────
# Node's classic CommonJS resolution failure and Python's import failure both
# name the exact missing package -- directly comparable against the combo's
# own expected library. Confirmed twice in this project (Express+jose+
# crypto-js, AdonisJS+routes.ts, see project memory) that a container
# crashing with "missing module X" where X does NOT match the image's own
# tagged library means Docker/BuildKit reused a cached layer (COPY app.js /
# COPY routes.ts) from a DIFFERENT combo's build -- the on-disk source for
# the failing combo was verified correct both times, and a plain rebuild
# fixed it. This is a real, if infrequent, risk under this project's own
# parallel builds (many combos sharing an identical Dockerfile prefix up to
# the point their app code diverges), not a code/registry bug to chase.
_MISSING_MODULE_PATTERNS = [
    re.compile(r"Cannot find module '([^']+)'"),   # Node (CommonJS require)
    re.compile(r"No module named '([^']+)'"),      # Python (ModuleNotFoundError)
]


def _stale_cache_hint(output: str, expected_library: str) -> str | None:
    """None for an ordinary failure. A short hint string when the container's
    logs name a missing module that does NOT match this combo's own
    expected library -- likely the stale-build-cache issue above, not a
    real incompatibility, so worth flagging distinctly rather than treating
    like every other failure."""
    if not expected_library:
        return None
    expected = expected_library.lower().lstrip("@").replace("/", "")
    for pattern in _MISSING_MODULE_PATTERNS:
        m = pattern.search(output)
        if not m:
            continue
        missing = m.group(1).split("/")[0].lower().lstrip("@")
        if missing and expected and missing not in expected and expected not in missing:
            return (f"missing module '{m.group(1)}' doesn't match this combo's own "
                     f"library ('{expected_library}') -- likely a stale Docker build "
                     f"cache serving a different combo's source file, not a real "
                     f"failure; this usually resolves on a plain rebuild/retest")
    return None


# ── Build cache warm-up ───────────────────────────────────────────────────────
# 2026-07-21: after a build-cache prune (or on a fresh host), the shared
# --mount=type=cache package-manager caches are empty, so the first build of
# every combo hits the registry for real. Building the full matrix at normal
# concurrency right after a wipe recreates the exact rate-limit-by-
# concurrency problem the cache mounts exist to prevent. Rather than making
# the user remember to warm the cache manually, _do_build()/_do_client_build()
# below detect a cold cache for any language in the batch and automatically
# build one representative combo per shared-dependency group SEQUENTIALLY
# first (workers=1) -- cheap enough to not matter, and it populates the
# expensive shared tree (framework parent POM/starter, or equivalent) before
# the real batch runs in parallel.

_LANGUAGE_CACHE_ID = {
    "java":   "maven-cache",
    "node":   "npm-cache",
    "python": "pip-cache",
    "php":    "composer-cache",
    "dotnet": "nuget-cache",
}


def _cache_mount_status(languages) -> dict:
    """Returns {language: is_cold} for each language in `languages` that has
    a known --mount=type=cache id (see _LANGUAGE_CACHE_ID). "Cold" means
    that language's cache mount has no entry at all in BuildKit's own cache
    store right now -- queried directly via `docker buildx du`, not a flag
    tracked separately, so it can't drift if the cache was cleared some
    other way (e.g. a raw `docker builder prune` run outside this dashboard).
    """
    wanted = {lang: cid for lang, cid in _LANGUAGE_CACHE_ID.items() if lang in languages}
    if not wanted:
        return {}

    present = set()
    try:
        r = subprocess.run(
            ["docker", "buildx", "du", "--format", "{{json .}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("Type") != "exec.cachemount":
                continue
            m = re.search(r'with id "([^"]+)"', rec.get("Description", ""))
            if m:
                # BuildKit reports the id with a leading "/" (confirmed via
                # a real `docker buildx du` -- e.g. "/maven-cache") even
                # though our Dockerfiles declare it without one; strip it so
                # this actually matches _LANGUAGE_CACHE_ID's plain names
                # instead of silently reporting every language as cold.
                present.add(m.group(1).lstrip("/"))
    except (subprocess.SubprocessError, OSError):
        # Can't ask Docker right now -- don't wedge a build behind a warm-up
        # it might not even need over a transient CLI hiccup.
        return {lang: False for lang in wanted}

    return {lang: (cid not in present) for lang, cid in wanted.items()}


def _pick_warmup_representatives(entries, cold_languages, image_tag_fn):
    """One entry per shared-dependency group that needs warming.

    Server-side entries group by (language, framework, fw_ver) -- the
    framework major is what determines the parent-POM/starter dependency
    tree, not the crypto library, so one build per group is enough to warm
    the expensive shared part. Client-side entries (no "framework" key)
    group by (language, http_client) instead.

    A group needs warming if EITHER its whole language's cache mount is
    cold (`cold_languages`), OR -- the finer-grained case -- no entry in
    that specific group has ever been built before on this host at all.
    The second check matters because the cache mount is one shared blob
    per language, not partitioned per framework: once ANY framework has
    populated e.g. maven-cache, the language no longer reads as "cold"
    even though a DIFFERENT framework's dependency tree (a brand new
    parent POM/BOM never fetched before) is still completely unwarmed --
    confirmed as a real gap, not hypothetical: re-testing Helidon after
    Spring Boot/Quarkus had already warmed maven-cache would otherwise
    skip the warm-up entirely and hit Maven Central concurrently for
    Helidon's own (never-before-fetched) dependency tree.
    """
    existing_repos = list_existing_image_repos()
    # None means Docker couldn't be reached -- treat as "assume already
    # built" so a transient CLI hiccup never forces an unneeded warm-up,
    # same fallback philosophy as _cache_mount_status().

    groups = {}
    for e in entries:
        if "framework" in e:
            key = (e["language"], e["framework"], e.get("fw_ver", ""))
        else:
            key = (e["language"], e.get("http_client", ""))
        groups.setdefault(key, []).append(e)

    picked = []
    for (language, *_rest), group_entries in groups.items():
        lang_cold = language in cold_languages
        group_never_built = (
            existing_repos is not None
            and not any(image_tag_fn(ge) in existing_repos for ge in group_entries)
        )
        if lang_cold or group_never_built:
            picked.append(group_entries[0])
    return picked


# ── Build ─────────────────────────────────────────────────────────────────────

def _do_build(entries, no_cache=False, skip_existing=False, log_fn=print,
              save_fn=None, stop_event=None, workers=4, _warmup_checked=False):
    """Build a Docker image for every entry.

    save_fn(entry, result_dict) is called immediately after each image completes.
    stop_event (threading.Event) can be set externally to cancel the loop early.
    workers controls how many docker build processes run in parallel.
    _warmup_checked is internal (set True on the recursive warm-up call below
    to avoid infinite recursion) -- callers should never pass it.
    Returns {tag: {"success": bool, "output": str, "elapsed": float, "skipped": bool}}.
    """
    if not _warmup_checked and not no_cache and entries:
        languages = {e["language"] for e in entries}
        cold = {lang for lang, is_cold in _cache_mount_status(languages).items() if is_cold}
        warmup_entries = _pick_warmup_representatives(entries, cold, _image_tag)
        if warmup_entries:
            note = (
                f"cold cache for {', '.join(sorted(cold))}" if cold
                else "never-built-before framework group(s) in an otherwise warm cache"
            )
            log_fn(
                f"\n⚠ {note} -- warming with {len(warmup_entries)} representative "
                f"combo(s) sequentially first (avoids a registry rate-limit burst) ...\n"
            )
            _do_build(
                warmup_entries, no_cache=False, skip_existing=False,
                log_fn=log_fn, save_fn=save_fn, stop_event=stop_event,
                workers=1, _warmup_checked=True,
            )
            if stop_event is not None and stop_event.is_set():
                return {}
            log_fn("\nCache warm-up complete -- continuing with the full batch ...\n")

    n = len(entries)
    pad = len(str(n))
    workers, cap_note = _capped_workers(entries, workers)
    note = "  (--no-cache)" if no_cache else ""
    parallel_note = f"  ({workers} parallel{', ' + cap_note if cap_note else ''})" if (workers > 1 or cap_note) else ""
    log_fn(f"\nBuilding {n:,} image(s){note}{parallel_note} ...\n")

    results: dict = {}
    _lock = threading.Lock()

    def _build_one(i, e):
        if stop_event is not None and stop_event.is_set():
            return

        tag     = _image_tag(e)
        context = str(PROJECT_ROOT / e["path"])

        if skip_existing and _image_exists(tag):
            log_fn(f"[{i:{pad}}/{n}] {tag}")
            log_fn(f"         SKIPPED  (image already exists)")
            result = {"success": True, "output": "", "elapsed": 0.0, "skipped": True}
            with _lock:
                results[tag] = result
                if save_fn is not None:
                    save_fn(e, result)
            return

        cmd = ["docker", "build", "-t", tag]
        if no_cache:
            cmd.append("--no-cache")
        cmd.append(context)

        t0      = time.monotonic()
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        proc    = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        # Drain pipes in background threads to prevent pipe-buffer deadlock
        # (docker build can produce >64 KB of output, which blocks the process
        # if the pipe is not read concurrently).
        _out, _err = [], []
        _t_out = threading.Thread(target=lambda: _out.extend(proc.stdout), daemon=True)
        _t_err = threading.Thread(target=lambda: _err.extend(proc.stderr), daemon=True)
        _t_out.start()
        _t_err.start()
        while proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                proc.kill()
                proc.wait()
                _t_out.join(timeout=2)
                _t_err.join(timeout=2)
                return
            time.sleep(0.3)
        _t_out.join()
        _t_err.join()
        stdout = "".join(_out)
        stderr = "".join(_err)
        elapsed  = time.monotonic() - t0
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        output   = "\n".join(s for s in (stderr, stdout) if s).strip()

        log_fn(f"[{i:{pad}}/{n}] {tag}")
        if proc.returncode == 0:
            log_fn(f"         OK  ({elapsed:.1f}s)")
            result = {"success": True,  "output": output,
                      "elapsed": elapsed, "skipped": False,
                      "started_at": started, "finished_at": finished}
        else:
            log_fn(f"         FAILED  ({elapsed:.1f}s)")
            reg_err = _registry_error(output)
            if reg_err:
                log_fn(f"         ! DOCKER INFRASTRUCTURE ERROR: {reg_err}")
                log_fn(f"         ! This is not an image problem — retry the build.")
            for line in output.splitlines()[-15:]:
                log_fn(f"         | {line}")
            result = {"success": False, "output": output,
                      "elapsed": elapsed, "skipped": False,
                      "started_at": started, "finished_at": finished}

        with _lock:
            results[tag] = result
            if save_fn is not None:
                save_fn(e, result)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_build_one, i, e): (i, e)
                for i, e in enumerate(entries, 1)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                i, _ = futs[fut]
                log_fn(f"[{i:{pad}}/{n}] ERROR: {exc}")

    ok        = sum(1 for v in results.values() if v["success"])
    failed    = sum(1 for v in results.values() if not v["success"])
    cancelled = n - len(results)
    parts     = [f"{ok} succeeded", f"{failed} failed"]
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    log_fn(f"\nBuild complete: {', '.join(parts)}.")
    return results


# ── Run ───────────────────────────────────────────────────────────────────────

def _do_run(entries, build_results=None, log_fn=print, display_host=None):
    """Start a detached container for every entry.

    build_results: dict {tag: result_dict} from _do_build; entries whose build
    failed are automatically skipped.  Pass None to run without a preceding
    build step (images must already exist in that case).

    display_host overrides _docker_target_host() for the printed URL only --
    _docker_target_host() says "localhost" whenever Docker is local to *this
    process*, which is correct for the CLI but wrong when the caller is a
    dashboard.py request from a browser on a different machine (that
    "localhost" would resolve to the browser's own machine, not the
    server). dashboard.py resolves the right host from the request and
    passes it in; plain CLI usage leaves this None and keeps the old
    behavior.
    """
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nStarting {n:,} container(s) ...\n")

    started = []   # [(name, url)]
    failed  = []

    for i, e in enumerate(entries, 1):
        tag  = _image_tag(e)
        name = tag   # container name == image tag

        # Skip entries whose build step failed
        if build_results is not None and not build_results.get(tag, {}).get("success"):
            log_fn(f"[{i:{pad}}/{n}] {name}")
            log_fn(f"         SKIPPED  (build failed)")
            continue

        # Without a preceding build, verify the image exists
        if build_results is None and not _image_exists(tag):
            log_fn(f"[{i:{pad}}/{n}] {name}")
            log_fn(f"         NOT FOUND  (run --build first)")
            failed.append(name)
            continue

        # Remove any existing container with this name (stopped or running)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", "0:8000", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

        log_fn(f"[{i:{pad}}/{n}] {name}")

        if proc.returncode != 0:
            log_fn(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-5:]:
                log_fn(f"         | {line}")
            failed.append(name)
            continue

        port = _get_host_port(name)
        if port == "?":
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            log_fn(f"         FAILED  (port not assigned, container state: {state})")
            failed.append(name)
            continue
        url  = f"http://{display_host or _docker_target_host()}:{port}"
        log_fn(f"         {url}")
        started.append((name, url))

    # ── Summary table ─────────────────────────────────────────────────────────
    if started:
        name_w = max(len(nm) for nm, _ in started)
        log_fn(f"\nRunning ({len(started)}):")
        log_fn(f"  {'Container':<{name_w}}  URL")
        log_fn(f"  {'-' * name_w}  ---")
        for nm, url in started:
            log_fn(f"  {nm:<{name_w}}  {url}")

    if failed:
        log_fn(f"\nFailed to start ({len(failed)}): {', '.join(failed)}")


# ── Stop ──────────────────────────────────────────────────────────────────────

def _do_stop(entries, log_fn=print):
    """Stop and remove the container for every entry."""
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nStopping {n:,} container(s) ...\n")

    stopped     = []
    not_running = []
    failed      = []

    for i, e in enumerate(entries, 1):
        name = _image_tag(e)
        log_fn(f"[{i:{pad}}/{n}] {name}")

        proc = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

        if proc.returncode == 0:
            log_fn(f"         STOPPED")
            stopped.append(name)
        else:
            err = proc.stderr.strip()
            if "No such container" in err:
                log_fn(f"         NOT RUNNING")
                not_running.append(name)
            else:
                log_fn(f"         FAILED")
                for line in err.splitlines()[-3:]:
                    log_fn(f"         | {line}")
                failed.append(name)

    parts = []
    if stopped:
        parts.append(f"{len(stopped)} stopped")
    if not_running:
        parts.append(f"{len(not_running)} not running")
    if failed:
        parts.append(f"{len(failed)} failed")
    log_fn(f"\nDone: {', '.join(parts)}.")


def _do_docker_cleanup(full=False, dry_run=False, log_fn=print):
    """Remove stopped containers, dangling images, build cache, and unused networks.

    full=True also removes all unused images and volumes.
    dry_run=True only reports what would be removed.
    """
    def run(*cmd):
        return subprocess.run(
            list(cmd), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def section(title):
        bar = "─" * max(0, 52 - len(title))
        log_fn(f"\n── {title} {bar}")

    section("Disk usage (before)")
    r = run("docker", "system", "df")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(line)

    if dry_run:
        log_fn("\n[DRY RUN] No changes will be made.\n")

        section("Would remove: stopped containers")
        r = run("docker", "ps", "-a",
                "--filter", "status=exited", "--filter", "status=created",
                "--format", "{{.ID}}  {{.Image}}  {{.Status}}")
        out = (r.stdout or "").strip()
        log_fn(out if out else "  (none)")

        section("Would remove: dangling images")
        r = run("docker", "images", "--filter", "dangling=true",
                "--format", "{{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}")
        out = (r.stdout or "").strip()
        log_fn(out if out else "  (none)")

        if full:
            section("Would remove: all unused images")
            r = run("docker", "images",
                    "--format", "{{.ID}}  {{.Repository}}:{{.Tag}}  {{.Size}}")
            out = (r.stdout or "").strip()
            log_fn(out if out else "  (none)")

        section("Would remove: unused volumes")
        r = run("docker", "volume", "ls", "--filter", "dangling=true",
                "--format", "{{.Name}}")
        out = (r.stdout or "").strip()
        log_fn(out if out else "  (none)")
        return

    section("Removing stopped containers")
    r = run("docker", "ps", "-aq",
            "--filter", "status=exited", "--filter", "status=created")
    ids = [x.strip() for x in (r.stdout or "").strip().splitlines() if x.strip()]
    if ids:
        run("docker", "rm", *ids)
        log_fn(f"  Removed {len(ids)} container(s).  OK")
    else:
        log_fn("  Nothing to remove.")

    section("Removing dangling images (<none>:<none>)")
    r = run("docker", "images", "-q", "--filter", "dangling=true")
    ids = [x.strip() for x in (r.stdout or "").strip().splitlines() if x.strip()]
    if ids:
        run("docker", "rmi", *ids)
        log_fn(f"  Removed {len(ids)} image(s).  OK")
    else:
        log_fn("  Nothing to remove.")

    if full:
        section("Removing all unused images")
        r = run("docker", "image", "prune", "-af")
        for line in (r.stdout or r.stderr or "").strip().splitlines():
            log_fn(f"  {line}")
        log_fn("  OK" if r.returncode == 0 else "  FAILED")

    # Deliberately NOT `docker builder prune` here (2026-07-21): that would
    # also wipe the shared --mount=type=cache package-manager caches (Maven/
    # npm/pip/Composer/NuGet) that make every combo's build reuse overlapping
    # dependencies instead of re-hitting the registry -- which is exactly
    # what was driving Maven Central's sustained rate-limiting before those
    # mounts existed. Neither Normal nor Full cleanup touches build cache at
    # all anymore; see _do_build_cache_prune() for the deliberately separate,
    # explicitly-opt-in action that does.

    section("Removing unused networks")
    r = run("docker", "network", "prune", "-f")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(f"  {line}")
    log_fn("  OK" if r.returncode == 0 else "  FAILED")

    if full:
        section("Removing unused volumes")
        r = run("docker", "volume", "prune", "-f")
        for line in (r.stdout or r.stderr or "").strip().splitlines():
            log_fn(f"  {line}")
        log_fn("  OK" if r.returncode == 0 else "  FAILED")

    section("Disk usage (after)")
    r = run("docker", "system", "df")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(line)

    log_fn("\nCleanup complete.")


def _do_build_cache_prune(dry_run=False, log_fn=print):
    """Remove Docker's ENTIRE build cache -- including the shared
    --mount=type=cache package-manager caches (maven-cache/npm-cache/
    pip-cache/composer-cache/nuget-cache) that let every combo's build reuse
    overlapping dependencies instead of re-hitting the registry.

    Deliberately its OWN action, never run as part of _do_docker_cleanup()
    (Normal/Full): wiping these brings back exactly the Maven Central
    rate-limiting they exist to prevent (see this project's own
    generator-safety notes). Only reach for this when disk usage from the
    caches is genuinely a problem, or a specific cache entry is suspected
    corrupt -- not as routine maintenance.
    """
    def run(*cmd):
        return subprocess.run(
            list(cmd), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def section(title):
        bar = "─" * max(0, 52 - len(title))
        log_fn(f"\n── {title} {bar}")

    section("Disk usage (before)")
    r = run("docker", "system", "df")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(line)

    if dry_run:
        log_fn(
            "\n[DRY RUN] No changes will be made. This would remove the ENTIRE "
            "build cache above, including the shared Maven/npm/pip/Composer/"
            "NuGet package caches -- expect renewed registry rate-limiting "
            "risk (e.g. Maven Central 429s) on the next big Java batch after "
            "a real run of this.\n"
        )
        return

    section("Removing build cache (including shared package-manager caches)")
    r = run("docker", "builder", "prune", "-af")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(f"  {line}")
    log_fn("  OK" if r.returncode == 0 else "  FAILED")

    section("Disk usage (after)")
    r = run("docker", "system", "df")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(line)

    log_fn("\nBuild cache prune complete.")


def _collect_client_tags(images_base=None) -> set:
    """Client-image equivalent of _collect() + _image_tag() combined -- one
    flat axis (language/lang_ver/http_client/hc_ver, no framework/library
    split), so no scoped-name mis-slicing risk exists here today. Kept as
    its own small walker rather than generalizing _collect() itself, since
    the two directory shapes are genuinely different, not just a special
    case of one another."""
    base = Path(images_base) if images_base else CLIENT_IMAGES_BASE
    tags = set()
    for dockerfile in base.rglob("Dockerfile"):
        parts = dockerfile.parent.relative_to(base).parts
        if len(parts) != 4:
            continue
        language, lang_ver, http_client, hc_ver = parts
        hc = http_client.lower().replace("/", "_").replace("@", "").replace(" ", "").replace(".", "-")
        tags.add(f"pqc-client-{language}-{lang_ver}-{hc}-{hc_ver}")
    return tags


def _find_orphaned_image_tags(images_base=None) -> list:
    """Docker images tagged 'pqc-*' that no longer correspond to any context
    in the current images/ or images_clients/ tree (e.g. a framework/
    library/version excluded by a later registry fix, or a whole language
    dropped from the project).

    docker's own `image prune` has no notion of "still valid per the current
    registry" -- it only tracks container references, which this project's
    build->test->stop workflow always clears anyway, so it can't tell an
    orphan from a deliberately-kept fleet image. This walks the real
    images/ and images_clients/ trees (the source of truth for what SHOULD
    exist) and diffs them against what actually exists on the engine.
    `docker images --format {{.Repository}}` filtered by a "pqc-" prefix
    matches server AND client tags alike (pqc-client-... starts with
    "pqc-" too) plus the dashboard-managed pqc-fingerprint-target helper
    image (built from scripts/fingerprint_target/, not from either tree) --
    all three needed covering here, or every client image and the target
    helper get flagged as "orphaned" on every single run.
    """
    base = Path(images_base) if images_base else IMAGES_BASE
    entries = _collect(base)
    expected_tags = {_image_tag(e) for e in entries}
    expected_tags |= _collect_client_tags()
    expected_tags.add(_FP_TARGET_IMAGE)

    r = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}"],
        capture_output=True, text=True,
    )
    actual_tags = {t for t in (r.stdout or "").splitlines() if t.startswith("pqc-")}
    return sorted(actual_tags - expected_tags)


def _do_remove_orphans(dry_run=False, log_fn=print):
    """Remove (or, if dry_run, just report) Docker images tagged 'pqc-*'
    that no longer correspond to anything in the current images/ tree."""
    log_fn("\nScanning for orphaned images (built, but not in the current registry) ...\n")
    orphans = _find_orphaned_image_tags()

    if not orphans:
        log_fn("No orphaned images found.")
        return

    if dry_run:
        log_fn(f"[DRY RUN] Would remove {len(orphans)} orphaned image(s):\n")
        for tag in orphans:
            log_fn(f"  {tag}")
        return

    log_fn(f"Removing {len(orphans)} orphaned image(s) ...\n")
    r = subprocess.run(["docker", "rmi", "-f", *orphans], capture_output=True, text=True)
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(f"  {line}")
    log_fn(f"\nRemoved {len(orphans)} orphaned image(s).")


def _do_stop_all(log_fn=print):
    """Stop and remove every container whose name starts with 'pqc-'."""
    proc = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=pqc-", "--format", "{{.Names}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    names = [n.strip() for n in proc.stdout.splitlines()
             if n.strip().startswith("pqc-")]

    if not names:
        log_fn("No PQC containers are running.")
        return

    n   = len(names)
    pad = len(str(n))
    log_fn(f"\nStopping all {n:,} PQC container(s) ...\n")

    stopped = 0
    for i, name in enumerate(names, 1):
        log_fn(f"[{i:{pad}}/{n}] {name}")
        r = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode == 0:
            log_fn(f"         STOPPED")
            stopped += 1
        else:
            log_fn(f"         FAILED")
            for line in r.stderr.strip().splitlines()[-3:]:
                log_fn(f"         | {line}")

    log_fn(f"\nDone: {stopped}/{n} stopped.")


# ── Fingerprint (network traffic capture) ─────────────────────────────────────
# Genuine on-the-wire capture, not a client-side reconstruction: a tcpdump
# sidecar joins the target container's own network namespace and sniffs the
# exact packets that hit it while a probe request is fired. Works the same
# whether Docker is local or remote (DOCKER_HOST=ssh://...) since it's just
# `docker` CLI calls, like everything else in this file.

_FP_MISSING_PATH  = "/__pqc_fingerprint_missing__"
_FP_SNIFFER_IMAGE = "nicolaka/netshoot"


def _send_probe(target_host, port, method, path, timeout=5):
    """Fire one HTTP request at the running container and report its outcome.
    The bytes exchanged are observed separately, by the tcpdump sidecar in
    _capture_traffic -- this just drives real traffic and reads the result.
    """
    req = urllib.request.Request(f"http://{target_host}:{port}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception as exc:
        return None, str(exc)


def _send_raw_malformed(target_host, port, timeout=5):
    """Fire a deliberately malformed request line (bogus HTTP version token)
    over a raw socket instead of urllib, which would refuse to build one.
    Almost every HTTP server rejects this at the request-line parser with
    400 Bad Request, but the exact response (status line wording, headers,
    body, or even just closing the connection) is often distinctively
    different per framework/server implementation -- that's the signal."""
    try:
        with socket.create_connection((target_host, port), timeout=timeout) as sock:
            sock.sendall(b"GET / HTTP/9.9\r\nHost: x\r\n\r\n")
            sock.settimeout(timeout)
            data = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        if not data:
            return None, "connection closed with no response"
        first_line = data.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        parts = first_line.split(" ", 2)
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
        return status, ""
    except Exception as exc:
        return None, str(exc)


def _capture_traffic(container, action_fn, capture_filter="tcp", timeout=8):
    """Run action_fn() while a tcpdump sidecar sniffs the exact packets
    hitting *container*'s network namespace. Returns (action_fn's return
    value, traffic_text, pcap_b64, sniffer_error).
    """
    cap_name = f"{container}-fpcap"
    subprocess.run(["docker", "rm", "-f", cap_name], capture_output=True)

    proc = subprocess.run(
        ["docker", "run", "-d", "--name", cap_name,
         "--network", f"container:{container}",
         "--cap-add", "NET_RAW", "--cap-add", "NET_ADMIN",
         _FP_SNIFFER_IMAGE, "tcpdump", "-i", "any", "-w", "/tmp/cap.pcap", "-U", capture_filter],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return action_fn(), "", "", f"sniffer container failed to start: {proc.stderr.strip()[-300:]}"

    # Wait for tcpdump to actually be listening before generating traffic.
    attached = False
    for _ in range(timeout * 5):
        logs = subprocess.run(["docker", "logs", cap_name], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if "listening on" in (logs.stdout + logs.stderr):
            attached = True
            break
        time.sleep(0.2)

    try:
        result = action_fn()
    finally:
        time.sleep(0.3)  # let the response finish and flush to the pcap file
        pcap_b64 = ""
        if attached:
            dump = subprocess.run(
                ["docker", "exec", cap_name, "tcpdump", "-r", "/tmp/cap.pcap", "-nn", "-XX", "-v"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            # The genuine binary capture, for reliable structured parsing
            # later -- the text dump above is a lossy, human-readable
            # derivation of this, not a substitute for it. `docker cp` works
            # the same over a remote DOCKER_HOST too, since it goes through
            # the Docker API rather than local filesystem access.
            raw = subprocess.run(
                ["docker", "cp", f"{cap_name}:/tmp/cap.pcap", "-"],
                capture_output=True,
            )
            if raw.returncode == 0:
                pcap_b64 = base64.b64encode(raw.stdout).decode("ascii")
        subprocess.run(["docker", "rm", "-f", cap_name], capture_output=True)

    if not attached:
        return result, "", "", "sniffer never reported 'listening on' -- capture skipped"
    traffic = dump.stdout.strip()
    error   = "" if dump.returncode == 0 else dump.stderr.strip()[-300:]
    return result, traffic, pcap_b64, error


def _capture_fingerprint(container, target_host, port):
    """Capture real network traffic for four probes against a running
    container: a valid call ('success'), a call to a nonexistent path
    ('failure', expected 404), an unsupported method on a valid path
    ('method_not_allowed', expected 405), and a deliberately malformed
    request line ('malformed', expected 400). Each framework's default
    handling of these differs -- that's the actual fingerprinting signal,
    not just the happy-path response. Returns a dict keyed by call_type."""
    records = {}

    (status, err), traffic, pcap_b64, cap_err = _capture_traffic(
        container, lambda: _send_probe(target_host, port, "GET", "/version"))
    records["success"] = {
        "method": "GET", "path": "/version", "status_code": status,
        "success": status == 200, "traffic_raw": traffic, "pcap_raw": pcap_b64,
        "error": err or cap_err,
    }

    (status, err), traffic, pcap_b64, cap_err = _capture_traffic(
        container, lambda: _send_probe(target_host, port, "GET", _FP_MISSING_PATH))
    records["failure"] = {
        "method": "GET", "path": _FP_MISSING_PATH, "status_code": status,
        "success": status is not None and status != 200, "traffic_raw": traffic,
        "pcap_raw": pcap_b64, "error": err or cap_err,
    }

    (status, err), traffic, pcap_b64, cap_err = _capture_traffic(
        container, lambda: _send_probe(target_host, port, "POST", "/version"))
    records["method_not_allowed"] = {
        "method": "POST", "path": "/version", "status_code": status,
        "success": status is not None, "traffic_raw": traffic,
        "pcap_raw": pcap_b64, "error": err or cap_err,
    }

    (status, err), traffic, pcap_b64, cap_err = _capture_traffic(
        container, lambda: _send_raw_malformed(target_host, port))
    records["malformed"] = {
        "method": "GET", "path": "(malformed request line)", "status_code": status,
        "success": status is not None, "traffic_raw": traffic,
        "pcap_raw": pcap_b64, "error": err or cap_err,
    }

    return records


# ── Client fingerprinting (the reverse direction) ─────────────────────────────
# A client image makes ONE outbound call to a persistent "fingerprint target"
# app (scripts/fingerprint_target/) instead of a server image answering
# probes. The tcpdump sidecar attaches to the TARGET's network namespace
# (not the client's) while the one-shot client container runs and exits --
# _capture_traffic() itself is unchanged, just pointed the other way.

_FP_TARGET_IMAGE     = "pqc-fingerprint-target"
_FP_TARGET_CONTAINER = "pqc-fingerprint-target"
_FP_TARGET_NETWORK   = "pqc-fingerprint-net"
_FP_TARGET_HTTP_PORT  = 9000
_FP_TARGET_HTTPS_PORT = 9443
_FP_TARGET_DOCKERFILE_DIR = PROJECT_ROOT / "scripts" / "fingerprint_target"

# Clients that drive their own raw TLS handshake (pyopenssl-raw/m2crypto-raw/
# node-forge-raw) need the target's HTTPS port, not the plain one every
# other client uses -- confirmed the hard way: pointing pyopenssl-raw at the
# plain HTTP port raised "wrong version number" (a TLS ClientHello arriving
# at a non-TLS listener); node-forge-raw instead just hangs until the test
# harness's own timeout, since forge's TLS client never gets a TLS
# ServerHello back from the plain HTTP listener. Kept as an explicit set
# here rather than inferred from the tag string, since new raw-TLS clients
# might not all share the "-raw" suffix.
_TLS_RAW_CLIENTS = {"pyopenssl-raw", "m2crypto-raw", "node-forge-raw"}


def _client_image_tag(e):
    hc = e["http_client"].lower().replace("/", "_").replace("@", "").replace(" ", "").replace(".", "-")
    return f"pqc-client-{e['language']}-{e['lang_ver']}-{hc}-{e['hc_ver']}"


def _do_client_build(entries, no_cache=False, skip_existing=False, log_fn=print,
                     save_fn=None, stop_event=None, workers=4, _warmup_checked=False):
    """Build a Docker image for every client-image entry. Same shape as
    _do_build(), just using _client_image_tag() instead of _image_tag().

    save_fn(entry, result_dict) is called immediately after each image completes.
    _warmup_checked is internal, see _do_build()'s docstring.
    Returns {tag: {"success": bool, "output": str, "elapsed": float, "skipped": bool}}.
    """
    if not _warmup_checked and not no_cache and entries:
        languages = {e["language"] for e in entries}
        cold = {lang for lang, is_cold in _cache_mount_status(languages).items() if is_cold}
        warmup_entries = _pick_warmup_representatives(entries, cold, _client_image_tag)
        if warmup_entries:
            note = (
                f"cold cache for {', '.join(sorted(cold))}" if cold
                else "never-built-before group(s) in an otherwise warm cache"
            )
            log_fn(
                f"\n⚠ {note} -- warming with {len(warmup_entries)} representative "
                f"combo(s) sequentially first (avoids a registry rate-limit burst) ...\n"
            )
            _do_client_build(
                warmup_entries, no_cache=False, skip_existing=False,
                log_fn=log_fn, save_fn=save_fn, stop_event=stop_event,
                workers=1, _warmup_checked=True,
            )
            if stop_event is not None and stop_event.is_set():
                return {}
            log_fn("\nCache warm-up complete -- continuing with the full batch ...\n")

    n = len(entries)
    pad = len(str(n))
    workers, cap_note = _capped_workers(entries, workers)
    note = "  (--no-cache)" if no_cache else ""
    parallel_note = f"  ({workers} parallel{', ' + cap_note if cap_note else ''})" if (workers > 1 or cap_note) else ""
    log_fn(f"\nBuilding {n:,} client image(s){note}{parallel_note} ...\n")

    results: dict = {}
    _lock = threading.Lock()

    def _build_one(i, e):
        if stop_event is not None and stop_event.is_set():
            return

        tag     = _client_image_tag(e)
        context = str(PROJECT_ROOT / e["path"])

        if skip_existing and _image_exists(tag):
            log_fn(f"[{i:{pad}}/{n}] {tag}")
            log_fn(f"         SKIPPED  (image already exists)")
            result = {"success": True, "output": "", "elapsed": 0.0, "skipped": True}
            with _lock:
                results[tag] = result
                if save_fn is not None:
                    save_fn(e, result)
            return

        cmd = ["docker", "build", "-t", tag]
        if no_cache:
            cmd.append("--no-cache")
        cmd.append(context)

        t0      = time.monotonic()
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        elapsed  = time.monotonic() - t0
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        output   = "\n".join(s for s in (proc.stderr, proc.stdout) if s).strip()

        log_fn(f"[{i:{pad}}/{n}] {tag}")
        if proc.returncode == 0:
            log_fn(f"         OK  ({elapsed:.1f}s)")
            result = {"success": True, "output": output, "elapsed": elapsed,
                      "skipped": False, "started_at": started, "finished_at": finished}
        else:
            log_fn(f"         FAILED  ({elapsed:.1f}s)")
            for line in output.splitlines()[-15:]:
                log_fn(f"         | {line}")
            result = {"success": False, "output": output, "elapsed": elapsed,
                      "skipped": False, "started_at": started, "finished_at": finished}

        with _lock:
            results[tag] = result
            if save_fn is not None:
                save_fn(e, result)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_build_one, i, e): (i, e)
                for i, e in enumerate(entries, 1)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                i, _ = futs[fut]
                log_fn(f"[{i:{pad}}/{n}] ERROR: {exc}")

    ok        = sum(1 for v in results.values() if v["success"])
    failed    = sum(1 for v in results.values() if not v["success"])
    cancelled = n - len(results)
    parts     = [f"{ok} succeeded", f"{failed} failed"]
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    log_fn(f"\nBuild complete: {', '.join(parts)}.")
    return results


def _do_client_remove(entries, log_fn=print):
    """Remove stopped containers and the Docker image for every client entry."""
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nRemoving {n:,} client image(s) ...\n")

    removed = 0
    for i, e in enumerate(entries, 1):
        tag = _client_image_tag(e)
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        if not _image_exists(tag):
            log_fn(f"         NOT FOUND")
            continue

        n_containers = _remove_stopped_containers(tag)
        if n_containers:
            log_fn(f"         CONTAINERS  ({n_containers} stopped container(s) removed)")

        proc = subprocess.run(
            ["docker", "rmi", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            log_fn(f"         REMOVED")
            removed += 1
        else:
            log_fn(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-3:]:
                log_fn(f"         | {line}")

    log_fn(f"\nDone: {removed}/{n} removed.")


def _do_client_run(entries, log_fn=print):
    """Start a detached container for every client entry, pointed at the
    persistent fingerprint-target app (ensuring it's up first). Unlike a
    server image, a client container has no listening port of its own -- it
    fires its one outbound call and exits almost immediately, so this is
    closer to "run once and inspect the logs" than a persistent service
    with a URL to open."""
    log_fn(f"\nEnsuring fingerprint-target app is up ...")
    target_container, target_err = _ensure_fingerprint_target()
    if target_container is None:
        log_fn(f"[ABORT] fingerprint-target unavailable: {target_err}")
        return

    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nStarting {n:,} client container(s) against {target_container} ...\n")

    started, failed = [], []
    for i, e in enumerate(entries, 1):
        tag  = _client_image_tag(e)
        name = tag
        target_url = _client_target_url(e.get("http_client", ""))
        log_fn(f"[{i:{pad}}/{n}] {name}")

        if not _image_exists(tag):
            log_fn(f"         NOT FOUND  (build first)")
            failed.append(name)
            continue

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", _FP_TARGET_NETWORK,
             "-e", f"PQC_TARGET_URL={target_url}", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            log_fn(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-5:]:
                log_fn(f"         | {line}")
            failed.append(name)
            continue

        log_fn(f"         STARTED  (targeting {target_url} -- "
               f"exits after its one call, check with 'docker logs {name}')")
        started.append(name)

    if started:
        log_fn(f"\nStarted ({len(started)}): {', '.join(started)}")
    if failed:
        log_fn(f"\nFailed to start ({len(failed)}): {', '.join(failed)}")


def _do_client_stop(entries, log_fn=print):
    """Stop and remove the container for every client entry."""
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nStopping {n:,} client container(s) ...\n")

    stopped, not_running, failed = [], [], []
    for i, e in enumerate(entries, 1):
        name = _client_image_tag(e)
        log_fn(f"[{i:{pad}}/{n}] {name}")

        proc = subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            log_fn(f"         STOPPED")
            stopped.append(name)
        else:
            err = proc.stderr.strip()
            if "No such container" in err:
                log_fn(f"         NOT RUNNING")
                not_running.append(name)
            else:
                log_fn(f"         FAILED")
                for line in err.splitlines()[-3:]:
                    log_fn(f"         | {line}")
                failed.append(name)

    parts = []
    if stopped:
        parts.append(f"{len(stopped)} stopped")
    if not_running:
        parts.append(f"{len(not_running)} not running")
    if failed:
        parts.append(f"{len(failed)} failed")
    log_fn(f"\nDone: {', '.join(parts) if parts else 'nothing to do'}.")


def _ensure_fingerprint_target() -> tuple[str | None, str]:
    """Build (if needed) and start the persistent fingerprint-target
    container on its own dedicated network. Returns (container_name, error)
    -- container_name is None if the build or start genuinely failed, so
    callers don't silently proceed into a capture pass with nothing actually
    listening. Idempotent -- safe to call before every client-fingerprint pass."""
    subprocess.run(["docker", "network", "create", _FP_TARGET_NETWORK],
                   capture_output=True)

    if not _image_exists(_FP_TARGET_IMAGE):
        proc = subprocess.run(
            ["docker", "build", "-t", _FP_TARGET_IMAGE, str(_FP_TARGET_DOCKERFILE_DIR)],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None, f"failed to build {_FP_TARGET_IMAGE}: {proc.stderr.strip()[-500:]}"

    state = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", _FP_TARGET_CONTAINER],
        capture_output=True, text=True,
    )
    if state.returncode != 0 or state.stdout.strip() != "true":
        subprocess.run(["docker", "rm", "-f", _FP_TARGET_CONTAINER], capture_output=True)
        proc = subprocess.run(
            ["docker", "run", "-d", "--name", _FP_TARGET_CONTAINER,
             "--network", _FP_TARGET_NETWORK, _FP_TARGET_IMAGE],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None, f"failed to start {_FP_TARGET_CONTAINER}: {proc.stderr.strip()[-500:]}"
    return _FP_TARGET_CONTAINER, ""


def _run_client_container(client_tag: str, container_name: str, network: str, target_url: str, timeout=15):
    """Run a one-shot client container to completion, return its parsed
    stdout (the client's own JSON summary) plus its exit code."""
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    proc = subprocess.run(
        ["docker", "run", "--rm", "--name", container_name, "--network", network,
         "-e", f"PQC_TARGET_URL={target_url}", client_tag],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def _client_target_url(http_client_name: str, target_override: str = "") -> str:
    """Same TLS-vs-plain routing used by both Test and Fingerprint -- the
    two raw-TLS clients need the target's HTTPS port, everything else the
    plain HTTP one. target_override, when set (the dashboard's
    fingerprint_target setting), replaces the built-in
    pqc-fingerprint-target container name -- e.g. to point at an external
    target host. Ports stay fixed, since that's what the target app itself
    listens on."""
    host = target_override.strip() or _FP_TARGET_CONTAINER
    if http_client_name in _TLS_RAW_CLIENTS:
        return f"https://{host}:{_FP_TARGET_HTTPS_PORT}/probe"
    return f"http://{host}:{_FP_TARGET_HTTP_PORT}/probe"


def _capture_client_fingerprint(client_tag: str, target_container: str, network: str,
                                 http_client_name: str = "", target_override: str = ""):
    """Capture real network traffic seen by the target app for one client
    image's single outbound call. Returns a result dict."""
    client_container = f"{client_tag}-fpclient"
    target_url = _client_target_url(http_client_name, target_override)

    (rc, out, err), traffic, pcap_b64, cap_err = _capture_traffic(
        target_container,
        lambda: _run_client_container(client_tag, client_container, network, target_url),
    )
    subprocess.run(["docker", "rm", "-f", client_container], capture_output=True)

    status_code = None
    try:
        status_code = json.loads(out).get("status_code")
    except (json.JSONDecodeError, AttributeError):
        pass

    # Genuinely on-the-wire signals -- independent of anything the client
    # itself claims in client_output, since these are parsed straight out of
    # the captured packets: the HTTP User-Agent header for plain-HTTP
    # clients, or the TLS ClientHello JA3 fingerprint for the raw-TLS ones.
    signals = net_signal.extract_network_signals(pcap_b64, _FP_TARGET_HTTPS_PORT, _FP_TARGET_HTTP_PORT)

    return {
        "status_code": status_code, "traffic_raw": traffic, "pcap_raw": pcap_b64,
        "error": (err or cap_err) if rc != 0 else cap_err,
        "client_output": out,
        "observed_user_agent": signals["user_agent"],
        "observed_ja3_hash": signals["ja3_hash"],
        "observed_ja3_string": signals["ja3_string"],
    }


def _do_client_test(entries, log_fn=print, save_fn=None, stop_event=None, workers=4,
                     target_override=""):
    """Test each client image: run it once, for real, from the actual built
    image against the persistent target app -- same one-shot container
    Fingerprint uses, just without the tcpdump capture. Success is just "did
    the call succeed" (the client exited cleanly and reported status 200),
    not a traffic capture.

    target_override, when set (the dashboard's fingerprint_target setting),
    points every client at that host instead of the built-in
    pqc-fingerprint-target container -- since it's presumably an externally
    managed target in that case, its own build/start is skipped.

    save_fn(entry, result_dict) is called immediately after each image
    completes -- result_dict is None if the client image isn't built.
    Returns {tag: {"success": bool, "output": str, "error": str}}.
    """
    n   = len(entries)
    pad = len(str(n))
    if target_override:
        target_container = target_override
        log_fn(f"\nUsing custom fingerprint target: {target_container}")
    else:
        log_fn(f"\nEnsuring fingerprint-target app is up ...")
        target_container, target_err = _ensure_fingerprint_target()
        if target_container is None:
            log_fn(f"[ABORT] fingerprint-target unavailable: {target_err}")
            if save_fn is not None:
                for e in entries:
                    save_fn(e, None)
            return {}

    workers, cap_note = _capped_workers(entries, workers)
    parallel_note = f"  ({workers} parallel{', ' + cap_note if cap_note else ''})" if (workers > 1 or cap_note) else ""
    log_fn(f"Testing {n:,} client image(s) against {target_container}{parallel_note} ...\n")

    results: dict = {}
    skipped = 0
    _lock = threading.Lock()

    def _test_one(i, e):
        nonlocal skipped
        if stop_event is not None and stop_event.is_set():
            return

        tag = _client_image_tag(e)

        if not _image_exists(tag):
            log_fn(f"[{i:{pad}}/{n}] {tag}")
            log_fn(f"         SKIP  (not built)")
            with _lock:
                skipped += 1
            if save_fn is not None:
                save_fn(e, None)
            return

        client_container = f"{tag}-test"
        target_url = _client_target_url(e.get("http_client", ""), target_override)
        started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rc, out, err = _run_client_container(tag, client_container, _FP_TARGET_NETWORK, target_url)
        finished = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        subprocess.run(["docker", "rm", "-f", client_container], capture_output=True)

        status_code = None
        try:
            status_code = json.loads(out).get("status_code")
        except (json.JSONDecodeError, AttributeError):
            pass
        success = rc == 0 and status_code == 200
        result = {"success": success, "output": out, "error": err if rc != 0 else "",
                  "started_at": started, "finished_at": finished}

        log_fn(f"[{i:{pad}}/{n}] {tag}")
        log_fn(f"         {'PASS' if success else 'FAIL'}  status={status_code}")

        with _lock:
            results[tag] = result
            if save_fn is not None:
                save_fn(e, result)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_test_one, i, e): (i, e)
                for i, e in enumerate(entries, 1)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                i, _ = futs[fut]
                log_fn(f"[{i:{pad}}/{n}] ERROR: {exc}")

    passed    = sum(1 for v in results.values() if v["success"])
    failed    = sum(1 for v in results.values() if not v["success"])
    cancelled = n - len(results) - skipped
    parts     = [f"{passed} passed", f"{failed} failed"]
    if skipped:
        parts.append(f"{skipped} skipped (not built)")
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    log_fn(f"\nTest complete: {', '.join(parts)}.")
    return results


def _do_client_fingerprint(entries, log_fn=print, save_fn=None, stop_event=None,
                            target_override=""):
    """Fingerprint each client image: run it once against the persistent
    target app, capture the traffic the target observed, then move on.

    target_override, when set (the dashboard's fingerprint_target setting),
    points every client at that host instead of the built-in
    pqc-fingerprint-target container -- its own build/start is skipped in
    that case, since it's presumably an externally managed target.

    save_fn(entry, record) is called immediately after each image completes
    -- record is None if the client image isn't built. Skips images that
    aren't built.
    """
    n   = len(entries)
    pad = len(str(n))
    if target_override:
        target_container = target_override
        log_fn(f"\nUsing custom fingerprint target: {target_container}")
    else:
        log_fn(f"\nEnsuring fingerprint-target app is up ...")
        target_container, target_err = _ensure_fingerprint_target()
        if target_container is None:
            log_fn(f"[ABORT] fingerprint-target unavailable: {target_err}")
            if save_fn is not None:
                for e in entries:
                    save_fn(e, None)
            return
    log_fn(f"Fingerprinting {n:,} client image(s) against {target_container} ...\n")
    captured = 0

    for i, e in enumerate(entries, 1):
        if stop_event is not None and stop_event.is_set():
            log_fn(f"\n[CANCELLED] Fingerprint stopped after {i - 1} of {n} image(s).")
            break

        tag = _client_image_tag(e)
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        if not _image_exists(tag):
            log_fn(f"         SKIP  (not built)")
            if save_fn is not None:
                save_fn(e, None)
            continue

        record = _capture_client_fingerprint(tag, target_container, _FP_TARGET_NETWORK,
                                              http_client_name=e.get("http_client", ""),
                                              target_override=target_override)
        log_fn(f"         CAPTURED  status={record['status_code']}  "
               f"client_output={record['client_output'][:120]}")
        captured += 1
        if save_fn is not None:
            save_fn(e, record)

    log_fn(f"\nFingerprint complete: {captured}/{n} captured.")


def _do_fingerprint(entries, log_fn=print, save_fn=None, stop_event=None):
    """Start each container, capture network traffic for four probes against
    the running service (see _capture_fingerprint), then stop it.

    save_fn(entry, records) is called immediately after each image completes
    -- records is None when the container never came up, otherwise a dict
    keyed by call_type ('success'/'failure'/'method_not_allowed'/'malformed').
    Skips images that aren't built.
    """
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nFingerprinting {n:,} image(s) ...\n")
    captured = 0

    for i, e in enumerate(entries, 1):
        if stop_event is not None and stop_event.is_set():
            log_fn(f"\n[CANCELLED] Fingerprint stopped after {i - 1} of {n} image(s).")
            break

        tag  = _image_tag(e)
        name = tag
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        if not _image_exists(tag):
            log_fn(f"         SKIP  (not built)")
            if save_fn is not None:
                save_fn(e, None)
            continue

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", "0:8000", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            log_fn(f"         FAIL  (container did not start)")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            if save_fn is not None:
                save_fn(e, None)
            continue

        port = _get_host_port(name)
        if port == "?":
            log_fn(f"         FAIL  (port not assigned)")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            if save_fn is not None:
                save_fn(e, None)
            continue

        records = _capture_fingerprint(name, _docker_target_host(), port)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        log_fn(f"         CAPTURED  success={records['success']['status_code']}  "
               f"failure={records['failure']['status_code']}  "
               f"405={records['method_not_allowed']['status_code']}  "
               f"malformed={records['malformed']['status_code']}")
        captured += 1
        if save_fn is not None:
            save_fn(e, records)

    log_fn(f"\nFingerprint complete: {captured}/{n} captured.")


# ── Test ─────────────────────────────────────────────────────────────────────

def _do_test(entries, build_results=None, log_fn=print, save_fn=None, stop_event=None,
             fingerprint=False, save_fingerprint_fn=None, workers=4):
    """Start each container, test / and /version, stop it.

    save_fn(entry, result_dict) is called immediately after each image completes.
    stop_event (threading.Event) can be set externally to cancel the loop early.
    When fingerprint=True, also captures network traffic for four probes
    (see _capture_fingerprint) against the same running container -- before
    it gets stopped -- instead of starting it a second time; save_fingerprint_fn
    (entry, records) is called right after, records keyed by call_type.
    workers controls how many containers are tested in parallel -- each entry
    runs under its own uniquely-tagged container/port, so this is safe the
    same way _do_build's parallel docker build processes are.
    Returns {tag: {"success": bool, "root_ok": bool, "version_ok": bool,
                   "error": str, "version_data": dict|None, "output": str}}.
    """
    n   = len(entries)
    pad = len(str(n))
    workers, cap_note = _capped_workers(entries, workers)
    parallel_note = f"  ({workers} parallel{', ' + cap_note if cap_note else ''})" if (workers > 1 or cap_note) else ""
    log_fn(f"\nTesting {n:,} image(s){parallel_note} ...\n")
    results = {}
    _lock = threading.Lock()

    def _test_one(i, e):
        if stop_event is not None and stop_event.is_set():
            return

        tag  = _image_tag(e)
        name = tag
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        def _finish(result):
            with _lock:
                results[tag] = result
                if save_fn is not None:
                    save_fn(e, result)

        if build_results is not None and not build_results.get(tag, {}).get("success"):
            log_fn(f"         SKIP  (build failed)")
            _finish({"success": False, "root_ok": False,
                     "version_ok": False, "error": "build failed",
                     "version_data": None, "output": ""})
            return

        if not _image_exists(tag):
            log_fn(f"         SKIP  (not built)")
            _finish({"success": False, "root_ok": False,
                     "version_ok": False, "error": "image not found",
                     "version_data": None, "output": ""})
            return

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        proc = subprocess.run(
            ["docker", "run", "-d", "--name", name, "-p", "0:8000", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode != 0:
            log_fn(f"         FAIL  (container did not start)")
            for line in proc.stderr.strip().splitlines()[-3:]:
                log_fn(f"         | {line}")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            _finish({"success": False, "root_ok": False,
                     "version_ok": False, "error": "container start failed",
                     "version_data": None, "output": proc.stderr.strip()})
            return

        port        = _get_host_port(name)
        root_ok     = False
        version_ok  = False
        version_data = None
        fail_reason  = ""
        last_err     = None

        if port == "?":
            # Container may have exited immediately; check its state
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            logs = subprocess.run(
                ["docker", "logs", name],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            output_text = (logs.stdout + logs.stderr).strip()
            log_fn(f"         FAIL  (port not assigned, container state: {state})")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            _finish({"success": False, "root_ok": False,
                     "version_ok": False, "error": f"port not assigned ({state})",
                     "version_data": None, "output": output_text})
            return

        for path, check in [("/",        lambda d: d.get("message") == "Hello World"),
                             ("/version", lambda d: isinstance(d, dict) and len(d) > 0)]:
            passed = False
            last_data = None
            for _ in range(20):
                try:
                    with urllib.request.urlopen(
                        f"http://{_docker_target_host()}:{port}{path}", timeout=2
                    ) as r:
                        last_data = json.loads(r.read().decode())
                        if check(last_data):
                            passed = True
                    break
                except Exception as exc:
                    last_err = exc
                    time.sleep(0.5)

            if path == "/":
                root_ok = passed
            else:
                version_ok   = passed
                version_data = last_data

            if not passed:
                fail_reason = path
                break

        logs = subprocess.run(
            ["docker", "logs", name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        output_text = (logs.stdout + logs.stderr).strip()

        cache_hint = None
        if not (root_ok and version_ok):
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            log_fn(f"         container state: {state}  last-err: {last_err}")
            for line in output_text.splitlines()[:30]:
                log_fn(f"         | {line}")
            cache_hint = _stale_cache_hint(output_text, e.get("library", ""))
            if cache_hint:
                log_fn(f"         ⚠ {cache_hint}")

        if fingerprint:
            fp_records = _capture_fingerprint(name, _docker_target_host(), port)
            log_fn(f"         FINGERPRINT  success={fp_records['success']['status_code']}  "
                   f"failure={fp_records['failure']['status_code']}  "
                   f"405={fp_records['method_not_allowed']['status_code']}  "
                   f"malformed={fp_records['malformed']['status_code']}")
            if save_fingerprint_fn is not None:
                save_fingerprint_fn(e, fp_records)

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        ok = root_ok and version_ok
        if ok:
            log_fn(f"         PASS")
        else:
            log_fn(f"         FAIL  ({fail_reason} did not respond correctly)")

        error_text = fail_reason if not ok else ""
        if cache_hint:
            error_text = f"{error_text} -- {cache_hint}" if error_text else cache_hint

        _finish({
            "success":      ok,
            "root_ok":      root_ok,
            "version_ok":   version_ok,
            "error":        error_text,
            "version_data": version_data,
            "output":       output_text,
        })

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_test_one, i, e): (i, e)
                for i, e in enumerate(entries, 1)}
        for fut in as_completed(futs):
            try:
                fut.result()
            except Exception as exc:
                i, _ = futs[fut]
                log_fn(f"[{i:{pad}}/{n}] ERROR: {exc}")

    passed    = sum(1 for v in results.values() if v["success"])
    failed    = sum(1 for v in results.values() if not v["success"])
    cancelled = n - len(results)
    parts     = [f"{passed} passed", f"{failed} failed"]
    if cancelled:
        parts.append(f"{cancelled} cancelled")
    log_fn(f"\nTest complete: {', '.join(parts)}.")
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


def _do_remove(entries, log_fn=print):
    """Remove stopped containers and the Docker image for every entry (docker rmi)."""
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nRemoving {n:,} image(s) ...\n")

    removed = 0
    for i, e in enumerate(entries, 1):
        tag = _image_tag(e)
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        if not _image_exists(tag):
            log_fn(f"         NOT FOUND")
            continue

        n_containers = _remove_stopped_containers(tag)
        if n_containers:
            log_fn(f"         CONTAINERS  ({n_containers} stopped container(s) removed)")

        proc = subprocess.run(
            ["docker", "rmi", tag],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if proc.returncode == 0:
            log_fn(f"         REMOVED")
            removed += 1
        else:
            log_fn(f"         FAILED")
            for line in proc.stderr.strip().splitlines()[-3:]:
                log_fn(f"         | {line}")

    log_fn(f"\nDone: {removed}/{n} removed.")


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
        "-w", "--workers",
        type=int,
        default=4,
        metavar="N",
        help="Number of parallel docker build processes (default: 4)",
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
        "--cleanup",
        action="store_true",
        help="Remove stopped containers, dangling images, and unused networks "
             "(never touches build cache -- see --prune-build-cache)",
    )
    actions.add_argument(
        "--cleanup-full",
        action="store_true",
        dest="cleanup_full",
        help="Full cleanup: also removes all unused images and volumes",
    )
    actions.add_argument(
        "--cleanup-dry-run",
        action="store_true",
        dest="cleanup_dry_run",
        help="Show what would be removed by --cleanup (no changes made)",
    )
    actions.add_argument(
        "--prune-build-cache",
        action="store_true",
        dest="prune_build_cache",
        help="Remove Docker's ENTIRE build cache, including the shared Maven/"
             "npm/pip/Composer/NuGet package caches -- separate from "
             "--cleanup on purpose, since this brings back registry "
             "rate-limiting risk. Only use for disk pressure or a suspected "
             "corrupt cache entry.",
    )
    actions.add_argument(
        "--prune-build-cache-dry-run",
        action="store_true",
        dest="prune_build_cache_dry_run",
        help="Show current build cache disk usage without removing anything",
    )
    actions.add_argument(
        "--remove-orphans",
        action="store_true",
        dest="remove_orphans",
        help="Remove Docker images tagged 'pqc-*' that no longer correspond to "
             "anything in the current images/ tree (e.g. after a registry fix "
             "narrowed a combo, or a language was dropped)",
    )
    actions.add_argument(
        "--remove-orphans-dry-run",
        action="store_true",
        dest="remove_orphans_dry_run",
        help="Show which images --remove-orphans would remove (no changes made)",
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

    lists = p.add_argument_group("lists")
    lists.add_argument(
        "--image-list", metavar="FILE", dest="image_list",
        help="Text file with one context path per line; only these images are included",
    )
    lists.add_argument(
        "--ignore-list", metavar="FILE", dest="ignore_list",
        help="Text file with context paths or image tags to skip",
    )

    return p


def main():
    # Reconfigure stdout/stderr to replace unencodable characters (e.g. Docker
    # progress symbols on Windows cp1252 terminals) instead of raising an error.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = _build_parser()
    args   = parser.parse_args()

    if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all
            or args.cleanup or args.cleanup_full or args.cleanup_dry_run
            or args.prune_build_cache or args.prune_build_cache_dry_run
            or args.remove_orphans or args.remove_orphans_dry_run):
        parser.print_help()
        sys.exit(0)

    # ── Cleanup is independent of the images tree ─────────────────────────────
    if args.cleanup or args.cleanup_full or args.cleanup_dry_run:
        _require_docker()
        _do_docker_cleanup(full=args.cleanup_full, dry_run=args.cleanup_dry_run)
        if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all):
            sys.exit(0)

    if args.prune_build_cache or args.prune_build_cache_dry_run:
        _require_docker()
        _do_build_cache_prune(dry_run=args.prune_build_cache_dry_run)
        if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all):
            sys.exit(0)

    if args.remove_orphans or args.remove_orphans_dry_run:
        _require_docker()
        _do_remove_orphans(dry_run=args.remove_orphans_dry_run)
        if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all):
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

    if args.image_list:
        path_set = _load_path_set(args.image_list)
        entries  = _filter_by_list(entries, path_set)

    if args.ignore_list:
        ignore_set = _load_path_set(args.ignore_list)
        entries    = _filter_by_ignore(entries, ignore_set)

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
        build_results = _do_build(entries, no_cache=args.no_cache, skip_existing=args.skip_existing, workers=args.workers)

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
        _do_test(entries, build_results, workers=args.workers)

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
