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
    "tweetnacl": {"npm": "tweetnacl", "blank": "tweetnacl", "sys_deps": []},
    "node-jose": {"npm": "node-jose", "blank": "node-jose", "sys_deps": []},
    "bcrypt": {
        "npm": "bcrypt", "blank": "bcrypt",
        # Native node-gyp binding, same toolchain class as sodium-native
        # (prebuilt binaries exist for most current combos, falls back to
        # source compile otherwise).
        "sys_deps": ["python3", "make", "g++"],
    },
    "bcryptjs": {"npm": "bcryptjs", "blank": "bcryptjs", "sys_deps": []},
    "argon2": {
        "npm": "argon2", "blank": "argon2",
        "sys_deps": ["python3", "make", "g++"],
    },
    # liboqs-node is NOT installed via npm at all (see make_dockerfile's
    # LIBOQS_NODE special case) -- its own published tarball is missing the
    # git submodules it needs to build, confirmed via a real `npm install`
    # failure. "npm": None here means write_context()/make_package_json()
    # correctly skip adding it as a package.json dependency; the blank-import
    # path below points at the absolute git-clone location instead of a
    # normal bare package name.
    "liboqs-node": {"npm": None, "blank": "/opt/liboqs-node/lib/index.js", "sys_deps": []},
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

# Express 1.x/2.x -- confirmed via a real docker run: entry point is
# `express.createServer()` (removed at 3.0), and `res.send(obj)` (an object
# short-circuits into a JSON response automatically) rather than
# `res.json(obj)`.
_EXPRESS_LEGACY_TPL = """\
var express = require("express");
__LIB_LINE__

var app = express.createServer();

app.get("/", function (req, res) {
\tres.send({ message: "Hello World" });
});

app.get("/version", function (req, res) {
\tres.send({
\t\tlanguage: { name: "Node.js", version: process.version },
\t\tframework: { name: "Express", version: pkgVersion("express") },
\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t});
});

app.listen(8000);
"""

# Koa 1.x -- confirmed via a real docker run: pre-async/await generator-
# function middleware (`function *()`, `this.body = ...`), native from
# Node 4 onward with no transpile step.
_KOA_LEGACY_TPL = """\
var koa = require("koa");
__LIB_LINE__

var app = koa();

app.use(function *() {
\tif (this.path === "/") {
\t\tthis.body = { message: "Hello World" };
\t} else if (this.path === "/version") {
\t\tthis.body = {
\t\t\tlanguage: { name: "Node.js", version: process.version },
\t\t\tframework: { name: "Koa", version: pkgVersion("koa") },
\t\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t\t};
\t} else {
\t\tthis.status = 404;
\t}
});

app.listen(8000);
"""

# Hapi (@hapi/hapi 17+) -- async server.route()/server.start(), confirmed
# via a real docker run on node:22-slim.
_HAPI_TPL = """\
const Hapi = require("@hapi/hapi");
__LIB_LINE__

const init = async () => {
\tconst server = Hapi.server({ port: 8000, host: "0.0.0.0" });

\tserver.route({
\t\tmethod: "GET",
\t\tpath: "/",
\t\thandler: () => ({ message: "Hello World" }),
\t});

\tserver.route({
\t\tmethod: "GET",
\t\tpath: "/version",
\t\thandler: () => ({
\t\t\tlanguage: { name: "Node.js", version: process.version },
\t\t\tframework: { name: "Hapi", version: pkgVersion("@hapi/hapi") },
\t\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t\t}),
\t});

\tawait server.start();
};

init();
"""

# Restify -- confirmed via a real docker run: handlers on the tracked
# majors (9-11) must be declared `async`, a plain sync handler throws a
# hard AssertionError at route-registration time.
#
# BUG FIXED 2026-07-13: `res.json(...)` throws "res.json is not a function"
# on major "0" (resolves to 0.5.7, restify's actual last-ever 0.x release,
# confirmed via the real npm version list) -- that ancient release only
# monkey-patches `http.ServerResponse.prototype.send`, `res.json()` doesn't
# exist as a convenience method yet. Confirmed via a real docker build+run
# failure (batch test run node-Restify-20260713-001, 672 failures spanning
# almost every tracked major, not just "0" -- see below). Fixed by switching
# to `res.send(200, {...})`, the one call shape confirmed to work across
# restify's ENTIRE tracked history: 0.5.7's `send()` requires a numeric
# first argument in its "legacy" positional form (an object with no `.code`
# property throws `TypeError: options.code must be a number`), while the
# latest restify (11.x) documents `res.send(201, {hello: 'world'})` as a
# first-class supported call shape in its own source comments -- so the
# explicit status code isn't just an 0.5.7 workaround, it's the one form
# guaranteed stable at both ends of the version range.
_RESTIFY_TPL = """\
const restify = require("restify");
__LIB_LINE__

const server = restify.createServer();

server.get("/", async (req, res) => {
\tres.send(200, { message: "Hello World" });
});

server.get("/version", async (req, res) => {
\tres.send(200, {
\t\tlanguage: { name: "Node.js", version: process.version },
\t\tframework: { name: "Restify", version: pkgVersion("restify") },
\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t});
});

server.listen(8000, "0.0.0.0");
"""

