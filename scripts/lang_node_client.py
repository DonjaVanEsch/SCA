"""
Node.js outbound-call client generator, for the client-fingerprinting
experiment (see registry python.json's own "_comment_http_clients" note for
the full design rationale, and registry node.json's own copy of that same
note). Mirrors lang_python_client.py's shape/conventions exactly, but for
Node's crypto-library set: instead of `requests`, plain transport uses
Node's built-in `http`/`https` modules directly (no extra npm dependency
needed for the sign-only clients).

Only `node-forge` has its own TLS implementation (`forge.tls`, a pure-JS
TLS 1.0-1.2 stack distinct from Node's OpenSSL-backed built-in `tls`
module) -- it alone gets a "-raw" variant, driving the handshake itself
against the target's :9443. Every other entry is sign-only: signs/hashes
the same fixed probe message with that library's own natural primitive and
attaches the result as a header (X-Signature, or Authorization: Bearer for
the two JWT-shaped ones), same "blind discovery" rationale as the Python
clients.

No async/await syntax anywhere (Promise .then()/.catch() chains instead) --
this project's own tracked Node majors reach back to 4/6/0.10 for several
of these libraries, and async/await is a hard SyntaxError below Node 7.6
(see lang_node.py's own docstring for the same constraint on server-side
app templates).

PyPI-equivalent version resolution is reused directly from lang_node.py
(same npm registry JSON API, same _resolve()/_fetch_releases()) rather than
duplicated.
"""

import json
import shutil
from pathlib import Path

from lang_node import (  # noqa: F401 (re-exported for callers)
    _resolve, _fetch_releases, _is_esm_only, _debian_archive_apt,
    _liboqs_node_stage, _LIBOQS_NODE_VERSION,
)

SCRIPT_DIR = Path(__file__).parent
CLIENT_OUT_BASE = SCRIPT_DIR.parent / "images_clients"

MESSAGE = "pqc-sca-fingerprint-probe"
SHARED_SECRET = "pqc-sca-shared-secret"

# Shared by every sign-only client: fires the actual outbound GET, attaches
# the given header, and prints the same JSON self-report shape every other
# language's clients use. Prepended (not templated via %-substitution) to
# each script below, then the client's own signing logic calls sendProbe()
# at the end. Uses url.parse() (available since Node 0.x) rather than the
# WHATWG URL class (only global since Node 10, a class under require("url")
# since Node 6.13/8) -- several of these libraries' own tracked compat
# reaches back to Node 4/0.10.
_REQUEST_HELPER = """\
"use strict";
const http = require("http");
const https = require("https");
const urlParse = require("url").parse;

function sendProbe(clientName, clientVersion, headerName, headerValue) {
\tconst target = process.env.PQC_TARGET_URL || "http://host.docker.internal:9000/probe";
\tconst parsed = urlParse(target);
\tconst transport = parsed.protocol === "https:" ? https : http;
\tconst options = {
\t\thostname: parsed.hostname,
\t\tport: parsed.port || (parsed.protocol === "https:" ? 443 : 80),
\t\tpath: parsed.path || "/",
\t\tmethod: "GET",
\t\theaders: {},
\t\trejectUnauthorized: false,
\t};
\toptions.headers[headerName] = headerValue;
\tconst req = transport.request(options, function (res) {
\t\tvar body = "";
\t\tres.on("data", function (chunk) { body += chunk; });
\t\tres.on("end", function () {
\t\t\tconsole.log(JSON.stringify({
\t\t\t\tclient: clientName, client_version: clientVersion,
\t\t\t\tlanguage_version: process.version.slice(1),
\t\t\t\tstatus_code: res.statusCode, body: body.slice(0, 500),
\t\t\t}));
\t\t});
\t});
\treq.on("error", function (err) {
\t\tconsole.log(JSON.stringify({ client: clientName, error: String(err) }));
\t});
\treq.end();
}

function pkgVersion(name) {
\ttry {
\t\treturn require(name + "/package.json").version;
\t} catch (e) {
\t\treturn "unknown";
\t}
}
"""


