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
│   └── java/
│       └── {jdk_version}/
│           └── {Framework}/
│               └── {fw_major}/
│                   └── {CryptoLib}/
│                       └── {lib_version}/
│                           ├── Dockerfile
│                           ├── pom.xml
│                           └── src/main/
│                               ├── java/app/Main.java
│                               └── resources/versions.properties (+ application.properties)
│
├── scripts/
│   ├── generate_images.py          # Entry point: reads registry → writes images/
│   ├── lang_python.py              # Python-specific templates + PyPI version resolver
│   ├── lang_go.py                  # Go-specific templates + Go module version resolver
│   ├── lang_node.py                # Node.js-specific templates + npm registry version resolver
│   ├── lang_java.py                # Java-specific templates + Maven Central version resolver
│   ├── registry python.json        # Python matrix: lang versions, frameworks, libs, compat rules
│   ├── registry go.json            # Go matrix: lang versions, frameworks, libs, compat rules
│   ├── registry node.json          # Node matrix: lang versions, frameworks, libs, compat rules
│   └── registry java.json          # Java matrix: lang versions, frameworks, libs, compat rules
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

### Java (5 lang versions × 5 frameworks × 4 libs × many versions ≈ 412 images)

| Framework | Major versions | Maven anchor coordinate | Notes |
|-----------|----------------|--------------------------|-------|
| Spring Boot | 1 (Java 8), 2 (Java 8+), 3 (Java 17+), 4 (Java 17+) | `org.springframework.boot:spring-boot-starter-parent` | 1.x is ALWAYS suffixed `.RELEASE` (no clean version ever existed on that line) — the resolver's version filter has to explicitly allow that suffix, see below. `spring-boot-starter-web` is deprecated in 4.x in favor of `spring-boot-starter-webmvc` but still resolves and works (verified against Maven Central metadata), so no template change was needed there. |
| Quarkus | 1 (Java 8), 2 (Java 11), 3 (Java 17+) | `io.quarkus:quarkus-bom` | 1.x/2.x are ALWAYS suffixed `.Final` (same filter issue as Spring Boot 1.x). Dependency coordinates AND the JAX-RS namespace both differ by major (`_quarkus_rest_coord()`/`_quarkus_jaxrs_pkg()` in `lang_java.py`): 1.x/2.x use the classic `quarkus-resteasy` + `quarkus-resteasy-jackson` (both existed since before 1.0, still published today — contrary to an earlier assumption that these were era-exclusive) with `javax.ws.rs`; 3.x uses `quarkus-rest-jackson` with `jakarta.ws.rs`. Quarkus 3's JDK floor jumped mid-line (Java 11 through 3.6, Java 17 from 3.7) — since this registry always resolves to the latest patch, the bucket reflects what's actually installed (17+). |
| Micronaut | 1 (Java 8), 2 (Java 8+), 3 (Java 8+), 4 (Java 17+), 5 (Java 25) | version-dependent, see below | Three distinct parent-POM eras, not two — see below. |
| Vert.x | 2 (Java 8), 3 (Java 8+), 4 (Java 8+), 5 (Java 11+) | `io.vertx:vertx-core` | `vertx-web` added alongside `vertx-core` for real path-based routing (present from 2.x onward too). 1.x is NOT tracked — see the structural-exclusion note below. |
| Helidon | 1 (Java 8, SE), 2 (Java 11+, SE), 3 (Java 17+, SE), 4 (Java 21+, SE) | `io.helidon.webserver:helidon-webserver` | SE (functional/programmatic) throughout, not MP (JAX-RS/CDI). Three incompatible WebServer API shapes across these majors — see below. |

| Crypto library | Version buckets | Maven coordinate | Notes |
|-----------------|------------------|-------------------|-------|
| JCA | built-in | — | `java.security`/`javax.crypto`, no dependency. |
| BouncyCastle | `1.70` (legacy), `1.72` (draft PQC), `1.79` (final PQC names), `1` (rolling latest) | `org.bouncycastle:bcprov-jdk18on` (`bcprov-jdk15on` only for the `1.70` bucket) | Four pinned milestones instead of "latest of the only line that ever existed" — see the PQC-milestone note below. |
| Tink | `1.21` (first PQC), `1` (rolling latest) | `com.google.crypto.tink:tink` | `1.21.0` added ML-DSA-87 — Tink's first post-quantum release. Still no ML-KEM/Kyber (KEM-side) as of the latest (1.22.0) — signatures only. |
| Conscrypt | `1` (legacy, up to 1.4.2), `2` (current stable) | `org.conscrypt:conscrypt-openjdk-uber` | Native (glibc-linked) — paired only with the Ubuntu/glibc `-jammy` temurin tag, never Alpine. Latest published version overall is a prerelease (`2.6-alpha5`); the resolver filters qualifier-suffixed versions. **Neither tracked line bundles ARM64/aarch64 native libs** (checked jar contents directly for both 1.4.2 and 2.5.2) — only the still-prerelease 2.6-alpha5 adds those; not an issue on this project's amd64 builds, but would break on an Apple Silicon or ARM Docker host. |

**Docker base images**: `maven:3-eclipse-temurin-{jdk}` (builder stage — bundles Maven + the matching JDK, avoids apt-installing Maven into a bare temurin image) → `eclipse-temurin:{jdk}-jre-jammy` (runtime stage). eclipse-temurin has **no `-slim` tags at all** (that's a Debian/apt convention from the old, unmaintained `openjdk` Docker Official Image — never adopted by Adoptium's replacement); every Java image in this project uses the Ubuntu/glibc `-jammy` variant, never Alpine.

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

**Build-verification status — not everything below has been independently confirmed with a real `docker build` yet.** Build-and-run verified in a real container: Spring Boot 2/3, Quarkus 3, Vert.x 4/5, Helidon 3/4, BC `1`, Tink `1`, Conscrypt `2` (JCA plus spot-checked crypto libs on Spring Boot); **all Micronaut majors (1/2/3/4/5) confirmed by the user — full build+test pass across the whole Micronaut matrix**, including the three fixes above (BOM-only pom for 1.x, `ServicesResourceTransformer` for 1.x/2.x bean discovery, the explicit protobuf-java pin for Tink) all landing correctly together. Still **research-based but not yet independently build-tested**: Spring Boot 1/4, Quarkus 1/2, Vert.x 2/3, Helidon 1/2, BC `1.70`/`1.72`/`1.79`, Tink `1.21`, Conscrypt `1`. If any of these fail to build, the error is most likely in the specific version-dependent branch just described for that framework/lib — the shared plumbing (Dockerfile, `versions.properties` mechanism, tag sanitization) is unchanged from the already-proven set.

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
python scripts/generate_images.py --lang java

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