# Restify majors 0-8 -- a plain (non-async) handler, needed because these
# majors' own compatibility ranges reach back to Node 4-9 (pre-7.6, unable
# to even PARSE `async (req, res) =>` syntax -- a hard SyntaxError, not a
# runtime one). Confirmed via a real docker build+run failure (Node 6,
# restify major "1") that the unconditional `async` in `_RESTIFY_TPL` broke
# every one of these older majors, even though majors 9-11 (whose own
# compatibility never reaches below Node 10, already async-capable)
# structurally REQUIRE async and must keep it -- there is no Node-version
# overlap between "major needs async" and "Node can't parse async", so a
# plain per-major template split (not a Node-version check) is the correct
# and simplest fix.
_RESTIFY_LEGACY_TPL = """\
const restify = require("restify");
__LIB_LINE__

const server = restify.createServer();

server.get("/", (req, res, next) => {
\tres.send(200, { message: "Hello World" });
\treturn next();
});

server.get("/version", (req, res, next) => {
\tres.send(200, {
\t\tlanguage: { name: "Node.js", version: process.version },
\t\tframework: { name: "Restify", version: pkgVersion("restify") },
\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t});
\treturn next();
});

server.listen(8000, "0.0.0.0");
"""

# Sails -- confirmed via a real docker run: every non-routing hook
# (grunt/views/session/policies/orm/pubsub/i18n) can be disabled via config,
# leaving a genuinely standalone 2-route app despite the full-MVC framework
# underneath.
#
# BUG FIXED 2026-07-13: without `i18n: false`, major "0" (0.12.14) crashed on
# Node 4 with `error: A hook (i18n) failed to load! [SyntaxError: Block-scoped
# declarations (let, const, function, class) not yet supported outside strict
# mode]` -- confirmed via a real docker run, then traced by manually
# require()-ing the actual failing file inside the container
# (`node_modules/sails/node_modules/i18n/node_modules/debug/src/index.js`).
# Root cause: the bundled `i18n` package's own package.json declares
# `"debug": "*"` -- an unpinned WILDCARD, meaning npm resolves it to
# whatever debug's own absolute-latest release is at install time, and a
# recent debug release uses `let`/`const` outside strict mode, which V8
# versions before ~Node 6 can't parse. This is a moving target (which debug
# version resolves can shift over time as new releases ship, unlike this
# project's other unpinned-transitive-dependency fixes, which pin a Node
# floor because OUR OWN package.json can't reach three levels into another
# package's nested node_modules to override it) -- rather than narrowing the
# registry's Node floor (which would have meant permanently dropping Node
# 4/5 support for a dependency this project's app code never even uses),
# simply disabling the i18n hook entirely sidesteps the whole unpinned
# dependency -- confirmed via a real docker run that Node 4 now boots and
# serves both routes correctly with `i18n: false` added alongside the other
# already-disabled hooks.
_SAILS_TPL = """\
const sails = require("sails");
__LIB_LINE__

sails.lift(
\t{
\t\thooks: { grunt: false, views: false, session: false, policies: false, orm: false, pubsub: false, i18n: false },
\t\tlog: { level: "warn" },
\t\troutes: {
\t\t\t"GET /": (req, res) => res.json({ message: "Hello World" }),
\t\t\t"GET /version": (req, res) =>
\t\t\t\tres.json({
\t\t\t\t\tlanguage: { name: "Node.js", version: process.version },
\t\t\t\t\tframework: { name: "Sails", version: pkgVersion("sails") },
\t\t\t\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t\t\t\t}),
\t\t},
\t\tport: 8000,
\t\thost: "0.0.0.0",
\t},
\t(err) => {
\t\tif (err) {
\t\t\tconsole.error(err);
\t\t\tprocess.exit(1);
\t\t}
\t}
);
"""

_APP_TPL = {
    "Express": _EXPRESS_TPL,
    "Fastify": _FASTIFY_TPL,
    "Koa":     _KOA_TPL,
    "Hapi":    _HAPI_TPL,
    "Restify": _RESTIFY_TPL,
    "Sails":   _SAILS_TPL,
}

# Framework majors that use a materially different template than their
# framework's default (Express 1/2's createServer() era, Koa 1's
# generator-function era) -- verified live, see the templates above.
_LEGACY_APP_TPL = {
    ("Express", "1"): _EXPRESS_LEGACY_TPL,
    ("Express", "2"): _EXPRESS_LEGACY_TPL,
    ("Koa", "1"):     _KOA_LEGACY_TPL,
    **{("Restify", str(m)): _RESTIFY_LEGACY_TPL for m in range(0, 9)},
}

