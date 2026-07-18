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

PROJECT_ROOT       = Path(__file__).parent
DB_PATH            = PROJECT_ROOT / "pqc_manager.db"
IMAGES_BASE        = PROJECT_ROOT / "images"
CLIENT_IMAGES_BASE = PROJECT_ROOT / "images_clients"
SCRIPTS_DIR        = PROJECT_ROOT / "scripts"

# Registry JSON file names (spaces in filename as they exist on disk)
_REGISTRY_FILES = [
    SCRIPTS_DIR / "registry python.json",
    SCRIPTS_DIR / "registry go.json",
    SCRIPTS_DIR / "registry node.json",
    SCRIPTS_DIR / "registry java.json",
    SCRIPTS_DIR / "registry dotnet.json",
    SCRIPTS_DIR / "registry php.json",
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
    note         TEXT,
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
    available     INTEGER NOT NULL DEFAULT 1, -- registry's own per-version "available" flag
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
    available     INTEGER NOT NULL DEFAULT 1, -- registry's own per-version "available" flag
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

-- ── Client images (outbound-call fingerprinting clients) ────────────────────
-- A deliberately separate set of tables, not a repurposed images/frameworks
-- with a "kind" flag: a client program has no server-side framework, what
-- varies is which library it uses to make its OWN outbound HTTP(S) call
-- (stdlib http.client, requests, httpx, urllib3, or a crypto-lib-driven raw
-- client like pyOpenSSL/M2Crypto). A crypto-lib-driven client is just
-- another http_clients entry (e.g. "pyopenssl-raw"), not a separate cross-
-- product dimension -- keeps this a 2D matrix (language x http_client)
-- instead of 3D like the server side's (language x framework x lib).
CREATE TABLE IF NOT EXISTS http_clients (
    id           INTEGER PRIMARY KEY,
    language_id  INTEGER NOT NULL REFERENCES languages(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    module_path  TEXT,
    notes        TEXT,
    include      INTEGER NOT NULL DEFAULT 1,
    UNIQUE(language_id, name)
);

CREATE TABLE IF NOT EXISTS http_client_versions (
    id              INTEGER PRIMARY KEY,
    http_client_id  INTEGER NOT NULL REFERENCES http_clients(id) ON DELETE CASCADE,
    version_nr      TEXT    NOT NULL,          -- version string or "builtin"
    release_date    TEXT,
    compatibility   TEXT,                      -- JSON array
    available       INTEGER NOT NULL DEFAULT 1, -- registry's own per-version "available" flag
    UNIQUE(http_client_id, version_nr)
);

CREATE TABLE IF NOT EXISTS client_images (
    id                      INTEGER PRIMARY KEY,
    lang_version_id         INTEGER NOT NULL REFERENCES lang_versions(id) ON DELETE CASCADE,
    http_client_version_id  INTEGER NOT NULL REFERENCES http_client_versions(id) ON DELETE CASCADE,
    image_tag               TEXT    UNIQUE NOT NULL,
    context_path            TEXT    NOT NULL,
    ignored                 INTEGER NOT NULL DEFAULT 0,
    ignore_reason           TEXT,
    synced_at               TEXT    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lang_version_id, http_client_version_id)
);

-- ── Run labels ────────────────────────────────────────────────────────────
-- docker_host: the DOCKER_HOST value active when the run was created ('' = local
-- engine). Lets the Reports tab show which remote/local target produced a run.
-- Unique per (name, docker_host) rather than name alone -- the auto-generated
-- batch name is date + active filters with no time component, so running the
-- same filter set against a different host later the same day must start a
-- new run instead of merging into the other host's run.
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
    status      TEXT    NOT NULL DEFAULT 'running',
    finished_at TEXT,
    docker_host TEXT    NOT NULL DEFAULT '',
    UNIQUE(name, docker_host)
);

-- ── Result tables ──────────────────────────────────────────────────────────
-- host: the DOCKER_HOST active when the build/test ran ('' = local engine).
-- build_results is keyed per (image_id, host) rather than per image_id alone
-- so switching Docker host gives a fresh built/tested matrix per image
-- without erasing another host's recorded status.
CREATE TABLE IF NOT EXISTS build_results (
    id          INTEGER PRIMARY KEY,
    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    host        TEXT    NOT NULL DEFAULT '',
    success     INTEGER NOT NULL,
    output      TEXT,
    started_at  TEXT,
    finished_at TEXT,
    run_id      INTEGER REFERENCES runs(id),
    UNIQUE(image_id, host)
);

CREATE TABLE IF NOT EXISTS test_results (
    id            INTEGER PRIMARY KEY,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    host          TEXT    NOT NULL DEFAULT '',
    success       INTEGER NOT NULL,
    root_ok       INTEGER,
    version_ok    INTEGER,
    error_msg     TEXT,
    response_data TEXT,
    output        TEXT,
    tested_at     TEXT DEFAULT CURRENT_TIMESTAMP,
    run_id        INTEGER REFERENCES runs(id)
);

