"""
Python outbound-call client generator, for the client-fingerprinting
experiment (see the "_comment_http_clients" note in registry python.json).

Mirrors lang_python.py's write_context()/make_dockerfile() shape, but for a
genuinely different kind of image: instead of a long-running server exposing
GET / and GET /version, each generated image is a one-shot client program
that fires a single outbound HTTP(S) call at a target URL (PQC_TARGET_URL
env var) using one specific HTTP-client library, prints a small JSON summary
of what happened, and exits. What varies here is the HTTP-client-library
axis, not a web framework -- a client program has no server-side framework.

Two entries (pyopenssl-raw/m2crypto-raw) don't use a normal HTTP-client
library at all -- they open a raw socket and drive the TLS handshake
themselves via that specific crypto library's own SSL API, so the crypto
library itself (not the language's default ssl module) is what's actually
visible in the connection's TLS fingerprint. They need PQC_TARGET_URL to
point at the target's HTTPS port (9443), not the plain HTTP one (9000).

PyPI version resolution is reused directly from lang_python.py (same
_resolve()/_fetch_releases() logic, same PyPI JSON API) rather than
duplicated -- it's the same resolution problem for the same package index.
"""

import shutil
from pathlib import Path

from lang_python import _resolve, _fetch_releases, _LIBOQS_PYTHON_STAGE  # noqa: F401 (re-exported for callers)

SCRIPT_DIR = Path(__file__).parent
CLIENT_OUT_BASE = SCRIPT_DIR.parent / "images_clients"


# ── Per-client-library app templates ──────────────────────────────────────────

def _http_client_app() -> str:
    return """\
import json
import os
import sys
import http.client
from urllib.parse import urlparse

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
u = urlparse(target)
conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=10) \\
    if u.scheme == "https" else \\
    http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
try:
    conn.request("GET", u.path or "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", errors="replace")
    print(json.dumps({
        "client": "http.client", "client_version": "built-in",
        "language_version": sys.version.split()[0],
        "status_code": resp.status, "body": body[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "http.client", "error": str(exc)}))
finally:
    conn.close()
"""


def _requests_app() -> str:
    return """\
import json
import os
import sys
import requests

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
try:
    r = requests.get(target, timeout=10)
    print(json.dumps({
        "client": "requests", "client_version": requests.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "requests", "error": str(exc)}))
"""


def _httpx_app() -> str:
    return """\
import json
import os
import sys
import httpx

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
try:
    r = httpx.get(target, timeout=10, verify=False)
    print(json.dumps({
        "client": "httpx", "client_version": httpx.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "httpx", "error": str(exc)}))
"""


def _urllib3_app() -> str:
    return """\
import json
import os
import sys
import urllib3

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
try:
    http = urllib3.PoolManager(cert_reqs="CERT_NONE")
    r = http.request("GET", target, timeout=10.0)
    print(json.dumps({
        "client": "urllib3", "client_version": urllib3.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status, "body": r.data.decode("utf-8", errors="replace")[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "urllib3", "error": str(exc)}))
"""


