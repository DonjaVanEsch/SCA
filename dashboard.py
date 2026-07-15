"""
PQC Dashboard – Flask web server.

Run:  python dashboard.py
Opens: http://localhost:5050
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import db
import manager
import check_updates
import registry_writer

app = Flask(__name__, static_folder=str(PROJECT_ROOT / "static"))

# ── Settings (Docker host, etc.) ──────────────────────────────────────────────

SETTINGS_FILE = PROJECT_ROOT / "dashboard_settings.json"
_DEFAULT_SETTINGS = {"docker_host": "", "default_workers": 4, "accessibility_mode": False}

# Fingerprinting is gated behind an environment flag -- capturing real network
# traffic against a running container is invasive enough that it should stay
# opt-in per deployment, not a per-user dashboard setting.
FINGERPRINT_ENABLED = os.environ.get("PQC_ENABLE_FINGERPRINT", "false").strip().lower() in ("1", "true", "yes")

# Multi-host scoping (the "local"/host badge that switches which Docker
# engine's build/tested status is shown) is only useful once more than one
# host is actually in play -- off by default, same on/off mechanism as
# fingerprinting, until it's needed again.
HOST_SCOPE_ENABLED = os.environ.get("PQC_ENABLE_HOST_SCOPE", "false").strip().lower() in ("1", "true", "yes")


def _load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return dict(_DEFAULT_SETTINGS)
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_SETTINGS)
    return {**_DEFAULT_SETTINGS, **data}


def _save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def _apply_docker_host(docker_host: str) -> None:
    """Point the `docker` CLI (and therefore every manager.py subprocess call)
    at a remote engine over SSH, or back at the local engine when cleared.

    Only ssh://user@host is supported — DOCKER_HOST auth over SSH is key-based
    (via the OS ssh client / ssh-agent), so make sure the host's public key is
    in the remote's authorized_keys before saving this.
    """
    if docker_host:
        os.environ["DOCKER_HOST"] = docker_host
    else:
        os.environ.pop("DOCKER_HOST", None)


_apply_docker_host(_load_settings()["docker_host"])


def _current_host() -> str:
    """The Docker host every build/test/status query is currently scoped to."""
    return _load_settings()["docker_host"]

# ── Active job registry ───────────────────────────────────────────────────────
# {job_id: {"q": Queue, "done": bool, "action": str}}
_jobs: dict = {}
_jobs_lock  = threading.Lock()


def _new_job(action: str) -> tuple[str, queue.Queue]:
    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    with _jobs_lock:
        _jobs[job_id] = {"q": q, "done": False, "action": action,
                         "stop_event": threading.Event()}
    return job_id, q


def _finish_job(job_id: str) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["done"] = True
            _jobs[job_id]["q"].put(None)   # sentinel → SSE stream ends


# ── Helpers ───────────────────────────────────────────────────────────────────

def _annotate_docker_exists(items: list[dict]) -> list[dict]:
    """Add `docker_exists` / `running` / `run_url` to each row.

    docker_exists: whether its image is actually present on the active
    Docker engine right now (vs. just recorded as built in the DB -- it may
    have since been pruned or removed manually). None means the engine
    couldn't be reached, so presence is unknown.

    running / run_url: whether a container for this image is currently
    running, and if so, the URL to reach it -- resolved through the same
    host logic manager.py uses for its own "Launch" log output, so this is
    never "localhost" when the active Docker engine is remote (SSH).

    manager._docker_target_host() says "localhost" when Docker is local to
    *this process* -- correct for the CLI, but dashboard.py's caller is a
    browser that may be on a different machine (e.g. dashboard.py + Docker
    both run on a Linux box, opened from a Windows browser at
    http://<server-ip>:5050). In that case "localhost" would resolve to the
    browser's own machine, not the server, so fall back to whatever
    hostname the browser actually used to reach us.
    """
    existing = manager.list_existing_image_repos()
    running  = manager.list_running_containers()
    target_host = manager._docker_target_host()
    if target_host == "localhost":
        browser_host = request.host.split(":")[0]
        if browser_host not in ("localhost", "127.0.0.1"):
            target_host = browser_host
    for item in items:
        item["docker_exists"] = None if existing is None else item.get("image_tag") in existing
        port = running.get(item.get("image_tag")) if running else None
        item["running"] = None if running is None else bool(port)
        item["run_url"] = f"http://{target_host}:{port}" if port else None
    return items


def _entries_from_db_rows(rows: list[dict]) -> list[dict]:
    """Convert image_details rows to manager.py entry dicts."""
    entries = []
    for r in rows:
        entries.append({
            "language":  r["language"],
            "lang_ver":  r["lang_version"],
            "framework": r["framework"],
            "fw_ver":    r["fw_version"],
            "library":   r["library"],
            "lib_ver":   r["lib_version"],
            "path":      "images/" + r["context_path"].replace("\\", "/"),
            "_id":       r["id"],           # kept for DB writes
        })
    return entries


def _entries_from_client_db_rows(rows: list[dict]) -> list[dict]:
    """Convert client_image_details rows to manager.py client-entry dicts."""
    entries = []
    for r in rows:
        entries.append({
            "language":    r["language"],
            "lang_ver":    r["lang_version"],
            "http_client": r["http_client"],
            "hc_ver":      r["http_client_version"],
            "path":        "images_clients/" + r["context_path"].replace("\\", "/"),
            "_id":         r["id"],
        })
    return entries


# ── Static serving ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "dashboard.html")


# ── Initialisation ────────────────────────────────────────────────────────────

@app.route("/api/init", methods=["POST"])
def init():
    """Load registry metadata and sync image contexts from disk."""
    reg_counts  = db.load_registry()
    total, ins, rem = db.sync_images()
    ctotal, cins, crem = db.sync_client_images()
    return jsonify({
        "registry": reg_counts,
        "images": {"total": total, "inserted": ins, "removed": rem},
        "client_images": {"total": ctotal, "inserted": cins, "removed": crem},
    })


@app.route("/api/sync", methods=["POST"])
def sync():
    """Sync image contexts from the images/ and images_clients/ directories
    (no registry reload)."""
    total, ins, rem = db.sync_images()
    ctotal, cins, crem = db.sync_client_images()
    return jsonify({
        "total": total, "inserted": ins, "removed": rem,
        "client_images": {"total": ctotal, "inserted": cins, "removed": crem},
    })


# ── Reference / filter data ───────────────────────────────────────────────────

@app.route("/api/filters")
def get_filters():
    """Return filter options, cascaded by any active filter params in the query string."""
    active = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    any_active = any(v for v in active.values())
    if any_active:
        return jsonify(db.get_cascading_filter_options(active))
    return jsonify(db.get_filter_options())


@app.route("/api/reference")
def get_reference():
    return jsonify(db.get_reference_data())


@app.route("/api/runs")
def get_runs():
    return jsonify(db.get_runs())


@app.route("/api/runs/summary")
def get_run_summary_route():
    """Pass/fail counts, duration, and the saved narrative log text for one
    run -- what the Reports tab's run summary/"view log" panel reads.
    Query params: name (required), host (default '', matching the active
    Docker host convention used everywhere else)."""
    name = request.args.get("name", "")
    host = request.args.get("host", "")
    if not name:
        return jsonify({"error": "name is required"}), 400
    summary = db.get_run_summary(name, host)
    if summary is None:
        return jsonify({"error": "Run not found"}), 404
    return jsonify(summary)


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats(host=_current_host()))


# ── Image list ────────────────────────────────────────────────────────────────
# Every route below is scoped to the currently active Docker host (Settings
# panel) -- switching host gives a fresh built/tested matrix without losing
# another host's recorded status.

@app.route("/api/images")
def get_images():
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version", "run",
    )}
    include_ignored = request.args.get("include_ignored", "true").lower() == "true"
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(200, int(request.args.get("per_page", 50))))
    sort_by  = request.args.get("sort_by",  "")
    sort_dir = request.args.get("sort_dir", "asc")
    result = db.get_images(filters, page, per_page, include_ignored, sort_by, sort_dir,
                            host=_current_host())
    _annotate_docker_exists(result["items"])
    return jsonify(result)


@app.route("/api/images/ids")
def get_all_ids():
    """Return all image ids matching current filters (for select-all)."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    include_ignored = request.args.get("include_ignored", "true").lower() == "true"
    status = request.args.get("status", "")
    ids = db.get_all_ids_for_filter(filters, include_ignored, status, host=_current_host())
    return jsonify({"ids": ids})


@app.route("/api/images/by-ids")
def get_images_by_ids_route():
    """Return full details for the given image ids, independent of the current
    table filters (used by the "review selection" panel)."""
    ids_param = request.args.get("ids", "")
    ids = [int(i) for i in ids_param.split(",") if i.strip().lstrip("-").isdigit()]
    items = db.get_images_by_ids(ids, host=_current_host())
    _annotate_docker_exists(items)
    return jsonify({"items": items})


@app.route("/api/images/ignored")
def get_ignored_images_route():
    """Return full details for every currently-ignored image (used by the
    "review ignore list" panel)."""
    items = db.get_ignored_images(host=_current_host())
    _annotate_docker_exists(items)
    return jsonify({"items": items})


# ── Actions ───────────────────────────────────────────────────────────────────

@app.route("/api/action", methods=["POST"])
def action():
    """Start a build / test / fingerprint / remove / stop job for the given image ids.

    Body: {"action": "build"|"test"|"fingerprint"|"remove"|"stop",
           "image_ids": [1, 2, ...],
           "options": {"no_cache": false, "skip_existing": false, "fingerprint": false}}
    Returns: {"job_id": "..."}

    "fingerprint" (options.fingerprint=true on a "test" action, or its own
    "fingerprint" action) captures network traffic -- one successful and one
    failed call -- against the running container. When both test and
    fingerprint are requested together, the frontend sends a single "test"
    action with options.fingerprint=true so the container only starts once.
    """
    data       = request.json or {}
    action_str = data.get("action", "")
    image_ids  = [int(i) for i in data.get("image_ids", [])]
    opts       = data.get("options", {})
    run_name   = data.get("run_name", "")

    if action_str not in ("build", "test", "fingerprint", "remove", "stop", "mark_success", "run_container"):
        return jsonify({"error": f"Unknown action: {action_str}"}), 400
    if not image_ids:
        return jsonify({"error": "No image ids provided"}), 400
    if not FINGERPRINT_ENABLED and (action_str == "fingerprint" or opts.get("fingerprint")):
        return jsonify({"error": "Fingerprinting is disabled (set PQC_ENABLE_FINGERPRINT=true to enable)"}), 400

    host = _current_host()
    rows = db.get_images_by_ids(image_ids, host=host)
    if not rows:
        return jsonify({"error": "No matching images found"}), 404

    entries = _entries_from_db_rows(rows)
    job_id, q = _new_job(action_str)

    log_lines = []

    def log(msg=""):
        text = str(msg)
        log_lines.append(text)
        q.put(text)

    def run():
        run_id     = db.get_or_create_run(run_name, host=host) if run_name else None
        stop_event = _jobs[job_id]["stop_event"]

        def _save_fp(entry, records):
            db.save_fingerprint_results(entry["_id"], records, run_id, host=host)

        try:
            if action_str == "build":
                def _save_build(entry, r):
                    db.save_build_result(
                        entry["_id"], r.get("success", False),
                        r.get("output", ""),
                        r.get("started_at"), r.get("finished_at"),
                        run_id, host=host,
                    )
                manager._do_build(
                    entries,
                    no_cache=bool(opts.get("no_cache")),
                    skip_existing=bool(opts.get("skip_existing")),
                    workers=int(opts.get("workers", 4)),
                    log_fn=log,
                    save_fn=_save_build,
                    stop_event=stop_event,
                )

            elif action_str == "test":
                def _save_test(entry, r):
                    db.save_test_result(
                        entry["_id"],
                        r.get("success",    False),
                        r.get("root_ok",    False),
                        r.get("version_ok", False),
                        r.get("error",      ""),
                        r.get("version_data"),
                        r.get("output",     ""),
                        run_id, host=host,
                    )
                fingerprint = bool(opts.get("fingerprint"))
                manager._do_test(
                    entries,
                    log_fn=log,
                    save_fn=_save_test,
                    stop_event=stop_event,
                    fingerprint=fingerprint,
                    save_fingerprint_fn=_save_fp if fingerprint else None,
                    workers=int(opts.get("workers", 4)),
                )

            elif action_str == "fingerprint":
                manager._do_fingerprint(
                    entries,
                    log_fn=log,
                    save_fn=_save_fp,
                    stop_event=stop_event,
                )

            elif action_str == "run_container":
                manager._do_run(entries, log_fn=log)

            elif action_str == "remove":
                manager._do_remove(entries, log_fn=log)

            elif action_str == "stop":
                manager._do_stop(entries, log_fn=log)

            elif action_str == "mark_success":
                now = datetime.now(timezone.utc).isoformat()
                total_imgs = len(rows)
                for idx, row in enumerate(rows, 1):
                    if stop_event.is_set():
                        break
                    img_id  = row["id"]
                    img_tag = row["image_tag"]
                    db.save_build_result(img_id, True,
                                         "Manually marked as successful",
                                         now, now, run_id, host=host)
                    db.save_test_result(img_id, True, True, True,
                                        "", None, "", run_id, host=host)
                    log(f"[{idx}/{total_imgs}] {img_tag} — MARKED OK")

        except Exception as exc:
            log(f"ERROR: {exc}")
        finally:
            if stop_event.is_set():
                log("[CANCELLED] Run was interrupted by the user.")
            if run_id is not None:
                status = "interrupted" if stop_event.is_set() else "completed"
                try:
                    db.update_run_status(run_id, status)
                    db.save_run_log(run_id, "\n".join(log_lines))
                except Exception:
                    pass
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


# ── Client images (Server/Client dashboard mode) ─────────────────────────────
# Mirrors the server-side routes above, over the simpler client 2D matrix.
# Client images have no "test" action -- build + fingerprint is the whole
# lifecycle (see client_build_results/client_fingerprints table comments).

@app.route("/api/client-stats")
def get_client_stats_route():
    return jsonify(db.get_client_stats(host=_current_host()))


@app.route("/api/client-filters")
def get_client_filters():
    return jsonify(db.get_client_filter_options())


@app.route("/api/client-images")
def get_client_images_route():
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "http_client", "http_client_version",
    )}
    include_ignored = request.args.get("include_ignored", "true").lower() == "true"
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(200, int(request.args.get("per_page", 50))))
    sort_by  = request.args.get("sort_by",  "")
    sort_dir = request.args.get("sort_dir", "asc")
    result = db.get_client_images(filters, page, per_page, include_ignored, sort_by, sort_dir,
                                  host=_current_host())
    _annotate_docker_exists(result["items"])
    return jsonify(result)


