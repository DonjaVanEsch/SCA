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
    SCRIPTS_DIR / "registry node.json",
    SCRIPTS_DIR / "registry java.json",
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
    output        TEXT,
    tested_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- ── Crypto Agility (C.A.M. Component 2) ──────────────────────────────────────
-- Structures facts that already existed as prose in registry "notes" fields
-- and in research memory -- not a new research effort, just giving already-
-- verified knowledge a queryable home. vulnerabilities/crypto_agility_scores
-- are schema-ready but deliberately seeded empty: no CVE or scoring-
-- methodology data exists in this project yet, and none is fabricated here.

CREATE TABLE IF NOT EXISTS crypto_algorithms (
    id       INTEGER PRIMARY KEY,
    name     TEXT UNIQUE NOT NULL,   -- "ML-KEM", "Kyber (draft)", "AES", ...
    family   TEXT NOT NULL,          -- 'post-quantum' | 'classical' | 'hybrid'
    standard TEXT,                   -- "FIPS 203" etc.; NULL for classical/draft
    notes    TEXT
);

CREATE TABLE IF NOT EXISTS lib_version_algorithms (
    id             INTEGER PRIMARY KEY,
    lib_version_id INTEGER NOT NULL REFERENCES lib_versions(id) ON DELETE CASCADE,
    algorithm_id   INTEGER NOT NULL REFERENCES crypto_algorithms(id) ON DELETE CASCADE,
    support_level  TEXT NOT NULL,    -- 'draft' | 'final' | 'native' | 'deprecated'
    source_note    TEXT,             -- how this was verified (jar diff, release notes, ...)
    UNIQUE(lib_version_id, algorithm_id)
);

-- Migration paths are algorithm-level (draft PQC name -> final standardized
-- name), optionally scoped to the library/version pair where the transition
-- was actually observed. from_algorithm_id is nullable for paths that don't
-- have a "from" (e.g. a brand-new capability with no predecessor).
CREATE TABLE IF NOT EXISTS migration_paths (
    id                  INTEGER PRIMARY KEY,
    from_algorithm_id   INTEGER REFERENCES crypto_algorithms(id) ON DELETE CASCADE,
    to_algorithm_id     INTEGER NOT NULL REFERENCES crypto_algorithms(id) ON DELETE CASCADE,
    library_id          INTEGER REFERENCES libraries(id) ON DELETE CASCADE,
    from_lib_version_id INTEGER REFERENCES lib_versions(id) ON DELETE CASCADE,
    to_lib_version_id   INTEGER REFERENCES lib_versions(id) ON DELETE CASCADE,
    description         TEXT NOT NULL,
    verified            INTEGER NOT NULL DEFAULT 0
);