# ── NestJS (TypeScript compile step) ────────────────────────────────────────
# Nest's decorator-based DI reflection depends on TypeScript's own
# emitDecoratorMetadata emission -- plain V8/Node runtime decorators don't
# produce the metadata Nest's container needs (confirmed via a real docker
# build: a hand-written plain-.js version using the --experimental-decorators
# *runtime* flag either doesn't parse on some Node majors or never actually
# wires up DI). This template is TypeScript source, compiled via `tsc`
# before running -- see make_dockerfile()'s "typescript" framework kind.
#
# BUG FIXED 2026-07-12: `app.listen(8000, "0.0.0.0")` -- the explicit host
# argument -- fails to type-check on @nestjs/core majors 1-5 ("Argument of
# type 'string' is not assignable to parameter of type '() => void'"),
# confirmed via a real docker build: `INestApplication.listen()`'s overload
# only gained an explicit host parameter in a later Nest version; before
# that it was `listen(port, callback?)`. Simply dropping the host argument
# (`app.listen(8000)`) works identically across every major and needs no
# version branching -- Node's own default `http.Server.listen(port)`
# behavior (no host given) already binds all interfaces, which is exactly
# why Express/Koa/Hapi's own templates in this file already just call
# `app.listen(8000)` with no host either.
_NESTJS_TPL = """\
import "reflect-metadata";
import { Controller, Get, Module } from "@nestjs/common";
import { NestFactory } from "@nestjs/core";
__LIB_LINE__

@Controller()
class AppController {
\t@Get("/")
\troot() {
\t\treturn { message: "Hello World" };
\t}

\t@Get("/version")
\tversion() {
\t\treturn {
\t\t\tlanguage: { name: "Node.js", version: process.version },
\t\t\tframework: { name: "NestJS", version: pkgVersion("@nestjs/core") },
\t\t\tlibrary: { name: "__LIB_NAME__", version: __LIB_VER_EXPR__ },
\t\t};
\t}
}

@Module({ controllers: [AppController] })
class AppModule {}

async function bootstrap() {
\tconst app = await NestFactory.create(AppModule);
\tawait app.listen(8000);
}
bootstrap();
"""

_APP_TPL["NestJS"] = _NESTJS_TPL

# ── AdonisJS (scaffold-then-inject-routes) ──────────────────────────────────
# Confirmed via a real docker build+run: AdonisJS's own official minimal
# starter (`create-adonisjs --kit=api`) is scaffolded IN the Dockerfile,
# then start/routes.ts is overwritten with the project's 2 standard routes
# -- a genuinely different Dockerfile shape (see make_dockerfile()'s
# "scaffold" framework kind) but not a reason to exclude the framework, per
# this project's standing rule that "needs different tooling" alone is
# never sufficient grounds to skip a real, buildable combination.
#
# AdonisJS's own module system is native ESM ("type": "module") -- a plain
# `require(...)` throws `require is not defined` at runtime (confirmed via
# a real docker run). Every other framework in this file is CommonJS, so
# rather than reworking _lib_blank_line's require()/dynamic-import() choice
# for this one ESM context, the routes file bridges via Node's own
# `createRequire`, which accepts the exact same bare-name/absolute-path
# targets _lib_blank_line already resolves (crucially including
# liboqs-node's absolute git-clone path, which isn't a valid static ESM
# import specifier at all).
_ADONIS_ROUTES_TPL = """\
import router from "@adonisjs/core/services/router";
import { createRequire } from "module";
const require = createRequire(import.meta.url);
__LIB_LINE__

router.get("/", () => ({ message: "Hello World" }));
router.get("/version", () => ({
\tlanguage: { name: "Node.js", version: process.version },
\tframework: { name: "AdonisJS", version: "__FW_VERSION__" },
\tlibrary: { name: "__LIB_NAME__", version: "__LIB_VERSION__" },
}));
"""


def _sub(tpl: str, **kw) -> str:
    for k, v in kw.items():
        tpl = tpl.replace(f"__{k}__", v)
    return tpl


# ── Framework metadata ──────────────────────────────────────────────────────

_FW_MODULE = {
    "Express": "express", "Fastify": "fastify", "Koa": "koa",
    "Hapi": "@hapi/hapi", "Restify": "restify", "NestJS": "@nestjs/core",
    "Sails": "sails", "AdonisJS": "@adonisjs/core",
}

