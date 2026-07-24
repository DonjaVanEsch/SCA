"""
Rust-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_rust").

Required exports:
    LANGUAGE_ID   - str
    REGISTRY_FILE - str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool
"""

import json
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "rust"
REGISTRY_FILE = "registry rust.json"


class CratesIoLookupError(Exception):
    """Raised when a crates.io fetch fails for a network/rate-limit reason
    -- deliberately distinct from _resolve() returning None for a crate/
    version actually checked and confirmed absent. Same bug class as every
    other language's *LookupError in this project (Java's
    MavenLookupError, .NET's NuGetLookupError, Node's NpmLookupError,
    Python's PyPiLookupError, PHP's PackagistLookupError): conflating the
    two used to make write_context() delete existing output on a
    transient failure (confirmed live for Java: a run during sustained
    Maven Central 429s wiped every Java image context on disk). Callers
    must not delete existing output on this exception."""


# ── crates.io version resolution ─────────────────────────────────────────────
# crates.io's REST API (crates.io/api/v1/crates/{name}/versions) lists every
# published version with its own `created_at` and `rust_version` (MSRV, when
# the publisher declared one) -- the direct analog of Maven's
# maven-metadata.xml / NuGet's flatcontainer index / Packagist's p2 API.
# crates.io's own API etiquette REQUIRES a descriptive, identifying
# User-Agent (unlike Packagist, which tolerates a generic one) -- confirmed
# via crates.io's own crawler policy page; using a bare default urllib
# User-Agent risks being blocked, not just a courtesy.
_CRATES_IO_UA = "pqc-sca-research/1.0 (+https://github.com/DonjaVanEsch/SCA)"

_CRATES_VERSIONS: dict = {}
_CRATES_RELEASE_DATES: dict = {}

# Cargo/SemVer prereleases use a literal hyphen (-rc.1, -beta.2, -dev) --
# same convention as NuGet/Packagist, a "no hyphen" regex is sufficient.
_STABLE_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _fetch_crates_versions(crate: str) -> list:
    """Raises CratesIoLookupError on a network/rate-limit failure -- does
    NOT cache that as "zero versions found" (see CratesIoLookupError's
    docstring)."""
    if crate in _CRATES_VERSIONS:
        return _CRATES_VERSIONS[crate]

    url = f"https://crates.io/api/v1/crates/{crate}/versions"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _CRATES_IO_UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        raw = data.get("versions", [])
        stable = [
            (e["num"], e.get("created_at", "")[:10])
            for e in raw
            if not e.get("yanked") and _STABLE_RE.match(e["num"])
        ]
        versions = sorted((v for v, _ in stable), key=_ver_key)
        _CRATES_RELEASE_DATES[crate] = {v: d for v, d in stable if d}
    except (URLError, OSError, ValueError, KeyError) as exc:
        raise CratesIoLookupError(f"{crate}: {exc}") from exc

    _CRATES_VERSIONS[crate] = versions
    return versions


def _release_date(crate: str, version: str) -> str | None:
    """release_date for one already-known version, e.g. for a newly
    detected major -- reuses _fetch_crates_versions()'s cache, no extra
    request."""
    try:
        _fetch_crates_versions(crate)
    except CratesIoLookupError:
        return None
    return _CRATES_RELEASE_DATES.get(crate, {}).get(version)


