"""
Node.js-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_node").

Required exports:
    LANGUAGE_ID   – str
    REGISTRY_FILE – str
    prefetch(lang_data)                                          -> None
    write_context(lang_ver, fw_name, fw_major,
                  lib_name, lib_ver, images_base)               -> bool
"""

import json
import re
import shutil
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "node"
REGISTRY_FILE = "registry node.json"


def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


# ── Library metadata ──────────────────────────────────────────────────────────

# blank: a concrete import target used so the module is loaded/exercised even
# though the app doesn't call it directly (mirrors Go's "blank_import").
LIB_META: dict = {
    "crypto":      {"npm": None, "blank": "crypto", "sys_deps": []},
    "node-forge":  {"npm": "node-forge",   "blank": "node-forge",   "sys_deps": []},
    "jose":        {"npm": "jose",         "blank": "jose",         "sys_deps": []},
    "crypto-js":   {"npm": "crypto-js",    "blank": "crypto-js",    "sys_deps": []},
    "sodium-native": {
        "npm": "sodium-native", "blank": "sodium-native",
        # node-gyp needs python3/make/g++; when no prebuilt binary exists for
        # the exact Node ABI (e.g. an old sodium-native major on a very new
        # Node major), it falls back to building libsodium from source via
        # autotools, which additionally needs autoconf/automake/libtool.
        "sys_deps": ["python3", "make", "g++", "autoconf", "automake", "libtool"],
    },
    # Placeholder blank -- @noble/curves and @noble/post-quantum have NO
    # usable root module at any version: root index.js unconditionally
    # `throw`s ("Incorrect usage. Import submodules instead" / "root module
    # cannot be imported"), verified against npm tarballs from 0.1.0 through
    # the latest 2.2.0 / 0.6.1. A real submodule must be picked per-version
    # instead (see _lib_blank_line + _NOBLE_CURVES_BLANK / _NOBLE_PQ_BLANK).
    "@noble/curves": {"npm": "@noble/curves", "blank": "@noble/curves", "sys_deps": []},
    "@noble/post-quantum": {"npm": "@noble/post-quantum", "blank": "@noble/post-quantum", "sys_deps": []},
}

# @noble/curves: the secp256k1 submodule's exports key changes shape across
# versions (verified by downloading and inspecting the actual npm tarball
# per version, not just the exports map, after the root-import guard above
# bit us once already):
#   < 0.5.0   -- secp256k1 doesn't exist as a submodule yet; only a handful
#                of internal helpers do. './utils' is present throughout.
#   0.5.0-1.9.5 -- exports './secp256k1' (no extension)
#   >= 1.9.6  -- exports './secp256k1.js' (extension required)
def _noble_curves_blank(lib_ver: str) -> str:
    v = _parse(lib_ver)
    if v < (0, 5):
        return "@noble/curves/utils"
    if v < (1, 9, 6):
        return "@noble/curves/secp256k1"
    return "@noble/curves/secp256k1.js"


# @noble/post-quantum: same shape of churn on the ml-kem submodule, verified
# the same way. < 0.6.0 exports './ml-kem' (no extension); >= 0.6.0 exports
# './ml-kem.js'. This lines up with the ESM-only threshold below, but it's
# a separate fact (an ESM package can still keep an unversioned subpath, and
# vice versa) so it's kept as its own function rather than reusing that dict.
def _noble_pq_blank(lib_ver: str) -> str:
    v = _parse(lib_ver)
    if v < (0, 6):
        return "@noble/post-quantum/ml-kem"
    return "@noble/post-quantum/ml-kem.js"

# jose 3.x (the whole 3.0.0-3.20.4 line) has an "exports" map but never
# defines a root (".") entry at all -- only granular submodules like
# ./jwt/sign, ./jwk/parse, ./util/base64url. require('jose') throws
# ERR_PACKAGE_PATH_NOT_EXPORTED for every 3.x release. './util/base64url'
# is present across the entire 3.x line, so use it as the blank target for
# that major only; every other tracked major (1,2,4,5,6) has a proper root
# export.
_JOSE_V3_BLANK = "jose/util/base64url"

# Library major versions that ship as ESM-only ("type": "module" in
# package.json) -- app.js (CommonJS) must dynamic-import() these instead of
# require()'ing them directly. Verified against the npm registry per-version;
# each library's earlier majors are plain CommonJS.
_ESM_ONLY_FROM: dict = {
    "jose":                (6, 0),
    "@noble/curves":       (2, 0),
    "@noble/post-quantum": (0, 6),
}


def _is_esm_only(lib_name: str, lib_ver: str) -> bool:
    threshold = _ESM_ONLY_FROM.get(lib_name)
    if threshold is None:
        return False
    return _parse(lib_ver) >= threshold


def _lib_npm(lib_name: str) -> str | None:
    return LIB_META[lib_name]["npm"]


def _lib_sys_deps(lib_name: str) -> list:
    return LIB_META[lib_name]["sys_deps"]