# "standard" -- write app.js, npm install, node app.js (the original shape).
# "typescript" -- write app.ts + tsconfig.json, npm install, tsc (compiled
#   in place, no outDir -- keeps __dirname-relative node_modules lookups in
#   pkgVersion() working; an outDir subdir broke that, confirmed via a real
#   docker run reporting "unknown" for the framework version), node app.js.
# "scaffold" -- run the framework's own official CLI scaffolder inside the
#   Dockerfile, then COPY a routes file over the generated one. Both new
#   kinds exist because "needs different tooling than a hand-written
#   index.js" is NOT, per this project's standing rule, a valid reason to
#   exclude an otherwise-real, buildable framework.
_FW_KIND = {
    "Express": "standard", "Fastify": "standard", "Koa": "standard",
    "Hapi": "standard", "Restify": "standard", "Sails": "standard",
    "NestJS": "typescript", "AdonisJS": "scaffold",
}

# NestJS needs several packages released in lockstep with @nestjs/core, plus
# two independently-versioned peer libraries whose own major has shifted
# over Nest's lifetime -- confirmed for the current era (rxjs 7, current
# reflect-metadata) via a real docker build; older eras' exact peer pins
# follow Nest's own documented compatibility matrix, not independently
# re-verified this pass (disclosed here, not silently assumed).
#
# BUG FIXED 2026-07-12: `@nestjs/platform-express` was unconditionally
# pinned to the same version as @nestjs/core for EVERY major, including
# 1-5 -- confirmed via a real docker build failure ("No matching version
# found for @nestjs/platform-express@1.0.2") that this package's own first
# ever npm release is 6.0.0-alpha.3; no 1.x-5.x line exists at all.
# NestJS's "platform adapter" architecture (separating the HTTP framework
# integration into its own package) was introduced in v6 -- before that,
# @nestjs/core bundled Express directly as its own dependency (confirmed
# via `npm view @nestjs/core@5.7.4 dependencies`, which lists `express`
# itself). `NestFactory.create(AppModule)` needs no platform-adapter import
# either way, so majors <=5 simply omit the package instead of needing any
# template change.
def _nestjs_extra_deps(fw_major: str) -> dict:
    major = int(fw_major)
    if major <= 5:
        rxjs, reflect = "^5.5.0", "^0.1.10"
    elif major <= 7:
        rxjs, reflect = "^6.6.0", "^0.1.13"
    else:
        rxjs, reflect = "^7.8.0", "^0.1.13"
    deps = {"@nestjs/common": None, "reflect-metadata": reflect, "rxjs": rxjs}
    if major >= 6:
        deps["@nestjs/platform-express"] = None
    return deps


_LIBOQS_NODE_VERSION = "0.1.0"
_LIBOQS_TAG = "v0.1.0"


def make_app_js(fw_name: str, fw_major: str, lib_name: str, lib_ver: str) -> str:
    tpl = _LEGACY_APP_TPL.get((fw_name, fw_major), _APP_TPL[fw_name])
    # crypto (built-in) and liboqs-node (git-cloned, not in node_modules) both
    # bypass the normal node_modules/<pkg>/package.json version lookup.
    if lib_name == "crypto":
        lib_ve = '"built-in"'
    elif lib_name == "liboqs-node":
        lib_ve = f'"{lib_ver}"'
    else:
        lib_ve = f'pkgVersion("{_lib_npm(lib_name)}")'
    body = _sub(
        tpl,
        LIB_LINE     = _lib_blank_line(lib_name, lib_ver),
        LIB_NAME     = lib_name,
        LIB_VER_EXPR = lib_ve,
    )
    return _PKG_VERSION_HELPER + "\n" + body


def make_adonis_routes(fw_resolved: str, lib_name: str, lib_ver: str) -> str:
    lib_version = "built-in" if lib_name == "crypto" else lib_ver
    return _sub(
        _ADONIS_ROUTES_TPL,
        LIB_LINE   = _lib_blank_line(lib_name, lib_ver),
        LIB_NAME   = lib_name,
        FW_VERSION = fw_resolved,
        LIB_VERSION = lib_version,
    )


# ── package.json generation ───────────────────────────────────────────────────

# BUG FIXED 2026-07-12: `typescript` (a devDependency, needed to compile
# NestJS's app.ts, not something the running app itself depends on at
# runtime) was pinned to a single unconditional `"^5.6.0"` regardless of
# which Node version was compiling it -- confirmed via a real docker build
# failure ("Unexpected token ?" from `npx tsc` itself, not from our app.ts)
# that this resolves to 5.9.x, which requires Node >=14.17 to even RUN (its
# own bundled JS uses syntax older V8 can't parse) -- meaning EVERY NestJS
# combo on Node <14 failed at the compile step, regardless of which crypto
# library or NestJS major was involved. Verified empirically (not just
# trusting each release's declared `engines` field) by installing and
# running `tsc --version` against the real Docker base image for each
# Node major: typescript 4.9.5 runs on node:8-slim (v8.17.0) and
# node:10-slim (v10.24.1); typescript 5.0.4 runs on node:12-slim
# (v12.22.12, which satisfies TS 5.0's >=12.20 floor but not 5.1+'s
# >=14.17); typescript 5.6.3 (the existing `^5.6.0` pin) runs fine on
# node:14-slim (v14.21.3, well past >=14.17).
def _nestjs_typescript_pin(node_ver: str) -> str:
    major = int(node_ver) if node_ver else 999
    if major < 12:
        return "4.9.5"
    if major < 14:
        return "5.0.4"
    return "^5.6.0"