def _pyopenssl_raw_app() -> str:
    return """\
import json
import os
import select
import socket
import sys
from urllib.parse import urlparse
import OpenSSL
from OpenSSL import SSL

target = os.environ.get("PQC_TARGET_URL", "https://host.docker.internal:9443/probe")
u = urlparse(target)
host, port, path = u.hostname, u.port or 443, (u.path or "/")

def _retry_ssl(fn):
    # pyOpenSSL's do_handshake()/recv()/send() can raise WantReadError/
    # WantWriteError even on a blocking socket (the underlying BIO layer
    # needing another pass) -- confirmed via a real run: a plain single
    # do_handshake() call raised WantReadError immediately. This is the
    # standard, documented retry pattern, not a workaround for anything
    # target-specific.
    while True:
        try:
            return fn()
        except SSL.WantReadError:
            select.select([sock], [], [])
        except SSL.WantWriteError:
            select.select([], [sock], [])

try:
    ctx = SSL.Context(SSL.TLS_METHOD)
    ctx.set_verify(SSL.VERIFY_NONE, lambda *a: True)
    sock = socket.create_connection((host, port), timeout=10)
    conn = SSL.Connection(ctx, sock)
    conn.set_connect_state()
    conn.set_tlsext_host_name(host.encode())
    _retry_ssl(conn.do_handshake)

    req = f"GET {path} HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n"
    _retry_ssl(lambda: conn.sendall(req.encode()))
    data = b""
    while True:
        try:
            chunk = _retry_ssl(lambda: conn.recv(4096))
        except (SSL.ZeroReturnError, SSL.SysCallError):
            # The target's plain http.server-based TLS wrapping doesn't send
            # a clean close_notify on its final write -- confirmed via a
            # real run this reliably happens AFTER the full response body
            # has already arrived, so it means "peer closed", not a real
            # transport failure; whatever's in `data` already is complete.
            break
        if not chunk:
            break
        data += chunk
    try:
        conn.shutdown()
    except SSL.Error:
        pass  # peer already closed abruptly (see the recv loop comment above)
    conn.close()

    first_line = data.split(b"\\r\\n", 1)[0].decode(errors="replace")
    parts = first_line.split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    body = data.split(b"\\r\\n\\r\\n", 1)[-1].decode(errors="replace")
    print(json.dumps({
        "client": "pyopenssl-raw", "client_version": OpenSSL.__version__,
        "language_version": sys.version.split()[0],
        "status_code": status, "body": body[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "pyopenssl-raw", "error": repr(exc)}))
"""


def _m2crypto_raw_app() -> str:
    return """\
import json
import os
import sys
from urllib.parse import urlparse
import M2Crypto
from M2Crypto import SSL

target = os.environ.get("PQC_TARGET_URL", "https://host.docker.internal:9443/probe")
u = urlparse(target)
host, port, path = u.hostname, u.port or 443, (u.path or "/")

try:
    ctx = SSL.Context("tls")
    ctx.set_verify(SSL.verify_none, depth=0)
    conn = SSL.Connection(ctx)
    conn.set_socket_read_timeout(SSL.timeout(10))
    conn.connect((host, port))

    req = f"GET {path} HTTP/1.1\\r\\nHost: {host}\\r\\nConnection: close\\r\\n\\r\\n"
    conn.send(req.encode())
    data = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    conn.close()

    first_line = data.split(b"\\r\\n", 1)[0].decode(errors="replace")
    parts = first_line.split(" ")
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    body = data.split(b"\\r\\n\\r\\n", 1)[-1].decode(errors="replace")
    print(json.dumps({
        "client": "m2crypto-raw", "client_version": M2Crypto.version,
        "language_version": sys.version.split()[0],
        "status_code": status, "body": body[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "m2crypto-raw", "error": str(exc)}))
"""


# ── Second wave: signing clients ──────────────────────────────────────────────
# Unlike pyopenssl-raw/m2crypto-raw, none of these libraries have a TLS/socket
# API of their own -- they use `requests` (or, for hashlib, plain stdlib
# urllib.request) for the actual network call, and instead sign/HMAC a fixed
# probe message with that specific library, attaching the result as a header.
# The point is blind discovery, not self-report: a later, separate detection
# pass should be able to tell libraries apart from the shape of that header
# (signature length/encoding/algorithm identifier) alone. All of them sign the
# same fixed message ("pqc-sca-fingerprint-probe"), so the header's shape is
# the only thing that varies between them.


def _hashlib_hmac_app() -> str:
    return """\
import hashlib
import hmac
import json
import os
import sys
import urllib.request

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"
_SHARED_SECRET = b"pqc-sca-shared-secret"

try:
    digest = hmac.new(_SHARED_SECRET, MESSAGE, hashlib.sha256).hexdigest()
    req = urllib.request.Request(target, headers={"X-Signature": f"hmac-sha256={digest}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        print(json.dumps({
            "client": "hashlib-hmac", "client_version": "built-in",
            "language_version": sys.version.split()[0],
            "status_code": resp.status, "body": body[:500],
        }))
except Exception as exc:
    print(json.dumps({"client": "hashlib-hmac", "error": str(exc)}))
"""


def _ecdsa_sign_app() -> str:
    return """\
import base64
import hashlib
import json
import os
import sys
import requests
import ecdsa
from ecdsa.util import sigencode_der

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    sk = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
    signature = sk.sign(MESSAGE, hashfunc=hashlib.sha256, sigencode=sigencode_der)
    headers = {"X-Signature": f"ecdsa-p256-sha256-der={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "ecdsa-sign", "client_version": ecdsa.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "ecdsa-sign", "error": str(exc)}))
"""


