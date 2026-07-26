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
        # rsa's own re-export of "sha2" (used as `rsa::sha2::Sha256`) has
        # been removed from its current releases -- confirmed live via
        # crates.io: rsa 0.9.9's own feature list no longer has a "sha2"
        # entry at all. `SigningKey<D>` is generic over any hash type
        # implementing the `digest` crate's traits, so the caller is
        # expected to depend on a hash crate (sha2) directly rather than
        # go through rsa's re-export -- added explicitly here instead.
        # `SigningKey::<Sha256>::new()` additionally requires `Sha256:
        # AssociatedOid` (from the `digest`/`const-oid` machinery) --
        # confirmed live via a real build failure (E0599, "trait bounds
        # were not satisfied ... AssociatedOid") that sha2's default
        # features do NOT provide this; its own "oid" feature (which just
        # enables `digest/oid`) does, per sha2's crates.io feature list.
        "crate": "rsa", "sys_deps": [],
        "extra_deps": 'rand = "0.8"\nsha2 = { version = "0.10", features = ["oid"] }\n',
        "imports": "use rsa::{RsaPrivateKey, RsaPublicKey};\nuse rsa::pkcs1v15::SigningKey;\nuse rsa::signature::{Keypair, RandomizedSigner, SignatureEncoding};\nuse sha2::Sha256;",
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
        # libsodium-sys's build.rs (confirmed via direct source inspection)
        # only tries pkg-config when EITHER its own "use-pkg-config" crate
        # feature is enabled OR the SODIUM_USE_PKG_CONFIG env var is set --
        # installing the pkg-config system package alone changes nothing,
        # it still falls back to compiling libsodium from source (which
        # then fails without a full autotools/make toolchain). "pkg-config"
        # added to sys_deps here for the TOOL; the env var is set in
        # make_dockerfile (see _LIB_BUILD_ENV) to actually activate it.
        "crate": "sodiumoxide", "sys_deps": ["libsodium-dev", "pkg-config"], "extra_deps": "",
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
        # No `password-hash` extra_dep: `argon2::password_hash::...`
        # already resolves SaltString/PasswordHasher via argon2's OWN
        # re-export, so an independently pinned `password-hash = "X"` was
        # both unnecessary AND actively harmful (confirmed live): it
        # pinned a version for the NEWEST API era regardless of which
        # argon2 bucket was in use, so for "0.1" specifically it added an
        # incompatible SECOND copy of the crate MSRV repair could never
        # reconcile with the one argon2 0.1.x actually needs. All 3
        # tracked buckets enable "password-hash" as a DEFAULT feature on
        # their own, confirmed via crates.io.
        #
        # `rand_core` IS pinned explicitly, though (confirmed live this
        # one's needed): `argon2::password_hash::rand_core::OsRng` doesn't
        # reliably resolve via the same re-export chain -- argon2's
        # default "rand" feature maps to password-hash's OWN "rand_core"
        # Cargo feature (gating internal functionality), not a guaranteed
        # public re-export of the whole rand_core module for downstream
        # use. rand_core itself is a small, stable, near-universally
        # transitively-present crate already, so depending on it directly
        # is simpler and more reliable than threading a re-export chain.
        "crate": "argon2", "sys_deps": [], "extra_deps": 'rand_core = { version = "0.6", features = ["getrandom"] }\n',
        "imports": "use argon2::Argon2;\nuse argon2::password_hash::{PasswordHasher, SaltString};\nuse rand_core::OsRng;",
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

