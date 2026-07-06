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
│   └── node/
│       └── {lang_version}/
│           └── {Framework}/
│               └── {fw_major}/
│                   └── {CryptoLib}/
│                       └── {lib_version}/
│                           ├── Dockerfile
│                           ├── app.js
│                           └── package.json
│
├── scripts/
│   ├── generate_images.py          # Entry point: reads registry → writes images/
│   ├── lang_python.py              # Python-specific templates + PyPI version resolver
│   ├── lang_go.py                  # Go-specific templates + Go module version resolver
│   ├── lang_node.py                # Node.js-specific templates + npm registry version resolver
│   ├── registry python.json        # Python matrix: lang versions, frameworks, libs, compat rules
│   ├── registry go.json            # Go matrix: lang versions, frameworks, libs, compat rules
│   └── registry node.json          # Node matrix: lang versions, frameworks, libs, compat rules
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
db.get_or_create_run(name)            # Returns run_id
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

---

## What is already covered

### Python (6 lang versions × ~10 frameworks × ~5 libs × many versions ≈ 2,668 images)

| Framework | Major versions | Notes |
|-----------|---------------|-------|
| Flask | 0, 1, 2, 3 | |
| Django | 1, 2, 3, 4, 5 | |
| FastAPI | 0 | requires uvicorn |

| Crypto library | Version range | Notes |
|----------------|--------------|-------|
| cryptography | 2.0 – 44.0 | needs Bullseye base for < 36 |
| PyNaCl | 0.x – 1.x | needs libsodium-dev |
| PyCryptodome | 3.x | |
| M2Crypto | 0.26 – 0.38 | needs swig |
| hashlib | built-in | |

### Go (12 lang versions × ~9 frameworks × ~7 libs × many versions ≈ 19,713 images)

| Framework | Major versions | Module path |
|-----------|---------------|-------------|
| Beego | 1, 2 | github.com/astaxie/beego, github.com/beego/beego/v2 |
| Echo | 1–5 | github.com/labstack/echo/v{N} |
| Fiber | 1–3 | github.com/gofiber/fiber/v{N} |
| Chi | 1–5 | github.com/go-chi/chi/v{N} |
| Gin | 1 | github.com/gin-gonic/gin |
| Gorilla/mux | 1 | github.com/gorilla/mux |
| Iris | 10–12 | github.com/kataras/iris/v12 |
| httprouter | 1 | github.com/julienschmidt/httprouter |
| net/http | built-in | |

| Crypto library | Version range | Module path |
|----------------|--------------|-------------|
| x/crypto | 0.x – 0.38 | golang.org/x/crypto |
| circl | 1.0 – 1.4 | github.com/cloudflare/circl |
| liboqs-go | 1.0 – 2.0 | github.com/open-quantum-safe/liboqs-go |
| mlkem768 | 0.x – 1.x | filippo.io/mlkem768 |
| tink-go | 1.x, 2.x | github.com/google/tink/go, tink.dev/go/tink |
| crypto | built-in | |
| crypto/mlkem | built-in (Go ≥ 1.24) | |

### Node.js (12 lang versions × 3 frameworks × 7 libs × many versions ≈ 1,319 images)

