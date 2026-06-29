"""
PQC Manager – database layer (SQLite).

Schema
------
Reference tables (loaded from registry_*.json):
  languages       – programming languages
  lang_versions   – language versions per language
  frameworks      – web frameworks per language
  fw_versions     – framework versions with release date + compatibility
  libraries       – crypto libraries per language
  lib_versions    – library versions with release date + compatibility

Image table (synced from images/ directory):
  images          – one row per Dockerfile, FK into the reference tables

Result tables:
  build_results   – latest build outcome per image (1:1 with images)
  test_results    – full test-run history per image (1:N with images)

Convenience view:
  image_details   – flat view joining everything; use this for queries
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DB_PATH      = PROJECT_ROOT / "pqc_manager.db"
IMAGES_BASE  = PROJECT_ROOT / "images"
SCRIPTS_DIR  = PROJECT_ROOT / "scripts"

# Registry JSON file names (spaces in filename as they exist on disk)
_REGISTRY_FILES = [
    SCRIPTS_DIR / "registry python.json",
    SCRIPTS_DIR / "registry go.json",
]


# ── Connection ────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

_SCHEMA = """
-- ── Reference tables ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS languages (
    id           INTEGER PRIMARY KEY,
    name         TEXT    UNIQUE NOT NULL,   -- "python", "go"
    display_name TEXT,                      -- "Python", "Go"
    port_base    INTEGER                    -- optional port base from registry
);

CREATE TABLE IF NOT EXISTS lang_versions (
    id           INTEGER PRIMARY KEY,
    language_id  INTEGER NOT NULL REFERENCES languages(id)  ON DELETE CASCADE,
    version_nr   TEXT    NOT NULL,
    release_date TEXT,
    include      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(language_id, version_nr)
);

CREATE TABLE IF NOT EXISTS frameworks (
    id           INTEGER PRIMARY KEY,
    language_id  INTEGER NOT NULL REFERENCES languages(id)  ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    module_path  TEXT,
    notes        TEXT,
    include      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(language_id, name)
);

CREATE TABLE IF NOT EXISTS fw_versions (
    id            INTEGER PRIMARY KEY,
    framework_id  INTEGER NOT NULL REFERENCES frameworks(id) ON DELETE CASCADE,
    version_nr    TEXT    NOT NULL,          -- major nr or "builtin"
    release_date  TEXT,
    compatibility TEXT,                      -- JSON array e.g. ["1.6+","3.3+"]
    UNIQUE(framework_id, version_nr)
);

CREATE TABLE IF NOT EXISTS libraries (
    id           INTEGER PRIMARY KEY,
    language_id  INTEGER NOT NULL REFERENCES languages(id)  ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    module_path  TEXT,
    notes        TEXT,
    include      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(language_id, name)
);

CREATE TABLE IF NOT EXISTS lib_versions (
    id            INTEGER PRIMARY KEY,
    library_id    INTEGER NOT NULL REFERENCES libraries(id)  ON DELETE CASCADE,
    version_nr    TEXT    NOT NULL,          -- version string or "builtin"
    release_date  TEXT,
    compatibility TEXT,                      -- JSON array
    UNIQUE(library_id, version_nr)
);

-- ── Image contexts ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS images (
    id               INTEGER PRIMARY KEY,
    lang_version_id  INTEGER NOT NULL REFERENCES lang_versions(id) ON DELETE CASCADE,
    fw_version_id    INTEGER NOT NULL REFERENCES fw_versions(id)   ON DELETE CASCADE,
    lib_version_id   INTEGER NOT NULL REFERENCES lib_versions(id)  ON DELETE CASCADE,
    image_tag        TEXT    UNIQUE NOT NULL,
    context_path     TEXT    NOT NULL,
    ignored          INTEGER NOT NULL DEFAULT 0,
    ignore_reason    TEXT,
    synced_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lang_version_id, fw_version_id, lib_version_id)
);