# argon2's registry buckets span a REAL PasswordHasher API break, the same
# class of thing _fw_kind() already handles for frameworks -- confirmed via
# direct source inspection (crates.io download + grep, not assumed):
# argon2 0.1.x's `hash_password` takes 5 args (password, alg_id, version_id,
# params, salt) and needs `SaltString::as_salt()` (password-hash 0.1.x has
# no `impl Into<Salt> for &SaltString`); 0.3.x/0.5.x collapsed this to the
# 2-arg (password, salt) form LIB_META's default "touch" string already
# targets. Confirmed live: building the "0.1" bucket with the 0.3/0.5-style
# 2-arg call fails with a real E0061 arg-count mismatch, not a MSRV issue.
_ARGON2_LEGACY_TOUCH = (
    'let salt = SaltString::generate(&mut OsRng);\n'
    '    let touch_result = Argon2::default()\n'
    '        .hash_password(b"pqc-sca probe", None, None, argon2::Params::default(), salt.as_salt())\n'
    '        .expect("argon2 hash")\n'
    '        .to_string();'
)


def _argon2_touch(lib_major: str) -> str:
    return _ARGON2_LEGACY_TOUCH if lib_major == "0.1" else LIB_META["argon2"]["touch"]


# ed25519-dalek 2.0 renamed its whole signing-key API: 1.x's `Keypair`
# (`Keypair::generate(&mut rng) -> Keypair`, `Signer` impl'd for `Keypair`)
# became 2.x's `SigningKey` (`SigningKey::generate`, `Signer` impl'd for
# `SigningKey` instead) -- confirmed via direct source inspection of the
# real 1.0.1 release, not assumed. LIB_META's default imports/touch already
# target the 2.x shape; bucket "1" needs the old one.
_ED25519_DALEK_1_IMPORTS = "use ed25519_dalek::{Keypair, Signer};\nuse rand::rngs::OsRng;"
_ED25519_DALEK_1_TOUCH = (
    'let keypair = Keypair::generate(&mut OsRng);\n'
    '    let signature = keypair.sign(b"pqc-sca probe");\n'
    '    let touch_result = format!("ed25519:{}", hex_encode(&signature.to_bytes()));'
)


def _ed25519_dalek_imports(lib_major: str) -> str:
    return _ED25519_DALEK_1_IMPORTS if lib_major == "1" else LIB_META["ed25519-dalek"]["imports"]


def _ed25519_dalek_touch(lib_major: str) -> str:
    return _ED25519_DALEK_1_TOUCH if lib_major == "1" else LIB_META["ed25519-dalek"]["touch"]


def _ed25519_dalek_extra_deps(lib_major: str) -> str:
    # ed25519-dalek "1" needs `rand ^0.7` (confirmed via crates.io
    # dependency metadata: rand_core ^0.5 transitively) -- NOT "^0.8" like
    # bucket "2". Using rand 0.8's `OsRng` with bucket "1"'s
    # `Keypair::generate<R: CryptoRng + RngCore>` fails with a real trait
    # mismatch (rand_core 0.5's CryptoRng/RngCore are distinct types from
    # rand_core 0.6's, even though same-named) -- confirmed live via
    # E0277 "trait bound OsRng: CryptoRng is not satisfied".
    return 'rand = "0.7"\n' if lib_major == "1" else LIB_META["ed25519-dalek"]["extra_deps"]


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


def make_main_rs(fw_name: str, fw_major: str, lib_name: str, lib_major: str) -> str:
    kind = _fw_kind(fw_name, fw_major)
    meta = LIB_META[lib_name]
    if lib_name == "argon2":
        imports = meta["imports"]
        touch = _argon2_touch(lib_major)
    elif lib_name == "ed25519-dalek":
        imports = _ed25519_dalek_imports(lib_major)
        touch = _ed25519_dalek_touch(lib_major)
    else:
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