-- Real network traffic captured (via a tcpdump sidecar sniffing the target
-- container's own network namespace) against a running container, for
-- fingerprinting. call_type distinguishes the four probes fired per pass:
-- 'success' (GET /version, expected 200), 'failure' (GET to a nonexistent
-- path, expected 404), 'method_not_allowed' (POST to a valid path, expected
-- 405 -- many frameworks emit a distinctive default body/headers for this)
-- and 'malformed' (a deliberately invalid request, expected 400). traffic_raw
-- is the tcpdump -XX -v text decode (kept for quick human reading); pcap_raw
-- is the actual binary capture (base64), the genuine on-the-wire packet
-- bytes a real network fingerprint should key off -- text is fine to skim
-- but is a lossy re-derivation, not a reliable parse source.
CREATE TABLE IF NOT EXISTS fingerprints (
    id            INTEGER PRIMARY KEY,
    image_id      INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    host          TEXT    NOT NULL DEFAULT '',
    call_type     TEXT    NOT NULL CHECK(call_type IN ('success','failure','method_not_allowed','malformed')),
    method        TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    status_code   INTEGER,
    traffic_raw   TEXT,
    pcap_raw      TEXT,
    error_msg     TEXT,
    captured_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    run_id        INTEGER REFERENCES runs(id)
);

-- Client-side fingerprint capture: one row per client-image run against the
-- persistent fingerprint-target app (scripts/fingerprint_target/), captured
-- by a tcpdump sidecar attached to the TARGET's network namespace (not the
-- client's) while the one-shot client container runs -- the reverse
-- direction of the server-side `fingerprints` table above. No call_type: a
-- client only ever makes the one outbound call its generated program makes.
-- client_image_id is the ground truth for the report the dashboard builds
-- (language + http-client-library + version), since we know exactly which
-- client image was run.
CREATE TABLE IF NOT EXISTS client_fingerprints (
    id               INTEGER PRIMARY KEY,
    client_image_id  INTEGER NOT NULL REFERENCES client_images(id) ON DELETE CASCADE,
    host             TEXT    NOT NULL DEFAULT '',
    status_code      INTEGER,
    traffic_raw      TEXT,
    pcap_raw         TEXT,
    error_msg        TEXT,
    client_output    TEXT,
    observed_user_agent TEXT,
    observed_ja3_hash   TEXT,
    observed_ja3_string TEXT,
    captured_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    run_id           INTEGER REFERENCES runs(id)
);

-- Mirrors build_results exactly.
CREATE TABLE IF NOT EXISTS client_build_results (
    id               INTEGER PRIMARY KEY,
    client_image_id  INTEGER NOT NULL REFERENCES client_images(id) ON DELETE CASCADE,
    host             TEXT    NOT NULL DEFAULT '',
    success          INTEGER NOT NULL,
    output           TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    run_id           INTEGER REFERENCES runs(id),
    UNIQUE(client_image_id, host)
);

-- A client image's "test" is a single real outbound call against the
-- persistent fingerprint-target app, run from the actual built image (same
-- one-shot container Fingerprint uses) -- no tcpdump capture, just pass/fail
-- on whether the call succeeded. Mirrors client_build_results' upsert shape
-- (one row per client image + host, not one row per run like server-side
-- test_results, since there's no root/version_ok breakdown to keep history
-- of here -- just a single success flag).
CREATE TABLE IF NOT EXISTS client_test_results (
    id               INTEGER PRIMARY KEY,
    client_image_id  INTEGER NOT NULL REFERENCES client_images(id) ON DELETE CASCADE,
    host             TEXT    NOT NULL DEFAULT '',
    success          INTEGER NOT NULL,
    output           TEXT,
    error_msg        TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    run_id           INTEGER REFERENCES runs(id),
    UNIQUE(client_image_id, host)
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

-- The JA3 TLS-ClientHello fingerprint has no external "known good" value to
-- check against (unlike the HTTP User-Agent header, where ground truth is
-- just the client_images row) -- so the first JA3 ever observed for a given
-- client image becomes its own reference baseline, and every later capture
-- of that same image is compared against it. One row per client_image_id.
CREATE TABLE IF NOT EXISTS client_ja3_reference (
    client_image_id INTEGER PRIMARY KEY REFERENCES client_images(id) ON DELETE CASCADE,
    ja3_hash        TEXT NOT NULL,
    ja3_string      TEXT,
    fingerprint_id  INTEGER REFERENCES client_fingerprints(id),
    first_seen_at   TEXT
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
CREATE INDEX IF NOT EXISTS idx_hcv_hc       ON http_client_versions(http_client_id);
CREATE INDEX IF NOT EXISTS idx_cimg_lv      ON client_images(lang_version_id);
CREATE INDEX IF NOT EXISTS idx_cimg_hcv     ON client_images(http_client_version_id);
CREATE INDEX IF NOT EXISTS idx_cimg_ignored ON client_images(ignored);
CREATE INDEX IF NOT EXISTS idx_cimg_tag     ON client_images(image_tag);
CREATE INDEX IF NOT EXISTS idx_cfp_image    ON client_fingerprints(client_image_id);
CREATE INDEX IF NOT EXISTS idx_cfp_time     ON client_fingerprints(captured_at);
CREATE INDEX IF NOT EXISTS idx_cbr_image    ON client_build_results(client_image_id);
CREATE INDEX IF NOT EXISTS idx_ctr_image    ON client_test_results(client_image_id);
CREATE INDEX IF NOT EXISTS idx_test_image   ON test_results(image_id);
CREATE INDEX IF NOT EXISTS idx_test_time    ON test_results(tested_at);
CREATE INDEX IF NOT EXISTS idx_fp_image     ON fingerprints(image_id);
CREATE INDEX IF NOT EXISTS idx_fp_time      ON fingerprints(captured_at);
CREATE INDEX IF NOT EXISTS idx_lva_libver   ON lib_version_algorithms(lib_version_id);
CREATE INDEX IF NOT EXISTS idx_lva_algo     ON lib_version_algorithms(algorithm_id);
CREATE INDEX IF NOT EXISTS idx_mp_lib       ON migration_paths(library_id);
CREATE INDEX IF NOT EXISTS idx_pc_langver   ON platform_constraints(lang_version_id);
CREATE INDEX IF NOT EXISTS idx_pc_libver    ON platform_constraints(lib_version_id);
CREATE INDEX IF NOT EXISTS idx_pc_fwver     ON platform_constraints(fw_version_id);
CREATE INDEX IF NOT EXISTS idx_vuln_lib     ON vulnerabilities(library_id);

-- ── Update-availability scanner (scripts/check_updates.py) ──────────────────
-- One row per (language, kind, name, new_major) combination the scanner has
-- ever seen upstream but that isn't yet a tracked registry.json bucket.
-- Detection only -- never auto-implemented; the human reviews and decides
-- whether to add the bucket and test it. `dismissed` survives re-detection
-- of the SAME new_major (upsert never resets it) so acknowledging an update
-- doesn't make it reappear next scan; a genuinely newer major creates a
-- fresh row instead.
-- `included`/`included_at`/`images_added` double this table as the
-- "update log" the user reads back later: once a pending row is included
-- (registry bucket added + generate_images run), it's never deleted --
-- just flagged, so `WHERE included=1` is a permanent history of what was
-- added and how many new images resulted, per (language, kind, name).
CREATE TABLE IF NOT EXISTS pending_updates (
    id             INTEGER PRIMARY KEY,
    language       TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK(kind IN ('framework','library')),
    name           TEXT NOT NULL,
    package_id     TEXT,
    new_major      TEXT NOT NULL,
    latest_version TEXT,
    tracked_majors TEXT,
    release_date   TEXT,
    detected_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dismissed      INTEGER NOT NULL DEFAULT 0,
    included       INTEGER NOT NULL DEFAULT 0,
    included_at    TEXT,
    images_added   INTEGER,
    UNIQUE(language, kind, name, new_major)
);
CREATE INDEX IF NOT EXISTS idx_pu_dismissed ON pending_updates(dismissed);

-- ── Manual include/exclude overrides (Registry editor) ──────────────────────
-- User-driven override layer on top of the registry JSON files' own
-- `available` field -- deliberately NOT written into the registry files
-- themselves (those stay pure upstream-tracked reference data). A row only
-- exists once the user has actually set something for that exact
-- (language, kind, name, nr); `available` NULL means "no override, defer
-- to whatever the registry file says", 0/1 is an explicit user force.
-- scripts/generate_images.py and scripts/generate_client_images.py both
-- read this table directly (so it's respected even when run standalone via
-- CLI/SSH, not just through the dashboard) and let it take precedence over
-- the registry's own `available` value whenever non-NULL.
CREATE TABLE IF NOT EXISTS version_overrides (
    id          INTEGER PRIMARY KEY,
    language    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK(kind IN ('framework','library','http_client')),
    name        TEXT NOT NULL,
    nr          TEXT NOT NULL,
    available   INTEGER,
    note        TEXT,
    updated_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(language, kind, name, nr)
);
"""

# Metadata only -- deliberately excludes build/test status. Build results are
# now scoped per (image_id, host) rather than one row per image (see
# build_results.UNIQUE above), so "the" build/test status only makes sense
# once a host is chosen -- callers that need status use _status_sql() below,
# parameterized by host, instead of this view.
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
    libv.compatibility AS lib_compatibility

FROM images i
JOIN lang_versions lv  ON lv.id  = i.lang_version_id
JOIN languages     l   ON l.id   = lv.language_id
JOIN fw_versions   fv  ON fv.id  = i.fw_version_id
JOIN frameworks    f   ON f.id   = fv.framework_id
JOIN lib_versions  libv ON libv.id = i.lib_version_id
JOIN libraries     lib  ON lib.id  = libv.library_id;
"""

# Same idea as image_details, over the simpler 2D client matrix (no
# framework/library dimensions -- see the client_images schema comment).
_CLIENT_VIEW = """
DROP VIEW IF EXISTS client_image_details;
CREATE VIEW client_image_details AS
SELECT
    ci.id,
    ci.image_tag,
    ci.context_path,
    ci.ignored,
    ci.ignore_reason,
    ci.synced_at,

    l.id           AS language_id,
    l.name         AS language,
    l.display_name AS language_display,

    lv.id           AS lang_version_id,
    lv.version_nr   AS lang_version,
    lv.release_date AS lang_release_date,

    hc.id          AS http_client_id,
    hc.name        AS http_client,
    hc.module_path AS http_client_module,
    hc.notes       AS http_client_notes,

    hcv.id            AS http_client_version_id,
    hcv.version_nr    AS http_client_version,
    hcv.release_date  AS http_client_release_date,
    hcv.compatibility AS http_client_compatibility

FROM client_images ci
JOIN lang_versions lv        ON lv.id  = ci.lang_version_id
JOIN languages l              ON l.id   = lv.language_id
JOIN http_client_versions hcv ON hcv.id = ci.http_client_version_id
JOIN http_clients hc          ON hc.id  = hcv.http_client_id;
"""


def _status_sql() -> str:
    """image_details, plus build/test/fingerprint status scoped to one Docker host.

    Contains four '?' placeholders (build host, test host, fingerprint-success
    host, fingerprint-failure host) that must all be bound to the SAME host
    value, in that order, before any other query params. Superset of
    image_details' columns, so existing filters/sorts on language/framework/...
    keep working unchanged; adds build_success, built_at, build_output,
    build_run, test_success, tested_at, test_output, test_error, fp_ok_status,
    fp_ok_traffic, fp_ok_at, fp_err_status, fp_err_traffic, fp_err_at.
    """
    return """
    SELECT
        d.*,
        b.success     AS build_success,
        b.finished_at AS built_at,
        b.output      AS build_output,
        br.name       AS build_run,
        t.success     AS test_success,
        t.tested_at   AS tested_at,
        t.output      AS test_output,
        t.error_msg   AS test_error,
        fpo.status_code  AS fp_ok_status,
        fpo.traffic_raw  AS fp_ok_traffic,
        fpo.captured_at  AS fp_ok_at,
        fpe.status_code  AS fp_err_status,
        fpe.traffic_raw  AS fp_err_traffic,
        fpe.captured_at  AS fp_err_at
    FROM image_details d
    LEFT JOIN build_results b ON b.image_id = d.id AND b.host = ?
    LEFT JOIN runs br ON br.id = b.run_id
    LEFT JOIN test_results t ON t.id = (
        SELECT tr.id FROM test_results tr
        WHERE tr.image_id = d.id AND tr.host = ?
        ORDER BY tr.tested_at DESC LIMIT 1
    )
    LEFT JOIN fingerprints fpo ON fpo.id = (
        SELECT f.id FROM fingerprints f
        WHERE f.image_id = d.id AND f.host = ? AND f.call_type = 'success'
        ORDER BY f.captured_at DESC LIMIT 1
    )
    LEFT JOIN fingerprints fpe ON fpe.id = (
        SELECT f.id FROM fingerprints f
        WHERE f.image_id = d.id AND f.host = ? AND f.call_type = 'failure'
        ORDER BY f.captured_at DESC LIMIT 1
    )
    """


def _client_status_sql() -> str:
    """client_image_details, plus build/test/fingerprint status scoped to
    one Docker host. Contains three '?' placeholders (build host, test
    host, fingerprint host) that must all be bound to the SAME host value,
    in that order, before any other query params."""
    return """
    SELECT
        d.*,
        b.success     AS build_success,
        b.finished_at AS built_at,
        b.output      AS build_output,
        br.name       AS build_run,
        t.success     AS test_success,
        t.finished_at AS tested_at,
        t.output      AS test_output,
        t.error_msg   AS test_error,
        cf.id            AS fp_id,
        cf.status_code   AS fp_status,
        cf.error_msg     AS fp_error,
        cf.captured_at   AS fp_at
    FROM client_image_details d
    LEFT JOIN client_build_results b ON b.client_image_id = d.id AND b.host = ?
    LEFT JOIN runs br ON br.id = b.run_id
    LEFT JOIN client_test_results t ON t.client_image_id = d.id AND t.host = ?
    LEFT JOIN client_fingerprints cf ON cf.id = (
        SELECT f.id FROM client_fingerprints f
        WHERE f.client_image_id = d.id AND f.host = ?
        ORDER BY f.captured_at DESC LIMIT 1
    )
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

        # build_results moved from "one row per image" (UNIQUE(image_id)) to
        # "one row per (image, host)" (UNIQUE(image_id, host)) so different
        # Docker hosts keep independent built/tested status. SQLite can't
        # ALTER a UNIQUE constraint in place, so rebuild the table when an
        # old-shape one is found; existing rows backfill host='' (local).
        build_cols = {row[1] for row in conn.execute("PRAGMA table_info(build_results)")}
        if build_cols and "host" not in build_cols:
            conn.executescript("""
                ALTER TABLE build_results RENAME TO build_results_old;
                CREATE TABLE build_results (
                    id          INTEGER PRIMARY KEY,
                    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
                    host        TEXT    NOT NULL DEFAULT '',
                    success     INTEGER NOT NULL,
                    output      TEXT,
                    started_at  TEXT,
                    finished_at TEXT,
                    run_id      INTEGER REFERENCES runs(id),
                    UNIQUE(image_id, host)
                );
                INSERT INTO build_results
                    (image_id, host, success, output, started_at, finished_at, run_id)
                    SELECT image_id, '', success, output, started_at, finished_at, run_id
                    FROM build_results_old;
                DROP TABLE build_results_old;
            """)

        # fingerprints moved from separate request_raw/response_raw text
        # reconstructions to a single traffic_raw genuine tcpdump capture,
        # then again to add pcap_raw + two new call_types (method_not_allowed/
        # malformed) -- drop and let _SCHEMA below recreate the new shape
        # each time. Rows are just a capture cache re-derived by re-running
        # the fingerprint action, so there's nothing worth migrating forward
        # (and as of this pass the table has never actually been populated).
        fp_cols = {row[1] for row in conn.execute("PRAGMA table_info(fingerprints)")}
        if fp_cols and ("traffic_raw" not in fp_cols or "pcap_raw" not in fp_cols):
            conn.execute("DROP TABLE fingerprints")

        conn.executescript(_SCHEMA)
        # Add columns to existing tables (safe to call repeatedly)
        for ddl in [
            "ALTER TABLE build_results ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE test_results  ADD COLUMN run_id INTEGER REFERENCES runs(id)",
            "ALTER TABLE test_results  ADD COLUMN output TEXT",
            "ALTER TABLE test_results  ADD COLUMN host TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN status TEXT NOT NULL DEFAULT 'running'",
            "ALTER TABLE runs ADD COLUMN finished_at TEXT",
            "ALTER TABLE runs ADD COLUMN docker_host TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE runs ADD COLUMN log_text TEXT",
            "ALTER TABLE pending_updates ADD COLUMN included INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pending_updates ADD COLUMN included_at TEXT",
            "ALTER TABLE pending_updates ADD COLUMN images_added INTEGER",
            "ALTER TABLE pending_updates ADD COLUMN tested INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE pending_updates ADD COLUMN tested_at TEXT",
            "ALTER TABLE pending_updates ADD COLUMN release_date TEXT",
            "ALTER TABLE lang_versions ADD COLUMN note TEXT",
            "ALTER TABLE client_fingerprints ADD COLUMN client_output TEXT",
            "ALTER TABLE client_fingerprints ADD COLUMN observed_user_agent TEXT",
            "ALTER TABLE client_fingerprints ADD COLUMN observed_ja3_hash TEXT",
            "ALTER TABLE client_fingerprints ADD COLUMN observed_ja3_string TEXT",
            "ALTER TABLE fw_versions ADD COLUMN available INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE lib_versions ADD COLUMN available INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE http_client_versions ADD COLUMN available INTEGER NOT NULL DEFAULT 1",
        ]:
            try:
                conn.execute(ddl)
            except Exception:
                pass

        # Index needs the ALTER-added `included` column above to already
        # exist, so it can't live in _SCHEMA (executescript runs before this
        # loop, and CREATE INDEX on a not-yet-added column fails outright on
        # any pre-existing database from before this column was added).
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pu_included ON pending_updates(included)")
        except Exception:
            pass

        # runs moved from UNIQUE(name) to UNIQUE(name, docker_host) so the
        # same batch name can be reused against a different Docker host
        # without merging into the other host's run. SQLite can't ALTER a
        # UNIQUE constraint in place, so rebuild when an old-shape table is
        # found (checked here, after the ALTER above guarantees docker_host
        # exists on every row).
        runs_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone()
        if runs_row and "UNIQUE(name, docker_host)" not in runs_row[0]:
            # build_results/test_results hold a run_id FK into runs, so a
            # plain rename+drop leaves their FK text pointing at the
            # dropped intermediate table -- disable FK enforcement for the
            # rebuild, then use a second rename through that same
            # intermediate name to make SQLite rewrite their FK text back
            # to "runs" (its rename-triggered FK-text-rewrite only fires
            # when the CURRENTLY referenced name is renamed away).
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript("""
                ALTER TABLE runs RENAME TO runs_old;
                CREATE TABLE runs_tmp (
                    id          INTEGER PRIMARY KEY,
                    name        TEXT    NOT NULL,
                    created_at  TEXT    DEFAULT CURRENT_TIMESTAMP,
                    status      TEXT    NOT NULL DEFAULT 'running',
                    finished_at TEXT,
                    docker_host TEXT    NOT NULL DEFAULT '',
                    UNIQUE(name, docker_host)
                );
                INSERT INTO runs_tmp (id, name, created_at, status, finished_at, docker_host)
                    SELECT id, name, created_at, status, finished_at, docker_host FROM runs_old;
                DROP TABLE runs_old;
                ALTER TABLE runs_tmp RENAME TO runs_old;
                ALTER TABLE runs_old RENAME TO runs;
            """)
            conn.execute("PRAGMA foreign_keys=ON")

        conn.executescript(_VIEW)
        conn.executescript(_CLIENT_VIEW)


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
               "fw_versions", "libraries", "lib_versions",
               "http_clients", "http_client_versions")}
    counts.update({f"{k}_removed": 0 for k in
                   ("lang_versions", "frameworks", "fw_versions",
                    "libraries", "lib_versions",
                    "http_clients", "http_client_versions")})

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
                        nr        = _norm_version(str(fv.get("nr", "")))
                        rdate     = fv.get("release_date")
                        compat    = json.dumps(fv.get("compatibility", []))
                        available = int(bool(fv.get("available", True)))
                        seen_fw_versions.add(nr)
                        conn.execute(
                            """INSERT INTO fw_versions
                                   (framework_id, version_nr, release_date, compatibility, available)
                               VALUES (?,?,?,?,?)
                               ON CONFLICT(framework_id, version_nr)
                               DO UPDATE SET release_date=excluded.release_date,
                                             compatibility=excluded.compatibility,
                                             available=excluded.available""",
                            (fw_id, nr, rdate, compat, available),
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
                        nr        = _norm_version(str(lv.get("nr", "")))
                        rdate     = lv.get("release_date")
                        compat    = json.dumps(lv.get("compatibility", []))
                        available = int(bool(lv.get("available", True)))
                        seen_lib_versions.add(nr)
                        conn.execute(
                            """INSERT INTO lib_versions
                                   (library_id, version_nr, release_date, compatibility, available)
                               VALUES (?,?,?,?,?)
                               ON CONFLICT(library_id, version_nr)
                               DO UPDATE SET release_date=excluded.release_date,
                                             compatibility=excluded.compatibility,
                                             available=excluded.available""",
                            (lib_id, nr, rdate, compat, available),
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

                # ── HTTP clients (outbound-call fingerprinting) ────────────
                # Same shape as the frameworks loop above, but a separate
                # table -- see the http_clients/client_images schema comment.
                seen_http_clients = set()
                for hc in lang_obj.get("http_clients", []):
                    hc_name = hc.get("name", "")
                    module  = hc.get("module")
                    notes   = hc.get("notes")
                    include = int(hc.get("include", True))
                    seen_http_clients.add(hc_name)

                    conn.execute(
                        """INSERT INTO http_clients
                               (language_id, name, module_path, notes, include)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(language_id, name)
                           DO UPDATE SET module_path=excluded.module_path,
                                         notes=excluded.notes,
                                         include=excluded.include""",
                        (lang_id, hc_name, module, notes, include),
                    )
                    hc_id = conn.execute(
                        "SELECT id FROM http_clients WHERE language_id=? AND name=?",
                        (lang_id, hc_name),
                    ).fetchone()[0]
                    counts["http_clients"] += 1

                    versions = hc.get("version", [])
                    if isinstance(versions, str):
                        versions = [{"nr": versions, "release_date": None,
                                     "compatibility": []}]

                    seen_hc_versions = set()
                    for hv in versions:
                        nr        = _norm_version(str(hv.get("nr", "")))
                        rdate     = hv.get("release_date")
                        compat    = json.dumps(hv.get("compatibility", []))
                        available = int(bool(hv.get("available", True)))
                        seen_hc_versions.add(nr)
                        conn.execute(
                            """INSERT INTO http_client_versions
                                   (http_client_id, version_nr, release_date, compatibility, available)
                               VALUES (?,?,?,?,?)
                               ON CONFLICT(http_client_id, version_nr)
                               DO UPDATE SET release_date=excluded.release_date,
                                             compatibility=excluded.compatibility,
                                             available=excluded.available""",
                            (hc_id, nr, rdate, compat, available),
                        )
                        counts["http_client_versions"] += 1

                    counts["http_client_versions_removed"] += _prune_missing(
                        conn, "http_client_versions", "http_client_id", hc_id,
                        "version_nr", seen_hc_versions,
                    )

                counts["http_clients_removed"] += _prune_missing(
                    conn, "http_clients", "language_id", lang_id,
                    "name", seen_http_clients,
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
    ("java", "BouncyCastle", "1.73", "Kyber (draft)", "draft", "Inherits 1.72's draft support (not independently jar-diffed per release) -- predates the 1.79 final-name rename."),
    ("java", "BouncyCastle", "1.73", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.73."),
    ("java", "BouncyCastle", "1.74", "Kyber (draft)", "draft", "Inherits 1.72's draft support -- predates the 1.79 final-name rename."),
    ("java", "BouncyCastle", "1.74", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.74."),
    ("java", "BouncyCastle", "1.75", "Kyber (draft)", "draft", "Inherits 1.72's draft support -- predates the 1.79 final-name rename."),
    ("java", "BouncyCastle", "1.75", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.75."),
    ("java", "BouncyCastle", "1.76", "Kyber (draft)", "draft", "Inherits 1.72's draft support -- predates the 1.79 final-name rename."),
    ("java", "BouncyCastle", "1.76", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.76."),
    ("java", "BouncyCastle", "1.77", "Kyber (draft)", "draft", "Inherits 1.72's draft support -- predates the 1.79 final-name rename."),
    ("java", "BouncyCastle", "1.77", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.77."),
    ("java", "BouncyCastle", "1.78", "Kyber (draft)", "draft", "Inherits 1.72's draft support -- last minor before the 1.79 final-name rename (confirmed via jar diff: 1.78 still lacks MLKEMParameterSpec/etc. entirely)."),
    ("java", "BouncyCastle", "1.78", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 1.78."),
    ("java", "BouncyCastle", "1.79", "ML-KEM", "final",
     "Verified by diffing actual jar contents: 1.78 lacks MLKEMParameterSpec/etc. entirely, 1.79 introduces the full provider implementation (FIPS 203 final names)."),
    ("java", "BouncyCastle", "1.79", "ML-DSA", "final",
     "Same verification as ML-KEM at 1.79 -- jar diff (MLDSAParameterSpec)."),
    ("java", "BouncyCastle", "1.79", "SLH-DSA", "final",
     "Same verification as ML-KEM at 1.79 -- jar diff (SLHDSAParameterSpec)."),
    ("java", "BouncyCastle", "1.80", "ML-KEM", "final", "Inherits 1.79's final-name support (not independently jar-diffed per release)."),
    ("java", "BouncyCastle", "1.80", "ML-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.80."),
    ("java", "BouncyCastle", "1.80", "SLH-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.80."),
    ("java", "BouncyCastle", "1.81", "ML-KEM", "final", "Inherits 1.79's final-name support."),
    ("java", "BouncyCastle", "1.81", "ML-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.81."),
    ("java", "BouncyCastle", "1.81", "SLH-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.81."),
    ("java", "BouncyCastle", "1.82", "ML-KEM", "final", "Inherits 1.79's final-name support."),
    ("java", "BouncyCastle", "1.82", "ML-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.82."),
    ("java", "BouncyCastle", "1.82", "SLH-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.82."),
    ("java", "BouncyCastle", "1.83", "ML-KEM", "final", "Inherits 1.79's final-name support."),
    ("java", "BouncyCastle", "1.83", "ML-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.83."),
    ("java", "BouncyCastle", "1.83", "SLH-DSA", "final", "Same inheritance reasoning as ML-KEM at 1.83."),
    ("java", "BouncyCastle", "1.84", "ML-KEM", "final",
     "Current latest tracked minor (replaces the old rolling-latest '1' bucket, dropped once every real minor was tracked individually); inherits 1.79's final-name support."),
    ("java", "BouncyCastle", "1.84", "ML-DSA", "final", "Same reasoning as ML-KEM at 1.84."),
    ("java", "BouncyCastle", "1.84", "SLH-DSA", "final", "Same reasoning as ML-KEM at 1.84."),
    ("java", "BouncyCastle", "1.84", "Kyber (draft)", "deprecated",
     "BC 1.84's own release notes announce removal of the draft Kyber/Dilithium/SphincsPlus wrapper names in a future release -- still present in 1.84 itself, but on the way out."),
    ("java", "BouncyCastle", "1.84", "Dilithium (draft)", "deprecated",
     "Same sunsetting note as Kyber (draft) at 1.84."),
    ("java", "Tink", "1.21", "ML-DSA", "final",
     "v1.21.0 (2026-03-24) added ML-DSA-87 signature support -- Tink's first post-quantum release of any kind. Verified via full-text search of every Tink Java GitHub release body (v1.8.0-v1.20.0 have zero PQC mentions)."),
    ("java", "Tink", "1.22", "ML-DSA", "final",
     "Current latest tracked minor (replaces the old rolling-latest '1' bucket, dropped once every real minor was tracked individually); adds ML-DSA-44 on top of 1.21's ML-DSA-87."),
    ("java", "Tink", "1.22", "SLH-DSA", "final",
     "v1.22.0 (2026-06-18) added SLH-DSA predefined signature parameters. Tink still has NO KEM-side PQC (no ML-KEM/Kyber) as of this version -- signatures only."),
    ("dotnet", "BouncyCastle.Cryptography", "2.0", "Kyber (draft)", "draft",
     "BouncyCastle.Cryptography 2.0.0 (2022-11-15), the official bcgit project's first NuGet release, shipped with draft/pre-standard Kyber support from day one -- explicitly flagged EXPERIMENTAL in the package description. Unlike bc-java, bc-csharp's draft-PQC and final-PQC milestones do NOT land on the same version numbers as their Java counterpart (1.72/1.79) despite being the same project family."),
    ("dotnet", "BouncyCastle.Cryptography", "2.0", "Dilithium (draft)", "draft",
     "Same release (2.0.0, 2022-11-15) and sourcing as the Kyber (draft) entry."),
    ("dotnet", "BouncyCastle.Cryptography", "2.1", "Kyber (draft)", "draft",
     "Inherits 2.0's draft support (not independently diffed minor-by-minor) -- 2.1.0 (2023-02-18) predates the 2.5.0 final-name rename."),
    ("dotnet", "BouncyCastle.Cryptography", "2.1", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 2.1."),
    ("dotnet", "BouncyCastle.Cryptography", "2.2", "Kyber (draft)", "draft",
     "Inherits 2.0's draft support -- 2.2.0 (2023-04-17) predates the 2.5.0 final-name rename."),
    ("dotnet", "BouncyCastle.Cryptography", "2.2", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 2.2."),
    ("dotnet", "BouncyCastle.Cryptography", "2.3", "Kyber (draft)", "draft",
     "Inherits 2.0's draft support -- 2.3.0 (2024-02-05) predates the 2.5.0 final-name rename."),
    ("dotnet", "BouncyCastle.Cryptography", "2.3", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 2.3."),
    ("dotnet", "BouncyCastle.Cryptography", "2.4", "Kyber (draft)", "draft",
     "Inherits 2.0's draft support -- 2.4.0 (2024-05-27) predates the 2.5.0 final-name rename."),
    ("dotnet", "BouncyCastle.Cryptography", "2.4", "Dilithium (draft)", "draft", "Same inheritance reasoning as Kyber (draft) at 2.4."),
    ("dotnet", "BouncyCastle.Cryptography", "2.5", "ML-KEM", "final",
     "2.5.0 (2024-12-01, per the NuGet registration API's authoritative 'published' field) added the final NIST-standardized ML-KEM (FIPS 203) -- and, unlike bc-java (which kept draft Kyber alongside ML-KEM for years), bc-csharp REMOVED draft Kyber support in this same release."),
    ("dotnet", "BouncyCastle.Cryptography", "2.5", "ML-DSA", "final",
     "Same release (2.5.0, 2024-12-01) added final ML-DSA (FIPS 204), replacing draft Dilithium, which was removed in the same release."),
    ("dotnet", "BouncyCastle.Cryptography", "2.5", "SLH-DSA", "final",
     "Same release (2.5.0, 2024-12-01) added final SLH-DSA (FIPS 205)."),
    ("dotnet", "BouncyCastle.Cryptography", "2.6", "ML-KEM", "final",
     "2.6.0 (2025-05-15) inherits 2.5.0's final-name support; currently the latest tracked minor (2.6.2)."),
    ("dotnet", "BouncyCastle.Cryptography", "2.6", "ML-DSA", "final", "Same inheritance reasoning as ML-KEM at 2.6."),
    ("dotnet", "BouncyCastle.Cryptography", "2.6", "SLH-DSA", "final", "Same inheritance reasoning as ML-KEM at 2.6."),
    ("dotnet", "System.Security.Cryptography.PQC", "builtin", "ML-KEM", "final",
     "Inbox in System.Security.Cryptography.dll starting .NET 10 (GA 2025-11-11) -- confirmed via dotnet/runtime GitHub issue #114453 (milestone 10.0.0, api-approved). The [Experimental] gate present during the .NET 9 dev cycle was REMOVED for ML-KEM in .NET 10, i.e. fully supported, not preview. Backed by MLKemOpenSsl on Linux (needs OpenSSL 3.5+) or MLKemCng on Windows -- see the matching platform_constraints entry."),
    ("dotnet", "System.Security.Cryptography.PQC", "builtin", "ML-DSA", "final",
     "Same .NET 10 GA / [Experimental]-removed status as ML-KEM."),
    ("dotnet", "System.Security.Cryptography.PQC", "builtin", "SLH-DSA", "draft",
     "Present inbox since .NET 10 but STILL marked [Experimental(\"SYSLIB5006\")] as of this writing, unlike ML-KEM/ML-DSA -- Microsoft's own docs attribute this to limited OS support. Recorded as 'draft' here (not 'final') to reflect that experimental-API gate, even though the algorithm itself is the final FIPS 205 standard."),
    ("php", "php-liboqs", "0.4", "ML-KEM", "final",
     "Wraps liboqs 0.15.0 directly (this project's pinned build) -- confirmed by an actual docker run: \\OQS\\KEM::keypair(\\OQS\\KEM::ALG_ML_KEM_768) genuinely returns a real keypair. Unlike BouncyCastle/NSec, php-liboqs has no separate draft-Kyber naming period of its own; it's a thin wrapper directly over whatever names liboqs itself uses, and liboqs has used the final FIPS 203 name since well before 0.15.0."),
    ("php", "php-liboqs", "0.4", "ML-DSA", "final",
     "Same reasoning as ML-KEM -- liboqs 0.15.0 already uses the final FIPS 204 name, exposed via \\OQS\\Signature."),
    ("php", "php-liboqs", "0.4", "SLH-DSA", "final",
     "Same reasoning as ML-KEM -- liboqs 0.15.0 already uses the final FIPS 205 name."),
    ("node", "liboqs-node", "0.1", "Kyber (draft)", "draft",
     "Unlike every other liboqs binding in this project (php-liboqs, LibOQS.NET, liboqs-go), this package's own vendored/pinned liboqs git submodule commit (~2021) predates the FIPS 203 final-name rename -- confirmed live via oqs.KEMs.getEnabledAlgorithms(), which lists 'Kyber512/768/1024' and has no 'ML-KEM-*' entries at all. The touch code must use the draft name for this specific library; every other language's liboqs binding in this project already exposes final NIST names."),
    ("python", "liboqs-python", "0.15", "ML-KEM", "final",
     "The official Open Quantum Safe project's own Python binding, paired with liboqs 0.15.0 in this project's Dockerfile -- confirmed live: oqs.KeyEncapsulation('ML-KEM-768').generate_keypair() returns a real 1184-byte public key. Like php-liboqs and unlike Node's liboqs-node, this binding has no draft-naming period of its own to track -- it's a thin ctypes wrapper directly over whatever names the paired liboqs C release uses, and 0.15.0 already uses final NIST names."),
    ("python", "liboqs-python", "0.15", "ML-DSA", "final", "Same reasoning as ML-KEM -- liboqs 0.15.0 already uses the final FIPS 204 name, exposed via oqs.Signature."),
    ("python", "liboqs-python", "0.15", "SLH-DSA", "final", "Same reasoning as ML-KEM -- liboqs 0.15.0 already uses the final FIPS 205 name."),
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
    ("java", "Tink", None, "SLH-DSA", None, "1.22",
     "v1.22.0 added SLH-DSA predefined signature parameters, again with no prior draft stage within Tink."),
    ("dotnet", "BouncyCastle.Cryptography", "Kyber (draft)", "ML-KEM", "2.0", "2.5",
     "bc-csharp's draft-to-final PQC rename landed at BouncyCastle.Cryptography 2.5.0 (2024-12-04) -- a SHARPER break than bc-java's equivalent transition (1.72->1.79): bc-java kept draft Kyber/Dilithium names side-by-side for years after 1.79, bc-csharp removed them in the SAME release that added the final names."),
    ("dotnet", "BouncyCastle.Cryptography", "Dilithium (draft)", "ML-DSA", "2.0", "2.5",
     "Same timeline and sourcing as the Kyber (draft) -> ML-KEM path; draft Dilithium was likewise removed in 2.5.0, not just deprecated."),
]

# (language, library, version_nr, constraint_type, description)
_LIB_PLATFORM_CONSTRAINTS = [
    ("java", "Conscrypt", "1.4", "architecture",
     "Resolves to the highest published 1.x release (1.4.2, ~2019) -- this bucket replaces the old rolling-latest '1' bucket now that every 1.x/2.x minor is tracked individually. Neither this nor the current stable 2.x line bundles ARM64/aarch64 native libraries -- confirmed by inspecting the actual jar contents directly. Fails to load its native security provider on an arm64 host (Apple Silicon Mac, ARM Docker Desktop/CI). Only the still-prerelease 2.6-alpha5 adds ARM64 natives."),
    ("java", "Conscrypt", "2.5", "architecture",
     "Resolves to the latest stable release (2.5.2) -- this bucket replaces the old rolling-latest '2' bucket. Same ARM64 gap as the '1.4' bucket -- confirmed by inspecting jar contents directly; only the still-prerelease 2.6-alpha5 has ARM64 natives."),
    ("java", "Conscrypt", "1.4", "glibc",
     "Bundled native code is glibc-linked per standard OpenJDK toolchain convention -- this project pairs Conscrypt only with eclipse-temurin's Ubuntu/glibc ('-jammy'/'-noble') tags, never Alpine/musl, to avoid a suspected (not yet empirically confirmed) UnsatisfiedLinkError."),
    ("java", "Conscrypt", "2.5", "glibc",
     "Same glibc-linkage reasoning as the '1.4' bucket."),
    ("node", "node-forge", "1.0", "runtime_engine",
     "lib/log.js unconditionally calls the global URLSearchParams constructor at require-time (whenever `console` exists, which is always true in Node) -- URLSearchParams only became a global in Node v10.0.0. Crashes with ReferenceError on Node <10 regardless of app code. Fixed upstream by node-forge 1.4.0 (the block is properly gated behind a `typeof window` check there)."),
    ("node", "crypto-js", "4", "runtime_engine",
     "Cipher files (e.g. blowfish.js) use bare let/const with no \"use strict\" pragma -- Node 4's V8 (4.5) only allowed block-scoped declarations in strict-mode code, throwing SyntaxError on Node <6. crypto-js 3.x is plain ES5 and works fine down to Node 4."),
    ("python", "cryptography", "2.0", "toolchain",
     "Python 3.12+ slim images ship no setuptools, and cryptography 2.0 has no abi3/py312 wheel -- source build fails without installing setuptools first. Capped at Python 3.11 in the registry."),
    ("python", "M2Crypto", "0.26", "compiler",
     "SWIG-generated code uses deprecated Python C API (e.g. PyEval_InitThreads) removed in Python 3.12; that Python version's slim image also lacks setuptools needed as a build backend. Capped at Python 3.11 for this bucket."),
    ("dotnet", "System.Security.Cryptography.PQC", "builtin", "native_dependency",
     "ML-KEM/ML-DSA are backed by MLKemOpenSsl/MLDsaOpenSsl on Linux, requiring OpenSSL 3.5+ at the OS level (MLKemCng/MLDsaCng on Windows instead). .NET 10's own default Linux base image moved to Ubuntu 'noble' (24.04), which ships OpenSSL 3.0.13 -- NOT 3.5+ -- and this project's generated Dockerfiles do not add any step to upgrade it. MLKem.IsSupported / MLDsa.IsSupported will most likely evaluate to false inside this project's own generated .NET 10 containers until the base image itself ships OpenSSL 3.5+; the touch/exercise code checks IsSupported defensively so builds and startup still succeed regardless."),
    ("dotnet", "NSec.Cryptography", "26", "native_dependency",
     "Depends on the separate 'libsodium' NuGet package (constrained >=1.0.22,<1.0.23), which ships prebuilt native binaries per runtime identifier -- 'dotnet restore'/'publish' pulls the native libsodium binary automatically, no apt-get or system package install needed in the container. Included here as a positive/informational finding, not a blocking constraint -- contrast with Conscrypt (Java) or sodium-native (Node), which both need real apt-get-installed toolchains or hit real platform gaps."),
    ("dotnet", "LibOQS.NET", "0.3", "maturity",
     "Community-maintained (github.com/filipw/maybe-liboqs-dotnet), NOT the official (now-archived/discontinued as of 2025-01-06) open-quantum-safe/liboqs-dotnet project despite the identical package name and similar version numbering -- verify which project any external reference to 'liboqs-dotnet'/'LibOQS.NET' means before reusing a claim about it. Young (~2K downloads at last check), pre-1.0, single-maintainer; included for PQC-research breadth (full liboqs algorithm surface: ML-KEM, ML-DSA, SLH-DSA, Falcon) rather than production maturity."),
    ("dotnet", "LibOQS.NET", "0.3", "native_dependency",
     "TWO real runtime bugs found via an actual docker run (a user hit a live DllNotFoundException), both fixed and re-verified end-to-end: (1) a framework-dependent 'dotnet publish' with no RuntimeIdentifier never copies the nupkg's real runtimes/linux-x64/native/liboqs.so into the publish output at all -- fixed with explicit RuntimeIdentifier=linux-x64 + SelfContained=false in the csproj. (2) the prebuilt liboqs.so needs glibc >=2.34 ('ldd' inside the container showed GLIBC_2.34' not found) -- Debian bullseye, the default base for .NET 6.0/7.0 images, ships only glibc 2.31. Fixed by switching to the '-bookworm-slim' image tag suffix (glibc 2.36+) specifically for this library on 6.0/7.0 only; .NET 8.0/9.0 (bookworm by default) and 10.0 (Ubuntu noble) were unaffected and keep their default tags. Confirmed working after both fixes on .NET 6.0, 7.0, 8.0, and 10.0 -- curled both endpoints on a live container, liboqs actually initialized."),
    ("php", "php-liboqs", "0.4", "native_dependency",
     "Requires the liboqs C library built from source in the same Dockerfile stage (git clone + cmake + ninja, mirroring this project's Go liboqs recipe) -- no prebuilt binary distribution. Its own README claims a liboqs '0.14.0 or newer' floor, but 0.14.0 is actually missing OQS_KEM_encaps_derand -- confirmed both by grepping the real tagged src/kem/kem.h (absent at 0.14.0, present at 0.15.0) and by an actual docker build compile failure ('implicit declaration of function'). This project pins liboqs 0.15.0, the latest real stable tag (0.16.0 only exists as an -rc1 prerelease as of this writing)."),
    ("php", "php-liboqs", "0.4", "toolchain",
     "Packagist lists secudoc/php-liboqs as `type: php-ext` (a `replace: {ext-oqs: '*'}` marker package with no PHP source to autoload) -- a plain `composer install` cannot build it at all, confirmed via an actual docker build failure ('these were not loaded, likely because it conflicts with another require'). The real .so is built directly via phpize + make install in the Dockerfile and deliberately left OUT of composer.json's require entirely; only its Packagist version number is resolved for /version reporting."),
    ("php", "php-liboqs", "0.4", "maturity",
     "Single young community project wrapping liboqs -- included for PQC-research breadth (full liboqs algorithm surface exposed via \\OQS\\KEM / \\OQS\\Signature) rather than production maturity, the same inclusion rationale as .NET's LibOQS.NET."),
    ("php", "phpseclib", "1", "toolchain",
     "phpseclib 1.0.30 (the only phpseclib 1.x release still installable at all) is flagged by a real Packagist security advisory (PKSA-mnsd-qtjt-pgcq) -- Composer 2.4+ blocks installing any advisory-flagged package by default, confirmed via an actual docker build failure. This project deliberately builds it anyway (studying an old/vulnerable crypto library version is the actual research goal) via the composer install --no-security-blocking flag -- a flag that does NOT exist on the 'composer:2.2' LTS tag used for pre-7.2 PHP (confirmed: hard 'option does not exist' error there), only on the plain 'composer:2' tag used for PHP 7.2+."),
    ("node", "liboqs-node", "0.1", "native_dependency",
     "The npm-published tarball is missing its own git submodules (deps/liboqs, deps/liboqs-cpp) -- npm doesn't capture submodule content -- and no prebuilt binary exists for modern Node ABIs either; a plain `npm install liboqs-node` fails outright, confirmed via an actual docker build. Fixed by git-cloning the repo directly with --recurse-submodules instead of depending on the npm registry tarball at all (the same 'clone from source' pattern as PHP's php-liboqs). Separately, the vendored liboqs commit (~2021) fails to compile under GCC 12+ (this project's node:*-slim base): its old SIKE implementation trips -Werror=array-parameter/stringop-overflow, warning classes added to GCC after that commit was written -- fixed by stripping -Werror from the vendored liboqs' own CMake files before building (SIKE itself was cryptographically broken and removed from liboqs entirely in 2022, unrelated to this compiler-strictness mismatch)."),
    ("node", "liboqs-node", "0.1", "maturity",
     "Community-maintained (TapuCosmo), explicitly marked EXPERIMENTAL by its own README ('do not use in production'). Only 4 patch releases ever published (0.0.1-0.1.0), all under a single major. Included for PQC-research breadth (full liboqs algorithm surface via KeyEncapsulation/Signature) rather than production maturity, the same inclusion rationale as .NET's LibOQS.NET and PHP's php-liboqs."),
    ("node", "bcrypt", "0", "toolchain",
     "Native node-gyp binding (same toolchain class as sodium-native) -- prebuilt binaries exist for most current Node-ABI/major combinations, falling back to a source compile (python3/make/g++) otherwise. Confirmed working via bcrypt.hashSync() on node:22-slim."),
    ("python", "liboqs-python", "0.15", "native_dependency",
     "A ctypes wrapper -- no C-extension build of the Python package itself, but the liboqs C library must be built from source and installed system-wide in the same Dockerfile stage (git clone + cmake + ninja, the same recipe already reused across every other language's liboqs binding in this project), with LD_LIBRARY_PATH pointed at it so the ctypes dlopen() call can find the resulting shared library at runtime. Confirmed working end-to-end via a real docker build+run."),
    ("python", "Tornado", "4", "runtime_engine",
     "Genuinely breaks on Python 3.10+, not an arbitrary cap: httputil.py subclasses collections.MutableMapping, an alias Python removed from the collections module itself (moved to collections.abc) in 3.10 -- confirmed via a real docker build (works on 3.9, AttributeError on 3.10). Major 5 has the exact same issue and the exact same fix boundary; only major 6 (2019) fixed it properly and has no upper bound."),
    ("python", "Tornado", "5", "runtime_engine",
     "Same collections.MutableMapping removal as Tornado 4 -- independently confirmed via a real docker build (works on 3.9, breaks on 3.10)."),
    ("python", "Tornado", "1", "runtime_engine",
     "Genuinely Python-2-only, not just untested on Python 3 -- confirmed via a real docker build: tornado/web.py uses Python-2-only tuple-parameter-unpacking lambda syntax (`lambda (l, s): ...`), a hard SyntaxError on ANY Python 3.x regardless of minor version. Major 2 (2014) has the identical issue."),
    ("python", "aiohttp", "1", "toolchain",
     "Leaves async-timeout completely unversioned in its own setup.py -- pip resolves it to async-timeout's latest release (4.0.2), which needs a newer Python than this major targets, causing a real TypeError at import time ('function() argument 1 must be code, not str', a stale/incompatible-bytecode symptom of the version mismatch). Confirmed via a real docker build; fixed with an explicit pin to async-timeout==3.0.1 (contemporaneous with this era). Also genuinely only imports successfully on Python 3.6 exactly -- verified directly on 3.5/3.6/3.7, only 3.6 works (helpers.py uses asyncio.async, a SyntaxError once async became a reserved keyword in Python 3.7)."),
    ("python", "aiohttp", "2", "toolchain",
     "Same unversioned async-timeout dependency and the same fix as aiohttp major 1 -- confirmed independently via a real docker build."),
    ("python", "CherryPy", "3", "runtime_engine",
     "Genuinely breaks on Python 3.8+, not an arbitrary cap: _cperror.py does `from cgi import escape`, removed from the cgi module in Python 3.8 (deprecated since 3.2) -- confirmed via a real docker build (works on 3.7, ImportError on 3.8)."),
]

# (language, framework, version_nr, constraint_type, description)
_FW_PLATFORM_CONSTRAINTS = [
    ("node", "Fastify", "1", "toolchain",
     "Dependency tree deterministically fails npm install on Node 6 with ENOTDIR on a .staging/@types/... path -- a known npm@3 (bundled with Node 6) race/bug with scoped packages. Reproduced directly: failed 5/5 tries on node:6-slim, succeeded 2/2 on node:8-slim. Fixed upstream by npm5 (bundled from Node 8 onward). Fastify 2.x's different dependency tree doesn't trigger it."),
    ("node", "Koa", "2", "runtime_engine",
     "koa's own dependency http-errors uses object destructuring (const { HttpError } = require(...)) that Node 6's V8 can't parse -- require('koa') itself throws SyntaxError: Unexpected token { on Node 6, loads cleanly from Node 8."),
    ("php", "Laravel", "4", "toolchain",
     "Pulls in kylekatarnls/update-helper (a transitive Carbon 1.x dependency) which ships a Composer plugin -- Composer 2.2+ blocks any third-party plugin by default unless allow-listed, confirmed via an actual docker build failure (PluginBlockedException). Fixed by setting config.allow-plugins to true in every generated PHP app's composer.json -- a blanket, build-time-only trust decision, consistent with this project already disabling the separate security-advisory block (see phpseclib '1') for the same deliberately-old-dependency-tree reason."),
    ("php", "Slim", "4", "toolchain",
     "slim/slim 4.x externalizes its PSR-7 implementation into a separate package (any of slim/psr7, nyholm/psr7, guzzlehttp/psr7 -- a full skeleton picks one via `composer create-project`); requiring bare slim/slim alone throws at runtime ('Could not detect any PSR-17 ResponseFactory implementation'), confirmed via an actual docker run. Fixed by adding slim/psr7 to composer.json's require whenever the framework major is Slim 4. Slim 1/2/3 don't need this -- each ships its own bundled response implementation."),
    ("php", "Slim", "1", "toolchain",
     "Slim 1.x's app class is the bare GLOBAL `Slim` (no namespace at all) -- confirmed the hard way: an actual docker run of a build using the namespaced `\\Slim\\Slim()` (Slim 2's real shape) threw \"Class 'Slim\\Slim' not found\", and inspecting vendor/slim/slim/Slim/Slim.php directly showed `class Slim {` with no `namespace` declaration. Slim 1.x also has no PSR-7-ish Response::headers bag available before a route is dispatched -- `$app->response()` returns null at that point; the correct API is the app's own `$app->contentType(...)` method instead."),
    ("node", "NestJS", "11", "toolchain",
     "Nest's decorator-based DI reflection depends on TypeScript's own emitDecoratorMetadata emission (design:paramtypes) -- plain V8/Node runtime decorators (the stage-3 proposal, --experimental-decorators as a *runtime* flag) do not produce that metadata, confirmed via an actual docker build: a hand-written plain-.js version either fails to parse on some Node majors or never wires up DI. Fixed by adding a real TypeScript compile step (tsc, no outDir -- an outDir subdirectory breaks this project's shared __dirname-relative node_modules version-lookup helper, confirmed via a live 'unknown' framework-version report) to the Dockerfile before running node. This is a materially different Dockerfile shape from every other Node framework here, but per this project's standing rule, needing different tooling is never on its own a reason to exclude an otherwise-real, buildable framework."),
    ("node", "AdonisJS", "6", "toolchain",
     "AdonisJS has no documented minimal/standalone bootstrap API (unlike NestJS's NestFactory.create()) -- confirmed by building it inline first (throws deep IoC-container errors with no application root present). Fixed by scaffolding AdonisJS's own official minimal starter (`npx create-adonisjs --kit=api`) directly inside the Dockerfile, then overwriting start/routes.ts with the project's 2 standard routes -- a real, build-verified pattern, not a reason to exclude the framework. AdonisJS's own module system is native ESM ('type': 'module') -- a plain require() throws 'require is not defined' at runtime (confirmed via a real docker run); the generated routes file bridges via Node's own createRequire(import.meta.url) instead, which accepts the exact same targets every other (CommonJS) framework's library touch-code uses, including liboqs-node's absolute git-clone path."),
    ("node", "Restify", "11", "runtime_engine",
     "Restify's route handlers on the currently-tracked majors must be declared `async` -- a plain sync handler throws a hard AssertionError (`actual == expected`) at route-registration time, confirmed via a real docker run. Older majors' exact handler-signature requirement was not independently re-tested per-major in this pass."),
    ("go", "Echo", "1", "runtime_engine",
     "Echo v1.x's API predates v3/v4's now-stable shape entirely: route methods are `Get`/`Post` (not `GET`/`POST`), the server starts via `Run(addr string)` (not `Start`), and Context is a struct (`*echo.Context`, not the `echo.Context` interface v2+ use) -- confirmed via the real v1.4.4 source and a live docker build+run. v1.x's own go.mod also leaves github.com/labstack/gommon completely unversioned, which `go build -mod=mod` resolves to gommon's latest release (needing go >=1.23) regardless of the target Go toolchain -- fixed with an explicit extra `require` pin (`_FW_EXTRA_REQS` in lang_go.py) resolved the same max_toolchain-aware way as Iris's existing fasthttp pin."),
    ("go", "Echo", "2", "runtime_engine",
     "A transitional API era, confirmed via real source inspection (not assumed to match v1 or v3): route methods are already `GET`/`POST` and Context is already the interface v3+/v4 use, but the server starts via `Run(engine.Server)` (constructed through the separate `echo/engine/standard` sub-package, e.g. `e.Run(standard.New(\":8000\"))`), not a bare address string -- that arrived only at v3.0.0, whose shape already matches v4 exactly (confirmed live), so v3 needed no template of its own. Same gommon-pin fix as Echo v1 applies here too."),
    ("go", "Fiber", "1", "runtime_engine",
     "Fiber v1.x's route handler type is `func(*fiber.Ctx)` with no return value -- a v2+-shaped `func(*fiber.Ctx) error` handler is a straight compile error ('cannot use func... as func(*fiber.Ctx) value'), confirmed via a real failing docker build. `ctx.JSON()` itself is unchanged (still returns an error in v1, same as v2+) -- only the handler signature registered with `app.Get()` differs."),
    ("go", "Chi", "1", "toolchain",
     "v1-v4 use the bare import path `github.com/go-chi/chi` (no /vN suffix) and, for v2/v3/v4, have NO go.mod of their own at all -- resolvable only via the Go module proxy's `+incompatible` pseudo-version mechanism. Confirmed working end-to-end via a real docker build (`go get github.com/go-chi/chi@v4.1.2` inside a plain Go-1.21 modules-mode build stage) with zero GOPATH-mode machinery needed, despite `_INCOMPATIBLE_FW`'s existing GOPATH-mode infrastructure (built for Gin/Gorilla/Beego/Iris/httprouter's own pre-modules majors) initially looking like the obvious tool for this job -- the `+incompatible` proxy mechanism is the simpler, already-standard-Go-toolchain-supported path and needed no new code beyond fixing the per-major import-path dispatch."),
    ("java", "Javalin", "7", "toolchain",
     "Javalin 3.x-6.x's `Javalin` class directly implements the routing interface (`class Javalin implements JavalinDefaultRoutingApi<Javalin>` in 6.0.0, confirmed via real source/javap inspection), so `app.get(path, handler)` works immediately after `Javalin.create()`. 7.x REMOVED that direct implementation -- confirmed the hard way via a real failing docker build: an assumed `app.get(...)` call throws 'cannot find symbol'. Routes must now be registered inside the `Javalin.create(cfg -> cfg.routes.get(...))` config consumer instead. Also, unlike some earlier lines, 7.x does NOT bundle a JSON object mapper -- `ctx.json(...)` throws a real HTTP 500 at REQUEST time ('It looks like you don't have an object mapper configured'), not build time, unless `jackson-databind` is added as an explicit separate Maven dependency -- confirmed via a real docker run, the same 'passing build proves nothing about runtime' lesson already documented repeatedly for .NET (LibOQS.NET/NancyFx/ServiceStack) in this project."),
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
    ("php", "5.6", "os_base_image",
     "php:5.6-cli's Debian base is Stretch, confirmed dropped from live deb.debian.org/security.debian.org mirrors via a direct apt-get update 404. Fixed via the same archive.debian.org sources.list redirect + -o Acquire::Check-Valid-Until=false + --allow-unauthenticated pattern already established for Go/Node's own Jessie/Stretch-era images. php:5.3/5.4 are schema-1 manifests modern Docker refuses to pull at all; php:5.5's manifest is valid but its layer blob is missing/corrupted on the registry -- 5.6 is the practical floor."),
    ("php", "7.0", "os_base_image",
     "Same Stretch base and archive.debian.org fix as PHP 5.6."),
    ("php", "7.1", "os_base_image",
     "php:7.1-cli's Debian base is Buster, also confirmed dropped from live mirrors (direct 404 on buster/buster-updates/buster-security) -- same archive.debian.org redirect fix as 5.6/7.0's Stretch base."),
    ("php", "7.2", "os_base_image",
     "Same Buster base and archive.debian.org fix as PHP 7.1."),
    ("php", "5.6", "toolchain",
     "Packagist has fully sunset its Composer-1.x-compatible metadata protocol -- confirmed via an actual docker build: composer:1's `composer install` fails to resolve ANY package ('could not be found in any version'), not a PHP-version-specific failure. Composer 2.3+ itself requires PHP >=7.2.5, so PHP <7.2 (5.6/7.0/7.1) uses the 'composer:2.2' LTS tag instead of 'composer:1' -- Composer's own 2.3+ binary suggests this exact fallback when run under old PHP ('please upgrade PHP or use Composer 2.2 LTS via composer self-update --2.2'), and 2.2 still speaks the modern Packagist API. Confirmed working via an actual docker build copying composer:2.2's binary into php:5.6-cli and running `composer --version` successfully."),
    ("php", "7.0", "toolchain",
     "Same composer:2.2 LTS requirement as PHP 5.6."),
    ("php", "7.1", "toolchain",
     "Same composer:2.2 LTS requirement as PHP 5.6/7.0."),
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


# ── Client image sync ─────────────────────────────────────────────────────────
# Mirrors the server-side sync_images() above, but over images_clients/ and
# the http_clients/http_client_versions/client_images tables. Layout is a
# flat language/lang_ver/http_client/http_client_ver (no library-depth
# ambiguity like crypto/des -- HTTP client names here don't contain '/').

def _client_image_tag_from_parts(language: str, lang_ver: str,
                                  http_client: str, hc_ver: str) -> str:
    hc = http_client.lower().replace("/", "_").replace("@", "").replace(" ", "").replace(".", "-")
    return f"pqc-client-{language}-{lang_ver}-{hc}-{hc_ver}"


def _resolve_client_image_fks(conn, language: str, lang_ver: str,
                               http_client: str, hc_ver: str):
    """Look up (lang_version_id, http_client_version_id) or return None."""
    lv = conn.execute(
        """SELECT lv.id FROM lang_versions lv
           JOIN languages l ON l.id = lv.language_id
           WHERE l.name=? AND lv.version_nr=?""",
        (language, lang_ver),
    ).fetchone()
    if not lv:
        return None

    hcv = conn.execute(
        """SELECT hcv.id FROM http_client_versions hcv
           JOIN http_clients hc ON hc.id = hcv.http_client_id
           JOIN languages    l  ON l.id  = hc.language_id
           WHERE l.name=? AND hc.name=? AND hcv.version_nr=?""",
        (language, http_client, hc_ver),
    ).fetchone()
    if not hcv:
        return None

    return lv[0], hcv[0]


def _parse_client_dockerfile_path(base: Path, dockerfile: Path) -> dict | None:
    """Parse a client Dockerfile path: <language>/<lang_ver>/<http_client>/<hc_ver>/Dockerfile."""
    rel   = dockerfile.parent.relative_to(base)
    parts = rel.parts
    if len(parts) != 4:
        return None
    language, lang_ver, http_client, hc_ver = parts
    return {
        "language": language, "lang_ver": lang_ver,
        "http_client": http_client, "hc_ver": hc_ver,
        "path": str(rel),
    }


def sync_client_images() -> tuple[int, int, int]:
    """Walk images_clients/ and upsert all Dockerfile contexts into the
    client_images table. Returns (total_on_disk, newly_inserted, removed)."""
    now = datetime.now(timezone.utc).isoformat()

    with _connect() as conn:
        existing_tags = {r[0]: r[1] for r in
                         conn.execute("SELECT image_tag, id FROM client_images")}

        disk_tags: dict[str, Path] = {}
        inserted  = 0
        total     = 0

        for dockerfile in sorted(CLIENT_IMAGES_BASE.rglob("Dockerfile")):
            total += 1
            cand = _parse_client_dockerfile_path(CLIENT_IMAGES_BASE, dockerfile)
            if cand is None:
                continue

            tag = _client_image_tag_from_parts(
                cand["language"], cand["lang_ver"], cand["http_client"], cand["hc_ver"])
            disk_tags[tag] = dockerfile

            if tag in existing_tags:
                continue

            ids = _resolve_client_image_fks(
                conn, cand["language"], cand["lang_ver"], cand["http_client"], cand["hc_ver"])
            if ids is None:
                continue

            lv_id, hcv_id = ids
            conn.execute(
                """INSERT INTO client_images
                       (lang_version_id, http_client_version_id, image_tag, context_path, synced_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(lang_version_id, http_client_version_id)
                   DO UPDATE SET image_tag=excluded.image_tag,
                                 context_path=excluded.context_path,
                                 synced_at=excluded.synced_at""",
                (lv_id, hcv_id, tag, cand["path"], now),
            )
            inserted += 1
            existing_tags[tag] = None

        gone_tags = set(existing_tags.keys()) - set(disk_tags.keys())
        removed   = 0
        if gone_tags:
            ph = ",".join("?" * len(gone_tags))
            conn.execute(
                f"DELETE FROM client_images WHERE image_tag IN ({ph})", list(gone_tags)
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


def _build_where(filters: dict, prefix: str = "", exclude: frozenset = frozenset()) -> tuple[str, list]:
    """exclude: filter keys to skip -- for callers whose base query doesn't
    expose every column _DETAIL_FILTER_MAP assumes. Notably "run" maps to
    "build_run", a column that only exists in _status_sql()'s superset
    (used by get_images()/get_pending_images()), NOT in the bare
    image_details view get_test_reports()/get_build_reports() query --
    those two already handle "run" themselves via their own properly-scoped
    `r.name` join, so they must exclude it here or crash with "no such
    column: build_run" the moment a batch filter is applied (confirmed via
    a real 500 error)."""
    clauses, params = [], []
    for col, key in _DETAIL_FILTER_MAP:
        if key in exclude:
            continue
        field = f"{prefix}{col}" if prefix else col
        frag, param = _wildcard_clause(field, filters.get(key, ""))
        if frag:
            clauses.append(frag)
            params.append(param)
    sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


_CLIENT_DETAIL_FILTER_MAP = [
    ("language",            "language"),
    ("lang_version",        "version"),
    ("http_client",         "http_client"),
    ("http_client_version", "http_client_version"),
    ("build_run",           "run"),
]


def _build_client_where(filters: dict) -> tuple[str, list]:
    clauses, params = [], []
    for col, key in _CLIENT_DETAIL_FILTER_MAP:
        frag, param = _wildcard_clause(col, filters.get(key, ""))
        if frag:
            clauses.append(frag)
            params.append(param)
    sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return sql, params


# ── Queries on image_details view ─────────────────────────────────────────────

def get_known_hosts() -> list[str]:
    """Return every distinct Docker host that has ever built or tested an
    image, or been used for a named run ('' for the local engine)."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT host FROM build_results
            UNION SELECT host FROM test_results
            UNION SELECT docker_host FROM runs
            ORDER BY host
        """).fetchall()
    return [r[0] for r in rows]


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
            "hosts":        get_known_hosts(),
        }


def get_or_create_run(name: str, host: str = "") -> int:
    """Get an existing run for this (name, host) pair or create it, returning
    the run id. Scoped by host so reusing a batch name against a different
    Docker host starts a new run rather than merging into the other host's."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM runs WHERE name=? AND docker_host=?", (name, host)
        ).fetchone()
        if row:
            return row[0]
        cur = conn.execute(
            "INSERT INTO runs (name, created_at, docker_host) VALUES (?,?,?)",
            (name, datetime.now(timezone.utc).isoformat(), host),
        )
        return cur.lastrowid


def get_runs(scope: str = "") -> list[dict]:
    """Return all runs ordered newest first, with duration_seconds computed.

    The runs table is shared by server AND client actions (a batch name
    typed for one kind has no meaning for the other) -- scope='server'
    restricts to runs referenced by a server build/test, scope='client' to
    runs referenced by a client build/test. Unscoped (the default) returns
    every run regardless of kind.
    """
    where = ""
    if scope == "server":
        where = ("WHERE EXISTS (SELECT 1 FROM build_results b WHERE b.run_id = runs.id) "
                 "OR EXISTS (SELECT 1 FROM test_results t WHERE t.run_id = runs.id)")
    elif scope == "client":
        where = ("WHERE EXISTS (SELECT 1 FROM client_build_results b WHERE b.run_id = runs.id) "
                 "OR EXISTS (SELECT 1 FROM client_test_results t WHERE t.run_id = runs.id)")
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id, name, created_at, status, finished_at, docker_host "
            f"FROM runs {where} ORDER BY created_at DESC"
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


def save_run_log(run_id: int, log_text: str) -> None:
    """Persist the full narrative log (the dashboard's bottom log panel
    output -- section headers, per-image PASS/FAIL lines, etc.) for a run,
    so it can be read back later. Distinct from build_results.output /
    test_results.output, which capture each individual image's raw
    stdout/stderr, not the combined run-level narrative."""
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET log_text=? WHERE id=?", (log_text, run_id)
        )


def get_run_summary(name: str, host: str = "") -> dict | None:
    """One run's metadata (name, host, status, duration, saved log text)
    plus build/test pass-fail counts, for the Reports tab's run summary.
    Keyed by (name, host), matching get_or_create_run/the run-picker's own
    dropdown value, rather than the internal numeric id."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, created_at, status, finished_at, docker_host, log_text "
            "FROM runs WHERE name=? AND docker_host=?", (name, host)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        run_id = d["id"]

        duration = None
        if d.get("created_at") and d.get("finished_at"):
            try:
                t0 = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(d["finished_at"].replace("Z", "+00:00"))
                duration = int((t1 - t0).total_seconds())
            except Exception:
                pass
        d["duration_seconds"] = duration

        build_row = conn.execute(
            "SELECT COALESCE(SUM(success), 0) AS passed, "
            "       COALESCE(SUM(1 - success), 0) AS failed "
            "FROM build_results WHERE run_id=?", (run_id,)
        ).fetchone()
        test_row = conn.execute(
            "SELECT COALESCE(SUM(success), 0) AS passed, "
            "       COALESCE(SUM(1 - success), 0) AS failed "
            "FROM test_results WHERE run_id=?", (run_id,)
        ).fetchone()
        d["build_passed"] = build_row["passed"]
        d["build_failed"] = build_row["failed"]
        d["test_passed"]  = test_row["passed"]
        d["test_failed"]  = test_row["failed"]
        return d


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
               sort_dir: str = "asc",
               host: str = "") -> dict:
    """Return a paginated result with build/test status for the given Docker host."""
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

    status_sql = _status_sql()
    with _connect() as conn:
        total  = conn.execute(
            f"SELECT COUNT(*) FROM ({status_sql}) s {where_sql}",
            [host, host, host, host] + params,
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows   = conn.execute(
            f"SELECT * FROM ({status_sql}) s {where_sql} {order_sql} LIMIT ? OFFSET ?",
            [host, host, host, host] + params + [per_page, offset],
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "items":    [dict(r) for r in rows],
    }


def get_images_by_ids(image_ids: list, host: str = "") -> list[dict]:
    if not image_ids:
        return []
    with _connect() as conn:
        ph   = ",".join("?" * len(image_ids))
        rows = conn.execute(
            f"SELECT * FROM ({_status_sql()}) s WHERE id IN ({ph})",
            [host, host, host, host] + list(image_ids),
        ).fetchall()
    return [dict(r) for r in rows]


def get_ignored_images(host: str = "") -> list[dict]:
    """Return every image currently on the ignore list, in full detail."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM ({_status_sql()}) s WHERE ignored = 1 "
            "ORDER BY language, framework, library",
            [host, host, host, host],
        ).fetchall()
    return [dict(r) for r in rows]