@app.route("/api/client-images/ids")
def get_all_client_ids():
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "http_client", "http_client_version",
    )}
    include_ignored = request.args.get("include_ignored", "true").lower() == "true"
    ids = db.get_all_client_ids_for_filter(filters, include_ignored, host=_current_host())
    return jsonify({"ids": ids})


@app.route("/api/client-fingerprints")
def list_client_fingerprints():
    client_image_id = request.args.get("client_image_id", "")
    cid = int(client_image_id) if client_image_id.isdigit() else None
    return jsonify({"items": db.get_client_fingerprints(cid)})


@app.route("/api/client-fingerprints/<int:fp_id>/report")
def client_fingerprint_report(fp_id):
    report = db.get_client_fingerprint_report(fp_id)
    if report is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(report)


@app.route("/api/client-action", methods=["POST"])
def client_action():
    """Start a build / test / fingerprint / remove / run / stop job for the
    given client-image ids.

    Body: {"action": "build"|"test"|"fingerprint"|"remove"|"run"|"stop",
           "client_image_ids": [1, 2, ...],
           "options": {"no_cache": false, "skip_existing": false}}
    Returns: {"job_id": "..."}
    """
    data       = request.json or {}
    action_str = data.get("action", "")
    ids        = [int(i) for i in data.get("client_image_ids", [])]
    opts       = data.get("options", {})
    run_name   = data.get("run_name", "")

    if action_str not in ("build", "test", "fingerprint", "remove", "run", "stop"):
        return jsonify({"error": f"Unknown action: {action_str}"}), 400
    if not ids:
        return jsonify({"error": "No client image ids provided"}), 400
    if not FINGERPRINT_ENABLED and action_str == "fingerprint":
        return jsonify({"error": "Fingerprinting is disabled (set PQC_ENABLE_FINGERPRINT=true to enable)"}), 400

    host = _current_host()
    rows = db.get_client_images_by_ids(ids, host=host)
    if not rows:
        return jsonify({"error": "No matching client images found"}), 404

    entries = _entries_from_client_db_rows(rows)
    job_id, q = _new_job(action_str)

    log_lines = []

    def log(msg=""):
        text = str(msg)
        log_lines.append(text)
        q.put(text)

    def run():
        run_id     = db.get_or_create_run(run_name, host=host) if run_name else None
        stop_event = _jobs[job_id]["stop_event"]

        try:
            if action_str == "build":
                def _save_build(entry, r):
                    db.save_client_build_result(
                        entry["_id"], r.get("success", False),
                        r.get("output", ""),
                        r.get("started_at"), r.get("finished_at"),
                        run_id, host=host,
                    )
                manager._do_client_build(
                    entries,
                    no_cache=bool(opts.get("no_cache")),
                    skip_existing=bool(opts.get("skip_existing")),
                    workers=int(opts.get("workers", 4)),
                    log_fn=log,
                    save_fn=_save_build,
                    stop_event=stop_event,
                )

            elif action_str == "test":
                def _save_ctest(entry, r):
                    if r is None:  # not built -- skipped, nothing to record
                        return
                    db.save_client_test_result(
                        entry["_id"], r.get("success", False),
                        r.get("output", ""), r.get("error", ""),
                        r.get("started_at"), r.get("finished_at"),
                        run_id, host=host,
                    )
                manager._do_client_test(
                    entries,
                    workers=int(opts.get("workers", 4)),
                    log_fn=log,
                    save_fn=_save_ctest,
                    stop_event=stop_event,
                )

            elif action_str == "fingerprint":
                def _save_cfp(entry, record):
                    db.save_client_fingerprint_results(entry["_id"], record, run_id, host=host)
                manager._do_client_fingerprint(
                    entries,
                    log_fn=log,
                    save_fn=_save_cfp,
                    stop_event=stop_event,
                )

            elif action_str == "remove":
                manager._do_client_remove(entries, log_fn=log)

            elif action_str == "run":
                manager._do_client_run(entries, log_fn=log)

            elif action_str == "stop":
                manager._do_client_stop(entries, log_fn=log)

        except Exception as exc:
            log(f"ERROR: {exc}")
        finally:
            if stop_event.is_set():
                log("[CANCELLED] Run was interrupted by the user.")
            if run_id is not None:
                status = "interrupted" if stop_event.is_set() else "completed"
                try:
                    db.update_run_status(run_id, status)
                    db.save_run_log(run_id, "\n".join(log_lines))
                except Exception:
                    pass
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/docker-cleanup", methods=["POST"])
def docker_cleanup():
    """Run Docker cleanup. Body: {"full": false, "dry_run": false}"""
    data    = request.json or {}
    full    = bool(data.get("full", False))
    dry_run = bool(data.get("dry_run", False))
    mode    = "dry-run" if dry_run else ("full" if full else "normal")
    job_id, q = _new_job(f"docker-cleanup-{mode}")

    def run():
        try:
            manager._do_docker_cleanup(
                full=full,
                dry_run=dry_run,
                log_fn=lambda msg="": q.put(str(msg)),
            )
        except Exception as exc:
            q.put(f"ERROR: {exc}")
        finally:
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/remove-orphans", methods=["POST"])
def remove_orphans():
    """Remove Docker images tagged 'pqc-*' with no context in the current
    images/ tree. Body: {"dry_run": false}"""
    data    = request.json or {}
    dry_run = bool(data.get("dry_run", False))
    job_id, q = _new_job(f"remove-orphans-{'dry-run' if dry_run else 'live'}")

    def run():
        try:
            manager._do_remove_orphans(
                dry_run=dry_run,
                log_fn=lambda msg="": q.put(str(msg)),
            )
        except Exception as exc:
            q.put(f"ERROR: {exc}")
        finally:
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stop-all", methods=["POST"])
def stop_all():
    """Stop all pqc-* containers (does not use image_ids)."""
    job_id, q = _new_job("stop-all")

    def run():
        try:
            manager._do_stop_all(log_fn=lambda msg="": q.put(str(msg)))
        except Exception as exc:
            q.put(f"ERROR: {exc}")
        finally:
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