# Every ecosystem-drift cap below exists ONLY because a specific crate's
# NEWEST release needs a rustc newer than some old target the registry
# still exercises. Confirmed live (2026-07-25) that applying them
# UNCONDITIONALLY was itself a real bug: at a NEW target (e.g. 1.55, or
# any target above these thresholds), the caps aren't just unneeded, they
# actively CONFLICT with legitimate exact-version requirements from other
# real dependencies -- e.g. digest 0.10.3 requires `subtle = "=2.4"`
# exactly, which a blanket `subtle < 2.3` cap makes unsatisfiable even
# though rustc 1.55 has no problem at all compiling subtle 2.4. Each cap
# is therefore gated on the EFFECTIVE msrv target actually being old
# enough to need it -- see each threshold's own comment for what it was
# confirmed against.
_SERDE_CORE_FLOOR = (1, 71)     # syn "^2.0.81"+/"^3" wall serde_core drags in
_TINYVEC_PARSE_FLOOR = (1, 60)  # namespaced-features manifest syntax
_EDITION2021_FLOOR = (1, 56)    # quote/proc-macro2's edition="2021" switch
_SUBTLE_COHERENCE_FLOOR = (1, 41)  # rebalance-coherence orphan-rule relaxation
_BASE64CT_EDITION2024_FLOOR = (1, 85)  # base64ct 1.8.0+ ships edition="2024"


def _ver_tuple2(v: str) -> tuple:
    parts = v.split(".")
    return tuple(int(p) for p in parts[:2])