def make_package_json(fw_name: str, fw_major: str, fw_resolved: str,
                      lib_name: str, lib_resolved: str, node_ver: str = "") -> dict:
    deps = {_FW_MODULE[fw_name]: fw_resolved}
    dev_deps = {}

    if fw_name == "NestJS":
        for pkg, pinned in _nestjs_extra_deps(fw_major).items():
            deps[pkg] = pinned or fw_resolved
        dev_deps["typescript"] = _nestjs_typescript_pin(node_ver)
        dev_deps["@types/node"] = "^22.0.0"

    # liboqs-node is never a package.json dependency -- see LIB_META's
    # "npm": None and make_dockerfile()'s LIBOQS_NODE build stage.
    lib_npm = _lib_npm(lib_name)
    if lib_npm and lib_resolved:
        deps[lib_npm] = lib_resolved

    manifest = {
        "name": "app",
        "private": True,
        "version": "0.0.0",
        "dependencies": deps,
    }
    if dev_deps:
        manifest["devDependencies"] = dev_deps
    return manifest


def make_package_json_text(*args, **kwargs) -> str:
    return json.dumps(make_package_json(*args, **kwargs), indent=2) + "\n"


# ── Dockerfile generation ─────────────────────────────────────────────────────

# `skipLibCheck`: confirmed via a real docker build failure that older
# @nestjs/core majors (1-5) ship their own .d.ts referencing types from
# @nestjs/microservices unconditionally (e.g. `nest-factory.d.ts` imports
# from '@nestjs/microservices/interfaces/...') even though microservices
# support is an entirely optional feature this project's minimal REST-only
# app never uses and deliberately doesn't install (see
# `_nestjs_extra_deps()`). Without this flag, `tsc` still type-checks
# every .d.ts it can find in node_modules and fails to resolve that
# unresolvable cross-package type reference. `skipLibCheck` is the
# standard, narrowly-scoped fix (skip type-checking *.d.ts files, which we
# don't control anyway) rather than installing an unused optional package
# just to satisfy its type declarations.
_TSCONFIG_JSON = json.dumps({
    "compilerOptions": {
        "module": "commonjs", "target": "ES2020",
        "experimentalDecorators": True, "emitDecoratorMetadata": True,
        "skipLibCheck": True,
    },
}, indent=2) + "\n"

# liboqs-node's npm-published tarball is missing its own git submodules
# (deps/liboqs, deps/liboqs-cpp) -- confirmed via a real `npm install`
# failure ("package could not be found" / node-gyp rebuild failure with no
# source present) -- so it's git-cloned with --recurse-submodules directly
# instead of npm-installed. The vendored liboqs commit (~2021) also fails to
# compile under GCC 12+ (Debian bookworm, this project's node:*-slim base):
# its old SIKE implementation trips -Werror=array-parameter/stringop-overflow,
# warning classes added to GCC after this commit was written -- fixed by
# stripping -Werror from the vendored liboqs' own CMake files before
# building. Confirmed working end-to-end (a real ML-KEM-equivalent Kyber768
# keypair generated) after both fixes.
def _liboqs_node_stage(apt_sources: str, apt_flag: str, allow_unauth: str) -> str:
    # BUG FIXED 2026-07-12: this stage's own `apt-get update` never used
    # this project's shared archive.debian.org fix (apt_sources/apt_flag/
    # allow_unauth, already computed by make_dockerfile() for every OTHER
    # apt-get call) -- confirmed via a real docker build failure on Node
    # 8/10 ("404 Not Found" against the long-dead deb.debian.org/stretch
    # mirror). Every other native-lib apt-get call in this file already
    # gets this fix; this one was just never wired up to it.
    return (
        f"{apt_sources}"
        f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
        "    python3 make g++ git cmake ninja-build ca-certificates libssl-dev pkg-config \\\n"
        "    && rm -rf /var/lib/apt/lists/*\n"
        "ENV PYTHON=python3\n"
        f"RUN git clone --recurse-submodules --depth 1 --branch {_LIBOQS_TAG} \\\n"
        "    https://github.com/TapuCosmo/liboqs-node /opt/liboqs-node \\\n"
        "    && find /opt/liboqs-node/deps/liboqs -type f \\( -name 'CMakeLists.txt' -o -name '*.cmake' \\) \\\n"
        "       -exec sed -i 's/-Werror//g' {} \\; \\\n"
        # `--unsafe-perm`: confirmed via a real docker build that `npm
        # install` (no args -- installing liboqs-node's OWN dependencies
        # for ITSELF, not as a third-party dependency of an outer project)
        # printed `npm WARN lifecycle ... cannot run in wd ... node-pre-gyp
        # install --fallback-to-build` and silently skipped it -- npm
        # refuses to run a LOCAL package's own lifecycle scripts as UID 0
        # without this flag (a safety guard against untrusted scripts
        # running with root privilege, moot here since this is our own
        # pinned build step). The whole point of this install IS that
        # script (it's what actually compiles oqs_node.node) -- exit code
        # 0 with no such binary produced silently passed this project's
        # own build step, only surfacing as a runtime crash ("Could not
        # locate the bindings file").
        "    && cd /opt/liboqs-node && npm install --no-audit --no-fund --unsafe-perm\n"
    )