-- ── Run labels ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    name        TEXT    UNIQUE NOT NULL,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    status      TEXT    NOT NULL DEFAULT 'running',
    finished_at TEXT
);

-- ── Result tables ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS build_results (
    id          INTEGER PRIMARY KEY,
    image_id    INTEGER UNIQUE NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    success     INTEGER NOT NULL,
    output      TEXT,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS test_results (
    id            INTEGER PRIMARY KEY,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    success       INTEGER NOT NULL,
    root_ok       INTEGER,
    version_ok    INTEGER,
    error_msg     TEXT,
    response_data TEXT,
    tested_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Indexes ────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_lv_lang      ON lang_versions(language_id);
CREATE INDEX IF NOT EXISTS idx_fwv_fw       ON fw_versions(framework_id);
CREATE INDEX IF NOT EXISTS idx_libv_lib     ON lib_versions(library_id);
CREATE INDEX IF NOT EXISTS idx_img_lv       ON images(lang_version_id);
CREATE INDEX IF NOT EXISTS idx_img_fwv      ON images(fw_version_id);
CREATE INDEX IF NOT EXISTS idx_img_libv     ON images(lib_version_id);
CREATE INDEX IF NOT EXISTS idx_img_ignored  ON images(ignored);
CREATE INDEX IF NOT EXISTS idx_img_tag      ON images(image_tag);
CREATE INDEX IF NOT EXISTS idx_test_image   ON test_results(image_id);
CREATE INDEX IF NOT EXISTS idx_test_time    ON test_results(tested_at);
"""

_VIEW = """
DROP VIEW IF EXISTS image_details;
CREATE VIEW image_details AS
SELECT
    i.id,
    i.image_tag,
    i.context_path,
    i.ignored,
    i.ignore_reason,
    i.synced_at,

    -- language
    l.id           AS language_id,
    l.name         AS language,
    l.display_name AS language_display,

    -- language version
    lv.id           AS lang_version_id,
    lv.version_nr   AS lang_version,
    lv.release_date AS lang_release_date,

    -- framework
    f.id          AS framework_id,
    f.name        AS framework,
    f.module_path AS fw_module,
    f.notes       AS fw_notes,

    -- framework version
    fv.id            AS fw_version_id,
    fv.version_nr    AS fw_version,
    fv.release_date  AS fw_release_date,
    fv.compatibility AS fw_compatibility,

    -- library
    lib.id          AS library_id,
    lib.name        AS library,
    lib.module_path AS lib_module,
    lib.notes       AS lib_notes,

    -- library version
    libv.id            AS lib_version_id,
    libv.version_nr    AS lib_version,
    libv.release_date  AS lib_release_date,
    libv.compatibility AS lib_compatibility,

    -- latest build
    b.success     AS build_success,
    b.finished_at AS built_at,
    b.output      AS build_output,
    br.name       AS build_run,

    -- latest test (subquery)
    (SELECT success
       FROM test_results WHERE image_id = i.id
       ORDER BY tested_at DESC LIMIT 1) AS test_success,
    (SELECT tested_at
       FROM test_results WHERE image_id = i.id
       ORDER BY tested_at DESC LIMIT 1) AS tested_at

FROM images i
JOIN lang_versions lv  ON lv.id  = i.lang_version_id
JOIN languages     l   ON l.id   = lv.language_id
JOIN fw_versions   fv  ON fv.id  = i.fw_version_id
JOIN frameworks    f   ON f.id   = fv.framework_id
JOIN lib_versions  libv ON libv.id = i.lib_version_id
JOIN libraries     lib  ON lib.id  = libv.library_id
LEFT JOIN build_results b ON b.image_id = i.id
LEFT JOIN runs br ON br.id = b.run_id;
"""


def init_db() -> None:
    """Create all tables and the image_details view if they do not exist."""
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Add run_id column to existing tables (safe to call repeatedly)
        for ddl in [
            "ALTER TABLE build_results ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE test_results  ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'running'",
            "ALTER TABLE runs ADD COLUMN finished_at TEXT",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass
        conn.executescript(_VIEW)


# ── Registry loader ───────────────────────────────────────────────────────────

def _norm_version(v: str) -> str:
    """Normalise registry version strings for directory/tag matching.

    'built-in' → 'builtin'  (the generated directory uses no hyphen)
    Everything else is left as-is.
    """
    return v.replace("-", "") if v == "built-in" else v


def load_registry() -> dict[str, int]:
    """Parse all registry_*.json files and populate the reference tables.

    Returns a dict with counts: {languages, lang_versions, frameworks,
    fw_versions, libraries, lib_versions}.
    Existing rows are updated (UPSERT) so the function is safe to re-run.
    """
    counts = {k: 0 for k in
              ("languages", "lang_versions", "frameworks",
               "fw_versions", "libraries", "lib_versions")}

    with _connect() as conn:
        for registry_path in _REGISTRY_FILES:
            if not registry_path.exists():
                continue

            data = json.loads(registry_path.read_text(encoding="utf-8"))

            for lang_obj in data.get("languages", []):
                lang_name    = lang_obj.get("id") or lang_obj.get("name", "")
                display_name = (lang_obj.get("display_name")
                                or lang_obj.get("name", lang_name))
                port_base    = lang_obj.get("port_base")

                row = conn.execute(
                    "SELECT id FROM languages WHERE name=?", (lang_name,)
                ).fetchone()
                if row:
                    lang_id = row[0]
                    conn.execute(
                        "UPDATE languages SET display_name=?, port_base=? WHERE id=?",
                        (display_name, port_base, lang_id),
                    )
                else:
                    cur = conn.execute(
                        "INSERT INTO languages (name, display_name, port_base) VALUES (?,?,?)",
                        (lang_name, display_name, port_base),
                    )
                    lang_id = cur.lastrowid
                    counts["languages"] += 1

                # ── Language versions ──────────────────────────────────────
                for lv in lang_obj.get("versions", []):
                    nr      = str(lv.get("nr", ""))
                    rdate   = lv.get("release_date")
                    include = int(lv.get("include", True))
                    conn.execute(
                        """INSERT INTO lang_versions
                               (language_id, version_nr, release_date, include)
                           VALUES (?,?,?,?)
                           ON CONFLICT(language_id, version_nr)
                           DO UPDATE SET release_date=excluded.release_date,
                                         include=excluded.include""",
                        (lang_id, nr, rdate, include),
                    )
                    counts["lang_versions"] += 1

                # ── Frameworks ─────────────────────────────────────────────
                for fw in lang_obj.get("frameworks", []):
                    fw_name  = fw.get("name", "")
                    module   = fw.get("module")
                    notes    = fw.get("notes")
                    include  = int(fw.get("include", True))

                    conn.execute(
                        """INSERT INTO frameworks
                               (language_id, name, module_path, notes, include)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(language_id, name)
                           DO UPDATE SET module_path=excluded.module_path,
                                         notes=excluded.notes,
                                         include=excluded.include""",
                        (lang_id, fw_name, module, notes, include),
                    )
                    fw_id = conn.execute(
                        "SELECT id FROM frameworks WHERE language_id=? AND name=?",
                        (lang_id, fw_name),
                    ).fetchone()[0]
                    counts["frameworks"] += 1

                    # versions can be an array OR the string "built-in"
                    versions = fw.get("version", [])
                    if isinstance(versions, str):
                        versions = [{"nr": versions, "release_date": None,
                                     "compatibility": []}]

                    for fv in versions:
                        nr      = _norm_version(str(fv.get("nr", "")))
                        rdate   = fv.get("release_date")
                        compat  = json.dumps(fv.get("compatibility", []))
                        conn.execute(
                            """INSERT INTO fw_versions
                                   (framework_id, version_nr, release_date, compatibility)
                               VALUES (?,?,?,?)
                               ON CONFLICT(framework_id, version_nr)
                               DO UPDATE SET release_date=excluded.release_date,
                                             compatibility=excluded.compatibility""",
                            (fw_id, nr, rdate, compat),
                        )
                        counts["fw_versions"] += 1

                # ── Crypto libraries ───────────────────────────────────────
                for lib in lang_obj.get("cryptography_libs", []):
                    lib_name = lib.get("name", "")
                    module   = lib.get("module")
                    notes    = lib.get("notes")
                    include  = int(lib.get("include", True))

                    conn.execute(
                        """INSERT INTO libraries
                               (language_id, name, module_path, notes, include)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(language_id, name)
                           DO UPDATE SET module_path=excluded.module_path,
                                         notes=excluded.notes,
                                         include=excluded.include""",
                        (lang_id, lib_name, module, notes, include),
                    )
                    lib_id = conn.execute(
                        "SELECT id FROM libraries WHERE language_id=? AND name=?",
                        (lang_id, lib_name),
                    ).fetchone()[0]
                    counts["libraries"] += 1

                    # versions can be array or "built-in" string
                    versions = lib.get("version", [])
                    if isinstance(versions, str):
                        versions = [{"nr": versions, "release_date": None,
                                     "compatibility": lib.get("compatibility", [])}]

                    for lv in versions:
                        nr      = _norm_version(str(lv.get("nr", "")))
                        rdate   = lv.get("release_date")
                        compat  = json.dumps(lv.get("compatibility", []))
                        conn.execute(
                            """INSERT INTO lib_versions
                                   (library_id, version_nr, release_date, compatibility)
                               VALUES (?,?,?,?)
                               ON CONFLICT(library_id, version_nr)
                               DO UPDATE SET release_date=excluded.release_date,
                                             compatibility=excluded.compatibility""",
                            (lib_id, nr, rdate, compat),
                        )
                        counts["lib_versions"] += 1

    return counts


# ── Image sync ────────────────────────────────────────────────────────────────

def _image_tag_from_parts(language: str, lang_ver: str, framework: str,
                           fw_ver: str, library: str, lib_ver: str) -> str:
    """Compute the canonical Docker image tag from resolved component names."""
    fw  = framework.lower().replace("/", "_")
    lib = library.lower().replace("/", "_")
    return f"pqc-{language}-{lang_ver}-{fw}-{fw_ver}-{lib}-{lib_ver}"


def _resolve_image_fks(conn, language: str, lang_ver: str,
                        framework: str, fw_ver: str,
                        library: str, lib_ver: str):
    """Look up (lang_version_id, fw_version_id, lib_version_id) or return None."""
    lv = conn.execute(
        """SELECT lv.id FROM lang_versions lv
           JOIN languages l ON l.id = lv.language_id
           WHERE l.name=? AND lv.version_nr=?""",
        (language, lang_ver),
    ).fetchone()
    if not lv:
        return None

    fv = conn.execute(
        """SELECT fv.id FROM fw_versions fv
           JOIN frameworks f ON f.id = fv.framework_id
           JOIN languages  l ON l.id = f.language_id
           WHERE l.name=? AND f.name=? AND fv.version_nr=?""",
        (language, framework, fw_ver),
    ).fetchone()
    if not fv:
        return None

    libv = conn.execute(
        """SELECT libv.id FROM lib_versions libv
           JOIN libraries lib ON lib.id = libv.library_id
           JOIN languages l   ON l.id  = lib.language_id
           WHERE l.name=? AND lib.name=? AND libv.version_nr=?""",
        (language, library, lib_ver),
    ).fetchone()
    if not libv:
        return None

    return lv[0], fv[0], libv[0]


def _parse_dockerfile_path(base: Path, dockerfile: Path):
    """Parse a Dockerfile path into component dicts, trying multiple strategies.

    Some library names contain '/' (e.g. "crypto/des", "crypto/md5") which
    creates an extra directory level.  We try lib_depth = 1 and 2 to handle
    both single-level (e.g. "cryptography/44.0") and multi-level library
    dirs (e.g. "crypto/des/builtin").

    Returns a list of candidate dicts (first successful FK resolution wins).
    """
    rel   = dockerfile.parent.relative_to(base)
    parts = rel.parts
    if len(parts) < 6:
        return []

    language = parts[0]
    lang_ver = parts[1]
    lib_ver  = parts[-1]
    context  = str(rel)

    candidates = []
    for lib_depth in range(1, min(4, len(parts) - 3)):
        # lib_depth = 1: library = parts[-2]
        # lib_depth = 2: library = parts[-3]/parts[-2]   etc.
        library   = "/".join(parts[-1 - lib_depth: -1])
        fw_ver    = parts[-2 - lib_depth]
        fw_parts  = parts[2: -2 - lib_depth]
        if not fw_parts:
            continue
        framework = "/".join(fw_parts)
        candidates.append({
            "language": language, "lang_ver": lang_ver,
            "framework": framework, "fw_ver": fw_ver,
            "library": library, "lib_ver": lib_ver,
            "path": context,
        })
    return candidates


def sync_images() -> tuple[int, int, int]:
    """Walk images/ and upsert all Dockerfile contexts into the images table.

    Handles multi-level library names (e.g. crypto/des) by trying multiple
    parse strategies per path.  Returns (total_on_disk, newly_inserted, removed).
    """
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        existing_tags = {r[0]: r[1] for r in
                         conn.execute("SELECT image_tag, id FROM images")}

        disk_tags: dict[str, Path] = {}
        inserted  = 0
        total     = 0

        for dockerfile in sorted(IMAGES_BASE.rglob("Dockerfile")):
            total += 1
            candidates = _parse_dockerfile_path(IMAGES_BASE, dockerfile)

            for cand in candidates:
                tag = _image_tag_from_parts(
                    cand["language"], cand["lang_ver"],
                    cand["framework"], cand["fw_ver"],
                    cand["library"], cand["lib_ver"],
                )
                disk_tags[tag] = dockerfile

                if tag in existing_tags:
                    break  # already in DB, no need to try further

                ids = _resolve_image_fks(
                    conn,
                    cand["language"], cand["lang_ver"],
                    cand["framework"], cand["fw_ver"],
                    cand["library"],  cand["lib_ver"],
                )
                if ids is None:
                    continue  # try next parse strategy

                lv_id, fv_id, libv_id = ids
                conn.execute(
                    """INSERT OR IGNORE INTO images
                           (lang_version_id, fw_version_id, lib_version_id,
                            image_tag, context_path, synced_at)
                       VALUES (?,?,?,?,?,?)""",
                    (lv_id, fv_id, libv_id, tag, cand["path"], now),
                )
                inserted += 1
                existing_tags[tag] = None  # mark as handled
                break  # success

        # Remove DB rows whose Dockerfile no longer exists on disk
        gone_tags = set(existing_tags.keys()) - set(disk_tags.keys())
        removed   = 0
        if gone_tags:
            ph = ",".join("?" * len(gone_tags))
            conn.execute(
                f"DELETE FROM images WHERE image_tag IN ({ph})", list(gone_tags)
            )
            removed = len(gone_tags)

    return total, inserted, removed


# ── Filter helpers ────────────────────────────────────────────────────────────

def _wildcard_clause(field: str, pattern: str):
    """Return (sql_fragment, param) or (None, None) if no filter is needed."""
    if not pattern or pattern.strip() in ("", "*"):
        return None, None
    p = pattern.strip()
    if "x" in p.lower():
        prefix = p[: p.lower().index("x")]
        return f"{field} LIKE ?", prefix + "%"
    return f"{field} = ?", p


_DETAIL_FILTER_MAP = [
    ("language",    "language"),
    ("lang_version","version"),
    ("framework",   "framework"),
    ("fw_version",  "framework_version"),
    ("library",     "library"),
    ("lib_version", "library_version"),
    ("build_run",   "run"),
]


def _build_where(filters: dict, prefix: str = "") -> tuple[str, list]:
    clauses, params = [], []
    for col, key in _DETAIL_FILTER_MAP:
        field = f"{prefix}{col}" if prefix else col
        frag, param = _wildcard_clause(field, filters.get(key, ""))
        if frag:
            clauses.append(frag)
            params.append(param)
    sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


# ── Queries on image_details view ─────────────────────────────────────────────

def get_filter_options() -> dict:
    """Return all distinct values per dimension (no cascading)."""
    with _connect() as conn:
        def vals(col):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM image_details ORDER BY {col}"
            )]
        return {
            "languages":    vals("language"),
            "lang_versions":vals("lang_version"),
            "frameworks":   vals("framework"),
            "fw_versions":  vals("fw_version"),
            "libraries":    vals("library"),
            "lib_versions": vals("lib_version"),
            "runs":         [r["name"] for r in get_runs()],
        }


def get_or_create_run(name: str) -> int:
    """Get an existing run by name or create it, returning the run id."""
    with _connect() as conn:
        row = conn.execute("SELECT id FROM runs WHERE name=?", (name,)).fetchone()
        if row:
            return row[0]
        cur = conn.execute("INSERT INTO runs (name, created_at) VALUES (?,?)",
                           (name, datetime.now(timezone.utc).isoformat()))
        return cur.lastrowid


def get_runs() -> list[dict]:
    """Return all runs ordered newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, status, finished_at FROM runs ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_run_status(run_id: int, status: str) -> None:
    """Mark a run as 'completed' or 'interrupted', recording the finish time."""
    finished_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status=?, finished_at=? WHERE id=?",
            (status, finished_at, run_id),
        )


def get_cascading_filter_options(active: dict) -> dict:
    """Return distinct values per dimension, each cascaded from the active filters
    of its parent dimensions.

    Dimension dependency order:
      language → lang_version → framework → fw_version → library → lib_version

    Each dimension's option list is computed from a WHERE that includes only the
    already-selected *parent* dimensions, so the child options narrow as the user
    fills in parents.
    """
    def _vals(col: str, restrict: dict) -> list:
        where_sql, params = _build_where(restrict)
        rows = _connect().execute(
            f"SELECT DISTINCT {col} FROM image_details {where_sql} ORDER BY {col}",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    lang    = active.get("language", "")
    ver     = active.get("version", "")
    fw      = active.get("framework", "")
    fw_ver  = active.get("framework_version", "")
    lib     = active.get("library", "")

    return {
        # Languages never cascade (always show all)
        "languages":     _vals("language",     {}),
        # Lang versions cascade on language only
        "lang_versions": _vals("lang_version", {"language": lang}),
        # Frameworks cascade on language + version
        "frameworks":    _vals("framework",    {"language": lang, "version": ver}),
        # FW versions cascade on language + version + framework
        "fw_versions":   _vals("fw_version",   {"language": lang, "version": ver,
                                                 "framework": fw}),
        # Libraries cascade on language + version + framework + fw_version
        "libraries":     _vals("library",      {"language": lang, "version": ver,
                                                 "framework": fw,
                                                 "framework_version": fw_ver}),
        # Lib versions cascade on all parents
        "lib_versions":  _vals("lib_version",  {"language": lang, "version": ver,
                                                 "framework": fw,
                                                 "framework_version": fw_ver,
                                                 "library": lib}),
    }


_SORTABLE_COLS = {
    "language", "lang_version", "framework", "fw_version",
    "library", "lib_version", "build_success", "test_success",
}

def get_images(filters: dict | None = None,
               page: int = 1,
               per_page: int = 50,
               include_ignored: bool = True,
               sort_by: str = "",
               sort_dir: str = "asc") -> dict:
    """Return a paginated result from image_details with build/test status."""
    filters   = filters or {}
    where_sql, params = _build_where(filters)

    if not include_ignored:
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} ignored = 0"

    # Build ORDER BY: primary sort col (if valid) then stable tie-breaker
    sort_col = sort_by if sort_by in _SORTABLE_COLS else ""
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
    if sort_col:
        order_sql = (f"ORDER BY {sort_col} {direction} NULLS LAST, "
                     "language, lang_version, framework, fw_version, library, lib_version")
    else:
        order_sql = "ORDER BY language, lang_version, framework, fw_version, library, lib_version"

    with _connect() as conn:
        total  = conn.execute(
            f"SELECT COUNT(*) FROM image_details {where_sql}", params
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows   = conn.execute(
            f"SELECT * FROM image_details {where_sql} {order_sql} LIMIT ? OFFSET ?",
            params + [per_page, offset],
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "items":    [dict(r) for r in rows],
    }


def get_images_by_ids(image_ids: list) -> list[dict]:
    if not image_ids:
        return []
    with _connect() as conn:
        ph   = ",".join("?" * len(image_ids))
        rows = conn.execute(
            f"SELECT * FROM image_details WHERE id IN ({ph})", image_ids
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_ids_for_filter(filters: dict,
                            include_ignored: bool = True) -> list[int]:
    """Return every image id matching filters (no pagination)."""
    where_sql, params = _build_where(filters)
    if not include_ignored:
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} ignored = 0"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM image_details {where_sql}", params
        ).fetchall()
    return [r[0] for r in rows]


def get_reference_data() -> dict:
    """Return full reference tables for the dashboard info panel."""
    with _connect() as conn:
        langs = [dict(r) for r in conn.execute(
            "SELECT * FROM languages ORDER BY name"
        )]
        for lang in langs:
            lang_id = lang["id"]
            lang["versions"] = [dict(r) for r in conn.execute(
                "SELECT * FROM lang_versions WHERE language_id=? ORDER BY version_nr",
                (lang_id,),
            )]
            lang["frameworks"] = []
            for fw in conn.execute(
                "SELECT * FROM frameworks WHERE language_id=? ORDER BY name",
                (lang_id,),
            ):
                fw_dict = dict(fw)
                fw_dict["versions"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM fw_versions WHERE framework_id=? ORDER BY version_nr",
                    (fw["id"],),
                )]
                lang["frameworks"].append(fw_dict)
            lang["libraries"] = []
            for lib in conn.execute(
                "SELECT * FROM libraries WHERE language_id=? ORDER BY name",
                (lang_id,),
            ):
                lib_dict = dict(lib)
                lib_dict["versions"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM lib_versions WHERE library_id=? ORDER BY version_nr",
                    (lib["id"],),
                )]
                lang["libraries"].append(lib_dict)
    return {"languages": langs}