# ── Cancel job ───────────────────────────────────────────────────────────────

@app.route("/api/cancel/<job_id>", methods=["POST"])
def cancel_job(job_id: str):
    """Signal a running build/test job to stop after the current image."""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    job["stop_event"].set()
    return jsonify({"ok": True})


# ── Settings ──────────────────────────────────────────────────────────────────

@app.route("/api/settings")
def get_settings():
    settings = _load_settings()
    # Read-only, environment-controlled -- not persisted in the settings file.
    settings["fingerprint_enabled"] = FINGERPRINT_ENABLED
    settings["host_scope_enabled"] = HOST_SCOPE_ENABLED
    return jsonify(settings)


@app.route("/api/settings", methods=["POST"])
def set_settings():
    """Body: {"docker_host": "ssh://user@host" | "", "default_workers": 4}"""
    data        = request.json or {}
    docker_host = str(data.get("docker_host", "")).strip()
    if docker_host and not docker_host.startswith("ssh://"):
        return jsonify({"error": "docker_host must start with ssh:// (or be empty for local Docker)"}), 400

    settings = _load_settings()
    settings["docker_host"] = docker_host
    if "default_workers" in data:
        try:
            default_workers = int(data["default_workers"])
        except (TypeError, ValueError):
            return jsonify({"error": "default_workers must be a number"}), 400
        settings["default_workers"] = max(1, min(16, default_workers))
    if "accessibility_mode" in data:
        settings["accessibility_mode"] = bool(data["accessibility_mode"])
    _save_settings(settings)
    _apply_docker_host(docker_host)
    return jsonify({"ok": True, "settings": settings})