def _lib_blank_line(lib_name: str, lib_ver: str) -> str:
    blank = LIB_META[lib_name]["blank"]
    if not blank:
        return ""
    if lib_name == "@noble/curves":
        blank = _noble_curves_blank(lib_ver)
    if lib_name == "@noble/post-quantum":
        blank = _noble_pq_blank(lib_ver)
    if lib_name == "jose" and _parse(lib_ver)[:1] == (3,):
        blank = _JOSE_V3_BLANK
    if _is_esm_only(lib_name, lib_ver):
        return f"import('{blank}').catch(() => {{}});"
    return f"require('{blank}');"


# ── Debian archive fix (only needed when a library has sys_deps, i.e.
#   sodium-native's node-gyp toolchain, on a Node version whose Debian base
#   has been dropped from the live mirrors) ────────────────────────────────

def _debian_archive_codename(node_ver: str):
    """Debian codename for node:{node_ver}-slim's base image, if it has been
    pulled from the live Debian mirrors and only survives on archive.debian.org.

    Verified directly against each node:X-slim image's /etc/os-release:
      0.10/0.12/4 -> jessie, 6/8/10/12 -> stretch, 14/16 -> buster (all three
      dropped from deb.debian.org/security.debian.org); 18+ -> bookworm/trixie
      (still live at the time of writing).
    """
    v = _parse(node_ver)
    if v < (6,):
        return "jessie"
    if v < (14,):
        return "stretch"
    if v < (18,):
        return "buster"
    return None


def _debian_archive_apt(node_ver: str):
    """Return (apt_sources, apt_flag, allow_unauth) Dockerfile fragments that
    redirect apt to archive.debian.org and tolerate its expired Release
    signatures, when node:{node_ver}-slim's base is no longer on the live
    mirrors. All three are empty strings when the base is still live.
    """
    codename = _debian_archive_codename(node_ver)
    apt_sources = (
        f"RUN echo 'deb http://archive.debian.org/debian {codename} main' > /etc/apt/sources.list \\\n"
        f"    && echo 'deb http://archive.debian.org/debian-security {codename}/updates main' >> /etc/apt/sources.list\n"
        if codename else ""
    )
    apt_flag     = "-o Acquire::Check-Valid-Until=false " if codename else ""
    allow_unauth = "--allow-unauthenticated "              if codename else ""
    return apt_sources, apt_flag, allow_unauth


# ── App templates ─────────────────────────────────────────────────────────────
# CommonJS throughout (require()) for compatibility across Node 4-26 -- ESM
# only stabilized at Node 12+, but the tracked Node range starts at 4.
# Tokens: __LIB_LINE__, __LIB_NAME__, __LIB_VER_EXPR__.
#
# No `async` handlers, for the same reason: async/await is a syntax error
# below Node 7.6. Fastify's "return the value as the response" convenience
# only works for async/Promise-returning handlers -- a plain sync handler
# that just `return`s a value never actually responds (confirmed by hanging
# a real request against it), so Fastify handlers call `reply.send()`
# explicitly instead. Koa's middleware doesn't have that restriction (every
# middleware call is wrapped in `Promise.resolve()` internally regardless of
# whether the function is async), so plain sync functions there work as-is.

_PKG_VERSION_HELPER = """\
const fs = require("fs");
const path = require("path");
function pkgVersion(name) {
\ttry {
\t\treturn JSON.parse(fs.readFileSync(path.join(__dirname, "node_modules", name, "package.json"), "utf8")).version;
\t} catch (e) {
\t\treturn "unknown";
\t}
}
"""

_EXPRESS_TPL = """\
const express = require("express");
__LIB_LINE__

const app = express();

app.get("/", (req, res) => {
\tres.json({ message: "Hello World" });
});

app.get("/version", (req, res) => {
\tres.json({
\t\tlanguage: { name: "Node.js", version: process.version },
\t\tframework: { name: "Express", version: pkgVersion("express") },
\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t});
});

app.listen(8000);
"""

_FASTIFY_TPL = """\
const fastify = require("fastify")();
__LIB_LINE__

fastify.get("/", (request, reply) => {
\treply.send({ message: "Hello World" });
});

fastify.get("/version", (request, reply) => {
\treply.send({
\t\tlanguage: { name: "Node.js", version: process.version },
\t\tframework: { name: "Fastify", version: pkgVersion("fastify") },
\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t});
});

fastify.listen({ port: 8000, host: "0.0.0.0" }, (err) => {
\tif (err) {
\t\tconsole.error(err);
\t\tprocess.exit(1);
\t}
});
"""

_KOA_TPL = """\
const Koa = require("koa");
__LIB_LINE__

const app = new Koa();

app.use((ctx) => {
\tif (ctx.path === "/") {
\t\tctx.body = { message: "Hello World" };
\t} else if (ctx.path === "/version") {
\t\tctx.body = {
\t\t\tlanguage: { name: "Node.js", version: process.version },
\t\t\tframework: { name: "Koa", version: pkgVersion("koa") },
\t\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t\t};
\t} else {
\t\tctx.status = 404;
\t}
});

app.listen(8000);
"""

