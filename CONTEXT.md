# SCA — Project Context

## What is this project?

SCA (Side-Channel / Software Crypto Analysis) is a **Docker image generation framework** for security research. It produces thousands of minimal containerized web applications, each representing a unique combination of:

```
language version  ×  API framework version  ×  crypto library version
```

Each generated image exposes two HTTP endpoints on port 8000:
- `GET /` → `{"message": "Hello World"}`
- `GET /version` → JSON with the exact runtime versions of language, framework, and crypto library

The purpose is to build a large, systematic matrix of crypto library configurations so that researchers can run side-channel analysis, compatibility testing, or behavioural fingerprinting across the full version history of each library.

---

## Repository layout

```
SCA/
├── images/                         # Generated Docker contexts (do not edit by hand)
│   ├── python/
│   │   └── {lang_version}/
│   │       └── {Framework}/
│   │           └── {fw_major}/
│   │               └── {CryptoLib}/
│   │                   └── {lib_version}/
│   │                       ├── Dockerfile
│   │                       ├── app.py
│   │                       └── requirements.txt
│   ├── go/
│   │   └── {lang_version}/
│   │       └── {Framework}/
│   │           └── {fw_major}/
│   │               └── {CryptoLib}/
│   │                   └── {lib_version}/
│   │                       ├── Dockerfile
│   │                       ├── main.go
│   │                       └── go.mod
│   ├── node/
│   │   └── {lang_version}/
│   │       └── {Framework}/
│   │           └── {fw_major}/
│   │               └── {CryptoLib}/
│   │                   └── {lib_version}/
│   │                       ├── Dockerfile
│   │                       ├── app.js
│   │                       └── package.json
│   ├── java/
│   │   └── {jdk_version}/
│   │       └── {Framework}/
│   │           └── {fw_major}/
│   │               └── {CryptoLib}/
│   │                   └── {lib_version}/
│   │                       ├── Dockerfile
│   │                       ├── pom.xml
│   │                       └── src/main/
│   │                           ├── java/app/Main.java
│   │                           └── resources/versions.properties (+ application.properties)
│   └── dotnet/
│       └── {lang_version}/
│           └── {Framework}/
│               └── {fw_major}/
│                   └── {CryptoLib}/
│                       └── {lib_version}/
│                           ├── Dockerfile
│                           ├── app.csproj
│                           ├── Json.cs / Versions.cs
│                           └── Program.cs (+ Startup.cs / AppModule.cs / AppHost.cs / Endpoints.cs)
│
├── scripts/
│   ├── generate_images.py          # Entry point: reads registry → writes images/
│   ├── lang_python.py              # Python-specific templates + PyPI version resolver
│   ├── lang_go.py                  # Go-specific templates + Go module version resolver
│   ├── lang_node.py                # Node.js-specific templates + npm registry version resolver
│   ├── lang_java.py                # Java-specific templates + Maven Central version resolver
│   ├── lang_dotnet.py              # .NET-specific templates + NuGet version resolver
│   ├── lang_php.py                 # PHP-specific templates + Packagist version resolver
│   ├── registry python.json        # Python matrix: lang versions, frameworks, libs, compat rules
│   ├── registry go.json            # Go matrix: lang versions, frameworks, libs, compat rules
│   ├── registry node.json          # Node matrix: lang versions, frameworks, libs, compat rules
│   ├── registry java.json          # Java matrix: lang versions, frameworks, libs, compat rules
│   ├── registry dotnet.json        # .NET matrix: lang versions, frameworks, libs, compat rules
│   └── registry php.json           # PHP matrix: lang versions, frameworks, libs, compat rules
│
├── static/
│   └── dashboard.html              # Single-page dashboard UI (served by dashboard.py)
│
├── manager.py                      # CLI: build / run / test / remove Docker images
│                                   # Also exposes _do_build / _do_test / _do_remove /
│                                   # _do_stop / _do_stop_all for use by dashboard.py
├── db.py                           # SQLite database layer (pqc_manager.db)
├── dashboard.py                    # Flask web dashboard (port 5050)
├── pqc_manager.db                  # SQLite database (auto-created, not committed)
└── CONTEXT.md                      # This file
```

---

## How generation works

### 1. Registry JSON (source of truth)

Each `scripts/registry {lang}.json` defines:

```jsonc
{
  "language_versions": ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"],
  "frameworks": {
    "Flask": {
      "major_versions": ["0", "1", "2", "3"],
      "compat": { "min_python": "3.6" }
    }
  },
  "libraries": {
    "cryptography": {
      "versions": ["2.0", "3.0", ..., "44.0"],
      "import": "cryptography",
      "system_deps": ["libssl-dev", "libffi-dev"],
      "compat": { "max_python_below_36": "3.11" }
    }
  }
}
```

### 2. `generate_images.py`

Iterates the full Cartesian product of `lang_version × framework × fw_major × library × lib_version`, applies compatibility filters, then calls language-specific generators.

Run with:
```bash
python scripts/generate_images.py --lang python
python scripts/generate_images.py --lang go
python scripts/generate_images.py --lang node
python scripts/generate_images.py --lang java
python scripts/generate_images.py --lang dotnet
```

### 3. Language modules (`lang_python.py`, `lang_go.py`)

Each module implements:
- `render_dockerfile(lang_ver, fw, fw_ver, lib, lib_ver, compat)` → Dockerfile string
- `render_app(lang_ver, fw, fw_ver, lib, lib_ver)` → app source string
- `render_deps(fw, fw_ver, lib, lib_ver)` → dependency manifest string (requirements.txt / go.mod)
- `resolve_versions(lib, version_spec)` → concrete version list (calls PyPI/pkg.go.dev API)

### 4. `manager.py`

Builds, runs and tests generated images via Docker:
```bash
python manager.py --build --language python --framework Flask --library cryptography
python manager.py --test  --language go    --framework Gin    --library-version 1.x
python manager.py --list
```

In addition to the CLI interface, `manager.py` exposes internal worker functions used by the dashboard:

| Function | Description |
|----------|-------------|
| `_do_build(entries, no_cache, skip_existing, log_fn, save_fn, stop_event)` | Build Docker images; calls `save_fn(entry, result)` per image |
| `_do_test(entries, log_fn, save_fn, stop_event)` | Start container → hit `/` and `/version` → stop; calls `save_fn` per image |
| `_do_remove(entries, log_fn)` | `docker rmi` each image |
| `_do_stop(entries, log_fn)` | Stop any running containers for the matched images |
| `_do_stop_all(log_fn)` | Stop all `pqc-*` containers |

`stop_event` is a `threading.Event`; set it to cancel the job after the current image finishes.

### 5. `db.py` — Database layer

Manages a SQLite database (`pqc_manager.db`) with the following schema:

**Reference tables** (loaded from `registry *.json` via `load_registry()`):

| Table | Contents |
|-------|----------|
| `languages` | Programming languages (`python`, `go`, …) |
| `lang_versions` | Language versions per language |
| `frameworks` | Web frameworks per language |
| `fw_versions` | Framework major versions with release date + compatibility JSON |
| `libraries` | Crypto libraries per language |
| `lib_versions` | Library versions with release date + compatibility JSON |

**Image table** (synced from `images/` via `sync_images()`):

| Table | Contents |
|-------|----------|
| `images` | One row per Dockerfile; FKs into reference tables; `ignored` flag + reason |

**Run tracking** (group build/test sessions with a label):

| Table | Contents |
|-------|----------|
| `runs` | Named run sessions with `status` (`running` / `completed` / `interrupted`) and timestamps |

**Result tables**:

| Table | Contents |
|-------|----------|
| `build_results` | Latest build outcome per image (1:1 with `images`); linked to a `run` |
| `test_results` | Full test-run history per image (1:N); `root_ok`, `version_ok`, `response_data` (JSON), `output` (container logs), linked to a `run` |

**Convenience view** `image_details` — flat join of all tables; used for all queries in the dashboard.

Key functions:

```python
db.init_db()                          # Create schema + view (idempotent)
db.load_registry()                    # Parse registry JSON → populate reference tables
db.sync_images()                      # Walk images/ → upsert image rows; returns (total, inserted, removed)
db.get_images(filters, page, per_page, include_ignored, sort_by, sort_dir)
db.get_filter_options()               # All distinct values per dimension
db.get_cascading_filter_options(active)  # Cascaded options (child narrows on parent selection)
db.get_all_ids_for_filter(filters, include_ignored)
db.get_images_by_ids(image_ids)
db.save_build_result(image_id, success, output, started_at, finished_at, run_id)
db.save_test_result(image_id, success, root_ok, version_ok, error_msg, response_data, output, run_id)
db.set_ignored(image_ids, ignored, reason)
db.get_or_create_run(name, host='')   # Returns run_id, scoped per (name, host)
db.update_run_status(run_id, status)
db.get_stats()                        # Aggregate counts for dashboard header
db.get_test_reports(filters, limit)
db.get_build_reports(filters, limit)
db.get_pending_images(filters, limit) # Images with NULL build or test result
```

### 6. `dashboard.py` — Web dashboard

A Flask server on **port 5050** that provides a single-page UI backed by REST + SSE APIs.

**Start it with:**
```bash
python dashboard.py
# → http://localhost:5050
```

On first start it auto-runs `db.init_db()`, `db.load_registry()`, and `db.sync_images()`.

**API endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Serve `static/dashboard.html` |
| `POST` | `/api/init` | Reload registry + re-sync images from disk |
| `POST` | `/api/sync` | Re-sync images only (no registry reload) |
| `GET`  | `/api/stats` | Aggregate counts (total, built OK, tested OK, …) |
| `GET`  | `/api/filters` | Filter options; cascades if query params are present |
| `GET`  | `/api/reference` | Full reference tables for the info panel |
| `GET`  | `/api/runs` | All runs ordered newest first |
| `GET`  | `/api/images` | Paginated image list with build/test status; supports filters + sorting |
| `GET`  | `/api/images/ids` | All image IDs matching current filters (for select-all) |
| `POST` | `/api/action` | Start a job: `build`, `test`, `remove`, `stop`, or `mark_success` |
| `POST` | `/api/stop-all` | Stop all `pqc-*` containers |
| `POST` | `/api/cancel/<job_id>` | Signal a running job to stop after the current image |
| `POST` | `/api/ignore` | Set/unset the ignored flag on a set of images |
| `GET`  | `/api/stream/<job_id>` | SSE stream of live log lines from a running job |
| `GET`  | `/api/reports/test` | Filtered test results (up to 2 000 rows) |
| `GET`  | `/api/reports/build` | Filtered build results (up to 2 000 rows) |
| `GET`  | `/api/reports/pending` | Images with no build or test result yet |
| `GET`  | `/api/export/ignore-list` | Download ignore list as plain text |
| `GET`  | `/api/export/image-list` | Download filtered image paths as plain text |

**Job system:**

Each `POST /api/action` spawns a background thread and returns a `{"job_id": "..."}`.  
The UI subscribes to `GET /api/stream/<job_id>` (SSE) to receive live log lines.  
`POST /api/cancel/<job_id>` sets the job's `stop_event`, which causes the worker to stop after the current image.  
The `run_name` field in the action body associates all results with a named `runs` row.

**New-versions scanner (`scripts/check_updates.py`, added 2026-07-13):**