def make_cargo_toml(fw_name: str, fw_major: str, fw_ver: str,
                    lib_name: str, lib_ver: str, msrv_target: str, lib_major: str) -> str:
    kind = _fw_kind(fw_name, fw_major)
    fw_crate = _FW_PACKAGE[fw_name]
    lib_crate = LIB_META[lib_name]["crate"]
    if lib_name == "ed25519-dalek":
        extra_lib_deps = _ed25519_dalek_extra_deps(lib_major)
    else:
        extra_lib_deps = LIB_META[lib_name]["extra_deps"]
    target_t = _ver_tuple2(msrv_target)

    if kind == "rocket-stable":
        fw_dep = f'{fw_crate} = {{ version = "{fw_ver}", features = ["json"] }}\n'
    else:
        fw_dep = f'{fw_crate} = "{fw_ver}"\n'

    if lib_name == "ed25519-dalek" and lib_major == "2":
        # SigningKey::generate() (used by our touch code) is gated behind
        # ed25519-dalek's own "rand_core" feature -- confirmed live via a
        # real build (E0599, "no function or associated item named
        # `generate`") and the crate's own source (signing.rs:
        # `#[cfg(feature = "rand_core")] pub fn generate...`). It is NOT
        # part of ed25519-dalek's default features (only fast/std/zeroize
        # are), so it must be requested explicitly. This is a genuine bug
        # in our own dependency declaration, not an MSRV/floor issue --
        # confirmed by reproducing the identical failure across multiple
        # rustc targets (1.61, 1.65, 1.85), including the newest one.
        lib_dep = f'{lib_crate} = {{ version = "{lib_ver}", features = ["rand_core"] }}\n'
    else:
        lib_dep = f'{lib_crate} = "{lib_ver}"\n'

    extra_fw_deps = ""
    if kind in ("axum-old", "axum-new", "warp"):
        extra_fw_deps = 'tokio = { version = "1", features = ["full"] }\n'
    elif kind == "iron":
        extra_fw_deps = 'router = "0.6"\n'

    # serde_json is a BASE dependency every template needs (for its own
    # serde_json::json!() calls) regardless of target -- confirmed live as
    # a real regression: an earlier version of this function only ever
    # emitted a serde_json line INSIDE the conditional cap block, so for
    # any target >= _SERDE_CORE_FLOOR (no cap needed) serde_json was never
    # declared as a dependency AT ALL, failing with "can't find crate for
    # `serde_json`" despite every template calling it directly. The cap
    # (when needed) replaces the bare requirement with a tighter range --
    # it must never be an ADDITIONAL line, since TOML doesn't allow two
    # "serde_json" keys.
    serde_json_dep = (
        'serde_json = ">=1, <1.0.144"\n' if target_t < _SERDE_CORE_FLOOR
        else 'serde_json = "1"\n'
    )

    ecosystem_caps = ""
    if target_t < _SERDE_CORE_FLOOR:
        ecosystem_caps += (
            'serde = ">=1, <1.0.220"\n'
            'serde_derive = ">=1, <1.0.160"\n'
        )
    if target_t < _TINYVEC_PARSE_FLOOR:
        ecosystem_caps += 'tinyvec = ">=1, <1.11.0"\n'
    if target_t < _EDITION2021_FLOOR:
        ecosystem_caps += (
            'proc-macro2 = ">=1, <1.0.66"\n'
            'quote = ">=1, <1.0.43"\n'
        )
    if target_t < _SUBTLE_COHERENCE_FLOOR:
        ecosystem_caps += 'subtle = ">=2, <2.3"\n'
    if target_t < _BASE64CT_EDITION2024_FLOOR:
        # base64ct is a transitive hub crate pulled in by both rsa
        # (rsa -> pkcs1/pkcs8 -> der -> base64ct) and argon2 (via
        # password-hash) -- confirmed live via crates.io version metadata
        # that 1.8.0+ switched to edition="2024" (rust_version 1.85), while
        # msrv_repair.py's own metadata-snapshot repair loop can end up
        # resolving an OLDER, floor-compliant base64ct at pin time while the
        # real `cargo build --release` (a separate, unpinned re-resolution --
        # only Cargo.toml, not Cargo.lock, crosses the lockgen->builder stage
        # boundary) picks the newest semver-compatible release instead.
        # Capping it directly in Cargo.toml removes that gap: every cargo
        # invocation, pinning or real build, then sees the same constraint.
        ecosystem_caps += 'base64ct = ">=1, <1.8.0"\n'

    return (
        "[package]\n"
        'name = "app"\n'
        'version = "0.0.0"\n'
        'edition = "2018"\n\n'
        "[dependencies]\n"
        f"{fw_dep}"
        f"{extra_fw_deps}"
        f"{lib_dep}"
        f"{extra_lib_deps}"
        # No template uses #[derive(Serialize)]/#[derive(Deserialize)] --
        # every response is built via serde_json::json!() (a macro that
        # constructs a serde_json::Value directly, which has its own
        # hand-written Serialize impl, not a derived one) -- so serde's
        # "derive" feature is genuinely unused.
        f"{serde_json_dep}"
        f"{ecosystem_caps}"
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

# Some libraries need an extra build-time ENV var beyond sys_deps' apt
# packages to actually use them. sodiumoxide's libsodium-sys (confirmed via
# direct build.rs inspection): installing the "pkg-config" system package
# alone does nothing -- its build.rs only tries pkg-config when EITHER its
# own "use-pkg-config" crate feature is enabled or SODIUM_USE_PKG_CONFIG is
# set, otherwise it silently falls back to compiling libsodium from source
# (and fails, since we don't install a full autotools/make toolchain).
_LIB_BUILD_ENV = {
    "sodiumoxide": "ENV SODIUM_USE_PKG_CONFIG=1\n",
}

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

def prefetch_schema_all(crates):
    # Same rationale as prefetch_all -- parallelize the sparse-index fetch
    # too, so effective_floor's per-version schema check (added after a
    # real cc/1.60 failure -- see _INDEX_SCHEMA2_FLOOR) doesn't serialize
    # one HTTP round-trip per crate on top of the REST-API prefetch above.
    todo = [c for c in dict.fromkeys(crates) if c not in _schema_cache]
    if not todo:
        return
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(_fetch_schema_versions, todo))

_EDITION_FLOOR = {"2015": (1, 0), "2018": (1, 31), "2021": (1, 56), "2024": (1, 85)}

