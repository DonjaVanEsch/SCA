"""
PQC Dashboard – Flask web server.

Run:  python dashboard.py
Opens: http://localhost:5050
"""

import json
import queue
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

import db
import manager

app = Flask(__name__, static_folder=str(PROJECT_ROOT / "static"))

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
    return jsonify({
        "registry": reg_counts,
        "images": {"total": total, "inserted": ins, "removed": rem},
    })


@app.route("/api/sync", methods=["POST"])
def sync():
    """Sync image contexts from the images/ directory (no registry reload)."""
    total, ins, rem = db.sync_images()
    return jsonify({"total": total, "inserted": ins, "removed": rem})


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


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats())


# ── Image list ────────────────────────────────────────────────────────────────

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
    return jsonify(db.get_images(filters, page, per_page, include_ignored, sort_by, sort_dir))


@app.route("/api/images/ids")
def get_all_ids():
    """Return all image ids matching current filters (for select-all)."""
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    include_ignored = request.args.get("include_ignored", "true").lower() == "true"
    ids = db.get_all_ids_for_filter(filters, include_ignored)
    return jsonify({"ids": ids})


# ── Actions ───────────────────────────────────────────────────────────────────

@app.route("/api/action", methods=["POST"])
def action():
    """Start a build / test / remove / stop job for the given image ids.

    Body: {"action": "build"|"test"|"remove"|"stop",
           "image_ids": [1, 2, ...],
           "options": {"no_cache": false, "skip_existing": false}}
    Returns: {"job_id": "..."}
    """
    data       = request.json or {}
    action_str = data.get("action", "")
    image_ids  = [int(i) for i in data.get("image_ids", [])]
    opts       = data.get("options", {})
    run_name   = data.get("run_name", "")

    if action_str not in ("build", "test", "remove", "stop", "mark_success"):
        return jsonify({"error": f"Unknown action: {action_str}"}), 400
    if not image_ids:
        return jsonify({"error": "No image ids provided"}), 400

    rows    = db.get_images_by_ids(image_ids)
    if not rows:
        return jsonify({"error": "No matching images found"}), 404

    entries = _entries_from_db_rows(rows)
    job_id, q = _new_job(action_str)

    def log(msg=""):
        q.put(str(msg))

    def run():
        run_id     = db.get_or_create_run(run_name) if run_name else None
        stop_event = _jobs[job_id]["stop_event"]
        try:
            if action_str == "build":
                def _save_build(entry, r):
                    db.save_build_result(
                        entry["_id"], r.get("success", False),
                        r.get("output", ""),
                        r.get("started_at"), r.get("finished_at"),
                        run_id,
                    )
                manager._do_build(
                    entries,
                    no_cache=bool(opts.get("no_cache")),
                    skip_existing=bool(opts.get("skip_existing")),
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
                        run_id,
                    )
                manager._do_test(
                    entries,
                    log_fn=log,
                    save_fn=_save_test,
                    stop_event=stop_event,
                )

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
                                         now, now, run_id)
                    db.save_test_result(img_id, True, True, True,
                                        "", None, run_id)
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
                except Exception:
                    pass
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
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version", "success", "run",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_test_reports(filters, page, per_page))


@app.route("/api/reports/pending")
def pending_reports():
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_pending_images(filters, page, per_page))


@app.route("/api/reports/build")
def build_reports():
    filters = {k: request.args.get(k, "") for k in (
        "language", "version", "framework",
        "framework_version", "library", "library_version", "success", "run",
    )}
    page     = max(1, int(request.args.get("page", 1)))
    per_page = max(1, min(500, int(request.args.get("per_page", 100))))
    return jsonify(db.get_build_reports(filters, page, per_page))


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

    if not (PROJECT_ROOT / "pqc_manager.db").exists() or \
       db._connect().execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0:
        print("Loading registry metadata …")
        counts = db.load_registry()
        print(f"  Registry: {counts}")
        print("Syncing image contexts from disk …")
        total, ins, rem = db.sync_images()
        print(f"  Images: {total} total, {ins} new, {rem} removed")
    else:
        print("Database already populated – skipping auto-sync (use /api/init to reload)")

    stats = db.get_stats()
    print(f"\nReady: {stats['total']:,} images  |  "
          f"built OK: {stats['built_ok']}  |  "
          f"tested OK: {stats['test_ok']}\n")
    print("Dashboard → http://localhost:5050\n")

    app.run(debug=False, host="0.0.0.0", port=5050, threaded=True)