@app.route("/api/settings/test-docker", methods=["POST"])
def test_docker_connection():
    """Run `docker version` against a host. Body: {"docker_host": "ssh://..." | ""}
    Tests the given value directly, without saving it or touching the
    process-wide DOCKER_HOST — lets the user try a host before committing to it.
    """
    data        = request.json or {}
    docker_host = str(data.get("docker_host", "")).strip()
    ok, output = manager.test_connection(docker_host)
    return jsonify({"ok": ok, "output": output})


# ── Ignore list ───────────────────────────────────────────────────────────────

@app.route("/api/ignore", methods=["POST"])
def set_ignore():
    data      = request.json or {}
    image_ids = [int(i) for i in data.get("image_ids", [])]
    ignored   = bool(data.get("ignored", True))
    reason    = data.get("reason", "")
    db.set_ignored(image_ids, ignored, reason)
    return jsonify({"ok": True, "count": len(image_ids)})


# ── SSE stream ────────────────────────────────────────────────────────────────

@app.route("/api/stream/<job_id>")
def stream(job_id: str):
    """Server-Sent Events endpoint for live job output."""
    def generate():
        with _jobs_lock:
            job = _jobs.get(job_id)
        if not job:
            yield "data: [ERROR] Job not found\n\n"
            return

        q: queue.Queue = job["q"]
        # Reconnect case: job finished and queue already drained by previous connection
        if job["done"] and q.empty():
            yield "data: [DONE]\n\n"
            return
        while True:
            try:
                line = q.get(timeout=25)
                if line is None:           # sentinel: job finished
                    yield "data: [DONE]\n\n"
                    break
                # Escape newlines inside the data value so SSE stays valid
                escaped = line.replace("\n", "\ndata: ")
                yield f"data: {escaped}\n\n"
            except queue.Empty:
                # Job may have finished while we were waiting for the next chunk
                if job["done"]:
                    yield "data: [DONE]\n\n"
                    break
                yield ": keepalive\n\n"    # keep connection alive

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering":"no",
        },
    )