# Cargo's registry-index schema has its own "v" field, separate from a
# crate's declared rust-version/edition -- confirmed live via a real build
# failure that this is NOT just another floor to compare against, it's a
# visibility cliff: a release using namespaced/weak-dependency features
# (the `dep:crate` syntax, index schema "v":2, stabilized in cargo 1.60)
# is not merely rejected by older cargo, it is INVISIBLE to it -- old
# cargo's candidate list for that crate silently stops at the newest
# schema-v1 release, as if the newer ones don't exist at all. Confirmed
# live (cc 1.2.55+, "v":2 for its namespaced `dep:libc`/`dep:jobserver`
# features) that this happens even under a completely fresh, never-before
# -used BuildKit cache mount (full ~450s index resync from scratch) --
# ruling out any local/shared cache staleness explanation. The crates.io
# REST API's own /versions endpoint (what `fetch_versions` above uses)
# does NOT expose this "v"/"features2" schema info at all -- only the
# real sparse index does -- so it has to be fetched separately.
_INDEX_SCHEMA2_FLOOR = (1, 60)
_schema_cache = {}

def _sparse_index_path(crate):
    name = crate.lower()
    if len(name) <= 2:
        return f"{len(name)}/{name}"
    if len(name) == 3:
        return f"3/{name[0]}/{name}"
    return f"{name[0:2]}/{name[2:4]}/{name}"