For every tracked framework/library across all 5 registries, checks the real upstream package registry (npm/PyPI/Packagist/Maven Central/NuGet, reusing each `lang_X.py`'s own existing version-fetch function) for majors *beyond* the current tracked ceiling. Results land in the `pending_updates` table (`db.py`) and surface as a "🔔 N new versions" badge bottom-right in the dashboard. Runs weekly via cron on the server (`0 6 * * 1`, Mondays 6am, logs to `check_updates.log`) in addition to the manual trigger ("New versions" in Settings, or the badge itself). Deliberately scoped to framework/library versions only, not new *language* major releases (those follow known per-language release calendars, a different problem).

Two review actions, both human-triggered — nothing happens on its own beyond detection:
- **Dismiss** (per row) — adds the version to its registry as a reference-only bucket (`"available": false`, this project's existing convention), then hides it from the pending list. Flip `available` to `true` by hand later if it turns out to be needed after all.
- **Include** (multi-select) — adds the version as a real, enabled bucket (compatibility inherited from the nearest lower already-tracked major, since an empty array would make `generate_images.py` skip it entirely — unverified until built/tested), regenerates images for every affected language, and reports how many new image contexts resulted per framework/library. Both actions write via `scripts/registry_writer.py`'s format-preserving text-surgery (never a raw `json.dump`, which would reformat these hand-curated files wholesale — verified this empirically before trusting it) and validate the result re-parses as JSON before touching disk. Included rows become permanent history in the "Included log" tab, so you can read back later which images still need building/testing.

**Release dates (added 2026-07-16):** the same detection pass now also fetches a real `release_date` for the one version a newly-found major resolves to (`pending_updates.release_date`), written into the registry bucket on Dismiss/Include instead of the `null` every auto-detected entry got before this existed. PyPI/npm/Packagist carry the date in the same version-list response already being fetched (free); Maven/NuGet's version-list endpoints don't, so those two do one small supplementary request (a HEAD on the resolved version's POM for Maven, the Registration API for NuGet) — only for that one version, not the whole history. See `check_updates._fetch_date()` and each `lang_X.py`'s own `_release_date()`.

---

## What is already covered

### Python (6 lang versions × 8 frameworks × 10 libs × many versions ≈ 6,048 images)

**Density note (2026-07-09 pass)**: added 5 new frameworks (Tornado, aiohttp, CherryPy, Bottle, Pyramid) and 4 new crypto libraries (liboqs-python, pyOpenSSL, ecdsa, Authlib) — Python previously had no liboqs binding at all despite every other language in this project having one. Every new addition surfaced its own real Python-version compatibility boundary once actually build-tested with the real app code path (not just a bare `import`) — see below; a bare `import tornado`-style smoke test would have missed every one of these.

| Framework | Major versions | Notes |
|-----------|---------------|-------|
| Flask | 0, 1, 2, 3 | |
| Django | 0 (reference-only), 1, 2, 3, 4, 5 | |
| FastAPI | 0 | requires uvicorn |
| Tornado | 1–6 | `tornado.web.Application` + `IOLoop.current().start()` |
| aiohttp | 1–3 | `web.Application()` + `web.run_app()` |
| CherryPy | 3, 17, 18 | `cherrypy.quickstart()` + `@cherrypy.expose` |
| Bottle | 0 | single-file micro-framework, never left 0.x |
| Pyramid | 1, 2 | `Configurator()` + plain WSGI |

| Crypto library | Version range | Notes |
|----------------|--------------|-------|
| cryptography | 2.0 – 44.0 | needs Bullseye base for < 36 |
| PyNaCl | 0.x – 1.x | needs libsodium-dev |
| PyCryptodome | 3.x | |
| M2Crypto | 0.26 – 0.38 | needs swig |
| hashlib | built-in | |
| liboqs-python | 0.14, 0.15 | needs liboqs C library built from source + `LD_LIBRARY_PATH` — see below |
| pyOpenSSL | 0.15, 17.5, 22.1, 26.3 | calendar-versioned since major 16; density subset, not every year |
| ecdsa | 0.19 | never left 0.x |
| Authlib | 0.15, 1.7 | OAuth/OIDC/JOSE, no PQC support yet |

**`liboqs-python` is the official Open Quantum Safe project's own Python binding (a ctypes wrapper, not a C extension) — the one liboqs binding this project was previously missing entirely.** Confirmed working end-to-end via a real docker build+run: `oqs.KeyEncapsulation('ML-KEM-768').generate_keypair()` returns a real 1184-byte public key. Needs the liboqs C library built from source in the same Dockerfile stage (git clone + cmake + ninja, the exact recipe already reused for every other language's liboqs binding in this project) plus `LD_LIBRARY_PATH=/usr/local/lib` so ctypes' `dlopen()` can find the resulting shared library at runtime. Unlike Node's `liboqs-node` (which is pinned to a pre-final-name liboqs commit), this binding is paired with a current liboqs release and exposes the final NIST algorithm names directly, no draft-name caveat needed.

**Every new framework surfaced a real Python-version ceiling that a bare `import` smoke test would have missed — found only by running the actual app code path.** Tornado 4/5 genuinely break starting Python 3.10: `httputil.py` subclasses `collections.MutableMapping`, an alias Python removed from the `collections` module itself (moved to `collections.abc`) in 3.10 — confirmed live (works on 3.9, `AttributeError` on 3.10); Tornado 1/2 are genuinely Python-2-only (`tornado/web.py` uses Python-2-only tuple-parameter-unpacking lambda syntax, a hard `SyntaxError` on any Python 3.x); only Tornado 6 (2019) fixed this properly. CherryPy 3 genuinely breaks starting Python 3.8: `_cperror.py` does `from cgi import escape`, removed from the `cgi` module in Python 3.8 — confirmed live (works on 3.7, `ImportError` on 3.8). aiohttp 1/2 genuinely only import successfully on Python 3.6 exactly (not a range) — `helpers.py` uses `asyncio.async`, a `SyntaxError` once `async` became a reserved keyword in Python 3.7, confirmed directly on 3.5/3.6/3.7. aiohttp 1/2 ALSO leave `async-timeout` (a transitive dependency) completely unversioned in their own setup.py — pip resolves it to async-timeout's latest release (4.0.2), which needs a newer Python than these majors target, causing a real `TypeError: function() argument 1 must be code, not str` at import time (a stale/incompatible-bytecode symptom of the version mismatch) — fixed with an explicit pin to `async-timeout==3.0.1` (`FW_VERSION_EXTRA` in `lang_python.py`), the same class of unpinned-transitive-dependency bug already documented for Go's Iris/Echo and several Node frameworks in this project. Pyramid needed one real runtime fix: `pyramid.response.Response(text, content_type=...)` requires an explicit `charset` kwarg for a text body, or it throws `TypeError` at request time (not build time) — fixed by adding `charset="UTF-8"`.

**CherryPy released 16 numbered majors between 2015 and 2018 (3 through 18)** — confirmed via PyPI's real release history, reflecting loose semver discipline in that era rather than 16 genuinely distinct API eras, the same pattern already documented for Node's Hapi package. Majors 4-16 are real, PyPI-installable releases but are NOT individually version-bucketed here (a disclosed density tradeoff, not a claim that they don't work); 3 and 17/18 were chosen as the long-lived-stable and current/final eras respectively.

**Django's pre-1.0 line (0.90-0.96, 2005-2007) is recorded as an explicit reference row** (`compatibility: []`) rather than silently omitted, per this project's standing convention — not independently build-verified (ancient Python 2.3/2.4-era code, predates PyPI's own package-upload conventions for most of that range).

**pyOpenSSL switched to calendar-year versioning at major 16 (2016)** — every year from 16 through 26 has at least one real release; this registry tracks a representative subset (0 legacy, 17/22/26) rather than all 11 yearly majors, a disclosed density tradeoff rather than exhaustive per-year coverage.

**Caveat, same shape as Node's Express 1/2 finding**: aiohttp majors 1/2 (compatible only with Python 3.6 exactly) and CherryPy major 3 (compatible only up to Python 3.7) are real, build-verified templates, but this registry's own included Python versions only start at 3.9 — so these buckets currently generate **zero** images. Flagged, not silently worked around, since expanding the included Python range below 3.9 would need its own separate compatibility sweep across every other framework/library here too, not just these three.

### Go (12 lang versions × ~9 frameworks × ~7 libs × many versions ≈ 17,772 images)

**Density note (2026-07-09 pass)**: Echo/Chi/Fiber's oldest majors (Echo 1-3, Chi 1-4, Fiber 1) were added as real, build-verified buckets, not reference-only rows — each turned out to be genuinely resolvable via the standard Go module proxy (no GOPATH-mode needed, despite that machinery already existing in this file for Gin/Gorilla/Beego/Iris/httprouter's own pre-modules majors) and each needed its own small, real fix once actually built. See the per-framework notes below.

| Framework | Major versions | Module path |
|-----------|---------------|-------------|
| Beego | 1, 2 | github.com/astaxie/beego, github.com/beego/beego/v2 |
| Echo | 1–5 | bare `github.com/labstack/echo` for 1-3, `/v{N}` for 4-5 |
| Fiber | 1–3 | bare `github.com/gofiber/fiber` for 1, `/v{N}` for 2-3 |
| Chi | 1–5 | bare `github.com/go-chi/chi` for 1-4, `/v5` from 5.0 |
| Gin | 1 | github.com/gin-gonic/gin |
| Gorilla/mux | 1 | github.com/gorilla/mux |
| Iris | 10–12 | github.com/kataras/iris/v12 |
| httprouter | 1 | github.com/julienschmidt/httprouter |
| net/http | built-in | |

**Echo has three distinct API eras across its tracked majors, not one break at v4.** v1.x: `Get`/`Post` (lowercase-style, not `GET`/`POST`), `Run(addr string)` to start, and `Context` is a struct (`*echo.Context`) — confirmed against the real v1.4.4 source and a live docker build+run, needed its own template (`_ECHO_V1_TPL` in `lang_go.py`). v2.x: already `GET`/`POST` and the `Context` interface v3+/v4 use, but starts via `Run(engine.Server)` constructed through a separate `echo/engine/standard` sub-package (`e.Run(standard.New(":8000"))`), not a bare address string — its own template (`_ECHO_V2_TPL`). v3.0.0's shape already matches v4 exactly (confirmed live), so v3 needed no template of its own, just the existing modern one. v1.x's own go.mod also leaves `github.com/labstack/gommon` completely unversioned — `go build -mod=mod` resolves it to gommon's latest release (needs go ≥1.23) regardless of the target toolchain; fixed with an explicit extra `require` pin (`_FW_EXTRA_REQS`) resolved the same max_toolchain-aware way as Iris's existing fasthttp pin, applied to Echo majors 1/2/3 (all three share the same unpinned dependency).

**Fiber v1's handler signature is void, not error-returning.** `app.Get(path, func(*fiber.Ctx) { ... })` — no `return`. A v2+-shaped `func(*fiber.Ctx) error` handler is a straight compile error (confirmed via a real failing docker build: "cannot use func... as func(*fiber.Ctx) value"). `ctx.JSON()` itself is unchanged (still returns an error internally, same as v2+) — only the registered handler's own signature differs. Needed its own template (`_FIBER_LEGACY_TPL`).

**Chi v1-v4 resolve via the standard Go module proxy's `+incompatible` mechanism, no GOPATH mode needed.** v1.0.0 has a real (if directive-less) go.mod; v2/v3/v4 have no go.mod of their own at all. Confirmed working end-to-end via a real docker build (`go get github.com/go-chi/chi@v4.1.2` inside a plain Go 1.21 modules-mode build stage succeeds with zero special-casing) — this looked at first like a job for `_INCOMPATIBLE_FW`'s existing GOPATH-mode machinery (built for Gin/Gorilla/Beego/Iris/httprouter's own pre-modules majors), but the module proxy's own `+incompatible` pseudo-version support is simpler and already fully supported by the existing `_resolve_go()` resolver — the only real fix needed was correcting `_fw_module()`'s per-major import-path dispatch (bare path below v5, `/v5` from v5.0), which had been hardcoded to `/v5` unconditionally even for these older majors.

| Crypto library | Version range | Module path |
|----------------|--------------|-------------|
| x/crypto | 0.x – 0.38 | golang.org/x/crypto |
| circl | 1.0 – 1.4 | github.com/cloudflare/circl |
| liboqs-go | 1.0 – 2.0 | github.com/open-quantum-safe/liboqs-go |
| mlkem768 | 0.x – 1.x | filippo.io/mlkem768 |
| tink-go | 1.x, 2.x | github.com/google/tink/go, tink.dev/go/tink |
| crypto | built-in | |
| crypto/mlkem | built-in (Go ≥ 1.24) | |

### Node.js (25 lang versions × 8 frameworks × 13 libs × many versions ≈ 8,598 images)

**Density note**: expanded from 3 to 8 frameworks and from 7 to 13 crypto libraries on request ("fill every real gap, and don't accept 'works differently' as a reason to exclude something — there must be a real technical reason"). This forced a real architectural change, not just more registry rows: the generator's Dockerfile now supports three distinct "kinds" per framework (`_FW_KIND` in `lang_node.py`) — **standard** (hand-written `app.js`, the original shape), **typescript** (NestJS: a real `tsc` compile step, needed because Nest's decorator-based DI depends on TypeScript's own `emitDecoratorMetadata` emission, which plain V8 runtime decorators don't produce), and **scaffold** (AdonisJS: the framework's own official CLI scaffolder runs inside the Dockerfile, then a routes file is `COPY`'d over the generated one). Every "this needs different tooling" case that would previously have been a candidate for exclusion was instead built out as a real Dockerfile variant and build-verified — see the per-framework notes below for what that surfaced.

| Framework | Major versions | npm package | Notes |
|-----------|---------------|--------------|-------|
| Express | 1, 2 (Node 0.10/0.12 only — see note), 3–5 | express | 3.x already uses the modern `express()` factory + `res.json()`/`res.send()` shape identical to 4.x/5.x (confirmed via a real docker build+run on node:8-slim) — the commonly-repeated claim that the API break lands at 4.0 is wrong. 1.x/2.x genuinely differ: `express.createServer()` (removed at 3.0) + `res.send(obj)` (object auto-detects as JSON) — confirmed working end-to-end on node:0.10-slim. **Caveat**: 1.x/2.x's only compatible Node majors (0.10/0.12) are themselves `include:false` in this registry (a pre-existing, disclosed density cut predating this pass, matching the even/LTS-only pattern) — so these two majors are real, verified, buildable templates that currently generate **zero** images. Flipping 0.10/0.12 to `include:true` would need its own separate compatibility sweep across every other framework/library in this registry, not just Express, and wasn't done in this pass. |
| Fastify | 1 (Node 8+), 2–5 | fastify | `.listen({port,host}, cb)` object form works across all majors. Handlers avoid `async` (Node <7.6 can't parse it) and call `reply.send()` explicitly instead of `return`ing a value — `return` as the response only works for async/Promise handlers; a plain sync handler that `return`s just hangs forever with no error (verified against a real container). Major 1's dependency tree also deterministically fails `npm install` on Node 6 (npm@3 `.staging` bug) — narrowed to Node 8+ in the registry. |
| Koa | 1 (Node 4-7), 2 (Node 8+), 3 | koa | 1.x uses pre-async/await generator-function middleware (`app.use(function *() {...})`, `this.body = ...`) — a real, build-verified bucket (generators are native from Node 4, no transpile needed), confirmed end-to-end on node:6-slim. 2.x/3.x use sync middleware (sets `ctx.body`, no `async`) — safe because `koa-compose` wraps every middleware call in `Promise.resolve()` regardless of whether it's async. Major 2's dependency tree (`http-errors`) also uses object destructuring that old V8 can't parse — `require("koa")` itself throws `SyntaxError: Unexpected token {` on Node 6, works from Node 8. Narrowed to Node 8+. |
| Hapi | 17–21 (`@hapi/hapi`) | @hapi/hapi | `Hapi.server({...})` + async `server.route()`/`server.start()`. The package was renamed/rescoped from the unscoped `hapi` at this same major — the pre-rename package has its own real 16-major history (2013-2018) that is NOT individually version-bucketed in this pass (a disclosed density tradeoff — 16 majors in ~5 years reflects loose semver discipline, not 16 genuinely distinct API shapes — see `_comment_hapi_legacy` in the registry), not a claim that those old versions don't work. |
| Restify | 0–11 | restify | `restify.createServer()` + `.get(path, handler)`, stable shape across all 12 tracked majors. 11.x's handlers must be declared `async` — a plain sync handler throws a hard AssertionError at route-registration time (confirmed via a real docker run); older majors' exact handler-signature requirement wasn't independently re-tested per-major. |
| NestJS | 1–11 | @nestjs/core | Decorator-based DI (`@Controller`, `@Get`, `NestFactory.create(AppModule)`) — genuinely standalone (not scaffold-bound, unlike its `nest new` CLI's prominence in Nest's own docs), but needs a real TypeScript compile step for `emitDecoratorMetadata` to populate Nest's DI reflection data; plain-.js with a runtime `--experimental-decorators` flag doesn't work (confirmed via a real failing docker build). Peer packages (`@nestjs/common`, `@nestjs/platform-express`) pin to the same resolved version as `@nestjs/core`; `reflect-metadata`/`rxjs` pins follow Nest's own documented compatibility eras by major (1-5 / 6-7 / 8+), not independently re-verified per era. |
| Sails | 0, 1 | sails | Full MVC framework (ORM/blueprints/views/policies/pubsub) but genuinely usable standalone — every non-routing hook can be disabled via config (`hooks: {grunt:false, views:false, session:false, policies:false, orm:false, pubsub:false}`), confirmed working end-to-end. Only 2 real majors exist total. |
| AdonisJS | 6, 7 (1/2/5 tracked but not independently re-verified this pass — see note) | @adonisjs/core | No documented standalone bootstrap API (unlike NestJS) — fixed by scaffolding the framework's own official minimal starter (`npx create-adonisjs --kit=api`) inside the Dockerfile, then overwriting `start/routes.ts`, confirmed working end-to-end. Native ESM (`"type": "module"`) — `require()` throws at runtime; the generated routes file bridges via `createRequire(import.meta.url)` instead. No `@adonisjs/core` package exists for majors 3/4 at all (npm jumps 2.x→5.x directly, confirmed via the registry API — not a gap in this research). Majors 1/2/5 used a different, older scaffolding CLI (`adonis new` from `@adonisjs/cli`, not today's `create-adonisjs`) and are tracked as real buckets pending that separate verification, not excluded. |

| Crypto library | Version range | npm package | Notes |
|----------------|--------------|-------------|-------|
| crypto | built-in | — | Node standard library |
| node-forge | 0.1 – 1.4 | node-forge | |
| tweetnacl | 0.x, 1.x | tweetnacl | Pure-JS NaCl/libsodium port, zero deps, plain ES5 — no Node-version floor beyond npm itself. Unmaintained since 2020 but still a common legacy dependency. |
| node-jose | 0.x – 2.x | node-jose | A genuinely different package from `jose` below (Cisco's `JWK`/keystore-object model vs. panva's newer zero-dependency functional API) — confirmed distinct, not a rename. |
| jose | 1.x – 6.x | jose | v6 is ESM-only |
| crypto-js | 3.x, 4.x | crypto-js | 4.x needs Node 6+ — its cipher files use bare `let`/`const` with no `"use strict"`, which old V8 (Node 4) rejects outside strict mode |
| sodium-native | 2.x – 5.x | sodium-native | needs node-gyp (python3/make/g++); v3 needs Node 14+ (`fs/promises`) |
| bcrypt | 0.x – 6.x | bcrypt | Native node-gyp binding around OpenBSD bcrypt (password hashing). Prebuilt binaries exist for most current combos, falls back to source compile otherwise. |
| bcryptjs | 1.x – 3.x | bcryptjs | Pure-JS bcrypt, zero native deps — the deliberate pure-JS/native pairing with `bcrypt` above, same pattern as `sodium-native`/`crypto-js`. |
| argon2 | 0.x (never left 0.x) | argon2 | Native node-gyp binding around the Argon2 reference implementation. |
| liboqs-node | 0.1 (only 4 patches ever published) | liboqs-node | The Node equivalent of Go's `liboqs-go`/PHP's `php-liboqs`/.NET's `LibOQS.NET` — see the dedicated note below, this one needed real fixes to even build. |
| @noble/curves | 0.x – 2.x | @noble/curves | v2 is ESM-only |
| @noble/post-quantum | 0.1 – 0.6 | @noble/post-quantum | ML-KEM/ML-DSA/SLH-DSA/FN-DSA; v0.6+ is ESM-only; Node.js equivalent of Go's circl/liboqs-go |

**`liboqs-node` needed three separate real-build fixes, none anticipated from its own README** (same category of finding as PHP's `php-liboqs`). (1) The npm-published tarball is missing its own git submodules (`deps/liboqs`, `deps/liboqs-cpp` — npm doesn't capture submodule content), and no prebuilt binary exists for modern Node ABIs either, so a plain `npm install liboqs-node` fails outright; fixed by git-cloning the repo directly with `--recurse-submodules` in the Dockerfile instead of depending on the npm tarball. (2) The vendored liboqs commit (~2021) fails to compile under GCC 12+ (this project's `node:*-slim` base is Debian bookworm): its old SIKE implementation trips `-Werror=array-parameter`/`-Werror=stringop-overflow`, warning classes added to GCC after that commit was written — fixed by stripping `-Werror` from the vendored liboqs' own CMake files before building (SIKE itself was cryptographically broken and removed from liboqs entirely in 2022, unrelated to this compiler-strictness mismatch). (3) Because of that same old pinned commit, only DRAFT algorithm names are exposed (`Kyber768`, not `ML-KEM-768`) — confirmed live via `oqs.KEMs.getEnabledAlgorithms()`; the touch code uses the draft name for this library specifically, unlike every other language's liboqs binding in this project, which already expose final NIST names. Confirmed working end-to-end after all three fixes: a real ML-KEM-equivalent (Kyber768) keypair generated in a running container.

Node versions span 4–26 (even/LTS majors included; odd majors reference-only) plus 0.10/0.12 (reference-only, pre-io.js-unification era — see the Express 1/2 caveat above for the one place this boundary actually matters for a real, otherwise-buildable combination). Debian base ages out the same way as Go's: jessie (Node <6), stretch (6–12), buster (14–16) all need the `archive.debian.org` redirect — exercised by `sodium-native`'s and `liboqs-node`'s native builds, and now also `bcrypt`'s.

**Build-verification status.** Build-and-run verified in a real container, including a live curl of both endpoints: every new framework (Hapi, Restify, NestJS, Sails, AdonisJS, plus Express 1/2/3 and Koa 1's legacy templates) on at least its most current tracked major, and every new crypto library (tweetnacl, node-jose, bcrypt, bcryptjs, argon2, liboqs-node) paired with at least one framework. Older majors of NestJS (1-10) and AdonisJS (1/2/5) share the same architecture/template as what was verified but weren't independently re-built this pass — flagged in their own notes above, not silently assumed.

#### Known environment quirk: Docker build cache serving stale `COPY app.js` content

Recurring symptom, not a code bug — logged here because it keeps resurfacing and looks like a generator bug at first glance:

```
Error: Cannot find module 'crypto-js'
Require stack:
- /app/app.js
    ...
  code: 'MODULE_NOT_FOUND',
```

The image's `package.json` is correct (lists the right dependency, e.g. `jose`), but the *running container's* `app.js` still contains an unrelated library's content (e.g. `require('crypto-js')`) — a combination the generator can never produce in one `write_context()` call, so it isn't a source-tree bug. Confirmed root cause: Docker (observed with Docker Desktop on Windows/WSL2) reuses a stale cached `COPY app.js .` layer from that image tag's build history while still re-running `COPY package.json .` fresh. `docker build --no-cache` on the exact same context immediately produces the correct file — proving the source is fine and the daemon's layer cache is the problem.

It has recurred even after `docker builder prune`, and even after being fixed once for a given tag it can reappear on a later rebuild of that same tag — this points at something concurrency-related (`manager.py`'s `_do_build` runs up to `workers` parallel `docker build` processes) rather than simple stale-cache-from-long-ago. Not yet root-caused to a specific Docker/BuildKit version bug.

Mitigations, in order of confidence:
1. Restart Docker Desktop before a big rebuild after a template/registry change.
2. Rebuild with "no cache" checked in the dashboard.
3. If it still recurs, drop the build "Workers" option to 1 (serializes `docker build` calls) — untested whether this fully eliminates it, but removes the concurrency that's the leading suspect.

Always verify a fix landed by extracting the file from the *built* image (`docker run --rm --entrypoint cat <tag> /app/app.js`, prefix with `MSYS_NO_PATHCONV=1` in Git Bash so `/app/...` isn't mangled into a Windows path) rather than trusting a green build alone.

### Java (20 lang versions × 6 frameworks × 4 libs × many versions ≈ 5,534 images)

**Javalin added (2026-07-09 density pass)** — a lightweight embedded-Jetty router, no annotations, single `Main.java`. Two distinct routing-API shapes across its tracked majors, not one unchanged surface: 3.x-6.x's `Javalin` class directly implements the routing interface, so `app.get(path, handler)` works immediately after `Javalin.create()` (confirmed via real `javap`/source inspection: `class Javalin implements JavalinDefaultRoutingApi<Javalin>` in 6.0.0). 7.x REMOVED that direct implementation — confirmed the hard way via a real failing docker build (`app.get(...)` throws "cannot find symbol"); routes must now be registered inside the `Javalin.create(cfg -> cfg.routes.get(...))` config consumer instead. Also, 7.x does not bundle a JSON object mapper — `ctx.json(...)` throws a real HTTP 500 at REQUEST time ("It looks like you don't have an object mapper configured"), not build time, unless `jackson-databind` is added as an explicit Maven dependency — the same "passing build proves nothing about runtime" lesson already hit repeatedly for .NET in this project. Both eras (Javalin 6 and 7) confirmed working end-to-end (build + run + curl both endpoints). Majors 3/4 are tracked as real buckets sharing 5/6's confirmed template but weren't independently re-built this pass.

**Dropwizard, Jersey (3 javax/jakarta eras), Spark Java, liboqs-java, and bc-fips were researched but NOT implemented this pass** — deferred due to time, not because of any technical blocker found. Notes from that research, for a future pass: Jersey needs a 3-era split matching this project's existing Quarkus/Vert.x precedent (`com.sun.jersey`/`javax.ws.rs` for 1.x → `org.glassfish.jersey.containers`/`javax.ws.rs` for 2.x → same groupId/`jakarta.ws.rs` for 3.x); Dropwizard needs a heavier `Application`/`Configuration`/`Environment` bootstrap than Javalin but still no parent POM; Spark Java is stalled (last release 2.9.x, ~2020) but should still resolve and build; **liboqs-java is NOT on Maven Central at all** (confirmed: 404 on `repo1.maven.org/maven2/org/openquantumsafe/`) — it would need the same "build from source" treatment as this project's other liboqs bindings, but with an added JNI-native-compile step via its own Maven profile system (`mvn package -P linux/windows`), a bigger lift than the ctypes/ffi-based bindings in other languages; bc-fips is a separate Maven Central artifact (`org.bouncycastle:bc-fips`) from the already-tracked `bcprov-jdk18on`, structurally simple to add but not yet done.

**Density note**: expanded from an original 5-JDK/4-libs-milestone-only design (412 images) on request, to match this project's Go/Python density and give the .NET registry a like-for-like sibling. Two independent axes grew: (1) JDK coverage widened from LTS-only (8/11/17/21/25) to include every non-LTS feature release that still has a live Docker image (18/19/20/22/23/24 — see below for which non-LTS versions do NOT, and why); (2) every crypto library's pinned-milestone buckets were replaced with EVERY real minor release (BouncyCastle 4→15 buckets, Tink 2→23, Conscrypt 2→11), the same "track every meaningful minor, drop the redundant rolling-latest placeholder" pattern applied to .NET's BouncyCastle.Cryptography/NSec.Cryptography/LibOQS.NET.

**JDK version history, verified live against Docker Hub's registry API.** Included: 8, 11, 17, 18, 19, 20, 21, 22, 23, 24, 25 — every version from 18 onward has a live `eclipse-temurin` image today (18/19/20/22/25 as `-jre-jammy`; **23/24 moved to Ubuntu noble as their default base, no `-jre-jammy` variant exists for them at all**, confirmed by listing every tag — handled via `_jdk_os_suffix()` in `lang_java.py`, the runtime stage only, since the `maven:3-eclipse-temurin-{jdk}` **builder** image's own OS-suffixed tags turned out to be only inconsistently published across versions and is left on its bare untagged default instead). Reference-only (`include:false`), no pullable image anywhere: 6, 7 (pre-Temurin, previously documented), and **9, 10, 12, 13, 14, 15, 16** — these predate Eclipse Temurin's own 2021 launch entirely; the deprecated `adoptopenjdk/openjdkN` Docker Hub repos DO still technically have live tags for them, but under a completely different JDK-only (no JRE-slim variant, no bundled-Maven builder image) tag scheme that would need a structurally different generator path — the same category of judgment call as Java 6/7's proprietary-Oracle-JDK dead end, not pursued.

**Real bug found via `docker build`, not anticipated: `maven:3-eclipse-temurin-18` bundles an outlier-old Maven (3.8.6) while every other tracked JDK's image bundles 3.9.x.** Confirmed by running `mvn --version` inside every builder image directly. This matters because Maven's own default plugin-version binding (used whenever a `<plugin>` has no explicit `<version>`) varies by Maven's own version — 3.8.6 falls back to the ancient `maven-compiler-plugin:3.1`, which predates support for the `<maven.compiler.release>` property entirely (added in 3.6+) and silently ignores it, falling back to ITS OWN hardcoded ancient default (source/target 1.5) — which JDK 18's javac has since removed support for, failing with `Source option 5 is no longer supported. Use 7 or later.` even though the pom explicitly requests JDK 18. Frameworks with a `<parent>` that manages plugin versions (Spring Boot) were never affected; Quarkus/Vert.x/Helidon/Micronaut-v1 have no such parent and all needed an explicit `<maven-compiler-plugin>3.13.0</maven-compiler-plugin>` pin (`_COMPILER_PLUGIN` in `lang_java.py`) to stop depending on whatever Maven happens to be bundled. Re-verified end-to-end (Helidon 2, Micronaut 3, Quarkus 3, Vert.x 4, all on JDK 18) after the fix.

**Framework JDK-compatibility ranges were extended from LTS-only to also cover the newly-tracked non-LTS versions**, using two different rules depending on what's actually knowable: frameworks with an explicit stated ceiling in prior research (Spring Boot 2 → "tested up to Java 18"; Quarkus 2 → "conservative, untested above 17") keep that ceiling unchanged. Frameworks with a floor-only requirement and no stated ceiling (Spring Boot 3/4, Quarkus 3, Vert.x 4/5) were extended to every tracked JDK ≥ floor — floor-based inference, not independently re-verified per JDK, flagged as such in each framework's notes. Micronaut and Helidon (both of which deliberately raise their own floor between majors, suggesting real JDK-version sensitivity) use a stricter **lifecycle-window** rule instead: a non-LTS JDK is only added to a major's compatibility if it was actually released during that major's own active-development window (its release date through the date it was superseded) — e.g. Micronaut 3 (active 2021-2023) gains 18/19/20, Micronaut 4 (active 2023-now) gains 22/23/24, not the other way around.

| Framework | Major versions | Maven anchor coordinate | Notes |
|-----------|----------------|--------------------------|-------|
| Spring Boot | 1 (Java 8), 2 (Java 8+, ceiling 18), 3 (Java 17+), 4 (Java 17+) | `org.springframework.boot:spring-boot-starter-parent` | 1.x is ALWAYS suffixed `.RELEASE` (no clean version ever existed on that line) — the resolver's version filter has to explicitly allow that suffix, see below. `spring-boot-starter-web` is deprecated in 4.x in favor of `spring-boot-starter-webmvc` but still resolves and works (verified against Maven Central metadata), so no template change was needed there. |
| Quarkus | 1 (Java 8), 2 (Java 11, ceiling 17), 3 (Java 17+) | `io.quarkus:quarkus-bom` | 1.x/2.x are ALWAYS suffixed `.Final` (same filter issue as Spring Boot 1.x). Dependency coordinates AND the JAX-RS namespace both differ by major (`_quarkus_rest_coord()`/`_quarkus_jaxrs_pkg()` in `lang_java.py`): 1.x/2.x use the classic `quarkus-resteasy` + `quarkus-resteasy-jackson` (both existed since before 1.0, still published today — contrary to an earlier assumption that these were era-exclusive) with `javax.ws.rs`; 3.x uses `quarkus-rest-jackson` with `jakarta.ws.rs`. Quarkus 3's JDK floor jumped mid-line (Java 11 through 3.6, Java 17 from 3.7) — since this registry always resolves to the latest patch, the bucket reflects what's actually installed (17+). |
| Micronaut | 1 (Java 8), 2 (Java 8+), 3 (Java 8+, +18/19/20 lifecycle-window), 4 (Java 17+, +22/23/24 lifecycle-window), 5 (Java 25) | version-dependent, see below | Three distinct parent-POM eras, not two — see below. |
| Vert.x | 2 (Java 8), 3 (Java 8+), 4 (Java 8+, extended to every JDK 8-25), 5 (Java 11+, extended to every JDK 11-25) | `io.vertx:vertx-core` | `vertx-web` added alongside `vertx-core` for real path-based routing (present from 2.x onward too). 1.x is NOT tracked — see the structural-exclusion note below. |
| Helidon | 1 (Java 8, SE), 2 (Java 11+, SE, +18/19 lifecycle-window), 3 (Java 17+, SE, +20), 4 (Java 21+, SE, +22/23/24) | `io.helidon.webserver:helidon-webserver` | SE (functional/programmatic) throughout, not MP (JAX-RS/CDI). Three incompatible WebServer API shapes across these majors — see below. |

| Crypto library | Version buckets | Maven coordinate | Notes |
|-----------------|------------------|-------------------|-------|
| JCA | built-in | — | `java.security`/`javax.crypto`, no dependency. |
| BouncyCastle | `1.70` (legacy) + every real minor `1.71`-`1.84` (15 buckets total) | `org.bouncycastle:bcprov-jdk18on` (`bcprov-jdk15on` only for the `1.70` bucket) | Expanded from 4 pinned milestones to every minor — dates confirmed via search.maven.org for 1.71-1.80 and repo1.maven.org's Last-Modified header for 1.81-1.84 (solr's well-documented lag topped out exactly at 1.80, matching this registry's own prior finding). Draft PQC (Kyber/Dilithium) support spans 1.72-1.78; final NIST names (ML-KEM/ML-DSA/SLH-DSA) span 1.79-1.84 — intermediate buckets inherit the nearest verified milestone's support level rather than being independently jar-diffed one by one. |
| Tink | Every real minor `1.0`-`1.22` (23 buckets total) | `com.google.crypto.tink:tink` | Expanded from a milestone+rolling-latest design to every minor, which also surfaces the exact Java-8-drop point (1.19.0) as a visible transition instead of a single prose fact. `1.21.0` added ML-DSA-87 — Tink's first post-quantum release. Still no ML-KEM/Kyber (KEM-side) as of the latest (`1.22`) — signatures only. |
| Conscrypt | `1.0`-`1.4` (legacy line) + `2.0`,`2.1`,`2.2`,`2.4`,`2.5` (current line; **no `2.3` was ever released**, confirmed via Maven metadata) — 10 buckets total, `2.6` is a reference-only row (alpha-only) | `org.conscrypt:conscrypt-openjdk-uber` | Native (glibc-linked) — paired only with the Ubuntu/glibc `-jammy`/`-noble` temurin tags, never Alpine. **Neither `1.4` (1.4.2) nor `2.5` (2.5.2) bundles ARM64/aarch64 native libs** (checked jar contents directly) — only the still-prerelease 2.6-alpha5 adds those; the other newly-added buckets are presumed to share this gap (same lineage) but weren't independently jar-inspected. Not an issue on this project's amd64 builds, but would break on an Apple Silicon or ARM Docker host. |

**Docker base images**: `maven:3-eclipse-temurin-{jdk}` (builder stage — bundles Maven + the matching JDK, avoids apt-installing Maven into a bare temurin image; always the bare untagged form, see the compiler-plugin bug above for why) → `eclipse-temurin:{jdk}-jre-jammy` (runtime stage; `-jre-noble` for JDK 23/24 specifically). eclipse-temurin has **no `-slim` tags at all** (that's a Debian/apt convention from the old, unmaintained `openjdk` Docker Official Image — never adopted by Adoptium's replacement); every Java image in this project uses the Ubuntu/glibc `-jammy`/`-noble` variant, never Alpine.

**JDK floor is 8, not an arbitrary scope cut.** Java 6 and 7 are listed in the registry as reference-only (`include: false`) rather than omitted outright, matching Node's treatment of 0.10/0.12 — but unlike Node's old base images (still pullable via an `archive.debian.org` redirect), there is **no Docker image at all, live or archived**, for Java 6/7: `eclipse-temurin` (Adoptium) never built JDK 6/7 in the first place (its own baseline starts at 8), and the older, now-deprecated `openjdk` Docker Official Image *did* historically ship 6/7/8, but those exact tags 404 today — verified directly against the Docker Hub registry API, not inferred. No archive mirror or redirect fixes this the way it did for Node/Go. Resurrecting them would mean self-building from an archived, proprietary Oracle JDK 6/7 distribution requiring license acceptance — a fundamentally different (and much riskier) category of effort than anything else in this project, so it wasn't pursued.

**Every framework major documented on Maven Central is now tracked** — the original deliberate scope/time cut (only Spring 2/3, Micronaut 3/4, Vert.x 4/5, Helidon 3/4) has been removed and every older/newer major added, each with its own researched-and-verified JDK floor. The one remaining exclusion is **structural, not a scope cut**: Vert.x 1.x lived under a completely different, hyphenated Maven groupId (`org.vert-x`), only 4 releases ever, predating the `io.vertx:vertx-core` lineage entirely — not the same kind of gap as everything else.

**Crypto library version buckets now include deliberately old-but-still-buildable lines, plus pinned post-quantum milestones** — the original "one bucket per lib" scope (justified at the time because BouncyCastle/Tink structurally never had a 2.0 release) has been expanded on request: BouncyCastle's dead pre-rename `bcprov-jdk15on` (frozen at 1.70) and Conscrypt's legacy 1.x line (up to 1.4.2) are both still resolvable from Maven Central today (confirmed live, HTTP 200) and are now tracked specifically because they're old-but-buildable, not because they're structurally distinct. Separately, BouncyCastle and Tink each gained a pinned milestone bucket at the version where post-quantum algorithm support first landed — see below.

**Version-qualifier filter used to exclude entire majors, not just prereleases — a real bug, not a hypothetical.** The Maven version filter originally required a bare `x.y.z` (no suffix at all), which works for Spring Boot 2.x+/Quarkus 3.3+ but not for **Spring Boot 1.x, which is ALWAYS suffixed `.RELEASE`** (no clean version ever existed on that line) or **Quarkus 1.x/2.x, which are ALWAYS suffixed `.Final`** — confirmed live against Maven Central metadata. The old filter returned zero candidates for these majors ("not resolvable"), caught only once they were actually added and generation was run. Fixed by extending the filter to accept an optional trailing `.RELEASE`/`.Final`/`.GA` (case-insensitive) while still rejecting genuine prereleases (Alpha/Beta/CR/RC/M<n>/SNAPSHOT).

**Helidon's WebServer API has three incompatible shapes across the tracked majors, not one break at 3.x.** Verified against real code snippets in Helidon's own docs at each version's branch: 1.x uses the static factory `WebServer.create(routing)` with a pre-built `Routing.builder()....build()`, blocking startup via a raw `CompletableFuture`; 2.x also uses `WebServer.create(...)` but takes an *un-built* `Routing.Builder` directly, blocking via Helidon's own `Single.await()`; 3.x/4.x use the fluent `WebServer.builder().routing(...).build().start()` shape. Implemented as three separate Main.java templates dispatched by major (`_helidon_main_tpl()` in `lang_java.py`).

**Micronaut's parent-POM situation has three distinct eras, not two.** `io.micronaut:micronaut-parent` has ZERO 1.x releases (starts at 2.0.0) — Micronaut 1.x projects used `io.micronaut:micronaut-bom` via `<dependencyManagement>` import instead of a `<parent>` at all, which also means annotation processing (`micronaut-inject-java`) needs manual `annotationProcessorPaths` wiring (normally free from the parent for every other major). 2.x uses `io.micronaut:micronaut-parent` (same groupId as 3.x — a third, separate case from 4.x/5.x's `io.micronaut.platform:micronaut-parent`, whose own latest 4.x version, 4.10.16, is *not* lockstep with `micronaut-core`'s 4.10.25 — asking Maven for the wrong version under the wrong groupId fails outright). Also, `micronaut-serde-jackson` has zero 1.x/2.x releases (its own versioning starts 2022-03, after Micronaut 3.0) — those majors rely on Jackson support bundled directly in `micronaut-http`/`micronaut-core` instead. See `_micronaut_parent_coord()` / `_pom_micronaut_v1()` in `lang_java.py`.

**BouncyCastle's post-quantum milestones were confirmed by diffing actual jar contents, not just reading release notes.** Draft Kyber/Dilithium first appear in `bcprov-jdk18on` **1.72** (2022, flagged experimental); the FINAL NIST-standardized ML-KEM/ML-DSA/SLH-DSA (FIPS 203/204/205) land together in **1.79** (2024) — 1.78 has none of the new parameter-spec classes, 1.79 introduces full provider implementations for all three while keeping the old draft names side-by-side. BC is actively sunsetting those draft names starting at 1.84, so the "1.79" and "1" (rolling latest) buckets will diverge more over time. **Tink's post-quantum support is a correction to an earlier assumption in this project**, not a new finding about Tink itself: v1.21.0 added Tink's first PQC support (ML-DSA-87 signatures) — verified by full-text search of every Tink Java release body (v1.8.0–v1.20.0 have zero PQC mentions). An earlier pass through this registry claimed Tink had no PQC support at all; that was true when written and is no longer true.

**Version resolution uses `repo1.maven.org/.../maven-metadata.xml` directly, not `search.maven.org`'s solr index** — verified live that the solr index lags Maven Central's real repository by months to over a year in places (e.g. it topped out at `bcprov-jdk18on` 1.80 while 1.84 had long been published). The generated `maven-metadata.xml` under `repo1.maven.org` is Maven Central's own index and is authoritative.

**Runtime version reading uses a plain baked-in `versions.properties` resource, not a framework API**, unlike Python/Go/Node's "read the actually-installed artifact's own metadata" pattern. Every framework-specific API for this turned out to have a sharp edge once checked: Quarkus ships an empty `MANIFEST.MF` since 3.1.0.Final; a shaded/uber jar merges every dependency's manifest into one, so `Package.getImplementationVersion()` on a class from an arbitrary dependency returns `null`; and each framework exposes its version through a different, framework-specific API. Sidestepped entirely: Maven pins an *exact* version per dependency (no npm-style range resolution), so whatever this generator resolves is exactly what gets installed. The resolved framework/library version strings are baked directly into a plain `versions.properties` file at generation time and read back identically across all five frameworks.

**Signed dependencies (BouncyCastle) break `maven-shade-plugin` uber-jars unless signature files are stripped.** Found via a real failing run, not anticipated: shading merges a signed dependency's jar into the uber-jar without updating that jar's now-stale `META-INF/*.SF`/`.DSA`/`.RSA` signature files, and the JVM refuses to load *any* class from the result — `SecurityException: Invalid signature file digest for Manifest main attributes`, thrown before `main()` even runs. Fixes any framework using the shared `_SHADE_PLUGIN` block (Micronaut, Vert.x, Helidon) paired with BouncyCastle. Fixed by adding a shade-plugin `<filters>` block excluding those three file patterns for every artifact (`*:*`) — this is required whenever shading a signed jar, not specific to BouncyCastle, so it's applied unconditionally rather than only when BouncyCastle is the selected library.

**Micronaut + Tink: `NoClassDefFoundError` on a protobuf class, fixed defensively without a fully confirmed root cause.** `com/google/protobuf/RuntimeVersion$RuntimeDomain` was missing at runtime. Ruled out: the class missing from the protobuf-java version Tink itself requires (downloaded the actual jar, it's present in 4.33.0); Micronaut's parent/BOM pinning an older protobuf-java via inherited `dependencyManagement` (read both POMs directly, no such entry). Spring Boot + Tink already worked, narrowing this to Micronaut/shading specifically, but without Maven/Docker access to run `dependency:tree` the exact mechanism wasn't pinned down. Fixed by having `_lib_dependency_xml()` explicitly declare `com.google.protobuf:protobuf-java` as a direct dependency whenever Tink is selected, pinned to whatever version Tink's own pom.xml requests (fetched dynamically, not hardcoded) — a direct declaration always wins Maven's resolution regardless of what's happening deeper in the tree, closing the gap even without confirming exactly where the older version was coming from.

**Micronaut 1.x/2.x's HTTP server never actually starts under `maven-shade-plugin` — another clean-looking log that wasn't actually a pass.** Micronaut 2.5.13 logged zero exceptions, then quietly: `No bean candidates found for type: interface io.micronaut.runtime.EmbeddedApplication` / `No embedded container found. Running as CLI application`. Confirmed root cause by downloading and inspecting the real jars: `micronaut-http-server-netty` on 2.x registers its beans via the plain `java.util.ServiceLoader` convention — one shared text file, `META-INF/services/io.micronaut.inject.BeanDefinitionReference`, listing multiple class names one per line. Shading multiple jars that each carry their own copy of that exact path silently keeps only the LAST one seen instead of merging them, so Netty's own bean registration gets clobbered. Confirmed this is version-specific, not a Micronaut-wide trait: `micronaut-http-server-netty` on 4.x uses a completely different, shading-safe scheme (each bean gets its own uniquely-named file under `META-INF/micronaut/<interface>/`, nothing to clobber) — which is exactly why this didn't show up when 3.x/4.x were verified earlier. Fixed by adding `ServicesResourceTransformer` to the shared `_SHADE_PLUGIN` block, applied uniformly to Micronaut/Vert.x/Helidon (harmless no-op for the newer scheme, and generally good practice for any shaded jar).

**Helidon 1.x/2.x's WebServer.create() defaults to a random port if not configured — a real test failure, not a hypothetical.** The V1/V2 templates originally called `WebServer.create(routing)` with no port argument, copying the "just start it" shape from research without checking what port that binds to. The container started cleanly with no exceptions (`Helidon SE 2.6.14 ... Channel '@default' started: [id: ..., L:/0:0:0:0:0:0:0:0:41635]`) — but bound to port **41635**, not 8000, so the test harness (which only checks the container's published port 8000) could never reach it and the test failed despite a clean startup log. A successful-looking startup log is not sufficient evidence a Helidon SE app works — check what port it actually bound. Fixed by explicitly passing `WebServer.create(ServerConfiguration.builder().port(8000), routing)` in both V1 and V2 (confirmed via Helidon's own source on GitHub that this overload and `ServerConfiguration.builder().port(int)` exist in both 1.x and 2.x, though `ServerConfiguration.Builder` is marked deprecated-in-favor-of-`WebServer.Builder` starting 2.0.0 — still functional, just not the "new" idiom).

**Build-verification status — not everything below has been independently confirmed with a real `docker build` yet.** Build-and-run verified in a real container, including a live curl of both endpoints: Spring Boot 2/3, Quarkus 3, Vert.x 4/5, Helidon 3/4, BC `1`, Tink `1`, Conscrypt `2` (JCA plus spot-checked crypto libs on Spring Boot); **all Micronaut majors (1/2/3/4/5) confirmed by the user — full build+test pass across the whole Micronaut matrix**, including the three fixes above (BOM-only pom for 1.x, `ServicesResourceTransformer` for 1.x/2.x bean discovery, the explicit protobuf-java pin for Tink) all landing correctly together. Also verified as part of the density/JDK expansion: Spring Boot 3 on JDK 23 (confirms the `-noble` runtime-tag dispatch), BC `1.75`, Tink `1.10`, Conscrypt `2.1` on their respective new buckets, and — after finding and fixing the `maven:3-eclipse-temurin-18` outlier-Maven-version bug documented above — Helidon 2, Micronaut 3, Quarkus 3, and Vert.x 4 all specifically on JDK 18. Still **research-based but not yet independently build-tested**: Spring Boot 1/4, Quarkus 1/2, Vert.x 2/3, Helidon 1/2, most of the newly-added individual BC/Tink/Conscrypt minor buckets beyond the four spot-checked above, and every non-LTS JDK combination not explicitly listed here. If any of these fail to build, check for the same class of issue first: a framework lacking parent-managed plugin versions paired with a JDK whose bundled Maven image happens to be an outlier — the shared plumbing otherwise (Dockerfile, `versions.properties` mechanism, tag sanitization) is unchanged from the already-proven set.

### .NET (13 lang versions × 5 frameworks × 5 libs × many versions = 825 images)

**Density note**: originally built with a "pinned milestone" approach (a handful of significant versions per library, e.g. only 4 BouncyCastle buckets) rather than tracking every real release the way Go/Python do. Expanded on request to track every meaningful major/minor across all 5 crypto libraries (e.g. BouncyCastle.Cryptography now has 8 buckets, one per real 2.x minor, instead of 4 milestones) — see the crypto-library table below for the current buckets. **Historical/excluded versions are now recorded as explicit reference rows, not silently omitted** — every framework and library below lists real, NuGet-confirmed prior majors that predate what's actively tracked (e.g. Carter 3-5 before the 6.0 API rewrite, ServiceStack 2-4 before its 5.0 cross-platform baseline, NancyFx's own 0.x/1.x lines), each with `compatibility:[]` (frameworks) or `available:false` (libraries) so they show up in the dashboard's reference tables and generate zero images — the same pattern Java uses for JDK 6/7. At the language level, `4.0` (a number .NET Core deliberately never used, to avoid confusion with .NET Framework 4.x) and `11.0` (current preview, not yet GA) are both explicit `include:false` rows; **.NET Framework (1.0-4.8.1)**, the entirely separate Windows-only predecessor product line, is documented in a `_comment_net_framework` note rather than as individual version rows, since its version numbers would collide with .NET Core's own early numbering in the same array — it's structurally out of scope (Windows-container-only, no Linux Docker image ever existed) rather than a scope cut.

| Framework | Major versions | NuGet package | Notes |
|-----------|----------------|----------------|-------|
| ASP.NET Core | built-in, versioned lockstep with the runtime (1.1–10.0) | — | Two structurally different hosting-model templates, dispatched by era: 1.1–5.0 use the classic split `Startup.cs` + `Program.cs` with `new WebHostBuilder().UseKestrel().UseStartup<Startup>()` (NOT `WebHost.CreateDefaultBuilder` — that convenience helper didn't exist until 2.0); 6.0–10.0 use the unified top-level-statement `Program.cs` with real Minimal APIs (`app.MapGet(...)`, introduced in .NET 6). `Microsoft.NET.Sdk.Web` only started *implicitly* pulling in the ASP.NET Core shared framework at 3.0 — 1.1 needs explicit `PackageReference`s to `Microsoft.AspNetCore.Hosting` + `Microsoft.AspNetCore.Server.Kestrel`, and 2.1/2.2 need a single `<PackageReference Include="Microsoft.AspNetCore.App" />` **with no `Version` attribute** (a metapackage with "special versioning semantics handled outside of NuGet" per Microsoft's own docs) — both found via real failing builds, not anticipated. |
| Carter | 6–10 (3, 4, 5 are reference-only — pre-`ICarterModule` rewrite) | `Carter` | Single-TFM-pinned per major (6.0.0→net6.0 only, ... 10.0.0→net10.0 only, no multi-targeting) — compatibility is an exact one-to-one match with the .NET version, not a range. Uses ASP.NET Core's own endpoint routing (`IEndpointRouteBuilder.MapGet` lives in `Microsoft.AspNetCore.Builder`, easy to miss the `using` for and get a confusing `IRouteBuilder`-overload error instead — found via a real failing build). |
| FastEndpoints | 5, 6, 7, 8 (1–4 are reference-only — pre-stabilization churn era) | `FastEndpoints` | Major `5` was a long-lived (2022–2025) net6.0-only line; 6/7/8 dropped net6.0 and multi-target net8.0/9.0/10.0 only. `EndpointWithoutRequest<TResponse>` + `HttpContext.Response.WriteAsync(...)` needs `Microsoft.AspNetCore.Http` explicitly imported (the extension method isn't reachable via the `FastEndpoints` namespace alone) — found via a real failing build. |
| NancyFx | 2 (`Nancy` 2.0.0, 2019-04-27) — 0 (pre-1.0) and 1 (1.x, indexer-syntax routing) are reference-only | `Nancy` | Deliberately legacy/abandoned (GitHub archived 2021), included for old-but-buildable breadth like BC's `1.70`/Conscrypt's `1` buckets. Hosted via the OWIN bridge (`Microsoft.AspNetCore.Owin` + `Nancy.Owin`, `app.UseOwin(x => x.UseNancy())`) rather than Nancy's own raw-`HttpListener` self-host, specifically so it reuses the same Kestrel/Dockerfile/port setup as every other framework here. Passed its FIRST build attempt, but a later real `docker run` crashed on every request with `Synchronous operations are disallowed` — Nancy's `TextResponse` writes its body via a synchronous `Stream.Write` baked into its pre-async/await Response model, and Kestrel disallows that by default since ASP.NET Core 3.0. Fixed with `builder.WebHost.ConfigureKestrel(o => o.AllowSynchronousIO = true)`; re-verified end-to-end with a live curl. **Correction**: an earlier note here claimed 1.4.4 was "never republished to NuGet after 2.0.0" — false, 1.4.4 AND an even newer 1.4.5 (2018-09-26) are both genuinely on NuGet; 2.0.0 is still what's tracked because it has the netstandard2.0 leg a Linux container needs, not because it's the only option. |
| ServiceStack | 5, 6, 8, 10 (2, 3, 4 are reference-only — Windows/.NET-Framework-only, predate cross-platform 5.0) | `ServiceStack` | TFM groups are ADDITIVE across majors (confirmed via NuGet nuspec inspection) — every tracked major restores against any SDK from net6.0 through net10.0. Free tier caps at 10 Request DTOs; irrelevant here (only 2 routes ever defined). Passed its FIRST build attempt, but a later real `docker run` crashed on startup with `RestPath '/' on Type 'RootRequest' is not Valid` — ServiceStack's `[Route]` path-matching splits on `/` and drops empty segments, so a bare `"/"` always has zero segments and can never be `IsValid`, structurally, regardless of attribute config. Fixed by routing `"/"` through a plain ASP.NET Core Minimal API endpoint registered before `app.UseServiceStack(...)`, leaving only `"/version"` on a ServiceStack `[Route]`-attributed DTO (`HttpResult(json, "application/json")` for the raw custom-content-type response); re-verified end-to-end with a live curl. |

| Crypto library | Version buckets | NuGet package | Notes |
|-----------------|------------------|-----------------|-------|
| System.Security.Cryptography | built-in | — | Classical BCL crypto (`SHA256.Create()`), available unchanged 1.1–10.0. |
| System.Security.Cryptography.PQC | built-in, gated to .NET 10 only | — | Tracked as a SEPARATE built-in entry from classical crypto, mirroring Go's `crypto` vs `crypto/mlkem` split. `MLKem`/`MLDsa` (FIPS 203/204) landed inbox and GA (non-experimental) in .NET 10; `SlhDsa` (FIPS 205) remains `[Experimental("SYSLIB5006")]`. **Empirically confirmed in a real container**: .NET 10's own Ubuntu-noble-based image ships OpenSSL 3.0.13, not the 3.5+ that `MLKemOpenSsl` needs on Linux — `MLKem.IsSupported` returns `false` in this project's own generated .NET 10 containers today. The touch code checks `IsSupported` and catches `PlatformNotSupportedException` defensively (mirrors Java Tink's try/catch), so the app still builds and serves both endpoints correctly regardless — verified by actually running the container and curling it. |
| BouncyCastle.Cryptography | `1.9` (legacy `Portable.BouncyCastle`), `2.0`-`2.6` (every real 2.x minor) | `Portable.BouncyCastle` (only for `1.9`) / `BouncyCastle.Cryptography` | Expanded from a 4-bucket milestone design to every real minor (8 buckets total) — bc-csharp's own PQC milestones (2.0 draft, 2.5 final) land on different version numbers and a different timeline than bc-java's (1.72/1.79) despite being the same project family — don't assume the two languages' buckets correspond to the same date. bc-csharp's 2.5.0 REMOVED draft Kyber/SIKE in the same release that added final ML-KEM/ML-DSA/SLH-DSA — a sharper break than bc-java, which kept draft names side-by-side for years after 1.79. `2.7` (still in beta as of writing) is a reference-only row. |
| NSec.Cryptography | `18`,`19`,`20`,`22`,`24`,`25`,`26` (every year with a real stable release; `21`/`23` never shipped one — reference-only) | `NSec.Cryptography` | libsodium wrapper, calendar-year versioning; the native `libsodium` binary is pulled automatically via a transitive NuGet dependency — no `apt-get` needed. Classical-only (Ed25519/X25519/AES-GCM/ChaCha20-Poly1305), no PQC. Compatibility per bucket derived from each version's own nuspec TFM groups (netstandard1.1 → netstandard2.0/2.1 → net5.0 → net6.0-only → net8.0-only → net9.0-only, floor rises each era). |
| LibOQS.NET | `0.1`,`0.2`,`0.3` (`0.4` is an rc-only reference row) | `LibOQS.NET` (+ `LibOQS.NET.Native`, transitive) | The .NET equivalent of Go's `liboqs-go` / Node's `@noble/post-quantum` — full liboqs algorithm surface (ML-KEM, ML-DSA, SLH-DSA, Falcon). **Important distinction verified live via the NuGet API**: this is an unrelated, independently-versioned COMMUNITY package (github.com/filipw/maybe-liboqs-dotnet) that happens to share its name and similar version numbers with the OFFICIAL `open-quantum-safe/liboqs-dotnet` project, which was never published to NuGet at all and was archived/discontinued 2025-01-06 with a "not recommended" notice. Young (~2K downloads), pre-1.0 — included for PQC-research breadth, not production maturity. **Two real runtime bugs found via a live `docker run` after this was first reported build-verified** (a passing build says nothing about a native P/Invoke dependency actually loading — same lesson as Java's Helidon port-binding gotcha): (1) a framework-dependent `dotnet publish` with no `RuntimeIdentifier` never copies the package's real `runtimes/linux-x64/native/liboqs.so` into the output — fixed with explicit `RuntimeIdentifier=linux-x64` + `SelfContained=false` in the csproj; (2) the prebuilt binary needs glibc ≥2.34, which Debian bullseye (.NET 6.0/7.0's default base) doesn't have — fixed by switching to the `-bookworm-slim` tag suffix *only* for this library on 6.0/7.0 (confirmed to exist via the MCR API), leaving every other 6.0/7.0 combo on its default tag. Re-verified end-to-end (both endpoints curled, liboqs actually initialized) on 6.0, 7.0, 8.0, and 10.0. |

**Docker image repository path moved twice, verified live against the MCR v2 registry API.** `microsoft/dotnet` (old Docker Hub repo, 1.0–2.2 era) → `mcr.microsoft.com/dotnet/core/{sdk,aspnet}` (2.0–3.1 era) → the unified `mcr.microsoft.com/dotnet/{sdk,aspnet}` (2.1+, backfilled). **1.1, 2.2, and 3.0 were never migrated to the unified repo** — their SDK/aspnet images exist ONLY under the old `dotnet/core/...` path (confirmed 404 on the new path for both 2.2 and 3.0). **.NET Core 1.0 and 2.0 have NO pullable Docker image at all** (confirmed via the MCR tag-list API) — smaller than Java's 6/7 gap, but the same class of finding. Every version from 1.1 through 10.0 is genuinely pullable and buildable today.

**No crypto library in this matrix needs `apt-get`, a first for this project.** BouncyCastle.Cryptography is pure managed C#; NSec.Cryptography and LibOQS.NET both bundle their native binaries as ordinary NuGet package assets restored automatically. This sidesteps the recurring Debian/Ubuntu archive-mirror EOL mitigation this project needed repeatedly for old Go/Node/Java base images — confirmed empirically by successfully building on .NET 1.1's jessie-based image with zero apt involvement.

**`dotnet publish` does not implicitly restore on old SDKs — a real, cascading build failure, not a hypothetical.** The .NET Core 1.1 SDK (1.1.14) fails with "Assets file '/src/obj/project.assets.json' not found" followed by dozens of unrelated-looking `CS0518`/`CS0246` errors (even `System.String` itself reported as "not defined") when `dotnet publish` is run without a prior `dotnet restore` — modern SDKs (roughly 2.x onward) restore implicitly during publish/build, 1.1 does not. Fixed by adding an explicit `RUN dotnet restore` before `RUN dotnet publish` in every generated Dockerfile, harmless on newer SDKs where it's a no-op.

**Runtime version reading bakes resolved versions into a generated `Versions.cs` static class**, the same reasoning Java applied to `versions.properties`: NuGet, like Maven, pins an *exact* version per `PackageReference` (no npm-style range resolution), so whatever this generator resolves is exactly what gets installed — no "did the resolver actually give me what I asked for" uncertainty to solve at runtime. JSON responses are hand-built via a shared `Json.cs` helper (`Esc`/`Obj`, mirroring Java Helidon's `esc()`/`obj()`) rather than any JSON library, sidestepping the `System.Text.Json` (inbox only from .NET Core 3.0 onward) vs `Newtonsoft.Json` (needed pre-3.0) split entirely.

**Build-verification status.** Build-and-run verified in a real container, including a live `curl` of both endpoints, for at least one combo per framework: ASP.NET Core on 1.1/2.1/2.2/3.1 (legacy hosting, all three package-reference eras), 8.0/10.0 (modern hosting, incl. the PQC-gated built-in library — confirmed `MLKem.IsSupported == false` in-container as documented above), Carter on 10 with BouncyCastle `2.5`, FastEndpoints on 5 (net6.0) and 8 (net10.0) with NSec, NancyFx 2 with BouncyCastle `1.9` (Owin-bridge hosting, after the Kestrel fix below), ServiceStack 8 with LibOQS.NET `0.3` (after the routing fix below), and LibOQS.NET on ASP.NET Core across 6.0/7.0/8.0/10.0 (after the two runtime fixes documented above). Every other combination in the matrix shares this same template/plumbing per framework, so an untested combination's risk is concentrated in whichever specific library/version branch it exercises, not the shared Dockerfile/csproj/JSON-helper machinery.

**Two more real runtime-only bugs, both reported by the user after this project had already called their frameworks "build-verified" from a `docker build`-only check — reinforcing the LibOQS.NET lesson below, not a one-off.** (1) **NancyFx**: `Nancy.Responses.TextResponse` (what the `Response res = someString;` implicit conversion produces) writes its body via a synchronous `Stream.Write` call baked into Nancy's pre-async/await Response model; Kestrel disallows synchronous response-stream I/O by default since ASP.NET Core 3.0, so EVERY request crashed with `Synchronous operations are disallowed`. There is no way to make `TextResponse` itself async — fixed with the standard remedy for legacy sync-writing OWIN middleware under Kestrel, `builder.WebHost.ConfigureKestrel(o => o.AllowSynchronousIO = true)`. (2) **ServiceStack**: `[Route("/")]` throws `RestPath '/' on Type 'RootRequest' is not Valid` at startup — read ServiceStack's own source (`RestPath.cs`) to find the real cause: a route path is split into segments with empty entries removed, and `IsValid` requires at least one non-empty segment, so a bare `"/"` is structurally always invalid, not fixable via any attribute configuration. Fixed by routing `"/"` through a plain ASP.NET Core Minimal API endpoint registered before `app.UseServiceStack(...)`, leaving ServiceStack to handle only `"/version"`. Both fixed and re-verified end-to-end with a live curl of both endpoints.

**Lesson, now reinforced three times in this one language's history (LibOQS.NET, NancyFx, ServiceStack) — a passing `docker build` proves the managed code compiles, nothing more.** It says nothing about a native dependency loading, a synchronous-I/O restriction, or a route-registration rule being satisfied at runtime. Every framework/library combination in this registry now has at least one combo actually `docker run` + curled on both endpoints, not just built — do the same for any future addition before calling it verified.

### PHP (17 lang versions × 3 frameworks × 5 libs × many versions ≈ 1,097 images)

**Built with full density from the first pass**, per the "apply from the start" convention below — no separate retrofit pass was needed, unlike Java/.NET.

**PHP version history, verified live via real `docker pull`/`docker run` attempts, not the tag-list API alone.** Included: 5.6, 7.0-7.4, 8.0-8.5. `php:5.3-cli`/`php:5.4-cli` are schema-1 manifests modern Docker refuses to pull (same class of failure as Go's earliest tags); `php:5.5-cli`'s manifest is valid but its layer blob is missing/corrupted on the registry (same class of failure as `golang:1.5`) — 5.6 is the practical floor, not an arbitrary cut. Reference-only (`include:false`): 6.0 (the abandoned PHP 6 project — numbering jumped 5.6→7.0, no image ever existed) and 8.6 (still RC as of writing).

| Framework | Major versions | Packagist package | Notes |
|-----------|----------------|--------------------|-------|
| Laravel | 4-13 (1-3 are reference-only — pre-Composer-package era) | `laravel/framework` | Component-only metapackage, no bundled skeleton (the real `laravel/laravel` package assumes `composer create-project` bootstrapping) — exercised via `Illuminate\Http\JsonResponse` directly with manual URI branching instead of idiomatic routing, the same "no skeleton available" pattern as .NET's legacy ASP.NET Core middleware. |
| Symfony | 2-8 (1 is reference-only — pre-Composer-package era) | `symfony/symfony` | Same component-only/no-skeleton situation as Laravel — exercised via `Symfony\Component\HttpFoundation\JsonResponse` directly. |
| Slim | 1-4 | `slim/slim` | **Four incompatible routing-API shapes, not one break at 3.x/4.x** — see below. |

| Crypto library | Version buckets | Packagist package | Notes |
|-----------------|------------------|---------------------|-------|
| openssl | built-in ext | — | `openssl_digest()` touch call. |
| sodium | built-in ext, PHP 7.2+ only | — | `sodium_crypto_generichash()` touch call. |
| sodium_compat | `1`, `2` | `paragonie/sodium_compat` | Touch calls `\ParagonIE_Sodium_Compat::crypto_generichash()` directly (not the global `sodium_crypto_generichash()` function name) to guarantee the polyfill itself is exercised even on a PHP version where the native `sodium` extension is also present. |
| phpseclib | `1`, `2`, `3` | `phpseclib/phpseclib` | **Namespace changed at every major** — see below. |
| php-liboqs | `0.4` | `secudoc/php-liboqs` | Native C extension wrapping liboqs — see below. |

**Slim's routing API has four incompatible shapes across its tracked majors, found by an actual `docker run` failure, not anticipated from docs.** 1.x uses the bare GLOBAL `Slim` class with NO namespace at all (`new \Slim()`) — confirmed by inspecting `vendor/slim/slim/Slim/Slim.php` directly after a namespaced `\Slim\Slim()` call (assumed correct, copied from 2.x's real shape) threw `Class 'Slim\Slim' not found` in a live container; 1.x also has no `response()->headers` bag available before a route dispatches (`response()` returns null at that point) — the real API is the app's own `$app->contentType(...)` method. 2.x is the same no-request/response-param closure shape but properly namespaced `\Slim\Slim`, with `response()->headers->set(...)` working as expected once a route is actually dispatched. 3.x uses `new \Slim\App()` with PSR-7-ish request/response objects passed into the closure (`$response->getBody()->write(...)`, `->withHeader(...)`), using Slim's own bundled `Http\Response` implementation. 4.x uses `AppFactory::create()` with true PSR-7 — but bare `slim/slim` alone has NO bundled PSR-7 implementation and throws `Could not detect any PSR-17 ResponseFactory implementation` at runtime (confirmed via a live container) unless `slim/psr7` is added as an explicit second Composer dependency, which this project's generator does automatically whenever the framework major is Slim 4.

**phpseclib's Random API has a different shape at every major, not just a namespace rename.** 1.x's `Crypt/Random.php` declares NO class at all — just a bare global function `crypt_random_string()`, autoloaded via Composer's `autoload.files` (not `psr-0`/`psr-4`) — confirmed after an assumed `\Crypt_Random::string()` static call threw `Class "Crypt_Random" not found` in a live container. 2.x introduced the `\phpseclib\Crypt\Random::string()` static method; 3.x kept the same method but renamed the namespace to `\phpseclib3\`.

**Laravel 4 × phpseclib is a genuine, unresolvable dependency conflict, not a convenience exclusion — confirmed via both a real `composer install` failure and the Packagist metadata itself.** `laravel/framework`'s own `composer.json` hard-requires `phpseclib/phpseclib: 0.3.*` on every single v4.x tag checked (v4.1.6 through v4.2.22, no exceptions) — none of this registry's three tracked phpseclib buckets resolve to a 0.3.x release (bucket `1` is the still-3.x/4.x-only "legacy" branch, latest patch 1.0.30), so Composer always fails with "requires phpseclib 0.3.* ... but it conflicts with your root composer.json require" for every bucket, not just one. Since Composer cannot install two versions of the same package in one tree, this combo (all 3 phpseclib buckets × all 12 tracked PHP versions, 36 image contexts) is skipped entirely in `lang_php.py`'s `_INCOMPATIBLE_COMBOS`, with a `[SKIP]` log line explaining why — the same "genuine technical impossibility" bar as every other exclusion in this project, not a "needs different tooling" shortcut.

**php-liboqs (native extension) needed three separate real-build fixes, none anticipated from its own README.** (1) The README claims a liboqs "0.14.0 or newer" floor, but liboqs 0.14.0 is actually missing `OQS_KEM_encaps_derand` — confirmed both by grepping the real tagged `src/kem/kem.h` (absent at 0.14.0, present at 0.15.0) and by an actual `docker build` compile failure (`implicit declaration of function`); fixed by pinning liboqs 0.15.0 instead (the latest real stable tag — 0.16.0 only exists as an `-rc1` prerelease). (2) Packagist lists `secudoc/php-liboqs` as `type: php-ext` (a `replace: {ext-oqs: '*'}` marker package with no PHP source to autoload) — a plain `composer install` cannot build it at all, confirmed via `these were not loaded, likely because it conflicts with another require`; fixed by building the extension directly via `phpize`/`make install` in the Dockerfile and leaving it OUT of `composer.json`'s `require` entirely, with only its Packagist version number resolved for `/version` reporting. (3) Once built and enabled, it's genuinely functional — confirmed live: `\OQS\KEM::keypair(\OQS\KEM::ALG_ML_KEM_768)` returns a real ML-KEM-768 keypair in a running container. liboqs already uses the final NIST names (ML-KEM/ML-DSA/SLH-DSA) at 0.15.0 — php-liboqs, unlike BouncyCastle/NSec, has no draft-naming period of its own to track.

**Composer's own modern defaults blocked THREE separate things needed to build old-but-intentionally-tracked packages — all found via real `docker build` failures, not anticipated.** (1) **Packagist has fully sunset its Composer-1.x-compatible metadata protocol** — `composer:1`'s `composer install` fails to resolve ANY package at all ("could not be found in any version"), not a PHP-version-specific failure; fixed by using the `composer:2.2` LTS tag instead for pre-7.2 PHP (5.6/7.0/7.1) — Composer 2.3+ itself suggests this exact fallback when run under old PHP, and 2.2 still speaks the modern Packagist API. (2) **Composer 2.4+ blocks installing any package flagged by a Packagist security advisory** by default (hit on `phpseclib/phpseclib` 1.0.30, PKSA-mnsd-qtjt-pgcq) — since this project deliberately builds old/vulnerable library versions on purpose (that's the actual research goal), fixed with `composer install --no-security-blocking`, a flag that only exists on the plain `composer:2` tag, NOT on the `2.2` LTS branch used for old PHP (confirmed: a hard "option does not exist" error there). (3) **Composer 2.2+ blocks any third-party Composer plugin** by default unless allow-listed (hit via `kylekatarnls/update-helper`, a transitive Carbon 1.x dependency pulled in by Laravel 4) — fixed by setting `config.allow-plugins: true` in every generated `composer.json`, a blanket build-time-only trust decision consistent with (2).

**Debian archive-mirror EOL fix needed for PHP's two oldest bases, verified live.** `php:5.6-cli`/`php:7.0-cli` run on Stretch, `php:7.1-cli`/`php:7.2-cli` on Buster — both confirmed dropped from live `deb.debian.org`/`security.debian.org` mirrors via a direct `apt-get update` 404, both fixed with the same `archive.debian.org` sources.list redirect + `-o Acquire::Check-Valid-Until=false` + `--allow-unauthenticated` pattern already established for Go/Node's own EOL bases. `php:7.3-cli`/`php:8.0-cli` (Bullseye) were confirmed STILL LIVE via a real `apt-get update` — no fix needed there.

**Runtime version reading bakes resolved Composer package versions into a generated `versions.php` file**, the same reasoning as Java's `versions.properties`/`.NET`'s `Versions.cs`: Composer, like Maven/NuGet, pins an exact version per `require` entry (once installed, no npm-style range resolution at runtime) — and critically, old PHP's Composer 1.x-era tooling has no equivalent of `Composer\InstalledVersions` (a Composer 2.0+-only runtime API), so baking at generation time is the only approach that works uniformly across this project's whole PHP version range, not just the simplest one. PHP's built-in `json_encode()` means no hand-rolled JSON-building helper is needed anywhere (unlike Java/.NET).

**Build-verification status.** Build-and-run verified in a real container, including a live curl of both endpoints: Slim 1/2/3/4 (all four routing-API shapes, catching both the namespace bug and the PSR-7 bug above), Laravel 4 (on PHP 5.6, catching the Composer-plugin-block bug above) and Laravel 12, Symfony 7, phpseclib 1/2/3 (catching the Random-API bug above), sodium_compat, sodium (built-in), and php-liboqs (catching all three fixes above, including a live ML-KEM-768 keypair generation). Every combination shares this same composer.json/Dockerfile/versions.php plumbing per framework/library, so an untested combination's risk is concentrated in whichever specific library/version branch it exercises, not the shared machinery.

---

## What still needs to be added

The following **common languages** are not yet covered. For each, add:
1. `scripts/registry {lang}.json`
2. `scripts/lang_{lang}.py` (or `.js`, `.rb`, etc.) implementing the four render functions
3. A case in `generate_images.py` to dispatch to the new module

### Priority languages and their typical stacks

#### Rust
- **Lang versions**: 1.70, 1.75, 1.80, 1.85 (stable releases)
- **Frameworks**: Axum (0.7, 0.8), Actix-web (4.x), Warp (0.3), Rocket (0.5)
- **Crypto libs**: `ring` (0.17), `RustCrypto/crypto` (various crates), `openssl` (0.10), `aws-lc-rs` (1.x), `dalek-cryptography` (ed25519-dalek 2.x)
- **App file**: `main.rs`
- **Deps file**: `Cargo.toml`
- **Base image**: `rust:{version}-slim` (builder) → `debian:slim` (runtime)

#### Ruby
- **Lang versions**: 3.1, 3.2, 3.3, 3.4
- **Frameworks**: Rails API (7.x, 8.x), Sinatra (3.x, 4.x), Grape (2.x), Hanami (2.x)
- **Crypto libs**: `openssl` (built-in), `rbnacl` (7.x), `ruby_rncryptor` (3.x), `jwt` (2.x)
- **App file**: `app.rb`
- **Deps file**: `Gemfile`
- **Base image**: `ruby:{version}-slim`

#### Kotlin / JVM
- Similar stack to Java but with Kotlin-native frameworks:
- **Frameworks**: Ktor (2.x, 3.x), Spring Boot (3.x with Kotlin DSL)
- **Crypto libs**: same JVM libs as Java + `KotlinCrypto` libraries

#### Swift
- **Lang versions**: 5.9, 5.10, 6.0
- **Frameworks**: Vapor (4.x), Hummingbird (2.x)
- **Crypto libs**: `swift-crypto` (3.x), `CryptoKit` (built-in)
- **Base image**: `swift:{version}`

---

## Conventions to follow when adding a new language

### File structure per image
```
images/{lang}/{lang_ver}/{Framework}/{fw_major}/{CryptoLib}/{lib_ver}/
├── Dockerfile
├── {entrypoint}          # app.js / Main.java / main.rs / Program.cs / app.rb / index.php
└── {manifest}            # package.json / pom.xml / Cargo.toml / app.csproj / Gemfile / composer.json
```

### Dockerfile pattern
1. **Multi-stage** where possible: compile/build in a fat image, copy artefact to a minimal runtime image. This was skipped for Python/PHP/Node's first passes and only retrofitted 2026-07-11 once ~276GB of built images made the cost obvious — **for any new language, decide this up front, not as a later retrofit**: check whether the Dockerfile installs a compiler/build toolchain (gcc, cmake, node-gyp's python3/make/g++, a JDK, a full SDK, etc.) that the RUNNING app never needs, and if so, split into a `builder` stage (keeps the toolchain, does the install/compile) and a final stage that starts fresh from the same base image and only `COPY --from=builder`s the built artefact (installed packages, a compiled binary, a JAR). Verify with a real `docker build+run` per combo (does it still start and serve `/version` correctly) AND measure the actual size delta (`docker images <tag> --format {{.Size}}` before/after) before rolling it out — don't assume it helps or is even needed (PHP's one compiling combo only shrank ~14%, since the base image's own weight dominated there, versus Python's universal ~70-73%). Also confirm multi-stage doesn't measurably slow the build itself (timed A/B on this project's own Python case: no measurable difference, since the expensive install/compile step is identical in both stages and the final stage's `COPY --from=builder` is cheap).
2. Install system deps before app deps for better layer caching
3. `EXPOSE 8000` always
4. `CMD` runs the app on `0.0.0.0:8000`

### App pattern
Both endpoints must return **JSON** (`Content-Type: application/json`):

```
GET /       → {"message": "Hello World"}

GET /version → {
  "language":  {"name": "...", "version": "exact runtime version string"},
  "framework": {"name": "...", "version": "exact installed version string"},
  "library":   {"name": "...", "version": "exact installed version string"}
}
```

The version strings must be **runtime-detected** (not hardcoded) wherever possible so that the actual installed artefact version is reported.

### Compatibility rules in registry JSON
- Document which library versions require which minimum/maximum language version
- Document OS-level dependencies (e.g., `libsodium-dev`, `swig`, native binaries)
- Document which base image variant is needed (e.g., Bullseye for older OpenSSL)

### Version resolution
- Prefer resolving concrete patch versions at generate-time by querying the package registry (PyPI, crates.io, npmjs, pkg.go.dev, rubygems, packagist, NuGet, Maven Central)
- Support `x` as a wildcard major (e.g., `1.x` → latest 1.y.z)
- Skip versions that fail to install on the target language version

### Version density — apply from the start, don't retrofit later
Java and .NET were both originally built with a "pinned milestone" design (a handful of significant versions per library/framework instead of every real release) and had to be expanded to real density in a separate follow-up pass once compared against Go/Python's much denser matrices (412 → 4,419 images for Java; 306 → 825 for .NET). **Any new language added from now on should get this density on the first pass, not as a later retrofit:**
- **Language versions**: track every version that still has a genuinely pullable Docker image — not just LTS-equivalent releases. Verify image availability live per version (registry tag-list API), don't assume based on a version's age or "long-term support" label alone.
- **Crypto libraries**: track every real minor release with a meaningfully distinct version number, not just 3-4 hand-picked milestones. A library's own minor-version bumps are usually real, dated, distinct releases (new algorithms, security fixes, API changes) — collapsing a decade of them into "draft PQC" / "final PQC" / "latest" undercounts real history and hides exactly the kind of migration-path data this project's crypto-agility mission cares about. Drop any "rolling latest" placeholder bucket once every real minor is tracked individually — it becomes redundant, and this project's convention (matching Go/Python) is literal version numbers that get updated as new releases ship, not "whatever is newest at generation time."
- **Frameworks**: track every major version that ever existed and is still resolvable from the package registry, not just the ones currently in common use. Extend each major's language-version compatibility to cover every language version it can plausibly run on — respecting any specific, already-researched ceiling (e.g. "tested up to X, unverified beyond"), and otherwise using floor-based inference (documented as inference, not verified fact) or, for frameworks whose own version-numbering deliberately raises its floor between majors (a sign of real version sensitivity), a stricter lifecycle-window rule: only extend to a newer language version if it was released during that major's own active-development window.
- **Historical/excluded versions must be recorded as explicit reference entries, never silently omitted** — for every axis (language, framework, library). Use `include:false` for language versions, `"compatibility": []` for framework versions, `"available": false` for library versions (all three keep the entry visible in the dashboard's reference tables while producing zero generated images), each with a genuine, researched reason in the notes — not a guess. This is the same pattern Java already used for JDK 6/7; apply it everywhere from the start rather than only adding it retroactively when asked.
- **Verify every one of the above claims live** (registry tag-list APIs, package-registry version-history endpoints), the same rigor as the rest of this document — a version-density pass done from guesses instead of verification will need the exact same "actually check" correction this project has already had to apply twice.

### Other subsystems that need extending too — not just the registry/generator
Adding a new language isn't done once the registry JSON and image generator support it — several other subsystems are hardcoded per-language and silently do nothing for a language they don't know about, rather than erroring:
- **New-versions scanner (`scripts/check_updates.py`)**: needs a new enumerator in `_ENUMERATORS` (see `_enumerate_registry_module`/`_enumerate_python`) plus a new `fetch_kind` branch in `_fetch()` **and** `_fetch_date()`, backed by a `_fetch_releases`/`_release_date` pair in the new language's own `lang_X.py` (see `lang_python.py`/`lang_node.py`/`lang_php.py` for languages where the release date rides along in the same version-list response for free, or `lang_java.py`/`lang_dotnet.py` for the pattern to follow when the upstream registry's version-list endpoint carries no per-version dates at all and a small supplementary request — only for a newly-detected major's one resolved version, never the whole history — is needed instead). Skipping this doesn't error, it just means the new language's frameworks/libraries never surface in the "🔔 New versions" badge at all.
- **Client-side fingerprinting (`CONTEXT_CLIENTS.md`)**: currently Python-only (`scripts/lang_python_client.py` + `scripts/generate_client_images.py`). A new server-side language does NOT automatically get client images — that needs its own `lang_X_client.py` (one-shot outbound-call generator per crypto library, following the Python client templates' pattern) wired into `generate_client_images.py`, plus registry entries under that language's `http_clients` section.

---

## Running the system

```bash
# Generate all image contexts for a language
python scripts/generate_images.py --lang python
python scripts/generate_images.py --lang node
python scripts/generate_images.py --lang java
python scripts/generate_images.py --lang dotnet
python scripts/generate_images.py --lang php
# Go was dropped from the project (2026-07-11); its registry/lang_go.py were
# moved to scripts/archive/ rather than deleted, in case it's ever revived.

# ── CLI (manager.py) ──────────────────────────────────────────────────────────

# Build a filtered subset
python manager.py --build --language python --framework Flask --library cryptography --library-version 44.0

# Test a subset (hits / and /version, checks HTTP 200 + JSON shape)
python manager.py --test --language go --framework Gin

# List all generated image paths
python manager.py --list --language python

# ── Web dashboard ─────────────────────────────────────────────────────────────

# Start the dashboard (auto-initialises the database on first run)
python dashboard.py
# → http://localhost:5050

# Re-load registry + re-sync images from the UI or via API:
curl -X POST http://localhost:5050/api/init

# Manually re-sync images (after adding new image contexts):
curl -X POST http://localhost:5050/api/sync
```

---

## Key design decisions

| Decision | Rationale |
|----------|-----------|
| Each combination is its own directory | Allows independent build, run, and analysis per artefact |
| Minimal apps (only / and /version) | Reduces noise for side-channel analysis; the crypto library is imported but not necessarily called |
| Runtime version detection | Ensures the reported version matches what is actually installed, not what was requested |
| Multi-stage Go builds | Produces a ~5 MB scratch image; avoids Go toolchain overhead in runtime analysis |
| Python Bullseye base for old crypto | cryptography < 36 and M2Crypto require OpenSSL 1.x which is only in Bullseye |
| Registry JSON as single source of truth | All version ranges and compat rules are centralised; generation is purely mechanical |
| SQLite for state (`pqc_manager.db`) | Zero-infrastructure persistence for build/test results, run labels, and ignore lists; WAL mode for concurrent dashboard reads + worker writes |
| `image_details` view | Flat join used by all dashboard queries — keeps query code simple and all column names consistent |
| SSE for live job output | Allows the dashboard to stream log lines from long-running build/test jobs without polling; connection kept alive with `: keepalive` comments every 25 s |
| `stop_event` per job | Lets the dashboard cancel a build/test loop cleanly after the current image, without killing the thread |
| Run labels | Groups build and test results under a named session so users can compare successive runs or filter reports to a specific experiment |