def _resolve(crate: str, registry_ver: str) -> str | None:
    """Resolve a registry version like '0.10' or '2' to the latest matching
    stable release on crates.io (e.g. '0.10' -> '0.10.76', '2' -> '2.14.0')."""
    versions = _fetch_crates_versions(crate)

    prefix = registry_ver + "."
    candidates = [v for v in versions if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in versions:
        return registry_ver

    return None


# ── Rust toolchain / Debian codename handling ────────────────────────────────
# Debian codename per rustc minor. bullseye-starting-at-1.55 and
# buster-through-1.54 are CONFIRMED live via
# `docker run rust:X.Y-slim cat /etc/os-release` across the whole 1.40-1.55
# transition window -- not estimated. The stretch/buster cutover itself
# (somewhere in 1.31-1.39) was NOT pinned as precisely -- 1.38/1.39 is a
# reasonable estimate from the confirmed "buster by 1.40" data point, not a
# per-version-verified fact; if a build in that narrow range hits an apt
# codename mismatch, correct the threshold below rather than assume it's a
# different bug.
def _rustc_minor(rust_ver: str) -> int:
    return int(rust_ver.split(".")[1])


# Full codename mapping (not just an EOL/current split) -- confirmed live
# via `docker run rust:X.Y-slim cat /etc/os-release`: bullseye starts
# exactly at 1.55, bookworm by 1.69 (both confirmed live). trixie's start
# (between 1.85 absent and 1.90 present per the research pass) and the
# stretch/buster cutover (~1.38/1.39) are reasonable estimates, not
# individually verified for every minor -- see _debian_archive_apt's
# docstring. This mapping matters for MORE than just the archive-mirror fix:
# the runtime stage's base image must match the SAME codename the builder
# stage's rust:X.Y-slim tag actually resolves to, or a dynamically-linked
# crate (openssl, sodiumoxide) can link against a DIFFERENT glibc/libssl
# than the runtime image ships -- the same class of bug already found for
# .NET's LibOQS.NET (a real GLIBC-version runtime crash caught via a real
# docker run, not a build failure).
def _debian_codename(rust_ver: str) -> str:
    minor = _rustc_minor(rust_ver)
    if minor <= 38:
        return "stretch"
    if minor <= 54:
        return "buster"
    if minor <= 68:
        return "bullseye"
    if minor <= 89:
        return "bookworm"
    return "trixie"


def _debian_archive_apt(rust_ver: str) -> tuple:
    """Same fix/tuple shape already established for every other language's
    old base images in this project (e.g. lang_php.py's _debian_archive_apt):
    returns (apt_sources, apt_flag, allow_unauth), all empty strings when no
    fix is needed."""
    codename = _debian_codename(rust_ver)
    if codename not in ("stretch", "buster"):
        codename = None
    apt_sources = (
        f"RUN echo 'deb http://archive.debian.org/debian {codename} main' > /etc/apt/sources.list \\\n"
        f"    && echo 'deb http://archive.debian.org/debian-security {codename}/updates main' >> /etc/apt/sources.list\n"
        if codename else ""
    )
    apt_flag     = "-o Acquire::Check-Valid-Until=false " if codename else ""
    allow_unauth = "--allow-unauthenticated "              if codename else ""
    return apt_sources, apt_flag, allow_unauth


# ── Framework metadata ────────────────────────────────────────────────────────
_FW_PACKAGE = {
    "Rocket":    "rocket",
    "actix-web": "actix-web",
    "axum":      "axum",
    "warp":      "warp",
    "Iron":      "iron",
}

# Rocket 0.4.x needs a PINNED nightly toolchain regardless of the selected
# rustc "language version" -- confirmed via real research: it needs unstable
# proc_macro_hygiene/decl_macro feature gates that were never stabilized in
# the form Rocket used, so no stable Rust ever worked for it. A specific
# nightly date is pinned here (rather than "nightly latest", which drifts
# and can break as the nightly compiler itself changes) -- 2021-04-13 is a
# real date downstream Rocket 0.4 projects are documented to pin.
_ROCKET_04_NIGHTLY = "nightly-2021-04-13"

# The cargo bundled with _ROCKET_04_NIGHTLY is on the ~1.53 release train
# (confirmed live: `rustup toolchain install nightly-2021-04-13` reports
# "rust version 1.53.0-nightly") -- this is the REAL compiler that will run
# `cargo build`, not the nominal rust_ver axis value (which is cosmetic for
# this one combo, see _fw_kind's docstring). The MSRV-repair step (see
# make_dockerfile's lockgen stage) must target this real version, or it
# would let through transitive crates the pinned nightly genuinely can't
# parse.
_ROCKET_04_NIGHTLY_MSRV = "1.53"


def _effective_msrv_target(rust_ver: str, kind: str) -> str:
    return _ROCKET_04_NIGHTLY_MSRV if kind == "rocket-nightly" else rust_ver


def _fw_kind(fw_name: str, fw_major: str) -> str:
    """Which main.rs/Cargo.toml template shape a combo needs -- frameworks
    with more than one real API era return a distinct kind per era."""
    if fw_name == "Rocket":
        return "rocket-nightly" if fw_major == "0.4" else "rocket-stable"
    if fw_name == "axum":
        # 0.6 uses axum::Server::bind(...).serve(...); 0.7+ uses
        # axum::serve(listener, app) with tokio::net::TcpListener --
        # confirmed via research as the single biggest breaking change in
        # axum's history (http crate 0.2->1.0, hyper 0.14->1.0).
        return "axum-old" if fw_major == "0.6" else "axum-new"
    return {"actix-web": "actix", "warp": "warp", "Iron": "iron"}[fw_name]


# ── Crypto library metadata ────────────────────────────────────────────────────
# "touch" mirrors every other language's LIB_META convention in this project:
# a real call into the library so it's provably loaded and exercised, not
# just declared as a dependency. Each touch snippet is a Rust expression
# (not a full statement) evaluating to a String suitable for embedding in the
# JSON response, so main.rs templates can embed it uniformly regardless of
# framework.
LIB_META = {
    "ring": {
        "crate": "ring", "sys_deps": [], "extra_deps": "",
        "imports": "use ring::{digest, hmac, rand};\nuse ring::rand::SecureRandom;",
        "touch": (
            'let rng = rand::SystemRandom::new();\n'
            '    let mut key_bytes = [0u8; 32];\n'
            '    rng.fill(&mut key_bytes).unwrap();\n'
            '    let key = hmac::Key::new(hmac::HMAC_SHA256, &key_bytes);\n'
            '    let tag = hmac::sign(&key, b"pqc-sca probe");\n'
            '    let touch_result = format!("hmac-sha256:{}", hex_encode(tag.as_ref()));'
        ),
    },
    "rsa": {
        "crate": "rsa", "sys_deps": [], "extra_deps": 'rand = "0.8"\n',
        "imports": "use rsa::{RsaPrivateKey, RsaPublicKey};\nuse rsa::pkcs1v15::SigningKey;\nuse rsa::signature::{Keypair, RandomizedSigner, SignatureEncoding};\nuse rsa::sha2::Sha256;",
        "touch": (
            'let mut rng = rand::thread_rng();\n'
            '    let priv_key = RsaPrivateKey::new(&mut rng, 512).expect("rsa keygen");\n'
            '    let signing_key = SigningKey::<Sha256>::new(priv_key);\n'
            '    let signature = signing_key.sign_with_rng(&mut rng, b"pqc-sca probe");\n'
            '    let touch_result = format!("rsa-pkcs1v15-sha256:{}", hex_encode(&signature.to_bytes())[..32].to_string());'
        ),
    },
    "ed25519-dalek": {
        "crate": "ed25519-dalek", "sys_deps": [], "extra_deps": 'rand = "0.8"\n',
        "imports": "use ed25519_dalek::{SigningKey, Signer};\nuse rand::rngs::OsRng;",
        "touch": (
            'let signing_key = SigningKey::generate(&mut OsRng);\n'
            '    let signature = signing_key.sign(b"pqc-sca probe");\n'
            '    let touch_result = format!("ed25519:{}", hex_encode(&signature.to_bytes()));'
        ),
    },
    "sodiumoxide": {
        "crate": "sodiumoxide", "sys_deps": ["libsodium-dev"], "extra_deps": "",
        "imports": "use sodiumoxide::crypto::sign;",
        "touch": (
            'sodiumoxide::init().ok();\n'
            '    let (_, sk) = sign::gen_keypair();\n'
            '    let signature = sign::sign_detached(b"pqc-sca probe", &sk);\n'
            '    let touch_result = format!("ed25519-libsodium:{}", hex_encode(signature.as_ref()));'
        ),
    },
    "bcrypt": {
        "crate": "bcrypt", "sys_deps": [], "extra_deps": "",
        "imports": "use bcrypt::hash;",
        "touch": 'let touch_result = hash("pqc-sca probe", 4).expect("bcrypt hash");',
    },
    "argon2": {
        "crate": "argon2", "sys_deps": [], "extra_deps": 'password-hash = "0.5"\n',
        "imports": "use argon2::Argon2;\nuse argon2::password_hash::{PasswordHasher, SaltString, rand_core::OsRng};",
        "touch": (
            'let salt = SaltString::generate(&mut OsRng);\n'
            '    let touch_result = Argon2::default()\n'
            '        .hash_password(b"pqc-sca probe", &salt)\n'
            '        .expect("argon2 hash")\n'
            '        .to_string();'
        ),
    },
    "rust-crypto": {
        "crate": "rust-crypto", "sys_deps": [], "extra_deps": "",
        "imports": "use crypto::sha2::Sha256;\nuse crypto::digest::Digest;",
        "touch": (
            'let mut hasher = Sha256::new();\n'
            '    hasher.input_str("pqc-sca probe");\n'
            '    let touch_result = hasher.result_str();'
        ),
    },
    "openssl": {
        "crate": "openssl", "sys_deps": ["libssl-dev", "pkg-config"], "extra_deps": "",
        "imports": "use openssl::hash::{hash, MessageDigest};",
        "touch": (
            'let digest = hash(MessageDigest::sha256(), b"pqc-sca probe").expect("openssl hash");\n'
            '    let touch_result = format!("sha256:{}", hex_encode(&digest));'
        ),
    },
}

# rust-crypto's own crate name on crates.io is literally "rust-crypto" but
# its lib name (used in `extern crate` / `use` paths) is "crypto" --
# confirmed via its own README/source. Every other crate's package name and
# lib name match.
_LIB_EXTERN_NAME = {"rust-crypto": "crypto"}


# ── App template (main.rs) ────────────────────────────────────────────────────
# A small hand-rolled hex encoder -- std has no built-in one, and pulling in
# yet another crate just for this would add an extra, version-sensitive
# dependency for zero real research value. Embedded in every generated
# main.rs regardless of whether that combo's touch code calls it (an unused
# function is a harmless warning in Rust, not a compile error).
_HEX_ENCODE_FN = (
    "fn hex_encode(bytes: &[u8]) -> String {\n"
    "    bytes.iter().map(|b| format!(\"{:02x}\", b)).collect()\n"
    "}\n"
)


def _version_json_expr(fw_name: str, lib_name: str) -> str:
    """A serde_json::json! expression for the /version endpoint, matching
    every other language's {language, framework, library} shape in this
    project."""
    return (
        'serde_json::json!({\n'
        '        "language": {"name": "Rust", "version": env!("PQC_RUSTC_VERSION")},\n'
        f'        "framework": {{"name": "{fw_name}", "version": env!("PQC_FW_VERSION")}},\n'
        f'        "library": {{"name": "{lib_name}", "version": env!("PQC_LIB_VERSION")}}\n'
        '    })'
    )


def make_main_rs(fw_name: str, fw_major: str, lib_name: str) -> str:
    kind = _fw_kind(fw_name, fw_major)
    meta = LIB_META[lib_name]
    imports = meta["imports"]
    touch = meta["touch"]
    version_json = _version_json_expr(fw_name, lib_name)

    if kind == "rocket-nightly":
        # Rocket 0.4: sync handlers returning raw JSON strings (no
        # rocket_contrib::json dependency needed -- keeps this era's
        # already-fragile nightly-toolchain build as simple as possible).
        return (
            "#![feature(proc_macro_hygiene, decl_macro)]\n"
            "#[macro_use] extern crate rocket;\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "#[get(\"/\")]\n"
            "fn index() -> String {\n"
            f"    {touch}\n"
            "    serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}).to_string()\n"
            "}\n\n"
            "#[get(\"/version\")]\n"
            "fn version() -> String {\n"
            f"    {version_json}.to_string()\n"
            "}\n\n"
            "fn main() {\n"
            "    rocket::ignite().mount(\"/\", routes![index, version]).launch();\n"
            "}\n"
        )

    if kind == "rocket-stable":
        return (
            "#[macro_use] extern crate rocket;\n"
            "use rocket::serde::json::Json;\n"
            "use serde_json::Value;\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "#[get(\"/\")]\n"
            "fn index() -> Json<Value> {\n"
            f"    {touch}\n"
            "    Json(serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}))\n"
            "}\n\n"
            "#[get(\"/version\")]\n"
            "fn version() -> Json<Value> {\n"
            f"    Json({version_json})\n"
            "}\n\n"
            "#[launch]\n"
            "fn rocket() -> _ {\n"
            "    rocket::build().mount(\"/\", routes![index, version])\n"
            "}\n"
        )

    if kind == "actix":
        return (
            "use actix_web::{get, App, HttpServer, HttpResponse};\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "#[get(\"/\")]\n"
            "async fn index() -> HttpResponse {\n"
            f"    {touch}\n"
            "    HttpResponse::Ok().json(serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}))\n"
            "}\n\n"
            "#[get(\"/version\")]\n"
            "async fn version() -> HttpResponse {\n"
            f"    HttpResponse::Ok().json({version_json})\n"
            "}\n\n"
            "#[actix_web::main]\n"
            "async fn main() -> std::io::Result<()> {\n"
            "    HttpServer::new(|| App::new().service(index).service(version))\n"
            "        .bind((\"0.0.0.0\", 8000))?\n"
            "        .run()\n"
            "        .await\n"
            "}\n"
        )

    if kind in ("axum-old", "axum-new"):
        serve_body = (
            # axum 0.6: axum::Server (hyper 0.14-based).
            '    axum::Server::bind(&"0.0.0.0:8000".parse().unwrap())\n'
            '        .serve(app.into_make_service())\n'
            '        .await\n'
            '        .unwrap();\n'
            if kind == "axum-old" else
            # axum 0.7+: axum::serve + tokio::net::TcpListener (hyper 1.0-based).
            '    let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await.unwrap();\n'
            '    axum::serve(listener, app).await.unwrap();\n'
        )
        return (
            "use axum::{routing::get, Router, Json};\n"
            "use serde_json::Value;\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "async fn index() -> Json<Value> {\n"
            f"    {touch}\n"
            "    Json(serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}))\n"
            "}\n\n"
            "async fn version() -> Json<Value> {\n"
            f"    Json({version_json})\n"
            "}\n\n"
            "#[tokio::main]\n"
            "async fn main() {\n"
            "    let app = Router::new().route(\"/\", get(index)).route(\"/version\", get(version));\n"
            f"{serve_body}"
            "}\n"
        )

    if kind == "warp":
        return (
            "use warp::Filter;\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "#[tokio::main]\n"
            "async fn main() {\n"
            "    let index = warp::path::end().map(|| {\n"
            f"        {touch}\n"
            "        warp::reply::json(&serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}))\n"
            "    });\n"
            "    let version = warp::path(\"version\").map(|| {\n"
            f"        warp::reply::json(&{version_json})\n"
            "    });\n"
            "    warp::serve(index.or(version)).run(([0, 0, 0, 0], 8000)).await;\n"
            "}\n"
        )

    if kind == "iron":
        return (
            "extern crate iron;\n"
            "extern crate router;\n"
            "extern crate serde_json;\n"
            "use iron::prelude::*;\n"
            "use iron::status;\n"
            "use router::Router;\n"
            f"{imports}\n"
            f"{_HEX_ENCODE_FN}\n"
            "fn index(_: &mut Request) -> IronResult<Response> {\n"
            f"    {touch}\n"
            "    let body = serde_json::json!({\"message\": \"Hello World\", \"touch_len\": touch_result.len()}).to_string();\n"
            "    Ok(Response::with((status::Ok, body)))\n"
            "}\n\n"
            "fn version(_: &mut Request) -> IronResult<Response> {\n"
            f"    let body = {version_json}.to_string();\n"
            "    Ok(Response::with((status::Ok, body)))\n"
            "}\n\n"
            "fn main() {\n"
            "    let mut router = Router::new();\n"
            "    router.get(\"/\", index, \"index\");\n"
            "    router.get(\"/version\", version, \"version\");\n"
            "    Iron::new(router).http(\"0.0.0.0:8000\").unwrap();\n"
            "}\n"
        )

    raise ValueError(f"Unknown framework kind: {kind}")


def make_cargo_toml(fw_name: str, fw_major: str, fw_ver: str,
                    lib_name: str, lib_ver: str) -> str:
    kind = _fw_kind(fw_name, fw_major)
    fw_crate = _FW_PACKAGE[fw_name]
    lib_crate = LIB_META[lib_name]["crate"]
    extra_lib_deps = LIB_META[lib_name]["extra_deps"]

    if kind == "rocket-stable":
        fw_dep = f'{fw_crate} = {{ version = "{fw_ver}", features = ["json"] }}\n'
    else:
        fw_dep = f'{fw_crate} = "{fw_ver}"\n'

    extra_fw_deps = ""
    if kind in ("axum-old", "axum-new", "warp"):
        extra_fw_deps = 'tokio = { version = "1", features = ["full"] }\n'
    elif kind == "iron":
        extra_fw_deps = 'router = "0.6"\n'

    return (
        "[package]\n"
        'name = "app"\n'
        'version = "0.0.0"\n'
        'edition = "2018"\n\n'
        "[dependencies]\n"
        f"{fw_dep}"
        f"{extra_fw_deps}"
        f'{lib_crate} = "{lib_ver}"\n'
        f"{extra_lib_deps}"
        'serde = { version = "1", features = ["derive"] }\n'
        'serde_json = "1"\n'
    )


# ── Dockerfile ────────────────────────────────────────────────────────────────
# `--mount=type=cache` for Cargo's registry+git caches -- same rationale as
# every other language's package-manager cache mount in this project (see
# lang_java.py's make_dockerfile comment for the full reasoning). Cargo's
# real default cache locations (confirmed via a real container run:
# `docker run rust:1-slim sh -c 'cargo --version; echo $CARGO_HOME'`):
# $CARGO_HOME defaults to /usr/local/cargo in the official rust Docker image
# (NOT /root/.cargo -- the official image sets CARGO_HOME explicitly via
# ENV, unlike every other language's default-HOME-relative cache path), with
# the registry cache at $CARGO_HOME/registry and git checkouts at
# $CARGO_HOME/git.
_CARGO_REGISTRY_CACHE_MOUNT = "--mount=type=cache,id=cargo-registry-cache,target=/usr/local/cargo/registry,sharing=locked"
_CARGO_GIT_CACHE_MOUNT = "--mount=type=cache,id=cargo-git-cache,target=/usr/local/cargo/git,sharing=locked"
# Compiled build artifacts (target/) are NOT shared across combos (each
# combo has different dependencies -- unlike the registry/git download
# caches, a shared target/ would mix incompatible incremental-compilation
# state between combos) -- deliberately NOT mounted as a shared cache,
# unlike the download-cache paths above.

# ── MSRV-aware lockfile repair ───────────────────────────────────────────────
# A real, confirmed-live bug class (found while build-testing Rocket/Iron/
# warp+sodiumoxide at rustc 1.75, all 3 failed identically): with no
# Cargo.lock committed, `cargo build` re-resolves every transitive dependency
# to its NEWEST semver-compatible version on every build. crates.io is a
# living registry -- widely-used transitive crates (zeroize, base64ct,
# idna_adapter, ...) keep shipping new releases, and some of those releases
# have since bumped to Rust edition2024 (stabilized at rustc 1.85). An OLD
# rustc's cargo can't even PARSE an edition2024 Cargo.toml (a hard parse
# error, not a soft "version incompatible, try older" -- confirmed live) --
# so a combo pinned to an old rust_ver can spuriously fail to build for a
# reason that has nothing to do with the framework/library actually being
# tested, and will keep failing more often as the ecosystem moves forward.
#
# Two tempting "obvious" fixes were tried and BOTH empirically falsified on
# the real server before writing this:
#   1. Declaring `rust-version = "{rust_ver}"` in our own Cargo.toml --
#      confirmed to NOT constrain stable cargo's resolver at all (it picked
#      base64ct 1.8.3, which itself declares rust-version=1.85, even with
#      our crate declaring rust-version=1.75). Cargo's MSRV-aware dependency
#      resolution is not enabled by default on stable.
#   2. `cargo +nightly generate-lockfile -Z minimal-versions` -- goes too far
#      the OTHER direction, picking genuinely ancient/broken crate releases
#      (confirmed live: it picked a `phantom-0.0.0` whose Cargo.toml uses a
#      pre-modern manifest format current cargo can't parse either).
#
# The fix that DOES work, confirmed live end-to-end (iron+argon2 and
# axum+ring at rustc 1.75, previously failing on base64ct 1.8.3, build
# clean after this): resolve normally with a MODERN cargo (so it can parse
# any candidate manifest, edition2024 included), downgrade any locked crate
# whose declared `rust_version` OR `edition` (whichever floor is stricter --
# crates.io's `traitobject` 0.1.1 has rust_version=None but edition="2021",
# a 2025 republish of an old version NUMBER, not an old release that simply
# never declared MSRV) exceeds our target, staying within the replacement's
# semver-compatible range (`cargo update --precise` cannot cross a
# consumer's own requirement boundary -- confirmed live: jumping byteorder
# 1.5.0 straight to 0.1.0 was rejected outright), then use `cargo metadata
# --filter-platform x86_64-unknown-linux-gnu` (correctly excludes
# Windows/WASM-only transitive noise like winapi/wasi) to rewrite Cargo.toml
# with every crate in the real dependency closure EXACT-pinned. The real
# builder stage then copies THIS pinned Cargo.toml (no Cargo.lock at all)
# and lets the pinned rust_ver's own (possibly old) cargo do a completely
# fresh, zero-ambiguity resolution in its OWN native lockfile format --
# sidesteps a separate, equally-real bug: a modern-cargo-written Cargo.lock
# can be in lockfile format v4, which old cargo cannot parse AT ALL
# (confirmed live) regardless of crate version content; only the target
# cargo writing its own lockfile natively avoids that entirely.
#
# Known remaining gap (confirmed, not yet fixed): rust_version/edition only
# capture DECLARED floors -- crates.io does not enforce them, and a few
# crates' actual compiled code needs more than they declare (confirmed
# live: unicode-bidi 0.3.15 declares rust_version=1.47 but its own source
# uses an unstable-until-later std feature, failing an ACTUAL rustc 1.35
# build). Only a full trial-compile-and-retry loop against the real target
# toolchain could catch this class generically; that's a materially bigger
# feature than this fix, deliberately not built until a REAL registry combo
# (not a synthetic worst-case stress test) hits it -- matching this
# project's established fix-as-discovered pattern for genuine ecosystem
# edge cases elsewhere (Slim-2, CherryPy 17, pycryptodome 3.0-3.6, etc).
_MSRV_REPAIR_PY = '''import sys, re, subprocess, urllib.request, json, time, tomllib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

TARGET = sys.argv[1]
UA = "pqc-sca-research/1.0 (+https://github.com/DonjaVanEsch/SCA)"

_STABLE_RE = re.compile(r"^\\d+(\\.\\d+){0,3}$")

def ver_tuple(v):
    parts = v.split(".")
    return tuple(int(p) for p in parts[:2])

TARGET_T = ver_tuple(TARGET)
_cache = {}

def _fetch_one(crate):
    req = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{crate}/versions",
        headers={"User-Agent": UA},
    )
    data = {"versions": []}
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.load(r)
            break
        except Exception as e:
            if attempt == 2:
                print(f"  ! failed to fetch {crate}: {e}", file=sys.stderr)
            else:
                time.sleep(1)
    return [v for v in data.get("versions", []) if _STABLE_RE.match(v["num"])]

def prefetch_all(crates):
    # crates.io lookups are pure I/O -- confirmed live that doing them one
    # at a time was NOT actually the dominant cost (see repair_loop's
    # docstring), but they still add up serially across ~50-80 unique
    # crates; a thread pool costs nothing to add and removes that tail.
    todo = [c for c in dict.fromkeys(crates) if c not in _cache]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=16) as pool:
        for crate, versions in zip(todo, pool.map(_fetch_one, todo)):
            _cache[crate] = versions

def fetch_versions(crate):
    if crate not in _cache:
        _cache[crate] = _fetch_one(crate)
    return _cache[crate]

def parse_lock():
    with open("Cargo.lock") as f:
        text = f.read()
    pkgs = []
    for m in re.finditer(r'\\[\\[package\\]\\]\\nname = "([^"]+)"\\nversion = "([^"]+)"', text):
        pkgs.append((m.group(1), m.group(2)))
    return pkgs

_EDITION_FLOOR = {"2015": (1, 0), "2018": (1, 31), "2021": (1, 56), "2024": (1, 85)}

def effective_floor(v):
    floor = _EDITION_FLOOR.get(v.get("edition"), (1, 0))
    rv = v.get("rust_version")
    if rv:
        try:
            rvt = ver_tuple(rv)
            if rvt > floor:
                floor = rvt
        except ValueError:
            pass
    return floor

def current_floor(crate, version):
    for v in fetch_versions(crate):
        if v["num"] == version:
            return effective_floor(v)
    return None

def caret_compatible(base, other):
    b = [int(x) for x in base.split(".")] + [0, 0]
    o = [int(x) for x in other.split(".")] + [0, 0]
    if b[0] != 0:
        return o[0] == b[0]
    if b[1] != 0:
        return o[0] == 0 and o[1] == b[1]
    return o[0] == 0 and o[1] == 0 and o[2] == b[2]

def candidate_order(crate, current_version):
    versions = [v for v in fetch_versions(crate) if not v.get("yanked")]
    same_class = [v for v in versions if caret_compatible(current_version, v["num"])]
    return [v["num"] for v in same_class if effective_floor(v) <= TARGET_T]

def _apply_batch(to_fix, original_toml):
    """Try to fix ALL of this round's downgrades with ONE cargo invocation
    instead of one `cargo update --precise` call per crate. Confirmed live
    this was the actual dominant cost, not the Python-side crates.io
    lookups: each `--precise` call independently re-syncs the crates.io
    index (~3s fixed cost every time, regardless of freshness), so N
    packages needing repair meant N x ~3s just in that overhead -- for
    rustc 1.31 (needing the most downgrades of any tracked target) this
    alone pushed a single combo's warm-up build past 13 minutes.

    The batch mechanism: temporarily add an exact `name = "=version"` pin
    per offending crate (forces zero-ambiguity resolution for exactly that
    crate), then `cargo update -p name@old_version ...` (repeated -p, no
    --precise) -- confirmed live this selectively updates ONLY the named
    packages to satisfy their new exact pin, leaving every other locked
    package (e.g. iron) untouched, in a single ~3s call regardless of how
    many packages are included.

    Falls back to the slower one-call-per-crate path (repair_loop, below)
    only if the batch itself is rejected outright -- e.g. two crates in
    the same round happen to share a name at different majors, which would
    collide as duplicate TOML keys."""
    pin_lines = "\\n".join(f'{name} = "={replacement}"' for name, _, replacement in to_fix)
    with open("Cargo.toml", "w") as f:
        f.write(original_toml + "\\n" + pin_lines + "\\n")
    specs = []
    for name, version, _ in to_fix:
        specs += ["-p", f"{name}@{version}"]
    r = subprocess.run(["cargo", "update"] + specs, capture_output=True, text=True)
    with open("Cargo.toml", "w") as f:
        f.write(original_toml)
    return r.returncode == 0

def repair_loop():
    with open("Cargo.toml") as f:
        original_toml = f.read()
    given_up = set()
    for round_num in range(12):
        pkgs = parse_lock()
        prefetch_all(name for name, _ in pkgs)

        to_fix = []
        for name, version in pkgs:
            if (name, version) in given_up:
                continue
            floor = current_floor(name, version)
            if floor is None or floor <= TARGET_T:
                continue
            candidates = [c for c in candidate_order(name, version) if c != version]
            if not candidates:
                print(f"  ! {name} {version} needs rust {floor} > {TARGET}, no compatible version found in the same semver range -- genuine floor")
                given_up.add((name, version))
                continue
            to_fix.append((name, version, candidates[0]))

        if not to_fix:
            print(f"stable after {round_num + 1} round(s)")
            return

        if _apply_batch(to_fix, original_toml):
            for name, version, replacement in to_fix:
                print(f"  downgraded {name} {version} (needs repair) -> {replacement}")
            continue

        # Batch rejected -- fall back to one cargo call per crate this
        # round (still correct, just the pre-optimization speed).
        for name, version, replacement in to_fix:
            r = subprocess.run(["cargo", "update", "-p", f"{name}@{version}", "--precise", replacement],
                                capture_output=True, text=True)
            if r.returncode == 0:
                print(f"  downgraded {name} {version} (individual fallback) -> {replacement}")
            else:
                print(f"  ! {name} {version} rejected even individually, giving up: {r.stderr[-200:]}")
                given_up.add((name, version))
    print("gave up after 12 rounds (may still have incompatible crates)")

def dep_line(name, version, orig_value):
    if isinstance(orig_value, dict):
        feats = orig_value.get("features")
        if feats:
            feat_str = ", ".join(f'"{f}"' for f in feats)
            return f'{name} = {{ version = "={version}", features = [{feat_str}] }}'
    return f'{name} = "={version}"'

def rewrite_pinned_toml():
    with open("Cargo.toml", "rb") as f:
        data = tomllib.load(f)
    pkg = data["package"]
    orig_deps = data.get("dependencies", {})
    protected = set(orig_deps.keys())

    meta = json.loads(subprocess.run(
        ["cargo", "metadata", "--filter-platform", "x86_64-unknown-linux-gnu", "--format-version", "1"],
        capture_output=True, text=True, check=True,
    ).stdout)

    by_name = defaultdict(set)
    for node in meta["resolve"]["nodes"]:
        pkg_id = node["id"]
        if pkg_id.startswith("path+"):
            continue
        after_hash = pkg_id.rsplit("#", 1)[-1]
        name, version = after_hash.rsplit("@", 1)
        by_name[name].add(version)

    lines = []
    for name in sorted(by_name):
        versions = sorted(by_name[name])
        if len(versions) == 1:
            v = versions[0]
            if name in protected:
                lines.append(dep_line(name, v, orig_deps[name]))
            else:
                lines.append(f'{name} = "={v}"')
        else:
            for i, v in enumerate(versions):
                if name in protected and i == 0:
                    lines.append(dep_line(name, v, orig_deps[name]))
                else:
                    alias = f"{name.replace('-', '_')}_pin{i}"
                    lines.append(f'{alias} = {{ package = "{name}", version = "={v}" }}')

    out = [
        "[package]",
        f'name = "{pkg["name"]}"',
        f'version = "{pkg["version"]}"',
        f'edition = "{pkg["edition"]}"',
        "",
        "[dependencies]",
        *lines,
        "",
    ]
    with open("Cargo.toml", "w") as f:
        f.write("\\n".join(out))
    print(f"rewrote Cargo.toml with {len(lines)} exact-pinned dependencies")

repair_loop()
rewrite_pinned_toml()
'''


def make_dockerfile(rust_ver: str, fw_name: str, fw_major: str, fw_ver: str,
                    lib_name: str, lib_ver: str) -> str:
    kind = _fw_kind(fw_name, fw_major)
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(rust_ver)
    sys_deps = LIB_META[lib_name]["sys_deps"]

    apt_block = ""
    if sys_deps:
        deps_line = " ".join(sys_deps)
        apt_block = (
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {deps_line} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )

    # Cache-key diversifier, built in from day one (2026-07-24) -- this
    # project's own confirmed cross-language bug class (a Dockerfile whose
    # TEXT doesn't vary by combo lets BuildKit's cache serve one combo's
    # build output for a different combo under concurrent builds; found and
    # fixed after the fact for Node and PHP). Baking framework+major+
    # library+version into an ARG from the start makes a collision here
    # structurally impossible rather than something to discover later.
    cache_bust = f'ARG PQC_COMBO_ID="{fw_name}-{fw_major}-{lib_name}@{lib_ver}"\n'

    if kind == "rocket-nightly":
        # Needs a PINNED nightly toolchain regardless of the base image's
        # own stable rustc -- see _ROCKET_04_NIGHTLY's docstring. Installed
        # via rustup on top of the selected stable base image.
        toolchain_setup = (
            f"RUN rustup toolchain install {_ROCKET_04_NIGHTLY} "
            f"&& rustup default {_ROCKET_04_NIGHTLY}\n"
        )
    else:
        toolchain_setup = ""

    # env!() reads these at COMPILE time, so they must be set as real
    # environment variables before `cargo build` runs, not just written
    # into a file cargo doesn't look at.
    version_env = (
        f"ENV PQC_RUSTC_VERSION={rust_ver} "
        f"PQC_FW_VERSION={fw_ver} "
        f"PQC_LIB_VERSION={lib_ver}\n"
    )

    msrv_target = _effective_msrv_target(rust_ver, kind)

    # lockgen: a throwaway stage on the latest stable rust image (needed so
    # it can parse ANY candidate manifest, including edition2024 ones, while
    # deciding what to avoid) that resolves + repairs a Cargo.lock -- see
    # _MSRV_REPAIR_PY's docstring for why this exists and why the two
    # simpler alternatives don't work. python3 is stdlib-only (urllib), no
    # pip install needed; ca-certificates is required for python's https
    # calls specifically (cargo's own TLS stack doesn't imply python has a
    # CA bundle).
    lockgen_stage = (
        "FROM rust:1-slim AS lockgen\n"
        "RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \\\n"
        "    python3 ca-certificates \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "WORKDIR /build\n"
        "COPY Cargo.toml .\n"
        "COPY src ./src\n"
        "COPY msrv_repair.py .\n"
        f"RUN {_CARGO_REGISTRY_CACHE_MOUNT} {_CARGO_GIT_CACHE_MOUNT} \\\n"
        f"    cargo generate-lockfile && python3 msrv_repair.py {msrv_target}\n"
        "\n"
    )

    return (
        f"{lockgen_stage}"
        f"FROM rust:{rust_ver}-slim AS builder\n"
        f"{apt_block}"
        f"{cache_bust}"
        f"{toolchain_setup}"
        f"{version_env}"
        "WORKDIR /build\n"
        "COPY --from=lockgen /build/Cargo.toml .\n"
        "COPY src ./src\n"
        f"RUN {_CARGO_REGISTRY_CACHE_MOUNT} {_CARGO_GIT_CACHE_MOUNT} \\\n"
        "    cargo build --release\n"
        "\n"
        f"FROM debian:{_debian_codename(rust_ver)}-slim\n"
        f"{apt_block if sys_deps else ''}"
        "WORKDIR /app\n"
        "COPY --from=builder /build/target/release/app ./app\n"
        "EXPOSE 8000\n"
        f"{'ENV ROCKET_ADDRESS=0.0.0.0 ROCKET_PORT=8000' + chr(10) if fw_name == 'Rocket' else ''}"
        'CMD ["./app"]\n'
    )


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write Cargo.toml / src/main.rs / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required
    crates.io package version cannot be resolved. Returns False WITHOUT
    touching any existing directory when the lookup itself failed
    (network/rate-limit) -- see CratesIoLookupError.
    """
    out = images_base / "rust" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    fw_pkg = _FW_PACKAGE[fw_name]
    try:
        fw_resolved = _resolve(fw_pkg, fw_major)
    except CratesIoLookupError as exc:
        print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
        return False
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on crates.io", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    lib_pkg = LIB_META[lib_name]["crate"]
    try:
        lib_resolved = _resolve(lib_pkg, lib_ver)
    except CratesIoLookupError as exc:
        print(f"  [WARN] {exc} -- leaving any existing context untouched", flush=True)
        return False
    if lib_resolved is None:
        print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on crates.io", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    out.mkdir(parents=True, exist_ok=True)
    (out / "src").mkdir(exist_ok=True)

    (out / "src" / "main.rs").write_text(
        make_main_rs(fw_name, fw_major, lib_name), encoding="utf-8"
    )
    (out / "Cargo.toml").write_text(
        make_cargo_toml(fw_name, fw_major, fw_resolved, lib_name, lib_resolved),
        encoding="utf-8",
    )
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name, fw_major, fw_resolved, lib_name, lib_resolved),
        encoding="utf-8",
    )
    (out / "msrv_repair.py").write_text(_MSRV_REPAIR_PY, encoding="utf-8")
    return True


def prefetch(lang_data: dict) -> None:
    """Warm the crates.io version cache for every framework/library this
    registry references, once, before the main write_context loop runs --
    same rationale as every other language's prefetch(): avoids one
    redundant network round-trip per (language_version x framework_major)
    combination sharing the same underlying crate. Also used by
    check_updates.py to detect newly-published versions. Swallows
    CratesIoLookupError per-crate (a warm-up failure isn't fatal -- the
    real write_context() calls will surface it properly, per-combo,
    afterward)."""
    for fw in lang_data.get("frameworks", []):
        try:
            _fetch_crates_versions(_FW_PACKAGE[fw["name"]])
        except CratesIoLookupError as exc:
            print(f"  [WARN] prefetch: {exc}", flush=True)

    for lib in lang_data.get("cryptography_libs", []):
        try:
            _fetch_crates_versions(LIB_META[lib["name"]]["crate"])
        except CratesIoLookupError as exc:
            print(f"  [WARN] prefetch: {exc}", flush=True)