def _fetch_schema_versions(crate):
    if crate in _schema_cache:
        return _schema_cache[crate]
    result = {}
    req = urllib.request.Request(
        f"https://index.crates.io/{_sparse_index_path(crate)}",
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            for line in r.read().decode().splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                result[entry["vers"]] = entry.get("v", 1)
    except Exception:
        pass  # unknown -- treat as schema v1 (no extra floor) rather than
        # block an otherwise-good candidate on a transient network failure
    _schema_cache[crate] = result
    return result

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
    schema_v = _fetch_schema_versions(v.get("crate", "")).get(v["num"], 1)
    if schema_v >= 2 and _INDEX_SCHEMA2_FLOOR > floor:
        floor = _INDEX_SCHEMA2_FLOOR
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

def _cargo_metadata():
    r = subprocess.run(
        ["cargo", "metadata", "--filter-platform", "x86_64-unknown-linux-gnu", "--format-version", "1"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)

def _split_pkg_id(pkg_id):
    after_hash = pkg_id.rsplit("#", 1)[-1]
    name, version = after_hash.rsplit("@", 1)
    return name, version

def _resolved_pkgs(meta):
    """The (name, version) set ACTUALLY REACHABLE from our own crate via
    active dependency edges -- NOT every node `cargo metadata` lists.
    `resolve.nodes` is the full closure of everything reachable under ANY
    feature combination, which can include packages our OWN build never
    actually activates. Confirmed live this mattered concretely:
    `serde_core` appeared as a bare node even with no `derive` feature
    requested anywhere, and blindly pinning it (rewrite_pinned_toml used
    to do the same raw-node iteration) force-activated its own hard,
    non-optional dependency on serde_derive -> syn -- recreating the
    exact wall this whole mechanism exists to avoid. A plain BFS from
    `resolve.root` over each node's `deps` (which cargo metadata DOES
    correctly restrict to feature-active edges -- confirmed live via a
    direct check of `app`'s own reported deps) gives the true reachable
    set instead."""
    nodes_by_id = {node["id"]: node for node in meta["resolve"]["nodes"]}
    root_id = meta["resolve"].get("root")
    if root_id is None:
        for node in meta["resolve"]["nodes"]:
            if node["id"].startswith("path+"):
                root_id = node["id"]
                break
    seen = set()
    stack = [root_id] if root_id else []
    while stack:
        node_id = stack.pop()
        if node_id in seen or node_id not in nodes_by_id:
            continue
        seen.add(node_id)
        for dep in nodes_by_id[node_id].get("deps", []):
            stack.append(dep.get("pkg", ""))
    pkgs = []
    for node_id in seen:
        if node_id.startswith("path+"):
            continue
        pkgs.append(_split_pkg_id(node_id))
    return pkgs

def _dependents_of(meta, target_name):
    """Direct parents (any resolved version) of `target_name` in the
    current dependency graph."""
    parents = []
    for node in meta["resolve"]["nodes"]:
        if node["id"].startswith("path+"):
            continue
        pname, pversion = _split_pkg_id(node["id"])
        for dep in node.get("deps", []):
            dep_id = dep.get("pkg", "")
            if dep_id.startswith("path+"):
                continue
            dep_name, _ = _split_pkg_id(dep_id)
            if dep_name == target_name:
                parents.append((pname, pversion))
    return parents

_dep_req_cache = {}

def _crate_dep_req(crate, version, dep_name):
    """(found, req): found=False means the API call itself failed
    (network/rate-limit) -- an UNKNOWN result, not evidence either way.
    found=True + req=None means the fetch succeeded and CONFIRMED
    `crate`@`version` does not depend on `dep_name` at all -- the best
    possible outcome for a cascade fix (see _cascade_fix), since an older
    parent that simply doesn't pull in the problem child eliminates it
    entirely, it doesn't just relax its requirement. Cached: cascade
    attempts revisit the same (crate, version, dep) triples repeatedly
    across parents/rounds."""
    key = (crate, version, dep_name)
    if key in _dep_req_cache:
        return _dep_req_cache[key]
    req_url = urllib.request.Request(
        f"https://crates.io/api/v1/crates/{crate}/{version}/dependencies",
        headers={"User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req_url, timeout=20) as r:
            data = json.load(r)
    except Exception:
        return (False, None)  # NOT cached -- a transient failure should
        # not permanently poison an otherwise-good candidate.
    result = (True, None)
    for d in data.get("dependencies", []):
        if d["crate_id"] == dep_name:
            result = (True, d.get("req"))
            break
    _dep_req_cache[key] = result
    return result

def _cascade_fix(name, version, meta):
    """When `name`@`version` has NO compatible candidate in its own
    semver class, a plain per-crate floor check can't fix it -- but the
    real cause is often a DIRECT PARENT that recently bumped its own
    requirement onto a newer major of `name` (confirmed live: serde_derive
    1.0.229, published the same day syn 3.0.0 shipped, requires syn "^3",
    which has no old-rustc-compatible release at all -- but serde_derive
    1.0.228, one patch older, requires syn "^2.0.81" instead, which does).
    Looks for a same-semver-class OLDER release of a direct parent whose
    OWN requirement on `name` opens up a compatible candidate, and returns
    both substitutions together (they must move as one unit) so the
    caller can batch them in the same round.

    Also handles the even better case (confirmed live: serde_core, a
    recent internal split out of serde with no old-rustc-compatible
    release at all): an older parent candidate that doesn't depend on the
    problem crate AT ALL. An earlier version of this function treated
    that (`req` falsy) as "not applicable, keep looking" -- exactly
    backwards, since a parent that drops the dependency entirely is a
    STRICTLY BETTER fix than merely relaxing it, and needs no further
    substitution for the child at all. Candidates are capped at 25 per
    parent to bound worst-case latency (each check is a live crates.io
    lookup) -- confirmed live an earlier, uncapped version of this loop
    could run for 20+ minutes against a crate with a long version history."""
    if meta is None:
        return None
    for pname, pversion in _dependents_of(meta, name):
        candidates = candidate_order(pname, pversion)[:25]
        # Prefetch every candidate's dep-req in parallel -- confirmed live
        # doing this sequentially (one HTTP round-trip per candidate,
        # tried in strict order) was the actual cause of a 20+ minute
        # hang. Order is still respected below (first match wins); this
        # just removes the network latency between checks.
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(lambda c: _crate_dep_req(pname, c, name), candidates))
        for p_candidate in candidates:
            found, req = _crate_dep_req(pname, p_candidate, name)
            if not found:
                continue  # transient failure -- try the next candidate
            if req is None:
                return [(pname, pversion, p_candidate)]
            bare_req = req.lstrip("^")
            try:
                child_candidates = [
                    v["num"] for v in fetch_versions(name)
                    if not v.get("yanked")
                    and caret_compatible(bare_req, v["num"])
                    and effective_floor(v) <= TARGET_T
                ]
            except ValueError:
                continue
            if child_candidates:
                return [(pname, pversion, p_candidate), (name, version, child_candidates[0])]
    return None

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
        meta = _cargo_metadata()
        if meta is None:
            print("  ! cargo metadata failed, aborting repair loop")
            return
        pkgs = _resolved_pkgs(meta)
        prefetch_all(name for name, _ in pkgs)
        prefetch_schema_all(name for name, _ in pkgs)

        to_fix = []
        scheduled_names = set()
        for name, version in pkgs:
            if (name, version) in given_up or name in scheduled_names:
                continue
            floor = current_floor(name, version)
            if floor is None or floor <= TARGET_T:
                continue
            candidates = [c for c in candidate_order(name, version) if c != version]
            if candidates:
                to_fix.append((name, version, candidates[0]))
                scheduled_names.add(name)
                continue

            # No fix within this crate's own semver class -- a DIRECT
            # PARENT may have relaxed its requirement in an older release
            # (see _cascade_fix's docstring: serde_derive+syn is the
            # confirmed-live case, but this isn't hardcoded to that pair).
            cascade = _cascade_fix(name, version, meta)
            if cascade and not any(n in scheduled_names for n, _, _ in cascade):
                for n, v, r in cascade:
                    to_fix.append((n, v, r))
                    scheduled_names.add(n)
                print(f"  ! {name} {version} needs rust {floor} > {TARGET} -- fixed via cascade (parent downgrade)")
                continue

            print(f"  ! {name} {version} needs rust {floor} > {TARGET}, no compatible version found in the same semver range -- genuine floor")
            given_up.add((name, version))

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
                continue
            if "did not match any packages" in r.stderr:
                # A cascade fix schedules a parent-downgrade + child-downgrade
                # pair together (see _cascade_fix) that must move as one unit
                # -- but this per-crate fallback applies them SEQUENTIALLY, so
                # if the parent's own update runs first, it can re-resolve the
                # child crate away from its old version as a side effect,
                # BEFORE the child's own turn in this same loop. Confirmed
                # live (syn 2.0.32, scheduled alongside its parent's
                # downgrade): by the time syn's own `cargo update -p
                # syn@2.0.32` ran, syn was already gone from the graph, so
                # cargo rejected the PkgId outright with "did not match any
                # packages" -- a false failure, not a real one. Check whether
                # the crate is still resolved at the stale version at all
                # before giving up on it.
                check_meta = _cargo_metadata()
                still_present = check_meta is not None and (name, version) in _resolved_pkgs(check_meta)
                if not still_present:
                    print(f"  {name} {version} already resolved away by a co-scheduled fix -- nothing to do")
                    continue
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

def _orig_req_string(orig_value):
    return orig_value.get("version", "") if isinstance(orig_value, dict) else orig_value

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
    for name, version in _resolved_pkgs(meta):
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
            # Multiple incompatible majors resolved for one crate name --
            # if it's one of OUR OWN direct deps, the "protected" (feature-
            # preserving) treatment must go to whichever candidate ACTUALLY
            # satisfies our own declared requirement, not just whichever
            # sorts first. Confirmed live this was a real bug: for
            # rand_core resolved at both 0.3.1/0.4.2/0.6.4 (pulled in by
            # unrelated old crates), the alphabetically-first 0.3.1 got our
            # own features=["getrandom"] applied to it (wrong -- we asked
            # for "0.6"), while the ACTUAL 0.6.4 we needed got aliased away
            # without that feature, breaking the OsRng import entirely.
            protected_idx = None
            if name in protected:
                req = _orig_req_string(orig_deps[name])
                for idx, v in enumerate(versions):
                    if req and caret_compatible(req, v):
                        protected_idx = idx
                        break
            for i, v in enumerate(versions):
                if i == protected_idx:
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

def _reconcile_new_pins(max_rounds=5):
    """rewrite_pinned_toml() pins everything `cargo metadata` reveals at
    THAT moment -- but confirmed live that re-resolving the JUST-WRITTEN,
    fully-pinned manifest can surface packages that were never part of the
    earlier (range-based) snapshot at all (e.g. `lock_api`, which only
    became part of the graph once some OTHER package's version was
    already pinned exactly) -- these were never floor-checked by
    repair_loop() and can silently carry a too-new requirement straight
    into the real build. repair_loop()'s own `cargo update --precise`
    mechanism can't fix this at this stage (the manifest now has hard "=X"
    requirements cargo won't let a plain update cross), so this edits the
    TOML text directly instead: floor-check every currently resolved
    package, and either fix its existing pin in place or add a brand new
    one, looping until the resolved set needs no more changes."""
    for round_num in range(max_rounds):
        meta = _cargo_metadata()
        if meta is None:
            print("  ! cargo metadata failed during pin verification")
            return
        with open("Cargo.toml", "rb") as f:
            data = tomllib.load(f)
        deps = data.get("dependencies", {})
        by_crate = defaultdict(list)
        for key, val in deps.items():
            if isinstance(val, dict) and "package" in val:
                crate = val["package"]
                ver = val.get("version", "").lstrip("=")
            else:
                crate = key
                ver = (val.get("version") if isinstance(val, dict) else val) or ""
                ver = ver.lstrip("=")
            by_crate[crate].append((key, ver))

        changed = False
        for name, version in _resolved_pkgs(meta):
            floor = current_floor(name, version)
            target_version = version
            if floor is not None and floor > TARGET_T:
                candidates = [c for c in candidate_order(name, version) if c != version]
                if candidates:
                    target_version = candidates[0]
                else:
                    print(f"  ! newly-surfaced {name} {version} needs rust {floor} > {TARGET}, no compatible version -- leaving as-is")

            if name not in by_crate:
                deps[name] = f"={target_version}"
                changed = True
                print(f"  newly-surfaced {name} -> pinned at ={target_version}")
                continue

            for key, ver in by_crate[name]:
                if ver == version and target_version != version:
                    val = deps[key]
                    if isinstance(val, dict):
                        val["version"] = f"={target_version}"
                    else:
                        deps[key] = f"={target_version}"
                    changed = True
                    print(f"  reconciled {name} {version} (needs {floor}) -> {target_version}")

        if not changed:
            print(f"pin set stable after {round_num} reconcile round(s)")
            return

        pkg = data["package"]
        lines = []
        for key in sorted(deps):
            val = deps[key]
            if isinstance(val, dict):
                if "package" in val:
                    lines.append(f'{key} = {{ package = "{val["package"]}", version = "{val["version"]}" }}')
                else:
                    feats = val.get("features")
                    if feats:
                        feat_str = ", ".join(f'"{ft}"' for ft in feats)
                        lines.append(f'{key} = {{ version = "{val["version"]}", features = [{feat_str}] }}')
                    else:
                        lines.append(f'{key} = "{val["version"]}"')
            else:
                lines.append(f'{key} = "{val}"')
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
    print("gave up reconciling new pins after max rounds")

repair_loop()
rewrite_pinned_toml()
_reconcile_new_pins()
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
    lib_build_env = _LIB_BUILD_ENV.get(lib_name, "")

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
        f"{lib_build_env}"
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

    msrv_target = _effective_msrv_target(lang_ver, _fw_kind(fw_name, fw_major))

    (out / "src" / "main.rs").write_text(
        make_main_rs(fw_name, fw_major, lib_name, lib_ver), encoding="utf-8"
    )
    (out / "Cargo.toml").write_text(
        make_cargo_toml(fw_name, fw_major, fw_resolved, lib_name, lib_resolved, msrv_target, lib_ver),
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