# ── Client images (Server/Client dashboard mode) ─────────────────────────────

_CLIENT_SORTABLE_COLS = {
    "language", "lang_version", "http_client", "http_client_version",
    "build_success", "test_success",
}


def get_client_images(filters: dict | None = None,
                      page: int = 1,
                      per_page: int = 50,
                      include_ignored: bool = True,
                      sort_by: str = "",
                      sort_dir: str = "asc",
                      host: str = "") -> dict:
    """Same shape as get_images(), over the client 2D matrix."""
    filters   = filters or {}
    where_sql, params = _build_client_where(filters)

    if not include_ignored:
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} ignored = 0"

    sort_col = sort_by if sort_by in _CLIENT_SORTABLE_COLS else ""
    direction = "DESC" if sort_dir.lower() == "desc" else "ASC"
    if sort_col:
        order_sql = f"ORDER BY {sort_col} {direction} NULLS LAST, language, lang_version, http_client, http_client_version"
    else:
        order_sql = "ORDER BY language, lang_version, http_client, http_client_version"

    status_sql = _client_status_sql()
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({status_sql}) s {where_sql}",
            [host, host, host] + params,
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(
            f"SELECT * FROM ({status_sql}) s {where_sql} {order_sql} LIMIT ? OFFSET ?",
            [host, host, host] + params + [per_page, offset],
        ).fetchall()

    return {
        "total":    total,
        "page":     page,
        "per_page": per_page,
        "pages":    max(1, (total + per_page - 1) // per_page),
        "items":    [dict(r) for r in rows],
    }


def get_client_images_by_ids(client_image_ids: list, host: str = "") -> list[dict]:
    if not client_image_ids:
        return []
    with _connect() as conn:
        ph = ",".join("?" * len(client_image_ids))
        rows = conn.execute(
            f"SELECT * FROM ({_client_status_sql()}) s WHERE id IN ({ph})",
            [host, host, host] + list(client_image_ids),
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_client_ids_for_filter(filters: dict, include_ignored: bool = True,
                                   host: str = "") -> list[int]:
    filters = filters or {}
    where_sql, params = _build_client_where(filters)
    if not include_ignored:
        connector = "AND" if where_sql else "WHERE"
        where_sql = f"{where_sql} {connector} ignored = 0"
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT id FROM ({_client_status_sql()}) s {where_sql}",
            [host, host, host] + params,
        ).fetchall()
    return [r[0] for r in rows]


def get_client_filter_options() -> dict:
    with _connect() as conn:
        def vals(col):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM client_image_details ORDER BY {col}"
            )]
        return {
            "languages":            vals("language"),
            "lang_versions":        vals("lang_version"),
            "http_clients":         vals("http_client"),
            "http_client_versions": vals("http_client_version"),
        }