| Framework | Major versions | npm package | Notes |
|-----------|---------------|--------------|-------|
| Express | 4, 5 | express | 5.x requires Node 18+ |
| Fastify | 1 (Node 8+), 2–5 | fastify | `.listen({port,host}, cb)` object form works across all majors. Handlers avoid `async` (Node <7.6 can't parse it) and call `reply.send()` explicitly instead of `return`ing a value — `return` as the response only works for async/Promise handlers; a plain sync handler that `return`s just hangs forever with no error (verified against a real container). Major 1's dependency tree also deterministically fails `npm install` on Node 6 (npm@3 `.staging` bug) — narrowed to Node 8+ in the registry. |
| Koa | 2 (Node 8+), 3 | koa | Sync middleware (sets `ctx.body`, no `async`) for the same Node <7.6 reason — safe because `koa-compose` wraps every middleware call in `Promise.resolve()` regardless of whether it's async. 1.x (generator-based) out of scope. Major 2's dependency tree (`http-errors`) also uses object destructuring that old V8 can't parse — `require("koa")` itself throws `SyntaxError: Unexpected token {` on Node 6, works from Node 8. Narrowed to Node 8+. |

| Crypto library | Version range | npm package | Notes |
|----------------|--------------|-------------|-------|
| crypto | built-in | — | Node standard library |
| node-forge | 0.1 – 1.4 | node-forge | |
| jose | 1.x – 6.x | jose | v6 is ESM-only |
| crypto-js | 3.x, 4.x | crypto-js | 4.x needs Node 6+ — its cipher files use bare `let`/`const` with no `"use strict"`, which old V8 (Node 4) rejects outside strict mode |
| sodium-native | 2.x – 5.x | sodium-native | needs node-gyp (python3/make/g++); v3 needs Node 14+ (`fs/promises`) |
| @noble/curves | 0.x – 2.x | @noble/curves | v2 is ESM-only |
| @noble/post-quantum | 0.1 – 0.6 | @noble/post-quantum | ML-KEM/ML-DSA/SLH-DSA/FN-DSA; v0.6+ is ESM-only; Node.js equivalent of Go's circl/liboqs-go |

Node versions span 4–26 (even/LTS majors included; odd majors reference-only). Debian base ages out the same way as Go's: jessie (Node <6), stretch (6–12), buster (14–16) all need the `archive.debian.org` redirect — only exercised by `sodium-native`'s native build, since every other library is pure JS with no system deps.

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

---

## What still needs to be added

The following **common languages** are not yet covered. For each, add:
1. `scripts/registry {lang}.json`
2. `scripts/lang_{lang}.py` (or `.js`, `.rb`, etc.) implementing the four render functions
3. A case in `generate_images.py` to dispatch to the new module

### Priority languages and their typical stacks

#### Java
- **Lang versions**: 11, 17, 21, 24 (LTS versions)
- **Frameworks**: Spring Boot (3.x), Quarkus (3.x), Micronaut (4.x), Vert.x (4.x), Helidon (4.x)
- **Crypto libs**: `Bouncy Castle` (1.7x), `Google Tink Java` (1.x), `JCA/JCE` (built-in), `conscrypt` (2.x)
- **App file**: `Main.java` (or Maven/Gradle project)
- **Deps file**: `pom.xml` or `build.gradle`
- **Base image**: `eclipse-temurin:{version}-slim` or `amazoncorretto:{version}`

#### Rust
- **Lang versions**: 1.70, 1.75, 1.80, 1.85 (stable releases)
- **Frameworks**: Axum (0.7, 0.8), Actix-web (4.x), Warp (0.3), Rocket (0.5)
- **Crypto libs**: `ring` (0.17), `RustCrypto/crypto` (various crates), `openssl` (0.10), `aws-lc-rs` (1.x), `dalek-cryptography` (ed25519-dalek 2.x)
- **App file**: `main.rs`
- **Deps file**: `Cargo.toml`
- **Base image**: `rust:{version}-slim` (builder) → `debian:slim` (runtime)

#### C# / .NET
- **Lang versions**: .NET 6, 8, 9, 10
- **Frameworks**: ASP.NET Core (6, 8, 9), Minimal APIs (built-in since .NET 6)
- **Crypto libs**: `System.Security.Cryptography` (built-in), `Bouncy Castle C#` (2.x), `NSec` (0.23), `libsodium-net` (2.x)
- **App file**: `Program.cs`
- **Deps file**: `app.csproj`
- **Base image**: `mcr.microsoft.com/dotnet/sdk:{version}` (builder) → `mcr.microsoft.com/dotnet/aspnet:{version}` (runtime)

#### Ruby
- **Lang versions**: 3.1, 3.2, 3.3, 3.4
- **Frameworks**: Rails API (7.x, 8.x), Sinatra (3.x, 4.x), Grape (2.x), Hanami (2.x)
- **Crypto libs**: `openssl` (built-in), `rbnacl` (7.x), `ruby_rncryptor` (3.x), `jwt` (2.x)
- **App file**: `app.rb`
- **Deps file**: `Gemfile`
- **Base image**: `ruby:{version}-slim`

#### PHP
- **Lang versions**: 8.1, 8.2, 8.3, 8.4
- **Frameworks**: Laravel (10, 11), Symfony (6, 7), Slim (4), Lumen (10)
- **Crypto libs**: `openssl` (built-in ext), `libsodium` (built-in ext), `paragonie/sodium_compat` (1.x), `phpseclib` (3.x)
- **App file**: `index.php`
- **Deps file**: `composer.json`
- **Base image**: `php:{version}-cli` or `php:{version}-apache`

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
1. **Multi-stage** where possible: compile/build in a fat image, copy artefact to a minimal runtime image
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

---

## Running the system

```bash
# Generate all image contexts for a language
python scripts/generate_images.py --lang python
python scripts/generate_images.py --lang go
python scripts/generate_images.py --lang node

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