def _pynacl_sign_app() -> str:
    return """\
import base64
import json
import os
import sys
import requests
import nacl
import nacl.signing

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    sk = nacl.signing.SigningKey.generate()
    signature = sk.sign(MESSAGE).signature  # raw, always exactly 64 bytes
    headers = {"X-Signature": f"ed25519={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "pynacl-sign", "client_version": nacl.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "pynacl-sign", "error": str(exc)}))
"""


def _cryptography_sign_app() -> str:
    return """\
import base64
import json
import os
import sys
import requests
import cryptography
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signature = private_key.sign(
        MESSAGE,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    headers = {"X-Signature": f"rsa2048-pss-sha256={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "cryptography-sign", "client_version": cryptography.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "cryptography-sign", "error": str(exc)}))
"""


def _pycryptodome_sign_app() -> str:
    return """\
import base64
import json
import os
import sys
import requests
import Crypto
from Crypto.PublicKey import RSA
from Crypto.Signature import pss
from Crypto.Hash import SHA256

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    key = RSA.generate(2048)
    signature = pss.new(key).sign(SHA256.new(MESSAGE))
    headers = {"X-Signature": f"rsa2048-pss-sha256={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    version = getattr(Crypto, "__version__", None) \\
        or __import__("importlib.metadata", fromlist=["version"]).version("pycryptodome")
    print(json.dumps({
        "client": "pycryptodome-sign", "client_version": version,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "pycryptodome-sign", "error": str(exc)}))
"""


def _pycrypto_sign_app() -> str:
    # Legacy PyCrypto's API predates PyCryptodome's `Signature.pss` module --
    # PKCS#1 v1.5 is what it actually had. Kept for completeness even though
    # this entry's registry compatibility (3.0-3.3) excludes it from every
    # lang_version this project builds -- see pycrypto-sign's registry notes.
    return """\
import base64
import json
import os
import sys
import requests
import Crypto
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA256

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    key = RSA.generate(2048)
    signature = PKCS1_v1_5.new(key).sign(SHA256.new(MESSAGE))
    headers = {"X-Signature": f"rsa2048-pkcs1v15-sha256={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "pycrypto-sign", "client_version": Crypto.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "pycrypto-sign", "error": str(exc)}))
"""


def _liboqs_sign_app() -> str:
    # oqs prints "liboqs-python faulthandler is disabled" to STDOUT on
    # import (confirmed via a real run) -- that line lands before our own
    # JSON self-report and breaks manager.py's json.loads(client_output),
    # the same way it would for any client. Silenced by redirecting stdout
    # for just the import, not something we can control from oqs's own side.
    return """\
import base64
import contextlib
import io
import json
import os
import sys
import requests
with contextlib.redirect_stdout(io.StringIO()):
    import oqs

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    with oqs.Signature("ML-DSA-65") as signer:
        signer.generate_keypair()
        signature = signer.sign(MESSAGE)
    headers = {"X-Signature": f"ml-dsa-65={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    version = __import__("importlib.metadata", fromlist=["version"]).version("liboqs-python")
    print(json.dumps({
        "client": "liboqs-sign", "client_version": version,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "liboqs-sign", "error": str(exc)}))
"""


def _authlib_jwt_app() -> str:
    return """\
import json
import os
import sys
import requests
import authlib
from authlib.jose import jwt

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
_SHARED_SECRET = "pqc-sca-shared-secret"

try:
    token = jwt.encode({"alg": "HS256"}, {"probe": "pqc-sca-fingerprint-probe"}, _SHARED_SECRET).decode("ascii")
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "authlib-jwt", "client_version": authlib.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "authlib-jwt", "error": str(exc)}))
"""


def _m2crypto_sign_app() -> str:
    return """\
import base64
import hashlib
import json
import os
import sys
import requests
import M2Crypto
from M2Crypto import RSA

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    rsa = RSA.gen_key(2048, 65537, lambda *a: None)
    digest = hashlib.sha256(MESSAGE).digest()
    signature = rsa.sign(digest, "sha256")
    headers = {"X-Signature": f"rsa2048-pkcs1v15-sha256={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "m2crypto-sign", "client_version": M2Crypto.version,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "m2crypto-sign", "error": str(exc)}))
"""


