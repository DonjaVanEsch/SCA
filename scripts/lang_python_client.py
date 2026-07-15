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

from lang_python import _resolve, _fetch_releases  # noqa: F401 (re-exported for callers)

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
}


def make_requirements(hc_name: str, hc_ver: str) -> str | None:
    """Returns the requirements.txt content, "" for a stdlib-only client
    with nothing to install, or None if hc_ver (a raw registry bucket like
    "0.48") can't be resolved to an exact installable PyPI version."""
    meta = _CLIENT_META[hc_name]
    if meta["pip"] is None:
        return ""
    exact = _resolve(meta["pip"], hc_ver)
    if exact is None:
        return None
    return f"{meta['pip']}=={exact}\n"


def make_client_dockerfile(python_ver: str, hc_name: str) -> str:
    meta = _CLIENT_META[hc_name]
    has_deps = meta["pip"] is not None

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