# ── Reports ───────────────────────────────────────────────────────────────────

@app.route("/api/reports/test")
def test_reports():
    """`host` is an explicit filter here (defaults to all hosts) so history can
    be inspected across every host that's ever tested here, not just the
    currently active one."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version", "success", "run", "host",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_test_reports(filters, page, per_page))


@app.route("/api/reports/pending")
def pending_reports():
    """Pending is inherently host-scoped -- always the currently active host."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_pending_images(filters, page, per_page, host=_current_host()))


@app.route("/api/reports/build")
def build_reports():
    """`host` is an explicit filter here (defaults to all hosts) -- see test_reports."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version", "success", "run", "host",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_build_reports(filters, page, per_page))


# ── Crypto Agility (C.A.M. Component 2) ───────────────────────────────────────

@app.route("/api/crypto-agility")
def crypto_agility():
    filters = {k: request.args.get(k, "") for k in ("language", "library")}
    return jsonify(db.get_crypto_agility(filters))


@app.route("/api/migration-paths")
def migration_paths():
    filters = {k: request.args.get(k, "") for k in ("language", "library")}
    return jsonify(db.get_migration_paths(filters))


@app.route("/api/platform-constraints/languages")
def language_platform_constraints():
    filters = {k: request.args.get(k, "") for k in ("language",)}
    return jsonify(db.get_language_platform_constraints(filters))


@app.route("/api/platform-constraints/frameworks")
def framework_platform_constraints():
    filters = {k: request.args.get(k, "") for k in ("language", "framework")}
    return jsonify(db.get_framework_platform_constraints(filters))


@app.route("/api/vulnerabilities")
def vulnerabilities():
    filters = {k: request.args.get(k, "") for k in ("language", "library")}
    return jsonify(db.get_vulnerabilities(filters))


# ── Update-availability scanner ───────────────────────────────────────────────

def _release_url(item: dict) -> str | None:
    """Link to the package's own registry page for the exact detected
    version -- lets the user skim the description/changelog/source link
    there to judge an Include before ever building anything. Deliberately
    just a URL-pattern construction (no extra network call, no rate-limit
    risk -- see the Maven Central 429 hit earlier this session) rather than
    trying to resolve an actual changelog link per package."""
    lang, pkg, ver = item["language"], item["package_id"], item["latest_version"]
    if not pkg or not ver:
        return None
    if lang == "python":
        return f"https://pypi.org/project/{pkg}/{ver}/"
    if lang == "node":
        return f"https://www.npmjs.com/package/{pkg}/v/{ver}"
    if lang == "php":
        return f"https://packagist.org/packages/{pkg}#{ver}"
    if lang == "java":
        if ":" not in pkg:
            return None
        group, artifact = pkg.split(":", 1)
        return f"https://search.maven.org/artifact/{group}/{artifact}/{ver}/jar"
    if lang == "dotnet":
        return f"https://www.nuget.org/packages/{pkg}/{ver}"
    return None


def _with_release_urls(items: list) -> list:
    for item in items:
        item["release_url"] = _release_url(item)
    return items


@app.route("/api/updates")
def list_updates():
    """Pending framework/library updates not yet dismissed (or all, with
    ?include_dismissed=1) -- detection only, see scripts/check_updates.py."""
    include_dismissed = request.args.get("include_dismissed") == "1"
    return jsonify({
        "count": db.count_pending_updates(),
        "items": _with_release_urls(db.get_pending_updates(include_dismissed=include_dismissed)),
    })


@app.route("/api/updates/check", methods=["POST"])
def run_update_check():
    """Manually trigger scripts/check_updates.py now, streamed like any
    other long-running action. Body: {"lang": "node"} to check one language,
    omit for all 5."""
    data = request.json or {}
    lang = data.get("lang") or None
    job_id, q = _new_job(f"update-check-{lang or 'all'}")

    def run():
        try:
            results = check_updates.check_all([lang] if lang else None)
            for r in results:
                db.save_pending_update(**r)
                q.put(f"NEW: {r['language']}/{r['kind']} {r['name']} -> "
                      f"major {r['new_major']} (latest {r['latest_version']})")
            q.put(f"Done -- {len(results)} update(s) found, "
                  f"{db.count_pending_updates()} total not yet dismissed.")
        except Exception as exc:
            q.put(f"ERROR: {exc}")
        finally:
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


_KIND_TO_SECTION = {"framework": "frameworks", "library": "cryptography_libs"}


@app.route("/api/updates/<int:update_id>/dismiss", methods=["POST"])
def dismiss_update(update_id: int):
    """Dismiss = record the version as a known-but-not-enabled reference row
    (`"available": false`, this project's existing convention -- see e.g.
    the NestJS 1-5 / bcrypt "0" exclusions from earlier this session)
    instead of just hiding it from the review queue. The user can flip
    `available` to true by hand later in the registry file if it turns out
    to be needed after all."""
    item = db.get_pending_update(update_id)
    if item is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    if not item["dismissed"] and not item["included"]:
        registry_path = registry_writer.registry_path_for(item["language"])
        section_key = _KIND_TO_SECTION[item["kind"]]
        # A stale row from a DIFFERENT host's database (each Docker host has
        # its own SQLite db, but they share the same checked-in registry
        # file) may already have this bucket if another host acted on it
        # first -- that's "already done", not an error, so only write if
        # it's genuinely still missing.
        if not registry_writer.bucket_exists(registry_path, section_key, item["name"], item["new_major"]):
            try:
                registry_writer.add_bucket(
                    registry_path, section_key, item["name"], item["new_major"],
                    None, [], available=False,
                )
            except registry_writer.RegistryWriteError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 500
    db.dismiss_pending_update(update_id)
    return jsonify({"ok": True, "count": db.count_pending_updates()})


@app.route("/api/updates/include", methods=["POST"])
def include_updates():
    """Multi-select: for each id, add a real (enabled) registry bucket --
    compatibility inherited from the nearest lower already-tracked major,
    since an empty array would make generate_images.py skip it entirely --
    then regenerate images for every affected language and report how many
    new image contexts resulted, per (language, framework/library, major).
    Body: {"ids": [1, 2, 3]}."""
    data = request.json or {}
    ids = data.get("ids") or []
    job_id, q = _new_job(f"include-updates-{len(ids)}")

    def run():
        try:
            _do_include_updates(ids, log_fn=lambda msg="": q.put(str(msg)))
        except Exception as exc:
            q.put(f"ERROR: {exc}")
        finally:
            _finish_job(job_id)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


def _do_include_updates(ids: list, log_fn=print) -> None:
    affected_languages = set()
    to_process = []

    for uid in ids:
        item = db.get_pending_update(uid)
        if item is None:
            log_fn(f"  [SKIP] update id {uid} not found")
            continue
        if item["dismissed"] or item["included"]:
            log_fn(f"  [SKIP] {item['language']}/{item['name']} {item['new_major']} "
                   f"already dismissed/included")
            continue

        section_key = _KIND_TO_SECTION[item["kind"]]
        registry_path = registry_writer.registry_path_for(item["language"])

        # A stale row from a DIFFERENT host's database (each Docker host has
        # its own SQLite db, but they share the same checked-in registry
        # file) may already have this bucket if another host included it
        # first -- that's "already done", not an error, so skip the write
        # but still regenerate/count/mark it for THIS host's own images/ tree.
        if registry_writer.bucket_exists(registry_path, section_key, item["name"], item["new_major"]):
            log_fn(f"{item['language']}/{item['kind']} {item['name']} major {item['new_major']} "
                   f"already tracked (added by another host?) -- skipping registry write")
        else:
            tracked = item["tracked_majors"]
            inherited_compat = []
            if tracked:
                inherited_compat = registry_writer.get_entry_compatibility(
                    registry_path, section_key, item["name"], tracked[-1]) or []

            log_fn(f"Including {item['language']}/{item['kind']} {item['name']} "
                   f"major {item['new_major']} (compatibility inherited from "
                   f"{tracked[-1] if tracked else 'n/a'}: {inherited_compat})")
            try:
                registry_writer.add_bucket(
                    registry_path, section_key, item["name"], item["new_major"],
                    None, inherited_compat, available=None,
                )
            except registry_writer.RegistryWriteError as exc:
                log_fn(f"  ERROR writing registry: {exc}")
                continue

        affected_languages.add(item["language"])
        to_process.append(item)

    if not to_process:
        log_fn("Nothing included.")
        return

    # New buckets were just written straight to the registry JSON via
    # registry_writer -- the DB's reference tables (fw_versions/lib_versions,
    # used below to FK-resolve newly generated images) won't see them until
    # the registry is re-parsed.
    log_fn("Syncing registry reference tables ...")
    db.load_registry()

    for lang in sorted(affected_languages):
        log_fn(f"Regenerating images for {lang} ...")
        proc = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_images.py"), "--lang", lang],
            capture_output=True, text=True,
        )
        for line in proc.stdout.splitlines():
            log_fn(f"  {line}")
        if proc.returncode != 0:
            log_fn(f"  [WARN] generate_images.py exited {proc.returncode}: {proc.stderr[-500:]}")

    for item in to_process:
        images_base = PROJECT_ROOT / "images" / item["language"]
        if item["kind"] == "framework":
            pattern = f"*/{item['name']}/{item['new_major']}/**/Dockerfile"
        else:
            pattern = f"*/*/*/{item['name']}/{item['new_major']}/Dockerfile"
        count = len(list(images_base.glob(pattern)))
        db.mark_pending_update_included(item["id"], count)
        log_fn(f"  {item['language']}/{item['name']} {item['new_major']}: {count} new image(s)")

    log_fn("Syncing image database ...")
    _total, inserted, _removed = db.sync_images()
    log_fn(f"Done -- {inserted} new image(s) synced into the database "
           f"(now build/test them via the Sources tab).")


@app.route("/api/updates/log")
def update_log():
    """Permanent history of included updates -- see db.get_update_log()."""
    hide_tested = request.args.get("hide_tested") == "1"
    return jsonify({"items": _with_release_urls(db.get_update_log(hide_tested=hide_tested))})


@app.route("/api/updates/<int:update_id>/mark-tested", methods=["POST"])
def mark_update_tested(update_id):
    db.mark_pending_update_tested(update_id)
    return jsonify({"ok": True})


# ── Export helpers ────────────────────────────────────────────────────────────

@app.route("/api/export/ignore-list")
def export_ignore_list():
    """Download current ignore list as a plain-text file (one path per line)."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT context_path FROM images WHERE ignored=1 ORDER BY context_path"
        ).fetchall()
    text = "\n".join(r[0] for r in rows)
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="ignore_list.txt"'},
    )