def _wrap(body: str) -> str:
    return _REQUEST_HELPER + "\n" + body


# ── crypto (built-in) ─────────────────────────────────────────────────────────

def _crypto_hmac_app(hc_ver: str = "") -> str:
    return _wrap("""\
const crypto = require("crypto");

try {
\tconst digest = crypto.createHmac("sha256", SHARED_SECRET_).update(MESSAGE_).digest("hex");
\tsendProbe("crypto-hmac", "built-in", "X-Signature", "hmac-sha256=" + digest);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "crypto-hmac", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET)))


# ── node-forge (the only raw-TLS-capable client) ──────────────────────────────

def _node_forge_raw_app(hc_ver: str = "") -> str:
    # Drives forge.tls's own pure-JS TLS handshake directly over a plain
    # net.Socket -- mirrors pyopenssl-raw/m2crypto-raw's "drive the target
    # library's own TLS stack, not the language's built-in one" design.
    return """\
"use strict";
const net = require("net");
const forge = require("node-forge");
const urlParse = require("url").parse;

const target = process.env.PQC_TARGET_URL || "https://host.docker.internal:9443/probe";
const parsed = urlParse(target);
const host = parsed.hostname;
const port = parseInt(parsed.port || "443", 10);
const path = parsed.path || "/";

try {
\tconst socket = net.connect(port, host);
\tlet responseData = "";
\tlet finished = false;

\tconst tlsConn = forge.tls.createConnection({
\t\tserver: false,
\t\tverify: function () { return true; },
\t\tconnected: function (connection) {
\t\t\tconst req = "GET " + path + " HTTP/1.1\\r\\nHost: " + host + "\\r\\nConnection: close\\r\\n\\r\\n";
\t\t\tconnection.prepare(req);
\t\t},
\t\ttlsDataReady: function (connection) {
\t\t\tconst data = connection.tlsData.getBytes();
\t\t\tsocket.write(Buffer.from(data, "binary"));
\t\t},
\t\tdataReady: function (connection) {
\t\t\tresponseData += connection.data.getBytes();
\t\t},
\t\tclosed: function () {
\t\t\tfinish();
\t\t},
\t\terror: function (connection, error) {
\t\t\tif (finished) return;
\t\t\tfinished = true;
\t\t\tconsole.log(JSON.stringify({ client: "node-forge-raw", error: String(error.message || error) }));
\t\t},
\t});

\tsocket.on("connect", function () {
\t\ttlsConn.handshake();
\t});
\tsocket.on("data", function (data) {
\t\ttlsConn.process(data.toString("binary"));
\t});
\tsocket.on("error", function (err) {
\t\tif (finished) return;
\t\tfinished = true;
\t\tconsole.log(JSON.stringify({ client: "node-forge-raw", error: String(err) }));
\t});

\tfunction finish() {
\t\tif (finished) return;
\t\tfinished = true;
\t\tconst firstLine = responseData.split("\\r\\n")[0] || "";
\t\tconst parts = firstLine.split(" ");
\t\tconst status = parts[1] ? parseInt(parts[1], 10) : null;
\t\tconst bodyIdx = responseData.indexOf("\\r\\n\\r\\n");
\t\tconst body = bodyIdx >= 0 ? responseData.slice(bodyIdx + 4) : "";
\t\tlet libVersion = "unknown";
\t\ttry { libVersion = require("node-forge/package.json").version; } catch (e) {}
\t\tconsole.log(JSON.stringify({
\t\t\tclient: "node-forge-raw", client_version: libVersion,
\t\t\tlanguage_version: process.version.slice(1),
\t\t\tstatus_code: status, body: body.slice(0, 500),
\t\t}));
\t}
} catch (err) {
\tconsole.log(JSON.stringify({ client: "node-forge-raw", error: String(err) }));
}
"""


def _node_forge_sign_app(hc_ver: str = "") -> str:
    # RSA-2048 PSS/SHA-256 via forge.pki/forge.pss -- same algorithm choice
    # as cryptography-sign (Python) on purpose, for cross-language
    # negative-control comparison (both should look byte-identical in size).
    #
    # BUG FIXED 2026-07-17: forge.pss.create() takes an OPTIONS OBJECT
    # ({md, mgf, saltLength}) only from some later version onward -- the
    # original API (still the ONLY form node-forge 0.1.x understands,
    # confirmed via a real docker run against the actual resolved 0.1.15)
    # is positional: create(hash, mgf, sLen). Passing an options object to
    # that old signature makes `hash` BE the whole options object, which
    # has no .start() method, crashing inside pss.js's own encode() with
    # "hash.start is not a function". The positional form works identically
    # on both 0.1.15 and the latest (1.4.0) release -- confirmed via a real
    # docker run on both -- so it's the one universally-compatible form.
    return _wrap("""\
const forge = require("node-forge");

try {
\tconst keys = forge.pki.rsa.generateKeyPair({ bits: 2048, e: 0x10001 });
\tconst pss = forge.pss.create(
\t\tforge.md.sha256.create(),
\t\tforge.mgf.mgf1.create(forge.md.sha256.create()),
\t\t32
\t);
\tconst md = forge.md.sha256.create();
\tmd.update(MESSAGE_, "utf8");
\tconst signature = keys.privateKey.sign(md, pss);
\tconst sigB64 = forge.util.encode64(signature);
\tsendProbe("node-forge-sign", pkgVersion("node-forge"), "X-Signature", "rsa2048-pss-sha256=" + sigB64);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "node-forge-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)))


# ── tweetnacl ──────────────────────────────────────────────────────────────────

def _tweetnacl_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const nacl = require("tweetnacl");

try {
\tconst keyPair = nacl.sign.keyPair();
\tconst messageBytes = Buffer.from(MESSAGE_, "utf8");
\tconst signature = nacl.sign.detached(messageBytes, keyPair.secretKey);
\tconst sigB64 = Buffer.from(signature).toString("base64");
\tsendProbe("tweetnacl-sign", pkgVersion("tweetnacl"), "X-Signature", "ed25519=" + sigB64);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "tweetnacl-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)))


# ── node-jose / jose (both JWT-shaped -- Authorization: Bearer, mirrors
# authlib-jwt) ─────────────────────────────────────────────────────────────────

def _node_jose_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const jose = require("node-jose");

const keystore = jose.JWK.createKeyStore();
keystore.generate("oct", 256, { alg: "HS256", use: "sig" })
\t.then(function (key) {
\t\treturn jose.JWS.createSign({ format: "compact" }, key).update(MESSAGE_, "utf8").final();
\t})
\t.then(function (jws) {
\t\tsendProbe("node-jose-sign", pkgVersion("node-jose"), "Authorization", "Bearer " + jws);
\t})
\t.catch(function (err) {
\t\tconsole.log(JSON.stringify({ client: "node-jose-sign", error: String(err) }));
\t});
""".replace("MESSAGE_", json.dumps(MESSAGE)))


def _jose_sign_app(hc_ver: str = "") -> str:
    # jose's own API changed dramatically across majors (1/2's older
    # callback/promise style vs 3+'s functional CompactSign API vs 6's
    # ESM-only). This targets the modern functional API (v3+) -- older
    # majors are a real, disclosed gap, expected to need a per-version fix
    # once actually build/test-verified, same iterative pattern as every
    # other version-specific bug already found elsewhere in this project.
    # BUG FIXED 2026-07-17: TextEncoder is only a GLOBAL since Node 11 --
    # confirmed via a real docker run on node:10-slim ("ReferenceError:
    # TextEncoder is not defined"), which jose's own tracked compat
    # (majors 1+ start at Node 10) reaches. require("util").TextEncoder
    # has existed since Node 8.3 and is the same class either way.
    body = """\
function run(joseLib) {
\ttry {
\t\tconst TextEncoder = require("util").TextEncoder;
\t\tconst secret = new TextEncoder().encode(SHARED_SECRET_);
\t\tconst payload = new TextEncoder().encode(MESSAGE_);
\t\tnew joseLib.CompactSign(payload)
\t\t\t.setProtectedHeader({ alg: "HS256" })
\t\t\t.sign(secret)
\t\t\t.then(function (jws) {
\t\t\t\tsendProbe("jose-sign", pkgVersion("jose"), "Authorization", "Bearer " + jws);
\t\t\t})
\t\t\t.catch(function (err) {
\t\t\t\tconsole.log(JSON.stringify({ client: "jose-sign", error: String(err) }));
\t\t\t});
\t} catch (err) {
\t\tconsole.log(JSON.stringify({ client: "jose-sign", error: String(err) }));
\t}
}

__LIB_LOAD__
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET))
    if _is_esm_only("jose", hc_ver) if hc_ver else False:
        load = 'import("jose").then(run).catch(function (err) {\n\tconsole.log(JSON.stringify({ client: "jose-sign", error: String(err) }));\n});\n'
    else:
        load = "run(require(\"jose\"));\n"
    return _wrap(body.replace("__LIB_LOAD__", load))


# ── crypto-js ──────────────────────────────────────────────────────────────────

def _crypto_js_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const CryptoJS = require("crypto-js");

try {
\tconst digest = CryptoJS.HmacSHA256(MESSAGE_, SHARED_SECRET_).toString(CryptoJS.enc.Hex);
\tsendProbe("crypto-js-sign", pkgVersion("crypto-js"), "X-Signature", "hmac-sha256=" + digest);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "crypto-js-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET)))


# ── sodium-native ──────────────────────────────────────────────────────────────

def _sodium_native_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const sodium = require("sodium-native");

try {
\tconst publicKey = Buffer.alloc(sodium.crypto_sign_PUBLICKEYBYTES);
\tconst secretKey = Buffer.alloc(sodium.crypto_sign_SECRETKEYBYTES);
\tsodium.crypto_sign_keypair(publicKey, secretKey);
\tconst message = Buffer.from(MESSAGE_, "utf8");
\tconst signature = Buffer.alloc(sodium.crypto_sign_BYTES);
\tsodium.crypto_sign_detached(signature, message, secretKey);
\tconst sigB64 = signature.toString("base64");
\tsendProbe("sodium-native-sign", pkgVersion("sodium-native"), "X-Signature", "ed25519=" + sigB64);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "sodium-native-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)))


# ── bcrypt / bcryptjs (native + pure-JS pairing, mirrors sodium/crypto-js) ────

def _bcrypt_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const bcrypt = require("bcrypt");

try {
\tconst hash = bcrypt.hashSync(MESSAGE_ + SHARED_SECRET_, 10);
\tsendProbe("bcrypt-sign", pkgVersion("bcrypt"), "X-Signature", "bcrypt=" + hash);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "bcrypt-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET)))


def _bcryptjs_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const bcrypt = require("bcryptjs");

try {
\tconst hash = bcrypt.hashSync(MESSAGE_ + SHARED_SECRET_, 10);
\tsendProbe("bcryptjs-sign", pkgVersion("bcryptjs"), "X-Signature", "bcrypt=" + hash);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "bcryptjs-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET)))


# ── argon2 (async-only API) ────────────────────────────────────────────────────

def _argon2_sign_app(hc_ver: str = "") -> str:
    return _wrap("""\
const argon2 = require("argon2");

argon2.hash(MESSAGE_ + SHARED_SECRET_)
\t.then(function (hash) {
\t\tsendProbe("argon2-sign", pkgVersion("argon2"), "X-Signature", "argon2id=" + hash);
\t})
\t.catch(function (err) {
\t\tconsole.log(JSON.stringify({ client: "argon2-sign", error: String(err) }));
\t});
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("SHARED_SECRET_", json.dumps(SHARED_SECRET)))


# ── liboqs-node (git-cloned, not npm-installed -- see _liboqs_node_stage) ─────

def _liboqs_node_sign_app(hc_ver: str = "") -> str:
    # Method names (generateKeypair()/sign()) confirmed correct via a real
    # docker run's Object.getOwnPropertyNames(oqs.Signature.prototype).
    #
    # BUG FIXED 2026-07-17: "Dilithium3" (the modern liboqs-python-style
    # name) is not a name this binding's vendored liboqs commit knows --
    # confirmed live via oqs.Sigs.getEnabledAlgorithms(): this old (~2021)
    # liboqs build only exposes the historical all-caps/underscore draft
    # name "DILITHIUM_3" (alongside DILITHIUM_2/DILITHIUM_4, Falcon,
    # SPHINCS+, etc.) -- the same "draft, not final NIST name" situation
    # this project's own lang_node.py already documented for this exact
    # binding's KEM side (Kyber768, not ML-KEM-768).
    return _wrap("""\
const oqs = require("/opt/liboqs-node/lib/index.js");

try {
\tconst sig = new oqs.Signature("DILITHIUM_3");
\tconst publicKey = sig.generateKeypair();
\tconst messageBytes = Buffer.from(MESSAGE_, "utf8");
\tconst signature = sig.sign(messageBytes);
\tconst sigB64 = Buffer.from(signature).toString("base64");
\tsendProbe("liboqs-node-sign", "0.1.0", "X-Signature", "dilithium3=" + sigB64);
} catch (err) {
\tconsole.log(JSON.stringify({ client: "liboqs-node-sign", error: String(err) }));
}
""".replace("MESSAGE_", json.dumps(MESSAGE)))


# ── @noble/curves / @noble/post-quantum (ESM-only from a threshold major,
# submodule paths need a fallback since the exact-extension boundary isn't
# independently re-verified per submodule -- see lang_node.py's own
# _noble_curves_blank()/_noble_pq_blank() for the analogous server-side
# churn on a DIFFERENT submodule) ─────────────────────────────────────────────

def _noble_curves_sign_app(hc_ver: str = "") -> str:
    # v2.0 renamed ed25519.utils.randomPrivateKey -> randomSecretKey as part
    # of the same rewrite that made the package ESM-only (confirmed live:
    # 1.0's utils only has randomPrivateKey, 2.0's only randomSecretKey) --
    # same version boundary as _is_esm_only, so reuse it instead of a second
    # threshold dict.
    key_fn = "randomSecretKey" if hc_ver and _is_esm_only("@noble/curves", hc_ver) else "randomPrivateKey"
    body = """\
function run(mod) {
\ttry {
\t\tconst ed25519 = mod.ed25519;
\t\tconst privateKey = ed25519.utils.KEY_FN_();
\t\tconst messageBytes = Buffer.from(MESSAGE_, "utf8");
\t\tconst signature = ed25519.sign(messageBytes, privateKey);
\t\tconst sigB64 = Buffer.from(signature).toString("base64");
\t\tsendProbe("noble-curves-sign", pkgVersion("@noble/curves"), "X-Signature", "ed25519=" + sigB64);
\t} catch (err) {
\t\tconsole.log(JSON.stringify({ client: "noble-curves-sign", error: String(err) }));
\t}
}

__LIB_LOAD__
""".replace("MESSAGE_", json.dumps(MESSAGE)).replace("KEY_FN_", key_fn)
    if hc_ver and _is_esm_only("@noble/curves", hc_ver):
        load = (
            'import("@noble/curves/ed25519").catch(function () { return import("@noble/curves/ed25519.js"); })\n'
            "\t.then(run)\n"
            '\t.catch(function (err) { console.log(JSON.stringify({ client: "noble-curves-sign", error: String(err) })); });\n'
        )
    else:
        load = (
            "let _mod;\n"
            'try { _mod = require("@noble/curves/ed25519"); } catch (e) { _mod = require("@noble/curves/ed25519.js"); }\n'
            "run(_mod);\n"
        )
    return _wrap(body.replace("__LIB_LOAD__", load))


def _noble_pq_sign_app(hc_ver: str = "") -> str:
    body = """\
function run(mod) {
\ttry {
\t\tconst mldsa = mod.ml_dsa65;
\t\tconst keys = mldsa.keygen();
\t\tconst messageBytes = Buffer.from(MESSAGE_, "utf8");
\t\tconst signature = mldsa.sign(keys.secretKey, messageBytes);
\t\tconst sigB64 = Buffer.from(signature).toString("base64");
\t\tsendProbe("noble-pq-sign", pkgVersion("@noble/post-quantum"), "X-Signature", "ml-dsa-65=" + sigB64);
\t} catch (err) {
\t\tconsole.log(JSON.stringify({ client: "noble-pq-sign", error: String(err) }));
\t}
}

__LIB_LOAD__
""".replace("MESSAGE_", json.dumps(MESSAGE))
    if hc_ver and _is_esm_only("@noble/post-quantum", hc_ver):
        load = (
            'import("@noble/post-quantum/ml-dsa").catch(function () { return import("@noble/post-quantum/ml-dsa.js"); })\n'
            "\t.then(run)\n"
            '\t.catch(function (err) { console.log(JSON.stringify({ client: "noble-pq-sign", error: String(err) })); });\n'
        )
    else:
        load = (
            "let _mod;\n"
            'try { _mod = require("@noble/post-quantum/ml-dsa"); } catch (e) { _mod = require("@noble/post-quantum/ml-dsa.js"); }\n'
            "run(_mod);\n"
        )
    return _wrap(body.replace("__LIB_LOAD__", load))


# ── Client metadata ────────────────────────────────────────────────────────────

_CLIENT_META = {
    "crypto-hmac":        {"npm": None,                 "sys_deps": [], "app": _crypto_hmac_app},
    "node-forge-raw":     {"npm": "node-forge",         "sys_deps": [], "app": _node_forge_raw_app},
    "node-forge-sign":    {"npm": "node-forge",         "sys_deps": [], "app": _node_forge_sign_app},
    "tweetnacl-sign":     {"npm": "tweetnacl",          "sys_deps": [], "app": _tweetnacl_sign_app},
    "node-jose-sign":     {"npm": "node-jose",          "sys_deps": [], "app": _node_jose_sign_app},
    "jose-sign":          {"npm": "jose",               "sys_deps": [], "app": _jose_sign_app},
    "crypto-js-sign":     {"npm": "crypto-js",          "sys_deps": [], "app": _crypto_js_sign_app},
    "sodium-native-sign": {"npm": "sodium-native",       "sys_deps": ["python3", "make", "g++", "autoconf", "automake", "libtool"],
                           "app": _sodium_native_sign_app},
    "bcrypt-sign":        {"npm": "bcrypt",              "sys_deps": ["python3", "make", "g++"], "app": _bcrypt_sign_app},
    "bcryptjs-sign":      {"npm": "bcryptjs",            "sys_deps": [], "app": _bcryptjs_sign_app},
    "argon2-sign":        {"npm": "argon2",              "sys_deps": ["python3", "make", "g++"], "app": _argon2_sign_app},
    "liboqs-node-sign":   {"npm": None,                  "sys_deps": [], "app": _liboqs_node_sign_app},
    "noble-curves-sign":  {"npm": "@noble/curves",       "sys_deps": [], "app": _noble_curves_sign_app},
    "noble-pq-sign":      {"npm": "@noble/post-quantum", "sys_deps": [], "app": _noble_pq_sign_app},
}


# ── package.json / Dockerfile generation ──────────────────────────────────────

def make_client_package_json(npm: str | None, version_resolved: str | None) -> str:
    deps = {}
    if npm and version_resolved:
        deps[npm] = version_resolved
    manifest = {"name": "client", "private": True, "version": "0.0.0", "dependencies": deps}
    return json.dumps(manifest, indent=2) + "\n"


def _make_liboqs_client_dockerfile(node_ver: str, cache_bust: str) -> str:
    apt_sources, apt_flag, allow_unauth = _debian_archive_apt(node_ver)
    return (
        f"FROM node:{node_ver}-slim\n"
        "WORKDIR /app\n"
        f"{cache_bust}"
        f"{_liboqs_node_stage(apt_sources, apt_flag, allow_unauth)}"
        "COPY client.js .\n"
        'CMD ["node", "client.js"]\n'
    )


def make_client_dockerfile(node_ver: str, hc_name: str, hc_ver: str = "") -> str:
    # Cache-key diversifier (2026-07-17): mirrors lang_node.py's own
    # server-side cache_bust ARG. Two different (hc_name, hc_ver) combos
    # sharing the same node_ver + apt/install shape have an otherwise
    # byte-identical Dockerfile prefix, and this project's own real-world
    # investigation (see lang_node.py's cache_bust comment) already
    # confirmed BuildKit's cache can alias a COPY layer across two
    # unrelated combos under parallel builds. Confirmed live here too: a
    # real test batch had several tweetnacl-sign images crash with
    # "Cannot find module 'tweetnacl'" despite a correct on-disk
    # package.json/client.js -- same root cause/class, just never guarded
    # against on the client side until now.
    cache_bust = f'ARG PQC_CLIENT_ID="{hc_name}@{hc_ver}"\n'

    if hc_name == "liboqs-node-sign":
        return _make_liboqs_client_dockerfile(node_ver, cache_bust)

    meta = _CLIENT_META[hc_name]
    sys_deps = meta["sys_deps"]
    has_deps = meta["npm"] is not None

    apt_block = ""
    if sys_deps:
        apt_sources, apt_flag, allow_unauth = _debian_archive_apt(node_ver)
        deps_line = " ".join(sys_deps)
        apt_block = (
            f"{apt_sources}"
            f"RUN apt-get {apt_flag}update && apt-get {apt_flag}install -y --no-install-recommends {allow_unauth}\\\n"
            f"    {deps_line} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )
        if "python3" in sys_deps:
            apt_block += "ENV PYTHON=python3\n"

    install_block = (
        "COPY package.json .\nRUN npm install --no-audit --no-fund\n" if has_deps else ""
    )

    return (
        f"FROM node:{node_ver}-slim\n"
        "WORKDIR /app\n"
        f"{cache_bust}"
        f"{apt_block}{install_block}"
        "COPY client.js .\n"
        'CMD ["node", "client.js"]\n'
    )


def write_client_context(lang_ver: str, hc_name: str, hc_ver: str, out_base: Path | None = None) -> bool:
    """Generate one client image context under
    images_clients/node/{lang_ver}/{hc_name}/{hc_ver}/, where hc_ver is the
    RAW registry bucket (e.g. "4"), matching generate_images.py's own
    convention of naming directories/DB rows from the unresolved bucket
    value. Returns True on success, False if hc_name isn't known or hc_ver
    isn't resolvable on npm."""
    meta = _CLIENT_META.get(hc_name)
    if meta is None:
        return False

    out = (out_base or CLIENT_OUT_BASE) / "node" / lang_ver / hc_name / hc_ver
    npm = meta["npm"]

    version_resolved = None
    if hc_name == "liboqs-node-sign":
        version_resolved = _LIBOQS_NODE_VERSION
    elif npm and hc_ver != "builtin":
        version_resolved = _resolve(npm, hc_ver)
        if version_resolved is None:
            print(f"  [SKIP] {hc_name} {hc_ver} not resolvable on npm", flush=True)
            if out.exists():
                shutil.rmtree(out)
            return False

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    (out / "Dockerfile").write_text(make_client_dockerfile(lang_ver, hc_name, hc_ver), encoding="utf-8", newline="\n")
    (out / "client.js").write_text(meta["app"](hc_ver), encoding="utf-8", newline="\n")
    if npm and hc_name != "liboqs-node-sign":
        (out / "package.json").write_text(
            make_client_package_json(npm, version_resolved), encoding="utf-8", newline="\n"
        )

    return True