def get_client_cascading_filter_options(active: dict) -> dict:
    """Client-mode counterpart to get_cascading_filter_options() -- narrows
    the http_client/version option lists to whatever actually exists for the
    already-selected parent dimensions, same language -> lang_version ->
    http_client -> http_client_version dependency order as the server side."""
    def _vals(col: str, restrict: dict) -> list:
        where_sql, params = _build_client_where(restrict)
        rows = _connect().execute(
            f"SELECT DISTINCT {col} FROM client_image_details {where_sql} ORDER BY {col}",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    lang = active.get("language", "")
    ver  = active.get("version", "")
    hc   = active.get("http_client", "")

    return {
        "languages":            _vals("language",             {}),
        "lang_versions":        _vals("lang_version",         {"language": lang}),
        "http_clients":         _vals("http_client",           {"language": lang, "version": ver}),
        "http_client_versions": _vals("http_client_version",   {"language": lang, "version": ver,
                                                                  "http_client": hc}),
    }


def save_client_build_result(client_image_id: int, success: bool,
                             output: str,
                             started_at: str, finished_at: str,
                             run_id: int | None = None,
                             host: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO client_build_results
                   (client_image_id, host, success, output, started_at, finished_at, run_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(client_image_id, host)
               DO UPDATE SET success=excluded.success,
                             output=excluded.output,
                             started_at=excluded.started_at,
                             finished_at=excluded.finished_at,
                             run_id=excluded.run_id""",
            (client_image_id, host, int(success), output, started_at, finished_at, run_id),
        )


def save_client_test_result(client_image_id: int, success: bool,
                            output: str, error_msg: str,
                            started_at: str, finished_at: str,
                            run_id: int | None = None,
                            host: str = "") -> None:
    """A client image's test is a single real outbound call against the
    fingerprint-target app, run from the actual built image -- success is
    just "did that call succeed", not a root/version_ok breakdown."""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO client_test_results
                   (client_image_id, host, success, output, error_msg, started_at, finished_at, run_id)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(client_image_id, host)
               DO UPDATE SET success=excluded.success,
                             output=excluded.output,
                             error_msg=excluded.error_msg,
                             started_at=excluded.started_at,
                             finished_at=excluded.finished_at,
                             run_id=excluded.run_id""",
            (client_image_id, host, int(success), output, error_msg, started_at, finished_at, run_id),
        )


def get_client_stats(host: str = "") -> dict:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM client_images").fetchone()[0]
        built = conn.execute(
            "SELECT COUNT(*) FROM client_build_results WHERE host=? AND success=1", (host,)
        ).fetchone()[0]
        tested = conn.execute(
            "SELECT COUNT(*) FROM client_test_results WHERE host=? AND success=1", (host,)
        ).fetchone()[0]
        fingerprinted = conn.execute(
            "SELECT COUNT(DISTINCT client_image_id) FROM client_fingerprints WHERE host=?", (host,)
        ).fetchone()[0]
    return {"total": total, "built": built, "tested": tested, "fingerprinted": fingerprinted}


_STATUS_CLAUSES = {
    "not_built":    "build_success IS NULL",
    "build_failed": "build_success = 0",
    "not_tested":   "test_success IS NULL",
    "test_failed":  "test_success = 0",
}


def get_all_ids_for_filter(filters: dict,
                            include_ignored: bool = True,
                            status: str = "",
                            host: str = "") -> list[int]:
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
            f"SELECT id FROM ({_status_sql()}) s {where_sql}",
            [host, host, host, host] + params,
        ).fetchall()
    return [r[0] for r in rows]


def _versions_with_parsed_compat(rows, overrides=None, kind=None, entry_name=None) -> list:
    """fw_versions/lib_versions/http_client_versions all store `compatibility`
    as a JSON-encoded TEXT column -- parse it into a real list (or None) so
    callers (the Reference tab) don't each have to JSON.parse it themselves.

    Also computes the effective `include` flag the Reference tab displays
    (blue badge / "Included" field): the registry's own per-version
    `available` column, overridden by a matching version_overrides row when
    one exists (same override the Registry Editor and generate_images.py /
    generate_client_images.py already honor at build time) -- so what the
    Reference tab shows always matches what actually gets built."""
    overrides = overrides or {}
    out = []
    for r in rows:
        d = dict(r)
        d["compatibility"] = json.loads(d["compatibility"]) if d.get("compatibility") else None
        override = overrides.get((kind, entry_name, d["version_nr"])) if kind else None
        base_available = bool(d.pop("available", True))
        d["include"] = base_available if override is None or override["available"] is None else override["available"]
        d["note"] = (override["note"] if override else "") or None
        out.append(d)
    return out


def get_reference_data() -> dict:
    """Return full reference tables for the dashboard info panel -- both the
    server-side axis (frameworks/cryptography_libs) and the client-side axis
    (http_clients), so the Reference tab can show compatibility ranges for
    either dashboard mode."""
    with _connect() as conn:
        langs = [dict(r) for r in conn.execute(
            "SELECT * FROM languages ORDER BY name"
        )]
        for lang in langs:
            lang_id = lang["id"]
            overrides = get_version_override_map(lang["name"])
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
                fw_dict["versions"] = _versions_with_parsed_compat(conn.execute(
                    "SELECT * FROM fw_versions WHERE framework_id=? "
                    "ORDER BY release_date IS NULL, release_date, version_nr",
                    (fw["id"],),
                ), overrides, "framework", fw["name"])
                lang["frameworks"].append(fw_dict)
            lang["libraries"] = []
            for lib in conn.execute(
                "SELECT * FROM libraries WHERE language_id=? ORDER BY name",
                (lang_id,),
            ):
                lib_dict = dict(lib)
                lib_dict["versions"] = _versions_with_parsed_compat(conn.execute(
                    "SELECT * FROM lib_versions WHERE library_id=? "
                    "ORDER BY release_date IS NULL, release_date, version_nr",
                    (lib["id"],),
                ), overrides, "library", lib["name"])
                lang["libraries"].append(lib_dict)
            lang["http_clients"] = []
            for hc in conn.execute(
                "SELECT * FROM http_clients WHERE language_id=? ORDER BY name",
                (lang_id,),
            ):
                hc_dict = dict(hc)
                hc_dict["versions"] = _versions_with_parsed_compat(conn.execute(
                    "SELECT * FROM http_client_versions WHERE http_client_id=? "
                    "ORDER BY release_date IS NULL, release_date, version_nr",
                    (hc["id"],),
                ), overrides, "http_client", hc["name"])
                lang["http_clients"].append(hc_dict)
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
                      run_id: int | None = None,
                      host: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            """INSERT INTO build_results
                   (image_id, host, success, output, started_at, finished_at, run_id)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(image_id, host)
               DO UPDATE SET success=excluded.success,
                             output=excluded.output,
                             started_at=excluded.started_at,
                             finished_at=excluded.finished_at,
                             run_id=excluded.run_id""",
            (image_id, host, int(success), output, started_at, finished_at, run_id),
        )