# ── Mutations ─────────────────────────────────────────────────────────────────

def set_ignored(image_ids: list, ignored: bool, reason: str = "") -> None:
    if not image_ids:
        return
    with _connect() as conn:
        ph = ",".join("?" * len(image_ids))
        conn.execute(
            f"UPDATE images SET ignored=?, ignore_reason=? WHERE id IN ({ph})",
            [int(ignored), reason] + list(image_ids),
        )


def save_build_result(image_id: int, success: bool,
                      output: str,
                      started_at: str, finished_at: str,
                      run_id: int | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO build_results
                   (image_id, success, output, started_at, finished_at, run_id)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(image_id)
               DO UPDATE SET success=excluded.success,
                             output=excluded.output,
                             started_at=excluded.started_at,
                             finished_at=excluded.finished_at,
                             run_id=excluded.run_id""",
            (image_id, int(success), output, started_at, finished_at, run_id),
        )


def save_test_result(image_id: int, success: bool,
                     root_ok: bool, version_ok: bool,
                     error_msg: str, response_data,
                     run_id: int | None = None) -> None:
    tested_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO test_results
                   (image_id, success, root_ok, version_ok,
                    error_msg, response_data, tested_at, run_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (image_id, int(success), int(bool(root_ok)), int(bool(version_ok)),
             error_msg,
             json.dumps(response_data) if response_data is not None else None,
             tested_at, run_id),
        )


# ── Reports ───────────────────────────────────────────────────────────────────

def get_test_reports(filters: dict | None = None, limit: int = 500) -> list[dict]:
    """Return recent test results joined with image metadata."""
    filters   = filters or {}
    where_sql, params = _build_where(filters)

    success_val = filters.get("success", "")
    if success_val not in ("", None):
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} t.success = ?"
        params.append(int(success_val))

    run_val = filters.get("run", "")
    run_filter = ""
    if run_val:
        connector = "AND" if where_sql else "WHERE"
        run_filter = f"{connector} r.name = ?"
        params.append(run_val)

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT
                t.id, t.success, t.root_ok, t.version_ok,
                t.error_msg, t.response_data, t.tested_at,
                r.name AS run_name, r.status AS run_status,
                d.language, d.lang_version, d.framework, d.fw_version,
                d.library, d.lib_version, d.image_tag,
                d.fw_release_date, d.lib_release_date,
                d.fw_compatibility, d.lib_compatibility
            FROM test_results t
            JOIN image_details d ON d.id = t.image_id
            LEFT JOIN runs r ON r.id = t.run_id
            {where_sql}
            {run_filter}
            ORDER BY t.tested_at DESC
            LIMIT ?
        """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def get_build_reports(filters: dict | None = None, limit: int = 500) -> list[dict]:
    """Return recent build results joined with image metadata."""
    filters   = filters or {}
    where_sql, params = _build_where(filters)

    success_val = filters.get("success", "")
    if success_val not in ("", None):
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} b.success = ?"
        params.append(int(success_val))

    run_val = filters.get("run", "")
    run_filter = ""
    if run_val:
        connector = "AND" if where_sql else "WHERE"
        run_filter = f"{connector} r.name = ?"
        params.append(run_val)

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT
                b.id, b.success, b.output, b.started_at, b.finished_at,
                r.name AS run_name, r.status AS run_status,
                d.language, d.lang_version, d.framework, d.fw_version,
                d.library, d.lib_version, d.image_tag
            FROM build_results b
            JOIN image_details d ON d.id = b.image_id
            LEFT JOIN runs r ON r.id = b.run_id
            {where_sql}
            {run_filter}
            ORDER BY b.finished_at DESC
            LIMIT ?
        """, params + [limit]).fetchall()
    return [dict(r) for r in rows]


def get_stats() -> dict:
    """Return aggregate statistics for the dashboard header."""
    with _connect() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        ignored  = conn.execute(
            "SELECT COUNT(*) FROM images WHERE ignored=1"
        ).fetchone()[0]
        built    = conn.execute(
            "SELECT COUNT(*) FROM build_results WHERE success=1"
        ).fetchone()[0]
        built_f  = conn.execute(
            "SELECT COUNT(*) FROM build_results WHERE success=0"
        ).fetchone()[0]
        tested   = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT image_id FROM test_results
                   GROUP BY image_id
                   HAVING MAX(CASE WHEN success=1 THEN 1 ELSE 0 END)=1
               )"""
        ).fetchone()[0]
        tested_f = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT image_id FROM test_results
                   GROUP BY image_id
                   HAVING MAX(CASE WHEN success=1 THEN 1 ELSE 0 END)=0
               )"""
        ).fetchone()[0]
        langs = conn.execute(
            "SELECT COUNT(DISTINCT language_id) FROM lang_versions WHERE include=1"
        ).fetchone()[0]
    return {
        "total": total, "ignored": ignored,
        "built_ok": built, "built_fail": built_f,
        "test_ok": tested, "test_fail": tested_f,
        "languages": langs,
    }