-- Component 4 (OS Age Database) territory: the ROOT CAUSE behind a
-- fw_versions/lib_versions.compatibility restriction, not the restriction
-- itself (that's already in the compatibility JSON column). Exactly one of
-- lang_version_id/lib_version_id/fw_version_id is set per row -- these facts
-- are about a language runtime's, a crypto library's, or a web framework's
-- own platform/OS requirements, never more than one at once. lang_version_id
-- exists specifically so language-runtime-level facts (e.g. a Docker base
-- image's Debian codename aging off the live apt mirrors) have a home
-- alongside the library/framework ones -- this is also the same dimension a
-- future network/TLS fingerprint would key off (language + language
-- version), so keeping it general now avoids a schema migration later.
CREATE TABLE IF NOT EXISTS platform_constraints (
    id              INTEGER PRIMARY KEY,
    lang_version_id INTEGER REFERENCES lang_versions(id) ON DELETE CASCADE,
    lib_version_id  INTEGER REFERENCES lib_versions(id)  ON DELETE CASCADE,
    fw_version_id   INTEGER REFERENCES fw_versions(id)   ON DELETE CASCADE,
    constraint_type TEXT NOT NULL,   -- 'glibc'|'openssl'|'kernel'|'compiler'|'architecture'|'runtime_engine'|'toolchain'|'os_base_image'
    description     TEXT NOT NULL,
    verified        INTEGER NOT NULL DEFAULT 1,
    CHECK ((lang_version_id IS NOT NULL) + (lib_version_id IS NOT NULL) + (fw_version_id IS NOT NULL) = 1)
);

-- Deliberately empty -- see header comment. Schema mirrors the shape a real
-- CVE feed would need (per-library, free-form affected-version-range since
-- real CVE ranges are rarely a single clean bucket, severity, source).
CREATE TABLE IF NOT EXISTS vulnerabilities (
    id                     INTEGER PRIMARY KEY,
    library_id             INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    cve_id                 TEXT,
    affected_version_range TEXT,
    severity               TEXT,
    description            TEXT,
    source_url             TEXT,
    added_at               TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Deliberately empty -- no scoring methodology has been defined yet. One
-- row per lib_version once a methodology exists and is actually computed.
CREATE TABLE IF NOT EXISTS crypto_agility_scores (
    id             INTEGER PRIMARY KEY,
    lib_version_id INTEGER UNIQUE NOT NULL REFERENCES lib_versions(id) ON DELETE CASCADE,
    score          REAL,
    rationale      TEXT,
    computed_at    TEXT
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
CREATE INDEX IF NOT EXISTS idx_lva_libver   ON lib_version_algorithms(lib_version_id);
CREATE INDEX IF NOT EXISTS idx_lva_algo     ON lib_version_algorithms(algorithm_id);
CREATE INDEX IF NOT EXISTS idx_mp_lib       ON migration_paths(library_id);
CREATE INDEX IF NOT EXISTS idx_pc_langver   ON platform_constraints(lang_version_id);
CREATE INDEX IF NOT EXISTS idx_pc_libver    ON platform_constraints(lib_version_id);
CREATE INDEX IF NOT EXISTS idx_pc_fwver     ON platform_constraints(fw_version_id);
CREATE INDEX IF NOT EXISTS idx_vuln_lib     ON vulnerabilities(library_id);
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
       ORDER BY tested_at DESC LIMIT 1) AS tested_at,
    (SELECT output
       FROM test_results WHERE image_id = i.id
       ORDER BY tested_at DESC LIMIT 1) AS test_output

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
        # platform_constraints' CHECK constraint widened (lib/fw-only ->
        # lang/lib/fw) after that table had already shipped -- SQLite can't
        # ALTER a CHECK constraint in place, and "CREATE TABLE IF NOT
        # EXISTS" silently no-ops against an existing table regardless of
        # schema drift, so an old on-disk table would keep rejecting
        # lang-version-only rows forever. Safe to just drop and let it be
        # recreated below: every row in this table is generator-managed
        # seed data from _seed_crypto_agility(), not user-entered, so
        # nothing is lost -- load_registry() repopulates it right after.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(platform_constraints)")}
        if cols and "lang_version_id" not in cols:
            conn.execute("DROP TABLE platform_constraints")

        conn.executescript(_SCHEMA)
        # Add run_id column to existing tables (safe to call repeatedly)
        for ddl in [
            "ALTER TABLE build_results ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE test_results  ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE test_results  ADD COLUMN output TEXT",
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


def _prune_missing(conn, table: str, fk_col: str, fk_val: int,
                   keep_col: str, keep_values: set) -> int:
    """Delete rows from `table` scoped to fk_col=fk_val whose keep_col value
    is no longer present in keep_values. Cascades to dependent rows (images,
    build/test results) via the schema's ON DELETE CASCADE.
    """
    if keep_values:
        placeholders = ",".join("?" * len(keep_values))
        cur = conn.execute(
            f"DELETE FROM {table} WHERE {fk_col}=? AND {keep_col} NOT IN ({placeholders})",
            (fk_val, *keep_values),
        )
    else:
        cur = conn.execute(f"DELETE FROM {table} WHERE {fk_col}=?", (fk_val,))
    return cur.rowcount


def load_registry() -> dict[str, int]:
    """Parse all registry_*.json files and populate the reference tables.

    Returns a dict with counts: {languages, lang_versions, frameworks,
    fw_versions, libraries, lib_versions, *_removed}.
    Existing rows are updated (UPSERT) so the function is safe to re-run.
    Rows no longer present in the registry (e.g. a language version that was
    renamed or dropped) are pruned so stale entries don't linger in the
    reference tables/dashboard forever.
    """
    counts = {k: 0 for k in
              ("languages", "lang_versions", "frameworks",
               "fw_versions", "libraries", "lib_versions")}
    counts.update({f"{k}_removed": 0 for k in
                   ("lang_versions", "frameworks", "fw_versions",
                    "libraries", "lib_versions")})

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
                seen_lang_versions = set()
                for lv in lang_obj.get("versions", []):
                    nr      = str(lv.get("nr", ""))
                    rdate   = lv.get("release_date")
                    include = int(lv.get("include", True))
                    seen_lang_versions.add(nr)
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

                counts["lang_versions_removed"] += _prune_missing(
                    conn, "lang_versions", "language_id", lang_id,
                    "version_nr", seen_lang_versions,
                )

                # ── Frameworks ─────────────────────────────────────────────
                seen_frameworks = set()
                for fw in lang_obj.get("frameworks", []):
                    fw_name  = fw.get("name", "")
                    module   = fw.get("module")
                    notes    = fw.get("notes")
                    include  = int(fw.get("include", True))
                    seen_frameworks.add(fw_name)

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

                    seen_fw_versions = set()
                    for fv in versions:
                        nr      = _norm_version(str(fv.get("nr", "")))
                        rdate   = fv.get("release_date")
                        compat  = json.dumps(fv.get("compatibility", []))
                        seen_fw_versions.add(nr)
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

                    counts["fw_versions_removed"] += _prune_missing(
                        conn, "fw_versions", "framework_id", fw_id,
                        "version_nr", seen_fw_versions,
                    )

                counts["frameworks_removed"] += _prune_missing(
                    conn, "frameworks", "language_id", lang_id,
                    "name", seen_frameworks,
                )

                # ── Crypto libraries ───────────────────────────────────────
                seen_libraries = set()
                for lib in lang_obj.get("cryptography_libs", []):
                    lib_name = lib.get("name", "")
                    module   = lib.get("module")
                    notes    = lib.get("notes")
                    include  = int(lib.get("include", True))
                    seen_libraries.add(lib_name)

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

                    seen_lib_versions = set()
                    for lv in versions:
                        nr      = _norm_version(str(lv.get("nr", "")))
                        rdate   = lv.get("release_date")
                        compat  = json.dumps(lv.get("compatibility", []))
                        seen_lib_versions.add(nr)
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

                    counts["lib_versions_removed"] += _prune_missing(
                        conn, "lib_versions", "library_id", lib_id,
                        "version_nr", seen_lib_versions,
                    )

                counts["libraries_removed"] += _prune_missing(
                    conn, "libraries", "language_id", lang_id,
                    "name", seen_libraries,
                )

    _seed_crypto_agility()
    return counts


# ── Crypto Agility seed data (C.A.M. Component 2) ─────────────────────────────
# Structures facts that already existed as prose in registry "notes" fields
# and in prior research (this session's Bouncy Castle/Tink jar-diffing, the
# Node/Java runtime-compatibility investigations) into the queryable tables
# above. Every fact here was already verified elsewhere before being encoded
# -- this function does not perform new research, only structures existing
# findings. Idempotent (INSERT OR IGNORE / ON CONFLICT DO NOTHING throughout)
# so it's safe to call on every load_registry() run.

def _lang_version_id(conn, language: str, version_nr: str):
    row = conn.execute(
        """SELECT lv.id FROM lang_versions lv
               JOIN languages g ON g.id = lv.language_id
           WHERE g.name = ? AND lv.version_nr = ?""",
        (language, version_nr),
    ).fetchone()
    return row[0] if row else None


def _lib_version_id(conn, language: str, library: str, version_nr: str):
    row = conn.execute(
        """SELECT lv.id FROM lib_versions lv
               JOIN libraries l ON l.id = lv.library_id
               JOIN languages g ON g.id = l.language_id
           WHERE g.name = ? AND l.name = ? AND lv.version_nr = ?""",
        (language, library, version_nr),
    ).fetchone()
    return row[0] if row else None


def _fw_version_id(conn, language: str, framework: str, version_nr: str):
    row = conn.execute(
        """SELECT fv.id FROM fw_versions fv
               JOIN frameworks f ON f.id = fv.framework_id
               JOIN languages g ON g.id = f.language_id
           WHERE g.name = ? AND f.name = ? AND fv.version_nr = ?""",
        (language, framework, version_nr),
    ).fetchone()
    return row[0] if row else None


def _library_id(conn, language: str, library: str):
    row = conn.execute(
        """SELECT l.id FROM libraries l
               JOIN languages g ON g.id = l.language_id
           WHERE g.name = ? AND l.name = ?""",
        (language, library),
    ).fetchone()
    return row[0] if row else None


def _algorithm_id(conn, name: str):
    row = conn.execute("SELECT id FROM crypto_algorithms WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


# (name, family, standard, notes)
_ALGORITHMS = [
    ("ML-KEM", "post-quantum", "FIPS 203",
     "Module-Lattice-based Key Encapsulation Mechanism; NIST-standardized KEM, successor to CRYSTALS-Kyber."),
    ("ML-DSA", "post-quantum", "FIPS 204",
     "Module-Lattice-based Digital Signature Algorithm; NIST-standardized signature scheme, successor to CRYSTALS-Dilithium."),
    ("SLH-DSA", "post-quantum", "FIPS 205",
     "Stateless Hash-based Digital Signature Algorithm; NIST-standardized signature scheme, successor to SPHINCS+."),
    ("Kyber (draft)", "post-quantum", None,
     "Pre-standardization NIST PQC finalist KEM; superseded by ML-KEM/FIPS 203."),
    ("Dilithium (draft)", "post-quantum", None,
     "Pre-standardization NIST PQC finalist signature scheme; superseded by ML-DSA/FIPS 204."),
    ("SPHINCS+ (draft)", "post-quantum", None,
     "Pre-standardization NIST PQC finalist signature scheme; superseded by SLH-DSA/FIPS 205."),
]

# (language, library, version_nr, algorithm_name, support_level, source_note)
_LIB_VERSION_ALGORITHMS = [
    ("java", "BouncyCastle", "1.72", "Kyber (draft)", "draft",
     "Verified by diffing actual jar contents: bcprov-jdk18on 1.71 has none of these classes, 1.72 introduces them; BC's own release notes flag it experimental."),
    ("java", "BouncyCastle", "1.72", "Dilithium (draft)", "draft",
     "Same verification as Kyber (draft) at 1.72 -- jar diff + BC release notes."),
    ("java", "BouncyCastle", "1.79", "ML-KEM", "final",
     "Verified by diffing actual jar contents: 1.78 lacks MLKEMParameterSpec/etc. entirely, 1.79 introduces the full provider implementation (FIPS 203 final names)."),
    ("java", "BouncyCastle", "1.79", "ML-DSA", "final",
     "Same verification as ML-KEM at 1.79 -- jar diff (MLDSAParameterSpec)."),
    ("java", "BouncyCastle", "1.79", "SLH-DSA", "final",
     "Same verification as ML-KEM at 1.79 -- jar diff (SLHDSAParameterSpec)."),
    ("java", "BouncyCastle", "1", "ML-KEM", "final",
     "This bucket always resolves to the latest 1.x release; inherits 1.79's final-name support."),
    ("java", "BouncyCastle", "1", "ML-DSA", "final", "Same rolling-latest reasoning as ML-KEM."),
    ("java", "BouncyCastle", "1", "SLH-DSA", "final", "Same rolling-latest reasoning as ML-KEM."),
    ("java", "BouncyCastle", "1", "Kyber (draft)", "deprecated",
     "BC 1.84's own release notes announce removal of the draft Kyber/Dilithium/SphincsPlus wrapper names in a future release -- still present in 1.84 itself, but on the way out."),
    ("java", "BouncyCastle", "1", "Dilithium (draft)", "deprecated",
     "Same sunsetting note as Kyber (draft) at the rolling-latest bucket."),
    ("java", "Tink", "1.21", "ML-DSA", "final",
     "v1.21.0 (2026-03-24) added ML-DSA-87 signature support -- Tink's first post-quantum release of any kind. Verified via full-text search of every Tink Java GitHub release body (v1.8.0-v1.20.0 have zero PQC mentions)."),
    ("java", "Tink", "1", "ML-DSA", "final",
     "Rolling-latest bucket (currently 1.22.0); adds ML-DSA-44 on top of 1.21's ML-DSA-87."),
    ("java", "Tink", "1", "SLH-DSA", "final",
     "v1.22.0 (2026-06-18) added SLH-DSA predefined signature parameters. Tink still has NO KEM-side PQC (no ML-KEM/Kyber) as of this version -- signatures only."),
]

# (library_language, library_name, from_algo, to_algo, from_ver, to_ver, description)
_MIGRATION_PATHS = [
    ("java", "BouncyCastle", "Kyber (draft)", "ML-KEM", "1.72", "1.79",
     "BC's own draft-to-final PQC rename landed at bcprov-jdk18on 1.79 (2024-10-30); draft Kyber classes were kept side-by-side for compatibility, but BC 1.84 (2026-04-14) announces their removal in a future release."),
    ("java", "BouncyCastle", "Dilithium (draft)", "ML-DSA", "1.72", "1.79",
     "Same timeline and sourcing as the Kyber (draft) -> ML-KEM path."),
    ("java", "BouncyCastle", "SPHINCS+ (draft)", "SLH-DSA", None, "1.79",
     "SPHINCS+ draft low-level API existed from bcprov-jdk18on 1.70, reached the JCA-usable BCPQC provider at 1.71 (neither is a tracked registry bucket here); the final standardized SLH-DSA name landed alongside ML-KEM/ML-DSA at 1.79."),
    ("java", "Tink", None, "ML-DSA", None, "1.21",
     "Tink's first post-quantum capability of any kind -- no prior draft/precursor stage within Tink itself (unlike BC, which carried draft Kyber/Dilithium/SPHINCS+ for years first). v1.21.0 added ML-DSA-87 directly at the final standardized name."),
    ("java", "Tink", None, "SLH-DSA", None, "1",
     "v1.22.0 added SLH-DSA predefined signature parameters, again with no prior draft stage within Tink."),
]

# (language, library, version_nr, constraint_type, description)
_LIB_PLATFORM_CONSTRAINTS = [
    ("java", "Conscrypt", "1", "architecture",
     "Resolves to the highest published 1.x release (1.4.2, ~2019). Neither this nor the current stable 2.x bucket bundles ARM64/aarch64 native libraries -- confirmed by inspecting the actual jar contents directly. Fails to load its native security provider on an arm64 host (Apple Silicon Mac, ARM Docker Desktop/CI). Only the still-prerelease 2.6-alpha5 adds ARM64 natives."),
    ("java", "Conscrypt", "2", "architecture",
     "Resolves to the latest stable release (2.5.2). Same ARM64 gap as the '1' bucket -- confirmed by inspecting jar contents directly; only the still-prerelease 2.6-alpha5 has ARM64 natives."),
    ("java", "Conscrypt", "1", "glibc",
     "Bundled native code is glibc-linked per standard OpenJDK toolchain convention -- this project pairs Conscrypt only with eclipse-temurin's Ubuntu/glibc ('-jammy') tags, never Alpine/musl, to avoid a suspected (not yet empirically confirmed) UnsatisfiedLinkError."),
    ("java", "Conscrypt", "2", "glibc",
     "Same glibc-linkage reasoning as the '1' bucket."),
    ("node", "node-forge", "1.0", "runtime_engine",
     "lib/log.js unconditionally calls the global URLSearchParams constructor at require-time (whenever `console` exists, which is always true in Node) -- URLSearchParams only became a global in Node v10.0.0. Crashes with ReferenceError on Node <10 regardless of app code. Fixed upstream by node-forge 1.4.0 (the block is properly gated behind a `typeof window` check there)."),
    ("node", "crypto-js", "4", "runtime_engine",
     "Cipher files (e.g. blowfish.js) use bare let/const with no \"use strict\" pragma -- Node 4's V8 (4.5) only allowed block-scoped declarations in strict-mode code, throwing SyntaxError on Node <6. crypto-js 3.x is plain ES5 and works fine down to Node 4."),
    ("python", "cryptography", "2.0", "toolchain",
     "Python 3.12+ slim images ship no setuptools, and cryptography 2.0 has no abi3/py312 wheel -- source build fails without installing setuptools first. Capped at Python 3.11 in the registry."),
    ("python", "M2Crypto", "0.26", "compiler",
     "SWIG-generated code uses deprecated Python C API (e.g. PyEval_InitThreads) removed in Python 3.12; that Python version's slim image also lacks setuptools needed as a build backend. Capped at Python 3.11 for this bucket."),
]

# (language, framework, version_nr, constraint_type, description)
_FW_PLATFORM_CONSTRAINTS = [
    ("node", "Fastify", "1", "toolchain",
     "Dependency tree deterministically fails npm install on Node 6 with ENOTDIR on a .staging/@types/... path -- a known npm@3 (bundled with Node 6) race/bug with scoped packages. Reproduced directly: failed 5/5 tries on node:6-slim, succeeded 2/2 on node:8-slim. Fixed upstream by npm5 (bundled from Node 8 onward). Fastify 2.x's different dependency tree doesn't trigger it."),
    ("node", "Koa", "2", "runtime_engine",
     "koa's own dependency http-errors uses object destructuring (const { HttpError } = require(...)) that Node 6's V8 can't parse -- require('koa') itself throws SyntaxError: Unexpected token { on Node 6, loads cleanly from Node 8."),
]

# (language, lang_version_nr, constraint_type, description) -- the language-
# runtime-level dimension (no specific library/framework involved), proving
# the schema widening above actually closes the gap it was meant to: these
# facts existed in registry go.json's notes/comments but had no structured
# home until lang_version_id was added.
_LANG_PLATFORM_CONSTRAINTS = [
    ("go", "1.6", "os_base_image",
     "golang:1.6's Debian base is Jessie, long dropped from deb.debian.org/security.debian.org -- apt-get update 404s once that happens. Fixed via a sources.list redirect to archive.debian.org plus -o Acquire::Check-Valid-Until=false (expired Release signatures) and --allow-unauthenticated (expired GPG keys). golang:1.6 is also the earliest Go version that reliably pulls and builds at all -- 1.0/1.1 don't exist on Docker Hub, 1.2-1.4 are schema-1 manifests modern Docker refuses to pull, 1.5's manifest is valid but its layer blob is missing/corrupted on the registry."),
    ("go", "1.9", "os_base_image",
     "golang:1.9-1.10's Debian base is Stretch, same archive.debian.org redirect needed as the Jessie-era (1.6-1.8) images."),
    ("go", "1.11", "os_base_image",
     "golang:1.11-1.15's Debian base is Buster, same archive.debian.org redirect needed. Buster also ships cmake 3.13.4 for the liboqs C build -- has -S/-B (added in cmake 3.13) but not 'cmake --install' (added 3.15); use 'cmake --build <dir> --target install' instead, portable back to 3.13 and still correct on newer cmake."),
]


def _seed_crypto_algorithms(conn) -> None:
    for name, family, standard, notes in _ALGORITHMS:
        conn.execute(
            """INSERT INTO crypto_algorithms (name, family, standard, notes)
               VALUES (?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET family=excluded.family,
                                               standard=excluded.standard,
                                               notes=excluded.notes""",
            (name, family, standard, notes),
        )


def _seed_lib_version_algorithms(conn) -> None:
    for language, library, version_nr, algo_name, support_level, source_note in _LIB_VERSION_ALGORITHMS:
        lv_id = _lib_version_id(conn, language, library, version_nr)
        algo_id = _algorithm_id(conn, algo_name)
        if lv_id is None or algo_id is None:
            continue  # registry bucket or algorithm not present (yet) -- skip, don't crash
        conn.execute(
            """INSERT INTO lib_version_algorithms (lib_version_id, algorithm_id, support_level, source_note)
               VALUES (?,?,?,?)
               ON CONFLICT(lib_version_id, algorithm_id)
               DO UPDATE SET support_level=excluded.support_level,
                             source_note=excluded.source_note""",
            (lv_id, algo_id, support_level, source_note),
        )


def _seed_migration_paths(conn) -> None:
    # No natural unique constraint across (from, to, library, versions) --
    # dedup via an explicit existence check per row instead (below) so this
    # stays idempotent across repeated load_registry() calls.
    for language, library, from_algo, to_algo, from_ver, to_ver, description in _MIGRATION_PATHS:
        lib_id = _library_id(conn, language, library)
        to_algo_id = _algorithm_id(conn, to_algo)
        if lib_id is None or to_algo_id is None:
            continue
        from_algo_id = _algorithm_id(conn, from_algo) if from_algo else None
        from_lv_id = _lib_version_id(conn, language, library, from_ver) if from_ver else None
        to_lv_id = _lib_version_id(conn, language, library, to_ver) if to_ver else None
        exists = conn.execute(
            """SELECT id FROM migration_paths
               WHERE library_id = ? AND to_algorithm_id = ?
                     AND from_algorithm_id IS ? AND to_lib_version_id IS ?""",
            (lib_id, to_algo_id, from_algo_id, to_lv_id),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            """INSERT INTO migration_paths
                   (from_algorithm_id, to_algorithm_id, library_id,
                    from_lib_version_id, to_lib_version_id, description, verified)
               VALUES (?,?,?,?,?,?,1)""",
            (from_algo_id, to_algo_id, lib_id, from_lv_id, to_lv_id, description),
        )


def _seed_platform_constraints(conn) -> None:
    for language, version_nr, ctype, description in _LANG_PLATFORM_CONSTRAINTS:
        lv_id = _lang_version_id(conn, language, version_nr)
        if lv_id is None:
            continue
        exists = conn.execute(
            "SELECT id FROM platform_constraints WHERE lang_version_id = ? AND constraint_type = ?",
            (lv_id, ctype),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE platform_constraints SET description = ? WHERE id = ?",
                (description, exists[0]),
            )
        else:
            conn.execute(
                """INSERT INTO platform_constraints (lang_version_id, constraint_type, description, verified)
                   VALUES (?,?,?,1)""",
                (lv_id, ctype, description),
            )

    for language, library, version_nr, ctype, description in _LIB_PLATFORM_CONSTRAINTS:
        lv_id = _lib_version_id(conn, language, library, version_nr)
        if lv_id is None:
            continue
        exists = conn.execute(
            "SELECT id FROM platform_constraints WHERE lib_version_id = ? AND constraint_type = ?",
            (lv_id, ctype),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE platform_constraints SET description = ? WHERE id = ?",
                (description, exists[0]),
            )
        else:
            conn.execute(
                """INSERT INTO platform_constraints (lib_version_id, constraint_type, description, verified)
                   VALUES (?,?,?,1)""",
                (lv_id, ctype, description),
            )

    for language, framework, version_nr, ctype, description in _FW_PLATFORM_CONSTRAINTS:
        fv_id = _fw_version_id(conn, language, framework, version_nr)
        if fv_id is None:
            continue
        exists = conn.execute(
            "SELECT id FROM platform_constraints WHERE fw_version_id = ? AND constraint_type = ?",
            (fv_id, ctype),
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE platform_constraints SET description = ? WHERE id = ?",
                (description, exists[0]),
            )
        else:
            conn.execute(
                """INSERT INTO platform_constraints (fw_version_id, constraint_type, description, verified)
                   VALUES (?,?,?,1)""",
                (fv_id, ctype, description),
            )


def _seed_crypto_agility() -> None:
    with _connect() as conn:
        _seed_crypto_algorithms(conn)
        _seed_lib_version_algorithms(conn)
        _seed_migration_paths(conn)
        _seed_platform_constraints(conn)


# ── Image sync ────────────────────────────────────────────────────────────────

def _image_tag_from_parts(language: str, lang_ver: str, framework: str,
                           fw_ver: str, library: str, lib_ver: str) -> str:
    """Compute the canonical Docker image tag from resolved component names."""
    fw  = framework.lower().replace("/", "_").replace("@", "").replace(" ", "")
    lib = library.lower().replace("/", "_").replace("@", "").replace(" ", "")
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
                # ON CONFLICT DO UPDATE (not INSERT OR IGNORE): if the tag-
                # computation logic changes (e.g. a new character needs
                # sanitizing), the FK triple already has a row under the OLD
                # tag. IGNORE would silently drop the corrected tag, and the
                # stale-row cleanup below wouldn't recreate it since deletion
                # happens after this loop -- so the image would vanish
                # entirely until a second sync_images() call.
                conn.execute(
                    """INSERT INTO images
                           (lang_version_id, fw_version_id, lib_version_id,
                            image_tag, context_path, synced_at)
                       VALUES (?,?,?,?,?,?)
                       ON CONFLICT(lang_version_id, fw_version_id, lib_version_id)
                       DO UPDATE SET image_tag=excluded.image_tag,
                                     context_path=excluded.context_path,
                                     synced_at=excluded.synced_at""",
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
    """Return all runs ordered newest first, with duration_seconds computed."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, created_at, status, finished_at FROM runs ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        duration = None
        if d.get("created_at") and d.get("finished_at"):
            try:
                from datetime import datetime, timezone
                t0 = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(d["finished_at"].replace("Z", "+00:00"))
                duration = int((t1 - t0).total_seconds())
            except Exception:
                pass
        d["duration_seconds"] = duration
        result.append(d)
    return result


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


_STATUS_CLAUSES = {
    "not_built":    "build_success IS NULL",
    "build_failed": "build_success = 0",
    "not_tested":   "test_success IS NULL",
    "test_failed":  "test_success = 0",
}


def get_all_ids_for_filter(filters: dict,
                            include_ignored: bool = True,
                            status: str = "") -> list[int]:
    """Return every image id matching filters (no pagination)."""
    where_sql, params = _build_where(filters)
    extra = []
    if status in _STATUS_CLAUSES:
        extra.append(_STATUS_CLAUSES[status])
    if not include_ignored:
        extra.append("ignored = 0")
    for clause in extra:
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} {clause}"
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
                "SELECT * FROM lang_versions WHERE language_id=? "
                "ORDER BY release_date IS NULL, release_date, version_nr",
                (lang_id,),
            )]
            lang["frameworks"] = []
            for fw in conn.execute(
                "SELECT * FROM frameworks WHERE language_id=? ORDER BY name",
                (lang_id,),
            ):
                fw_dict = dict(fw)
                fw_dict["versions"] = [dict(r) for r in conn.execute(
                    "SELECT * FROM fw_versions WHERE framework_id=? "
                    "ORDER BY release_date IS NULL, release_date, version_nr",
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
                    "SELECT * FROM lib_versions WHERE library_id=? "
                    "ORDER BY release_date IS NULL, release_date, version_nr",
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
                     output: str = "",
                     run_id: int | None = None) -> None:
    tested_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO test_results
                   (image_id, success, root_ok, version_ok,
                    error_msg, response_data, output, tested_at, run_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (image_id, int(success), int(bool(root_ok)), int(bool(version_ok)),
             error_msg,
             json.dumps(response_data) if response_data is not None else None,
             output, tested_at, run_id),
        )


# ── Reports ───────────────────────────────────────────────────────────────────

def get_test_reports(filters: dict | None = None,
                     page: int = 1, per_page: int = 100) -> dict:
    """Return paginated test results joined with image metadata."""
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
        total = conn.execute(f"""
            SELECT COUNT(*) FROM test_results t
            JOIN image_details d ON d.id = t.image_id
            LEFT JOIN runs r ON r.id = t.run_id
            {where_sql} {run_filter}
        """, params).fetchone()[0]

        offset = (page - 1) * per_page
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
            ORDER BY d.language, d.lang_version, d.framework, d.fw_version,
                     d.library, d.lib_version, t.tested_at DESC
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "items": [dict(r) for r in rows],
    }


def get_build_reports(filters: dict | None = None,
                      page: int = 1, per_page: int = 100) -> dict:
    """Return paginated build results joined with image metadata."""
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
        total = conn.execute(f"""
            SELECT COUNT(*) FROM build_results b
            JOIN image_details d ON d.id = b.image_id
            LEFT JOIN runs r ON r.id = b.run_id
            {where_sql} {run_filter}
        """, params).fetchone()[0]

        offset = (page - 1) * per_page
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
            ORDER BY d.language, d.lang_version, d.framework, d.fw_version,
                     d.library, d.lib_version
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "items": [dict(r) for r in rows],
    }


def get_pending_images(filters: dict | None = None,
                       page: int = 1, per_page: int = 100) -> dict:
    """Return paginated non-ignored images where build or test has no result yet."""
    filters = filters or {}
    where_sql, params = _build_where(filters)
    connector = "AND" if where_sql else "WHERE"
    where_sql = (f"{where_sql} {connector} "
                 "(build_success IS NULL OR test_success IS NULL) AND ignored = 0")
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM image_details {where_sql}", params
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(f"""
            SELECT id, image_tag, language, lang_version, framework, fw_version,
                   library, lib_version, build_success, test_success, synced_at
            FROM image_details
            {where_sql}
            ORDER BY language, lang_version, framework, fw_version, library, lib_version
            LIMIT ? OFFSET ?
        """, params + [per_page, offset]).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "items": [dict(r) for r in rows],
    }


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
        not_built = conn.execute(
            """SELECT COUNT(*) FROM images
               WHERE ignored=0 AND id NOT IN (SELECT image_id FROM build_results)"""
        ).fetchone()[0]
        not_tested = conn.execute(
            """SELECT COUNT(*) FROM images
               WHERE ignored=0 AND id NOT IN (SELECT image_id FROM test_results)"""
        ).fetchone()[0]
        langs = conn.execute(
            "SELECT COUNT(DISTINCT language_id) FROM lang_versions WHERE include=1"
        ).fetchone()[0]
    return {
        "total": total, "ignored": ignored,
        "built_ok": built, "built_fail": built_f,
        "test_ok": tested, "test_fail": tested_f,
        "not_built": not_built, "not_tested": not_tested,
        "languages": langs,
    }


# ── Crypto Agility queries (C.A.M. Component 2) ───────────────────────────────

def get_crypto_agility(filters: dict | None = None) -> list[dict]:
    """One row per lib_version that has at least one recorded algorithm or
    platform constraint, each with its algorithms/constraints nested as
    lists. Rows with neither (the vast majority of lib_versions -- this is a
    curated, high-signal view, not a dump of every version) are omitted.
    """
    filters = filters or {}
    where = []
    params: list = []
    if filters.get("language"):
        where.append("g.name = ?")
        params.append(filters["language"])
    if filters.get("library"):
        where.append("l.name = ?")
        params.append(filters["library"])
    extra_where = (" AND " + " AND ".join(where)) if where else ""

    with _connect() as conn:
        lib_versions = conn.execute(f"""
            SELECT DISTINCT lv.id, g.name AS language, l.name AS library,
                   lv.version_nr, lv.release_date
            FROM lib_versions lv
            JOIN libraries l ON l.id = lv.library_id
            JOIN languages g ON g.id = l.language_id
            WHERE lv.id IN (SELECT lib_version_id FROM lib_version_algorithms
                             UNION SELECT lib_version_id FROM platform_constraints
                                    WHERE lib_version_id IS NOT NULL)
                  {extra_where}
            ORDER BY g.name, l.name, lv.release_date
        """, params).fetchall()

        result = []
        for lv in lib_versions:
            algos = conn.execute("""
                SELECT a.name, a.family, a.standard, lva.support_level, lva.source_note
                FROM lib_version_algorithms lva
                JOIN crypto_algorithms a ON a.id = lva.algorithm_id
                WHERE lva.lib_version_id = ?
                ORDER BY a.family, a.name
            """, (lv["id"],)).fetchall()
            constraints = conn.execute("""
                SELECT constraint_type, description, verified
                FROM platform_constraints
                WHERE lib_version_id = ?
                ORDER BY constraint_type
            """, (lv["id"],)).fetchall()
            result.append({
                "language": lv["language"], "library": lv["library"],
                "version_nr": lv["version_nr"], "release_date": lv["release_date"],
                "algorithms": [dict(a) for a in algos],
                "platform_constraints": [dict(c) for c in constraints],
            })
    return result


def get_migration_paths(filters: dict | None = None) -> list[dict]:
    """Every recorded algorithm migration path, optionally filtered by
    language/library. Small, curated dataset -- no pagination needed."""
    filters = filters or {}
    where = []
    params: list = []
    if filters.get("language"):
        where.append("g.name = ?")
        params.append(filters["language"])
    if filters.get("library"):
        where.append("l.name = ?")
        params.append(filters["library"])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT g.name AS language, l.name AS library,
                   fa.name AS from_algorithm, ta.name AS to_algorithm,
                   fv.version_nr AS from_version, tv.version_nr AS to_version,
                   mp.description, mp.verified
            FROM migration_paths mp
            JOIN libraries l ON l.id = mp.library_id
            JOIN languages g ON g.id = l.language_id
            JOIN crypto_algorithms ta ON ta.id = mp.to_algorithm_id
            LEFT JOIN crypto_algorithms fa ON fa.id = mp.from_algorithm_id
            LEFT JOIN lib_versions fv ON fv.id = mp.from_lib_version_id
            LEFT JOIN lib_versions tv ON tv.id = mp.to_lib_version_id
            {where_sql}
            ORDER BY g.name, l.name, tv.release_date
        """, params).fetchall()
    return [dict(r) for r in rows]


def get_language_platform_constraints(filters: dict | None = None) -> list[dict]:
    """Language-runtime-scoped platform constraints (e.g. Go's Debian
    archive-mirror thresholds per version) -- the lang_version_id side of
    platform_constraints. This is the dimension a future network/TLS
    fingerprint (keyed by language + language version) would also use."""
    filters = filters or {}
    where = ["pc.lang_version_id IS NOT NULL"]
    params: list = []
    if filters.get("language"):
        where.append("g.name = ?")
        params.append(filters["language"])

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT g.name AS language, lv.version_nr,
                   pc.constraint_type, pc.description, pc.verified
            FROM platform_constraints pc
            JOIN lang_versions lv ON lv.id = pc.lang_version_id
            JOIN languages g ON g.id = lv.language_id
            WHERE {' AND '.join(where)}
            ORDER BY g.name, lv.version_nr
        """, params).fetchall()
    return [dict(r) for r in rows]


def get_framework_platform_constraints(filters: dict | None = None) -> list[dict]:
    """Framework-scoped platform constraints (e.g. Fastify/Koa's Node
    version floors) -- the fw_version_id side of platform_constraints,
    kept separate from get_crypto_agility() since these are about web
    frameworks, not crypto libraries."""
    filters = filters or {}
    where = ["pc.fw_version_id IS NOT NULL"]
    params: list = []
    if filters.get("language"):
        where.append("g.name = ?")
        params.append(filters["language"])
    if filters.get("framework"):
        where.append("f.name = ?")
        params.append(filters["framework"])
    where_sql = f"WHERE {' AND '.join(where)}"

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT g.name AS language, f.name AS framework, fv.version_nr,
                   pc.constraint_type, pc.description, pc.verified
            FROM platform_constraints pc
            JOIN fw_versions fv ON fv.id = pc.fw_version_id
            JOIN frameworks f ON f.id = fv.framework_id
            JOIN languages g ON g.id = f.language_id
            {where_sql}
            ORDER BY g.name, f.name, fv.version_nr
        """, params).fetchall()
    return [dict(r) for r in rows]


def get_vulnerabilities(filters: dict | None = None) -> list[dict]:
    """Returns an empty list today -- no CVE/vulnerability data has been
    entered yet (see the vulnerabilities table's own comment in _SCHEMA).
    Wired up now so the dashboard has a stable place to call once that data
    exists, instead of needing new plumbing added later."""
    filters = filters or {}
    where = []
    params: list = []
    if filters.get("language"):
        where.append("g.name = ?")
        params.append(filters["language"])
    if filters.get("library"):
        where.append("l.name = ?")
        params.append(filters["library"])
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT g.name AS language, l.name AS library, v.cve_id,
                   v.affected_version_range, v.severity, v.description, v.source_url
            FROM vulnerabilities v
            JOIN libraries l ON l.id = v.library_id
            JOIN languages g ON g.id = l.language_id
            {where_sql}
            ORDER BY v.severity, g.name, l.name
        """, params).fetchall()
    return [dict(r) for r in rows]