# Multi-stage (2026-07-11): a native node-gyp compile (sodium-native/bcrypt/
# argon2's apt_block: python3/make/g++[/autoconf/automake/libtool]) or
# liboqs-node's own heavier build (cmake/ninja/git/libssl-dev/pkg-config,
# ~250MB+) previously stayed baked into the final image forever, and NestJS
# needed the full TypeScript compiler present just to run its own compiled
# output. `builder` keeps the toolchain, does the (only) `npm install`, and
# for "typescript" additionally runs `npx tsc`; `npm prune --omit=dev`
# then removes devDependency packages from the ALREADY-installed tree
# in place. The final stage starts fresh and only `COPY --from=builder`s
# the pruned `node_modules` (+ compiled `app.js` for NestJS, + the
# self-contained `/opt/liboqs-node` tree for liboqs-node) -- no apt-get at
# all in the final stage, since the native addons were already compiled by
# npm in the builder and don't need recompiling. Deliberately NOT
# `npm install --omit=dev` a second time in the final stage: that would
# re-trigger the exact same native compile there, needing the toolchain a
# second time and defeating the whole point. See registry node.json's
# top-level notes for background.
def _needs_multi_stage(kind: str, apt_block: str, lib_name: str) -> bool:
    return bool(apt_block) or lib_name == "liboqs-node" or kind == "typescript"


