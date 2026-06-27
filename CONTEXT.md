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
│   └── go/
│       └── {lang_version}/
│           └── {Framework}/
│               └── {fw_major}/
│                   └── {CryptoLib}/
│                       └── {lib_version}/
│                           ├── Dockerfile
│                           ├── main.go
│                           └── go.mod
│
├── scripts/
│   ├── generate_images.py          # Entry point: reads registry → writes images/
│   ├── lang_python.py              # Python-specific templates + PyPI version resolver
│   ├── lang_go.py                  # Go-specific templates + Go module version resolver
│   ├── registry python.json        # Python matrix: lang versions, frameworks, libs, compat rules
│   └── registry go.json            # Go matrix: lang versions, frameworks, libs, compat rules
│
├── manager.py                      # CLI: build / run / test / remove Docker images
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

---

## What still needs to be added

The following **common languages** are not yet covered. For each, add:
1. `scripts/registry {lang}.json`
2. `scripts/lang_{lang}.py` (or `.js`, `.rb`, etc.) implementing the four render functions
3. A case in `generate_images.py` to dispatch to the new module

### Priority languages and their typical stacks

#### Node.js / JavaScript
- **Lang versions**: 18, 20, 22, 23 (LTS + current)
- **Frameworks**: Express (4, 5), Fastify (4, 5), Koa (2), Hapi (21), NestJS (10)
- **Crypto libs**: `node:crypto` (built-in), `node-forge` (1.x), `jose` (5.x), `crypto-js` (4.x), `sodium-native` (4.x), `noble/curves` (1.x)
- **App file**: `app.js` or `app.mjs`
- **Deps file**: `package.json`
- **Base image**: `node:{version}-slim`

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

# Build a filtered subset
python manager.py --build --language python --framework Flask --library cryptography --library-version 44.0

# Test a subset (hits / and /version, checks HTTP 200 + JSON shape)
python manager.py --test --language go --framework Gin

# List all generated image paths
python manager.py --list --language python
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