def save_test_result(image_id: int, success: bool,
                     root_ok: bool, version_ok: bool,
                     error_msg: str, response_data,
                     output: str = "",
                     run_id: int | None = None,
                     host: str = "") -> None:
    tested_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO test_results
                   (image_id, host, success, root_ok, version_ok,
                    error_msg, response_data, output, tested_at, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (image_id, host, int(success), int(bool(root_ok)), int(bool(version_ok)),
             error_msg,
             json.dumps(response_data) if response_data is not None else None,
             output, tested_at, run_id),
        )


def save_fingerprint_results(image_id: int,
                             records: dict | None,
                             run_id: int | None = None,
                             host: str = "") -> None:
    """Store the 4-record network-traffic capture (success/failure/
    method_not_allowed/malformed, see manager._capture_fingerprint) taken
    against a running container for one fingerprint pass. `records` is None
    if the container never came up far enough to probe at all; individual
    call_type entries within it are never None once present."""
    if records is None:
        return
    captured_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        for call_type, rec in records.items():
            if rec is None:
                continue
            conn.execute(
                """INSERT INTO fingerprints
                       (image_id, host, call_type, method, path, status_code,
                        traffic_raw, pcap_raw, error_msg, captured_at, run_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (image_id, host, call_type, rec.get("method", ""), rec.get("path", ""),
                 rec.get("status_code"), rec.get("traffic_raw", ""), rec.get("pcap_raw", ""),
                 rec.get("error", ""), captured_at, run_id),
            )


def save_client_fingerprint_results(client_image_id: int,
                                     record: dict | None,
                                     run_id: int | None = None,
                                     host: str = "") -> None:
    """Store one client-fingerprint capture (see manager._capture_client_fingerprint)
    -- `record` is None if the client container never ran at all. client_output
    is the client's own self-reported JSON summary (client/client_version/
    language_version) -- kept verbatim so it can be compared against the
    ground truth (what we know we actually built) later."""
    if record is None:
        return
    captured_at = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO client_fingerprints
                   (client_image_id, host, status_code, traffic_raw, pcap_raw,
                    error_msg, client_output, observed_user_agent,
                    observed_ja3_hash, observed_ja3_string, captured_at, run_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (client_image_id, host, record.get("status_code"),
             record.get("traffic_raw", ""), record.get("pcap_raw", ""),
             record.get("error", ""), record.get("client_output", ""),
             record.get("observed_user_agent"), record.get("observed_ja3_hash"),
             record.get("observed_ja3_string"),
             captured_at, run_id),
        )
        ja3_hash = record.get("observed_ja3_hash")
        if ja3_hash:
            # First JA3 ever seen for this exact image becomes its reference
            # baseline -- there's no external "expected" value to check
            # against, so later captures compare against whatever this one
            # observed.
            conn.execute(
                """INSERT INTO client_ja3_reference
                       (client_image_id, ja3_hash, ja3_string, fingerprint_id, first_seen_at)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(client_image_id) DO NOTHING""",
                (client_image_id, ja3_hash, record.get("observed_ja3_string"),
                 cur.lastrowid, captured_at),
            )


def _parse_client_reported(client_output: str | None) -> dict:
    """Parse a client's own self-reported JSON summary into structured
    fields, tolerating a missing/malformed value (a client that errored
    before printing anything, or an unexpected format) rather than breaking
    the whole row."""
    if client_output:
        try:
            data = json.loads(client_output)
            return {
                "reported_client":           data.get("client"),
                "reported_client_version":   data.get("client_version"),
                "reported_language_version": data.get("language_version"),
            }
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass
    return {"reported_client": None, "reported_client_version": None, "reported_language_version": None}


def _version_matches(ground_truth: str | None, reported: str | None) -> bool | None:
    """None = nothing to compare (client never self-reported a version).
    Otherwise: does the client's own self-reported version match what we
    know we actually built, accounting for our own bucket-vs-exact-patch
    resolution (e.g. ground truth "0.48" should match a self-reported
    "0.48.0", and "built-in" should match "builtin")."""
    if reported is None or ground_truth is None:
        return None
    gt  = ground_truth.strip().lower().replace("-", "")
    rep = reported.strip().lower().replace("-", "")
    return rep == gt or rep.startswith(gt + ".")


def _user_agent_matches(http_client: str, http_client_version: str, observed_ua: str | None) -> bool | None:
    """None = nothing to compare -- http.client sets no User-Agent header at
    all, and the raw-TLS clients never reach the HTTP layer unencrypted, so
    no User-Agent is ever observed for either. Otherwise: does the
    User-Agent actually seen on the wire (parsed from the captured packets,
    not from anything the client claims about itself) contain the
    ground-truth library name and version?"""
    if not observed_ua:
        return None
    ua = observed_ua.strip().lower()
    name_ok = http_client.strip().lower() in ua
    ver_ok = bool(http_client_version) and http_client_version.strip().lower() in ua
    return name_ok and ver_ok


def _with_client_comparison(row: dict) -> dict:
    """Add reported_* fields (the client's own self-reported claim) plus
    match booleans against ground truth -- and, separately,
    observed_client_match against observed_user_agent, which is parsed
    straight out of the captured packets and isn't something the client's
    self-report can influence."""
    reported = _parse_client_reported(row.pop("client_output", None))
    row.update(reported)
    row["client_name_match"] = (
        None if reported["reported_client"] is None
        else reported["reported_client"].strip().lower() == row["http_client"].strip().lower()
    )
    row["client_version_match"] = _version_matches(row["http_client_version"], reported["reported_client_version"])
    row["language_version_match"] = _version_matches(row["language_version"], reported["reported_language_version"])
    row["observed_client_match"] = _user_agent_matches(
        row["http_client"], row["http_client_version"], row.get("observed_user_agent"))

    # JA3 has no external ground truth -- the first capture of a given image
    # became its own reference baseline (see save_client_fingerprint_results),
    # so later captures compare against that instead.
    ref_hash = row.pop("reference_ja3_hash", None)
    ref_fp_id = row.pop("reference_fingerprint_id", None)
    row["ja3_is_baseline"] = False
    row["ja3_match"] = None
    if row.get("observed_ja3_hash"):
        if ref_fp_id == row.get("id"):
            row["ja3_is_baseline"] = True
        elif ref_hash:
            row["ja3_match"] = row["observed_ja3_hash"] == ref_hash
    return row


def get_client_fingerprint_report(client_fingerprint_id: int) -> dict | None:
    """One captured client call, joined with its ground truth (language +
    http-client-library + version) -- what the dashboard shows as the
    'report' for a client fingerprint: what we can prove was actually
    running, since we're the ones who triggered the call, compared against
    what the client itself self-reported in its own JSON output."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT
                cf.id, cf.host, cf.status_code, cf.traffic_raw, cf.pcap_raw,
                cf.error_msg, cf.client_output, cf.observed_user_agent,
                cf.observed_ja3_hash, cf.observed_ja3_string, cf.captured_at,
                l.name  AS language, lv.version_nr AS language_version,
                hc.name AS http_client, hcv.version_nr AS http_client_version,
                ci.image_tag,
                ref.ja3_hash AS reference_ja3_hash, ref.fingerprint_id AS reference_fingerprint_id
            FROM client_fingerprints cf
            JOIN client_images ci          ON ci.id = cf.client_image_id
            JOIN lang_versions lv           ON lv.id = ci.lang_version_id
            JOIN languages l                ON l.id = lv.language_id
            JOIN http_client_versions hcv   ON hcv.id = ci.http_client_version_id
            JOIN http_clients hc            ON hc.id = hcv.http_client_id
            LEFT JOIN client_ja3_reference ref ON ref.client_image_id = cf.client_image_id
            WHERE cf.id = ?
        """, (client_fingerprint_id,)).fetchone()
    return _with_client_comparison(dict(row)) if row else None


def get_client_fingerprints(client_image_id: int | None = None) -> list:
    """List client-fingerprint captures, newest first, each joined with its
    ground truth and compared against the client's own self-reported
    version -- optionally scoped to one client image."""
    where = "WHERE ci.id = ?" if client_image_id is not None else ""
    params = (client_image_id,) if client_image_id is not None else ()
    with _connect() as conn:
        rows = conn.execute(f"""
            SELECT
                cf.id, cf.host, cf.status_code, cf.client_output,
                cf.observed_user_agent, cf.observed_ja3_hash, cf.captured_at,
                l.name  AS language, lv.version_nr AS language_version,
                hc.name AS http_client, hcv.version_nr AS http_client_version,
                ci.image_tag,
                ref.ja3_hash AS reference_ja3_hash, ref.fingerprint_id AS reference_fingerprint_id
            FROM client_fingerprints cf
            JOIN client_images ci          ON ci.id = cf.client_image_id
            JOIN lang_versions lv           ON lv.id = ci.lang_version_id
            JOIN languages l                ON l.id = lv.language_id
            JOIN http_client_versions hcv   ON hcv.id = ci.http_client_version_id
            JOIN http_clients hc            ON hc.id = hcv.http_client_id
            LEFT JOIN client_ja3_reference ref ON ref.client_image_id = cf.client_image_id
            {where}
            ORDER BY cf.captured_at DESC
        """, params).fetchall()
    return [_with_client_comparison(dict(r)) for r in rows]


# ── Reports ───────────────────────────────────────────────────────────────────

def get_test_reports(filters: dict | None = None,
                     page: int = 1, per_page: int = 100) -> dict:
    """Return paginated test results joined with image metadata."""
    filters   = filters or {}
    where_sql, params = _build_where(filters, exclude=frozenset({"run"}))

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

    host_val = filters.get("host", "")
    host_filter = ""
    if host_val not in ("", None):
        connector = "AND" if (where_sql or run_filter) else "WHERE"
        host_filter = f"{connector} t.host = ?"
        params.append(host_val)

    with _connect() as conn:
        total = conn.execute(f"""
            SELECT COUNT(*) FROM test_results t
            JOIN image_details d ON d.id = t.image_id
            LEFT JOIN runs r ON r.id = t.run_id
            {where_sql} {run_filter} {host_filter}
        """, params).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(f"""
            SELECT
                t.id, t.success, t.root_ok, t.version_ok,
                t.error_msg, t.response_data, t.output, t.tested_at, t.host,
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
            {host_filter}
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
    where_sql, params = _build_where(filters, exclude=frozenset({"run"}))

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

    host_val = filters.get("host", "")
    host_filter = ""
    if host_val not in ("", None):
        connector = "AND" if (where_sql or run_filter) else "WHERE"
        host_filter = f"{connector} b.host = ?"
        params.append(host_val)

    with _connect() as conn:
        total = conn.execute(f"""
            SELECT COUNT(*) FROM build_results b
            JOIN image_details d ON d.id = b.image_id
            LEFT JOIN runs r ON r.id = b.run_id
            {where_sql} {run_filter} {host_filter}
        """, params).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(f"""
            SELECT
                b.id, b.success, b.output, b.started_at, b.finished_at, b.host,
                r.name AS run_name, r.status AS run_status,
                d.language, d.lang_version, d.framework, d.fw_version,
                d.library, d.lib_version, d.image_tag
            FROM build_results b
            JOIN image_details d ON d.id = b.image_id
            LEFT JOIN runs r ON r.id = b.run_id
            {where_sql}
            {run_filter}
            {host_filter}
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
                       page: int = 1, per_page: int = 100,
                       host: str = "") -> dict:
    """Return paginated non-ignored images where build or test has no result yet."""
    filters = filters or {}
    where_sql, params = _build_where(filters)
    connector = "AND" if where_sql else "WHERE"
    where_sql = (f"{where_sql} {connector} "
                 "(build_success IS NULL OR test_success IS NULL) AND ignored = 0")
    status_sql = _status_sql()
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM ({status_sql}) s {where_sql}",
            [host, host, host, host] + params,
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(f"""
            SELECT id, image_tag, language, lang_version, framework, fw_version,
                   library, lib_version, build_success, test_success, synced_at
            FROM ({status_sql}) s
            {where_sql}
            ORDER BY language, lang_version, framework, fw_version, library, lib_version
            LIMIT ? OFFSET ?
        """, [host, host, host, host] + params + [per_page, offset]).fetchall()

    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "items": [dict(r) for r in rows],
    }


def get_stats(host: str = "") -> dict:
    """Return aggregate statistics for the dashboard header, scoped to a Docker host."""
    with _connect() as conn:
        total    = conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
        ignored  = conn.execute(
            "SELECT COUNT(*) FROM images WHERE ignored=1"
        ).fetchone()[0]
        built    = conn.execute(
            "SELECT COUNT(*) FROM build_results WHERE success=1 AND host=?", (host,)
        ).fetchone()[0]
        built_f  = conn.execute(
            "SELECT COUNT(*) FROM build_results WHERE success=0 AND host=?", (host,)
        ).fetchone()[0]
        tested   = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT image_id FROM test_results WHERE host=?
                   GROUP BY image_id
                   HAVING MAX(CASE WHEN success=1 THEN 1 ELSE 0 END)=1
               )""", (host,)
        ).fetchone()[0]
        tested_f = conn.execute(
            """SELECT COUNT(*) FROM (
                   SELECT image_id FROM test_results WHERE host=?
                   GROUP BY image_id
                   HAVING MAX(CASE WHEN success=1 THEN 1 ELSE 0 END)=0
               )""", (host,)
        ).fetchone()[0]
        not_built = conn.execute(
            """SELECT COUNT(*) FROM images
               WHERE ignored=0 AND id NOT IN
                   (SELECT image_id FROM build_results WHERE host=?)""", (host,)
        ).fetchone()[0]
        not_tested = conn.execute(
            """SELECT COUNT(*) FROM images
               WHERE ignored=0 AND id NOT IN
                   (SELECT image_id FROM test_results WHERE host=?)""", (host,)
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


# ── Update-availability scanner (scripts/check_updates.py) ──────────────────

def save_pending_update(language: str, kind: str, name: str, package_id: str | None,
                        new_major: str, latest_version: str | None,
                        tracked_majors: list, release_date: str | None = None) -> None:
    """Upsert one detected-but-not-yet-tracked major. Never touches
    `dismissed` on conflict, so re-detecting the same new_major on a later
    scan doesn't resurrect a row the user already dismissed."""
    with _connect() as conn:
        conn.execute("""
            INSERT INTO pending_updates
                (language, kind, name, package_id, new_major, latest_version,
                 tracked_majors, release_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(language, kind, name, new_major) DO UPDATE SET
                latest_version = excluded.latest_version,
                tracked_majors = excluded.tracked_majors,
                package_id     = excluded.package_id,
                release_date   = excluded.release_date
        """, (language, kind, name, package_id, new_major, latest_version,
              json.dumps(tracked_majors), release_date))


def _rows_with_parsed_majors(rows) -> list:
    result = []
    for r in rows:
        d = dict(r)
        d["tracked_majors"] = json.loads(d["tracked_majors"]) if d["tracked_majors"] else []
        result.append(d)
    return result


def get_pending_updates(include_dismissed: bool = False) -> list:
    """Active review queue -- excludes already-included rows (those live in
    get_update_log() instead) and, by default, dismissed ones too."""
    with _connect() as conn:
        where = "WHERE included = 0"
        if not include_dismissed:
            where += " AND dismissed = 0"
        rows = conn.execute(f"""
            SELECT * FROM pending_updates {where}
            ORDER BY language, kind, name, new_major
        """).fetchall()
    return _rows_with_parsed_majors(rows)


def get_update_log(hide_tested: bool = False) -> list:
    """Permanent history of every update actually included (registry bucket
    added + generate_images run), newest first -- what the user reads back
    to see which images still need building/testing. `hide_tested` drops
    rows already marked as having a successful test run, once the user is
    done with them."""
    with _connect() as conn:
        where = "WHERE included = 1"
        if hide_tested:
            where += " AND tested = 0"
        rows = conn.execute(f"""
            SELECT * FROM pending_updates {where}
            ORDER BY included_at DESC
        """).fetchall()
    return _rows_with_parsed_majors(rows)


def dismiss_pending_update(update_id: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE pending_updates SET dismissed = 1 WHERE id = ?", (update_id,))


def get_pending_update(update_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM pending_updates WHERE id = ?", (update_id,)).fetchone()
    if row is None:
        return None
    return _rows_with_parsed_majors([row])[0]


def mark_pending_update_included(update_id: int, images_added: int) -> None:
    with _connect() as conn:
        conn.execute("""
            UPDATE pending_updates
            SET included = 1, included_at = CURRENT_TIMESTAMP, images_added = ?
            WHERE id = ?
        """, (images_added, update_id))


def mark_pending_update_tested(update_id: int) -> None:
    """Record that the user ran a successful test on the images this update
    added -- lets the Included log be filtered down to what still needs
    attention."""
    with _connect() as conn:
        conn.execute("""
            UPDATE pending_updates
            SET tested = 1, tested_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (update_id,))


def count_pending_updates() -> int:
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM pending_updates WHERE dismissed = 0 AND included = 0"
        ).fetchone()[0]


# ── Manual include/exclude overrides (Registry editor) ──────────────────────

def get_version_overrides(language: str) -> list[dict]:
    """Every active override row for a language -- used by the dashboard's
    registry-editor GET route to annotate the registry data it returns."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM version_overrides WHERE language = ? ORDER BY kind, name, nr",
            (language,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_version_override_map(language: str) -> dict:
    """(kind, name, nr) -> {"available": bool|None, "note": str}, for
    scripts/generate_images.py's and generate_client_images.py's one-time
    per-run lookup while deciding what to build."""
    return {
        (r["kind"], r["name"], r["nr"]): {
            "available": None if r["available"] is None else bool(r["available"]),
            "note": r["note"] or "",
        }
        for r in get_version_overrides(language)
    }


def set_version_override(language: str, kind: str, name: str, nr: str,
                         available: bool | None, note: str | None) -> None:
    """Upsert one override. Deletes the row instead when both `available` is
    None and `note` is empty, so clearing an override never leaves a dead
    neutral row behind -- "no override" is the absence of a row, not a row
    full of NULLs."""
    note = (note or "").strip()
    with _connect() as conn:
        if available is None and not note:
            conn.execute(
                "DELETE FROM version_overrides WHERE language=? AND kind=? AND name=? AND nr=?",
                (language, kind, name, nr),
            )
            return
        conn.execute("""
            INSERT INTO version_overrides (language, kind, name, nr, available, note, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(language, kind, name, nr) DO UPDATE SET
                available  = excluded.available,
                note       = excluded.note,
                updated_at = CURRENT_TIMESTAMP
        """, (language, kind, name, nr, None if available is None else int(available), note))


def set_lang_version_note(language: str, nr: str, note: str) -> None:
    """Set/clear the Reference-tab tooltip note for one language version.
    Unlike framework/library versions, a language version has no separate
    override table (version_overrides.kind doesn't cover 'language' -- its
    only other axis, include/exclude, comes straight from the registry
    JSON's own include flag, not a DB override) -- this is purely an
    annotation on the existing lang_versions row, not an override of
    anything. sync_registry()'s own INSERT...ON CONFLICT for this table
    only touches release_date/include, so this survives future syncs."""
    with _connect() as conn:
        conn.execute("""
            UPDATE lang_versions SET note = ?
            WHERE version_nr = ? AND language_id = (SELECT id FROM languages WHERE name = ?)
        """, (note.strip(), nr, language))
