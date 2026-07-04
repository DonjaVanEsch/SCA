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
      --cleanup             Remove stopped containers, dangling images, build cache,
                            and unused networks.
      --cleanup-full        Same as --cleanup, plus all unused images and volumes.
      --cleanup-dry-run     Preview what would be removed (no changes made).

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
import json
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    fw  = e["framework"].lower().replace("/", "_")
    lib = e["library"].lower().replace("/", "_")
    return (
        f"pqc-{e['language']}-{e['lang_ver']}"
        f"-{fw}-{e['fw_ver']}"
        f"-{lib}-{e['lib_ver']}"
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


# ── Build ─────────────────────────────────────────────────────────────────────

def _do_build(entries, no_cache=False, skip_existing=False, log_fn=print,
              save_fn=None, stop_event=None, workers=4):
    """Build a Docker image for every entry.

    save_fn(entry, result_dict) is called immediately after each image completes.
    stop_event (threading.Event) can be set externally to cancel the loop early.
    workers controls how many docker build processes run in parallel.
    Returns {tag: {"success": bool, "output": str, "elapsed": float, "skipped": bool}}.
    """
    n = len(entries)
    pad = len(str(n))
    note = "  (--no-cache)" if no_cache else ""
    parallel_note = f"  ({workers} parallel)" if workers > 1 else ""
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

def _do_run(entries, build_results=None, log_fn=print):
    """Start a detached container for every entry.

    build_results: dict {tag: result_dict} from _do_build; entries whose build
    failed are automatically skipped.  Pass None to run without a preceding
    build step (images must already exist in that case).
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
        url  = f"http://localhost:{port}"
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

    section("Removing build cache")
    r = run("docker", "builder", "prune", "-af")
    for line in (r.stdout or r.stderr or "").strip().splitlines():
        log_fn(f"  {line}")
    log_fn("  OK" if r.returncode == 0 else "  FAILED")

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


# ── Test ─────────────────────────────────────────────────────────────────────

def _do_test(entries, build_results=None, log_fn=print, save_fn=None, stop_event=None):
    """Start each container, test / and /version, stop it.

    save_fn(entry, result_dict) is called immediately after each image completes.
    stop_event (threading.Event) can be set externally to cancel the loop early.
    Returns {tag: {"success": bool, "root_ok": bool, "version_ok": bool,
                   "error": str, "version_data": dict|None}}.
    """
    n   = len(entries)
    pad = len(str(n))
    log_fn(f"\nTesting {n:,} image(s) ...\n")
    results = {}

    for i, e in enumerate(entries, 1):
        if stop_event is not None and stop_event.is_set():
            log_fn(f"\n[CANCELLED] Test stopped after {i - 1} of {n} image(s).")
            break

        tag  = _image_tag(e)
        name = tag
        log_fn(f"[{i:{pad}}/{n}] {tag}")

        if build_results is not None and not build_results.get(tag, {}).get("success"):
            log_fn(f"         SKIP  (build failed)")
            results[tag] = {"success": False, "root_ok": False,
                            "version_ok": False, "error": "build failed",
                            "version_data": None}
            if save_fn is not None:
                save_fn(e, results[tag])
            continue

        if not _image_exists(tag):
            log_fn(f"         SKIP  (not built)")
            results[tag] = {"success": False, "root_ok": False,
                            "version_ok": False, "error": "image not found",
                            "version_data": None}
            if save_fn is not None:
                save_fn(e, results[tag])
            continue

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
            results[tag] = {"success": False, "root_ok": False,
                            "version_ok": False, "error": "container start failed",
                            "version_data": None}
            if save_fn is not None:
                save_fn(e, results[tag])
            continue

        port        = _get_host_port(name)
        root_ok     = False
        version_ok  = False
        version_data = None
        fail_reason  = ""

        if port == "?":
            # Container may have exited immediately; check its state
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            log_fn(f"         FAIL  (port not assigned, container state: {state})")
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            results[tag] = {"success": False, "root_ok": False,
                            "version_ok": False, "error": f"port not assigned ({state})",
                            "version_data": None}
            if save_fn is not None:
                save_fn(e, results[tag])
            continue

        for path, check in [("/",        lambda d: d.get("message") == "Hello World"),
                             ("/version", lambda d: isinstance(d, dict) and len(d) > 0)]:
            passed = False
            last_data = None
            last_err  = None
            for _ in range(20):
                try:
                    with urllib.request.urlopen(
                        f"http://localhost:{port}{path}", timeout=2
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

        if not (root_ok and version_ok):
            state = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", name],
                capture_output=True, text=True,
            ).stdout.strip()
            logs = subprocess.run(
                ["docker", "logs", name],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            all_lines = (logs.stdout + logs.stderr).strip().splitlines()
            log_fn(f"         container state: {state}  last-err: {last_err}")
            for line in all_lines[:30]:
                log_fn(f"         | {line}")

        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

        ok = root_ok and version_ok
        if ok:
            log_fn(f"         PASS")
        else:
            log_fn(f"         FAIL  ({fail_reason} did not respond correctly)")

        results[tag] = {
            "success":      ok,
            "root_ok":      root_ok,
            "version_ok":   version_ok,
            "error":        fail_reason if not ok else "",
            "version_data": version_data,
        }
        if save_fn is not None:
            save_fn(e, results[tag])

    passed = sum(1 for v in results.values() if v["success"])
    failed = len(results) - passed
    log_fn(f"\nTest complete: {passed} passed, {failed} failed.")
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
        help="Remove stopped containers, dangling images, build cache, and unused networks",
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

    if not (args.list or args.build or args.run or args.test or args.remove or args.stop or args.stop_all or args.cleanup or args.cleanup_full or args.cleanup_dry_run):
        parser.print_help()
        sys.exit(0)

    # ── Cleanup is independent of the images tree ─────────────────────────────
    if args.cleanup or args.cleanup_full or args.cleanup_dry_run:
        _require_docker()
        _do_docker_cleanup(full=args.cleanup_full, dry_run=args.cleanup_dry_run)
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