def _pyopenssl_sign_app() -> str:
    # OpenSSL.crypto.sign()/verify() -- the module-level convenience
    # functions this originally used -- no longer exist in current pyOpenSSL
    # (confirmed via a real run: AttributeError, "module 'OpenSSL.crypto'
    # has no attribute 'sign'"). pyOpenSSL still generates the keypair via
    # its own PKey API, then hands off to `cryptography` (which pyOpenSSL
    # already depends on and wraps for all its own X.509/key operations) via
    # the documented to_cryptography_key() interop method to actually sign.
    return """\
import base64
import json
import os
import sys
import requests
import OpenSSL
from OpenSSL import crypto
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

target = os.environ.get("PQC_TARGET_URL", "http://host.docker.internal:9000/probe")
MESSAGE = b"pqc-sca-fingerprint-probe"

try:
    pkey = crypto.PKey()
    pkey.generate_key(crypto.TYPE_RSA, 2048)
    private_key = pkey.to_cryptography_key()
    signature = private_key.sign(
        MESSAGE,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    headers = {"X-Signature": f"rsa2048-pss-sha256={base64.b64encode(signature).decode()}"}
    r = requests.get(target, headers=headers, timeout=10)
    print(json.dumps({
        "client": "pyopenssl-sign", "client_version": OpenSSL.__version__,
        "language_version": sys.version.split()[0],
        "status_code": r.status_code, "body": r.text[:500],
    }))
except Exception as exc:
    print(json.dumps({"client": "pyopenssl-sign", "error": str(exc)}))
"""


# "pip": PyPI package to install (None for stdlib-only clients).
# "sys_deps": apt packages needed at build time (C-extension clients only).
# "app": generator function producing client.py's source.
_CLIENT_META = {
    "http.client":    {"pip": None,          "sys_deps": [], "app": _http_client_app},
    "requests":       {"pip": "requests",    "sys_deps": [], "app": _requests_app},
    "httpx":          {"pip": "httpx",       "sys_deps": [], "app": _httpx_app},
    "urllib3":        {"pip": "urllib3",     "sys_deps": [], "app": _urllib3_app},
    "pyopenssl-raw":  {"pip": "pyOpenSSL",   "sys_deps": [], "app": _pyopenssl_raw_app},
    "m2crypto-raw":   {"pip": "M2Crypto",    "sys_deps": ["build-essential", "swig", "libssl-dev"],
                        "app": _m2crypto_raw_app},

    # Second wave: signing clients (see registry's _comment_http_clients).
    # "extra_pip": unpinned packages needed alongside the version-tracked one
    # (almost always just `requests`, since that's the transport for all of
    # these -- we're not testing requests itself here, so its own version
    # isn't tracked).
    "hashlib-hmac":      {"pip": None,           "sys_deps": [], "app": _hashlib_hmac_app},
    "ecdsa-sign":        {"pip": "ecdsa",         "sys_deps": [], "extra_pip": ["requests"],
                           "app": _ecdsa_sign_app},
    "pynacl-sign":       {"pip": "PyNaCl",        "sys_deps": ["libsodium-dev", "libsodium23"],
                           "extra_pip": ["requests"], "app": _pynacl_sign_app},
    "cryptography-sign": {"pip": "cryptography",  "sys_deps": [], "extra_pip": ["requests"],
                           "app": _cryptography_sign_app},
    "pycryptodome-sign": {"pip": "pycryptodome",  "sys_deps": [], "extra_pip": ["requests"],
                           "app": _pycryptodome_sign_app},
    "pycrypto-sign":     {"pip": "pycrypto",      "sys_deps": [], "extra_pip": ["requests"],
                           "app": _pycrypto_sign_app},
    # sys_deps unused here -- make_client_dockerfile() special-cases
    # liboqs-sign entirely (its own multi-stage builder needs a different
    # apt-get set than the single-stage template below applies).
    "liboqs-sign":       {"pip": "liboqs-python", "sys_deps": [],
                           "extra_pip": ["requests"], "app": _liboqs_sign_app},
    "authlib-jwt":       {"pip": "Authlib",       "sys_deps": [], "extra_pip": ["requests"],
                           "app": _authlib_jwt_app},
    "m2crypto-sign":     {"pip": "M2Crypto",      "sys_deps": ["build-essential", "swig", "libssl-dev"],
                           "extra_pip": ["requests"], "app": _m2crypto_sign_app},
    "pyopenssl-sign":    {"pip": "pyOpenSSL",     "sys_deps": [], "extra_pip": ["requests"],
                           "app": _pyopenssl_sign_app},
}