_APP_TPL = {
    "Express": _EXPRESS_TPL,
    "Fastify": _FASTIFY_TPL,
    "Koa":     _KOA_TPL,
}


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


def make_app_js(fw_name: str, lib_name: str, lib_ver: str) -> str:
    lib_ve = '"built-in"' if lib_name == "crypto" else f'pkgVersion("{_lib_npm(lib_name)}")'
    body = _sub(
        _APP_TPL[fw_name],
        LIB_LINE     = _lib_blank_line(lib_name, lib_ver),
        LIB_NAME     = lib_name,
        LIB_VER_EXPR = lib_ve,
    )
    return _PKG_VERSION_HELPER + "\n" + body


# ── package.json generation ───────────────────────────────────────────────────

def make_package_json(fw_name: str, fw_resolved: str,
                      lib_name: str, lib_resolved: str) -> str:
    deps = {}
    fw_module = {"Express": "express", "Fastify": "fastify", "Koa": "koa"}[fw_name]
    deps[fw_module] = fw_resolved
    lib_npm = _lib_npm(lib_name)
    if lib_npm and lib_resolved:
        deps[lib_npm] = lib_resolved
    manifest = {
        "name": "app",
        "private": True,
        "version": "0.0.0",
        "dependencies": deps,
    }
    return json.dumps(manifest, indent=2) + "\n"


# ── Dockerfile generation ─────────────────────────────────────────────────────

def make_dockerfile(node_ver: str, lib_name: str) -> str:
    sys_deps = _lib_sys_deps(lib_name)
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(node_ver) if sys_deps else ("", "", "")

    apt_block = ""
    if sys_deps:
        deps_line = " ".join(sys_deps)
        apt_block = (
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {deps_line} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )

    return (
        f"FROM node:{node_ver}-slim\n"
        "WORKDIR /app\n"
        f"{apt_block}"
        "COPY package.json .\n"
        "RUN npm install --no-audit --no-fund\n"
        "COPY app.js .\n"
        "EXPOSE 8000\n"
        'CMD ["node", "app.js"]\n'
    )


# ── npm registry version resolution ──────────────────────────────────────────

_NPM_RELEASES: dict = {}


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in re.findall(r"\d+", v.split("-")[0]))
    except ValueError:
        return (0,)


def _fetch_releases(npm_name: str) -> list:
    if npm_name in _NPM_RELEASES:
        return _NPM_RELEASES[npm_name]

    safe_name = urllib.parse.quote(npm_name, safe="")
    url = f"https://registry.npmjs.org/{safe_name}?fields=versions"
    releases = []
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        releases = sorted(
            (v for v in data.get("versions", {}) if re.match(r"^\d+(\.\d+)*$", v)),
            key=_ver_key,
        )
    except (URLError, KeyError, json.JSONDecodeError, OSError) as exc:
        print(f"  [WARN] npm lookup failed for {npm_name}: {exc}", flush=True)

    _NPM_RELEASES[npm_name] = releases
    return releases


def _resolve(npm_name: str, registry_ver: str) -> str | None:
    """Resolve a registry version like '4' or '0.10' to the latest matching
    release on npm (e.g. '4' -> '4.22.2')."""
    releases = _fetch_releases(npm_name)

    prefix = registry_ver + "."
    candidates = [v for v in releases if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in releases:
        return registry_ver

    return None


# ── Pre-fetch ─────────────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch version lists from the npm registry for all packages."""
    npm_names: set = set()
    for fw in lang_data.get("frameworks", []):
        if not fw.get("include", True):
            continue
        npm_names.add(fw["module"])
    for lib in lang_data.get("cryptography_libs", []):
        if lib.get("version") == "built-in":
            continue
        npm = _lib_npm(lib["name"])
        if npm:
            npm_names.add(npm)

    print("Fetching available versions from npm ...")
    for name in sorted(npm_names):
        releases = _fetch_releases(name)
        print(f"  {name}: {len(releases)} version(s) found")
    print()


# ── Public interface ──────────────────────────────────────────────────────────

def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write app.js / package.json / Dockerfile for one image context.

    Returns False (and removes any stale directory) when a required package
    version cannot be resolved on the npm registry.
    """
    out = images_base / "node" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    fw_module = {"Express": "express", "Fastify": "fastify", "Koa": "koa"}[fw_name]
    fw_resolved = _resolve(fw_module, fw_major)
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on npm", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    lib_resolved = ""
    lib_npm = _lib_npm(lib_name)
    if lib_npm and lib_ver != "builtin":
        lib_resolved = _resolve(lib_npm, lib_ver)
        if lib_resolved is None:
            print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on npm", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    out.mkdir(parents=True, exist_ok=True)
    (out / "app.js").write_text(
        make_app_js(fw_name, lib_name, lib_resolved or lib_ver), encoding="utf-8"
    )
    (out / "package.json").write_text(
        make_package_json(fw_name, fw_resolved, lib_name, lib_resolved), encoding="utf-8"
    )
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, lib_name), encoding="utf-8"
    )
    return True