@app.route("/api/export/image-list")
def export_image_list():
    """Download filtered image list as a plain-text file (one path per line)."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    ids   = db.get_all_ids_for_filter(filters)
    rows  = db.get_images_by_ids(ids)
    text  = "\n".join(r["context_path"] for r in rows)
    return Response(
        text,
        mimetype="text/plain",
        headers={"Content-Disposition": 'attachment; filename="image_list.txt"'},
    )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    print("Initialising database …")
    db.init_db()

    if not db.IMAGES_BASE.exists() or not any(db.IMAGES_BASE.rglob("Dockerfile")):
        print(f"Warning: no image contexts found under '{db.IMAGES_BASE}'.")
        print("Run 'python scripts/generate_images.py' first, then reload with /api/init.")

    # load_registry() is cheap (pure JSON parsing, no network calls) and
    # idempotent, so it always runs -- skipping it based on "images already
    # has rows" is a poor proxy for "every reference table this version of
    # the code expects is populated". Confirmed the hard way: a DB that
    # already had images but predated the http_clients/http_client_versions
    # tables silently resolved zero client image FKs, since this used to
    # skip load_registry() entirely and those tables stayed empty forever.
    print("Loading registry metadata …")
    counts = db.load_registry()
    print(f"  Registry: {counts}")

    if not (PROJECT_ROOT / "pqc_manager.db").exists() or \
       db._connect().execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0:
        print("Syncing image contexts from disk …")
        total, ins, rem = db.sync_images()
        print(f"  Images: {total} total, {ins} new, {rem} removed")
    else:
        print("Images already synced – skipping filesystem walk (use /api/init to reload)")

    if db.CLIENT_IMAGES_BASE.exists() and any(db.CLIENT_IMAGES_BASE.rglob("Dockerfile")):
        print("Syncing client image contexts from disk …")
        ctotal, cins, crem = db.sync_client_images()
        print(f"  Client images: {ctotal} total, {cins} new, {crem} removed")

    stats = db.get_stats()
    print(f"\nReady: {stats['total']:,} images  |  "
          f"built OK: {stats['built_ok']}  |  "
          f"tested OK: {stats['test_ok']}\n")
    print("Dashboard → http://localhost:5050\n")

    app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