def make_dockerfile(node_ver: str, fw_name: str, lib_name: str, lib_ver: str = "") -> str:
    kind = _FW_KIND[fw_name]
    sys_deps = list(_lib_sys_deps(lib_name))
    needs_apt = bool(sys_deps) or lib_name == "liboqs-node" or fw_name == "AdonisJS"
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(node_ver) if needs_apt else ("", "", "")

    # Cache-key diversifier (2026-07-12): EVERY combo sharing the same
    # node_ver+framework+kind has an otherwise byte-identical Dockerfile
    # prefix (FROM/WORKDIR/apt block are templated, not content-specific) --
    # under this project's own parallel builds (--workers), confirmed via a
    # real docker run that this lets BuildKit's cache alias a COPY layer
    # across two totally unrelated combos: roughly half of all built
    # `koa-*-jose-3` images were found to have `crypto-js`'s `app.js` baked
    # in instead of jose's, even though package.json (and the on-disk source
    # tree) were always correct for jose -- same root cause/class as the
    # AdonisJS `routes.ts` collision below, just in the plain/multi-stage
    # templates which never got the same fix. `ARG` (baked directly into the
    # generated Dockerfile text, not passed via `--build-arg`) forces a
    # distinct cache lineage per combo from this point on -- doesn't fix
    # whatever race exists in BuildKit's cache store under concurrency, but
    # makes a collision across combos structurally impossible instead of
    # merely unlikely.
    lib_npm = _lib_npm(lib_name)
    cache_bust = f'ARG PQC_LIB_ID="{lib_npm or lib_name}@{lib_ver}"\n'

    apt_block = ""
    if sys_deps:
        deps_line = " ".join(sys_deps)
        apt_block = (
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {deps_line} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )
        # node-gyp bundled with old npm/Node (node-gyp <4, shipped through
        # roughly Node 8) hardcodes looking for an executable literally
        # named "python" (Python 2) -- confirmed via a real docker build
        # failure on Node 4/6: "Can't find Python executable 'python'",
        # even though python3 (this project's own sys_dep, Python 2 isn't
        # installable on a current Debian release at all) is right there.
        # The error message itself names the documented workaround.
        if "python3" in sys_deps:
            apt_block += "ENV PYTHON=python3\n"

    liboqs_stage = (
        _liboqs_node_stage(apt_sources, apt_flag, allow_unauth)
        if lib_name == "liboqs-node" else ""
    )
    liboqs_copy = (
        "COPY --from=builder /opt/liboqs-node /opt/liboqs-node\n"
        if lib_name == "liboqs-node" else ""
    )

    if kind == "typescript":
        return (
            f"FROM node:{node_ver}-slim AS builder\n"
            "WORKDIR /app\n"
            f"{cache_bust}"
            f"{apt_block}"
            f"{liboqs_stage}"
            "COPY package.json tsconfig.json .\n"
            # `--legacy-peer-deps`: @nestjs/core's own OPTIONAL peer deps
            # (@nestjs/websockets, @nestjs/microservices) are declared with
            # a matching-major range (e.g. major 1 wants
            # @nestjs/microservices@^1.0.0) -- confirmed via a real docker
            # build failure on Node 18 (npm 10, which auto-installs peer
            # deps by default since npm 7) that @nestjs/microservices never
            # had a 1.x release at all (its own first-ever version is
            # 2.0.0), so npm's auto-resolution hits a real ETARGET. Node
            # 10's older bundled npm 6 only warns about unmet peers instead
            # of trying to install them, which is why this only surfaced on
            # newer Node/npm pairings. This project's minimal REST-only Nest
            # app never needs the optional websockets/microservices
            # features, so skipping strict peer resolution entirely (same
            # bypass already used for AdonisJS's bcrypt peer conflict) is
            # the correct fix rather than trying to chase down a real
            # version for a package release that doesn't exist.
            "RUN npm install --no-audit --no-fund --legacy-peer-deps\n"
            "COPY app.ts .\n"
            "RUN npx tsc\n"
            # `npm prune` re-resolves the dependency tree on its own and does
            # NOT inherit `--legacy-peer-deps` from the earlier `npm install`
            # call (it's a separate command invocation) -- confirmed via a
            # real docker build failure on Node 18/npm 10 hitting the exact
            # same ETARGET on @nestjs/microservices@^1.0.0 as the install
            # step above, just one command later. Needs the same flag again.
            "RUN npm prune --omit=dev --legacy-peer-deps\n"
            "\n"
            f"FROM node:{node_ver}-slim\n"
            "WORKDIR /app\n"
            f"{cache_bust}"
            f"{liboqs_copy}"
            "COPY --from=builder /app/node_modules ./node_modules\n"
            "COPY --from=builder /app/app.js .\n"
            "EXPOSE 8000\n"
            'CMD ["node", "app.js"]\n'
        )

    if kind == "scaffold":
        # AdonisJS: scaffold the official minimal API starter, then
        # overwrite its routes file -- confirmed working end-to-end via a
        # real docker build+run (both endpoints curled on a live container).
        #
        # BUG FIXED 2026-07-12: the scaffold (`create-adonisjs`) creates its
        # OWN package.json (Adonis's own deps only) -- write_context() never
        # wrote a package.json for this "kind" at all, and nothing here ever
        # installed the target crypto library, even though the injected
        # routes.ts unconditionally `require()`s it. Every combo except
        # "crypto" (Node builtin, no install needed) and "liboqs-node"
        # (fetched separately, no npm install needed) failed at runtime with
        # "Cannot find module '<lib>'" -- confirmed via a real docker run.
        # Fixed by running `npm install <pkg>@<resolved-version>` against
        # the scaffolded project AFTER scaffolding (adds to its existing
        # package.json/node_modules rather than replacing them). The same
        # gap existed for native (node-gyp) libraries' own sys_deps
        # (python3/make/g++/...) -- previously only `git` was installed
        # here, so bcrypt/argon2/sodium-native would have failed to compile
        # too; merged into the same apt-get call below.
        #
        # `--legacy-peer-deps`: AdonisJS's own scaffold declares an OPTIONAL
        # peer dependency on a specific bcrypt major via `@adonisjs/hash`
        # (e.g. `@adonisjs/core@7.3.5` wants `bcrypt@^6.0.0`) -- confirmed
        # via a real docker build failure installing `bcrypt@5.1.1` (npm's
        # strict peer-dependency resolution, default since npm 7, refuses
        # any version that doesn't satisfy it). This project deliberately
        # installs specific old/pinned library versions for research
        # regardless of what a framework's own optional peer wants, so
        # `--legacy-peer-deps` (skip peer-dependency validation entirely,
        # not `--force`, which also re-resolves already-installed packages)
        # is the correct bypass -- the same class of fix as Composer's
        # `--no-security-blocking` elsewhere in this project.
        npm_install_line = (
            f"RUN npm install --no-audit --no-fund --legacy-peer-deps {lib_npm}@{lib_ver}\n"
            if lib_npm else ""
        )
        scaffold_deps = " ".join(["git", *sys_deps])
        # `cache_bust` (defined above, shared with every other template kind)
        # is placed AFTER the `create-adonisjs` scaffold step here rather
        # than right after WORKDIR like the other kinds -- every AdonisJS
        # combo sharing the same node_ver+major has an identical, expensive
        # (~60s) scaffold step that's deliberately left cacheable; the
        # diversifier only needs to guard the instructions from here on
        # (routes.ts + the target library's install), which is where the
        # actual collision was confirmed via a real docker build+run: a
        # `tweetnacl` image crashing at runtime with `Cannot find module
        # 'node-jose'`, even though the on-disk routes.ts for that build was
        # verified correct.
        # NOT converted to multi-stage this pass: our CMD runs AdonisJS's
        # own dev-mode `ace serve`, which genuinely needs the scaffold's
        # devDependencies (its CLI/hot-reload tooling) present at runtime --
        # pruning them the way the other kinds do would break it. AdonisJS
        # does have an official production `ace build` flow that compiles
        # to a dependency-pruned `build/` directory instead, which is the
        # right fix for its (by far the largest, 823MB-1.6GB) image size --
        # flagged as a follow-up needing its own per-major verification
        # pass, not silently done here alongside the safer conversions.
        return (
            f"FROM node:{node_ver}-slim\n"
            "WORKDIR /app\n"
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {scaffold_deps} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
            f"{liboqs_stage}"
            "RUN npx --yes create-adonisjs@latest . --kit=api\n"
            f"{cache_bust}"
            f"{npm_install_line}"
            "COPY routes.ts start/routes.ts\n"
            "EXPOSE 8000\n"
            'ENV PORT=8000 HOST=0.0.0.0\n'
            'CMD ["node", "ace", "serve"]\n'
        )

    if _needs_multi_stage(kind, apt_block, lib_name):
        return (
            f"FROM node:{node_ver}-slim AS builder\n"
            "WORKDIR /app\n"
            f"{cache_bust}"
            f"{apt_block}"
            f"{liboqs_stage}"
            "COPY package.json .\n"
            "RUN npm install --no-audit --no-fund\n"
            "RUN npm prune --omit=dev\n"
            "\n"
            f"FROM node:{node_ver}-slim\n"
            "WORKDIR /app\n"
            f"{cache_bust}"
            f"{liboqs_copy}"
            "COPY --from=builder /app/node_modules ./node_modules\n"
            "COPY app.js .\n"
            "EXPOSE 8000\n"
            'CMD ["node", "app.js"]\n'
        )

    return (
        f"FROM node:{node_ver}-slim\n"
        "WORKDIR /app\n"
        f"{cache_bust}"
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
    """Write the image context (app.js/app.ts/routes.ts + package.json +
    Dockerfile, depending on the framework's kind) for one combination.

    Returns False (and removes any stale directory) when a required package
    version cannot be resolved on the npm registry.
    """
    out = images_base / "node" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    fw_resolved = _resolve(_FW_MODULE[fw_name], fw_major)
    if fw_resolved is None:
        print(f"  [SKIP] {fw_name} {fw_major} not resolvable on npm", flush=True)
        if out.exists():
            shutil.rmtree(out)
        return False

    lib_resolved = ""
    if lib_name == "liboqs-node":
        lib_resolved = _LIBOQS_NODE_VERSION
    else:
        lib_npm = _lib_npm(lib_name)
        if lib_npm and lib_ver != "builtin":
            lib_resolved = _resolve(lib_npm, lib_ver)
            if lib_resolved is None:
                print(f"  [SKIP] {lib_name} {lib_ver} not resolvable on npm", flush=True)
                if out.exists():
                    shutil.rmtree(out)
                return False

    # BUG FIXED 2026-07-13: regenerating an EXISTING context directory used to
    # just overwrite each file in place (`.write_text()` on an already-present
    # file keeps the same inode on most filesystems) -- confirmed via a real
    # docker build investigation that this can leave a stale BuildKit build
    # CONTEXT snapshot keyed to that inode/path, served instead of the file's
    # current content, even with `--no-cache` and even under a brand-new
    # destination tag (ruled both out with real docker builds). Deleting the
    # directory first forces a genuinely fresh inode on every regeneration,
    # closing off that stale-context class of bug at the source rather than
    # only mitigating cross-combo layer collisions (see the `ARG` cache-bust
    # fix elsewhere in this file, which is a different, narrower mechanism).
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    kind = _FW_KIND[fw_name]

    if kind == "scaffold":
        (out / "routes.ts").write_text(
            make_adonis_routes(fw_resolved, lib_name, lib_resolved or lib_ver), encoding="utf-8"
        )
    elif kind == "typescript":
        (out / "app.ts").write_text(
            make_app_js(fw_name, fw_major, lib_name, lib_resolved or lib_ver), encoding="utf-8"
        )
        (out / "tsconfig.json").write_text(_TSCONFIG_JSON, encoding="utf-8")
        (out / "package.json").write_text(
            make_package_json_text(fw_name, fw_major, fw_resolved, lib_name, lib_resolved, lang_ver),
            encoding="utf-8"
        )
    else:
        (out / "app.js").write_text(
            make_app_js(fw_name, fw_major, lib_name, lib_resolved or lib_ver), encoding="utf-8"
        )
        (out / "package.json").write_text(
            make_package_json_text(fw_name, fw_major, fw_resolved, lib_name, lib_resolved), encoding="utf-8"
        )

    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, fw_name, lib_name, lib_resolved or lib_ver), encoding="utf-8"
    )
    return True