def make_requirements(hc_name: str, hc_ver: str) -> str | None:
    """Returns the requirements.txt content, "" for a stdlib-only client
    with nothing to install, or None if hc_ver (a raw registry bucket like
    "0.48") can't be resolved to an exact installable PyPI version."""
    meta = _CLIENT_META[hc_name]
    lines = []
    if meta["pip"] is not None:
        exact = _resolve(meta["pip"], hc_ver)
        if exact is None:
            return None
        lines.append(f"{meta['pip']}=={exact}")
    lines += meta.get("extra_pip", [])
    return ("\n".join(lines) + "\n") if lines else ""


def make_client_dockerfile(python_ver: str, hc_name: str) -> str:
    meta = _CLIENT_META[hc_name]
    if hc_name == "liboqs-sign":
        return _make_liboqs_client_dockerfile(python_ver)

    has_deps = meta["pip"] is not None or bool(meta.get("extra_pip"))

    sys_deps_block = ""
    if meta["sys_deps"]:
        deps_line = " \\\n    ".join(sorted(meta["sys_deps"]))
        sys_deps_block = (
            "RUN apt-get update && apt-get install -y --no-install-recommends \\\n"
            f"    {deps_line} \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n\n"
        )

    install_block = (
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n\n"
        if has_deps else ""
    )
    return f"""\
FROM python:{python_ver}-slim

WORKDIR /app
{sys_deps_block}{install_block}COPY client.py .

CMD ["python", "client.py"]
"""


def _make_liboqs_client_dockerfile(python_ver: str) -> str:
    """liboqs-sign needs the liboqs C library compiled from source (same
    recipe as lang_python.py's server-side liboqs-python images) -- kept in
    its own builder stage so the heavy cmake/ninja/git toolchain, and the
    compiled liboqs source tree, don't end up in the final one-shot client
    image. Python's own `site` module auto-adds
    ~/.local/lib/python{{X.Y}}/site-packages for the SAME interpreter version,
    so `pip install --user` in the builder + a plain COPY of ~/.local is
    enough -- no PYTHONPATH wiring needed (same reasoning as lang_python.py's
    own multi-stage Dockerfiles)."""
    return f"""\
FROM python:{python_ver}-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \\
    build-essential cmake gcc git libffi-dev libssl-dev ninja-build pkg-config \\
    && rm -rf /var/lib/apt/lists/*

{_LIBOQS_PYTHON_STAGE}
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

FROM python:{python_ver}-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/liboqs* /usr/local/lib/
RUN ldconfig
ENV LD_LIBRARY_PATH=/usr/local/lib
COPY --from=builder /root/.local /root/.local
COPY client.py .

CMD ["python", "client.py"]
"""


def write_client_context(python_ver: str, hc_name: str, hc_ver: str, out_base: Path | None = None) -> bool:
    """Generate one client image context under
    images_clients/python/{python_ver}/{hc_name}/{hc_ver}/, where hc_ver is
    the RAW registry bucket (e.g. "0.48"), matching generate_images.py's own
    convention of naming directories/DB rows from the unresolved bucket
    value, not the exact patch version resolved for pip. Returns True on
    success, False if hc_name isn't known or hc_ver isn't resolvable."""
    meta = _CLIENT_META.get(hc_name)
    if meta is None:
        return False

    reqs = make_requirements(hc_name, hc_ver)
    if reqs is None:
        return False

    out = (out_base or CLIENT_OUT_BASE) / "python" / python_ver / hc_name / hc_ver
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    (out / "Dockerfile").write_text(make_client_dockerfile(python_ver, hc_name), encoding="utf-8", newline="\n")
    (out / "client.py").write_text(meta["app"](), encoding="utf-8", newline="\n")
    if reqs:
        (out / "requirements.txt").write_text(reqs, encoding="utf-8", newline="\n")

    return True
