"""
Python-specific metadata, app templates and context generation.

Consumed by generate_images.py via importlib.import_module("lang_python").

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
import urllib.request
from pathlib import Path
from urllib.error import URLError

LANGUAGE_ID   = "python"
REGISTRY_FILE = "registry python.json"


def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


# ── Library metadata ──────────────────────────────────────────────────────────

LIB_META = {
    "PyNaCl": {
        "pip": "PyNaCl",
        "import_stmt": "import nacl",
        "version_expr": "nacl.__version__",
        "sys_deps": ["libsodium-dev"],
    },
    "cryptography": {
        "pip": "cryptography",
        "import_stmt": "import cryptography",
        "version_expr": "cryptography.__version__",
        "sys_deps": [],
    },
    "PyCryptodome": {
        "pip": "pycryptodome",
        "import_stmt": "import Crypto",
        "version_expr": "getattr(Crypto, '__version__', None) or __import__('importlib.metadata', fromlist=['version']).version('pycryptodome')",
        "sys_deps": [],
    },
    "M2Crypto": {
        "pip": "M2Crypto",
        "import_stmt": "import M2Crypto",
        "version_expr": "M2Crypto.version",
        "sys_deps": ["swig", "libssl-dev"],
    },
    "PyCrypto": {
        "pip": "pycrypto",
        "import_stmt": "import Crypto",
        "version_expr": "Crypto.__version__",
        "sys_deps": [],
    },
    "hashlib": {
        "pip": None,
        "import_stmt": "import hashlib",
        "version_expr": '"built-in"',
        "sys_deps": [],
    },
    "pyOpenSSL": {
        "pip": "pyOpenSSL",
        "import_stmt": "import OpenSSL",
        "version_expr": "OpenSSL.__version__",
        "sys_deps": [],
    },
    "ecdsa": {
        "pip": "ecdsa",
        "import_stmt": "import ecdsa",
        "version_expr": "ecdsa.__version__",
        "sys_deps": [],
    },
    "Authlib": {
        "pip": "Authlib",
        "import_stmt": "import authlib",
        "version_expr": "authlib.__version__",
        "sys_deps": [],
    },
    # liboqs-python is a ctypes wrapper -- no C-extension build of its own,
    # but it needs the liboqs C library present at runtime (built from
    # source in make_dockerfile()'s LIBOQS_PYTHON stage, the same recipe
    # already used for this project's other liboqs bindings) and
    # LD_LIBRARY_PATH pointed at it so the ctypes loader can find
    # liboqs.so. Confirmed working end-to-end via a real docker run.
    "liboqs-python": {
        "pip": "liboqs-python",
        "import_stmt": "import oqs",
        "version_expr": "__import__('importlib.metadata', fromlist=['version']).version('liboqs-python')",
        "sys_deps": [],
    },
}

FW_PIP = {
    ("Flask",   "0"): "Flask>=0.1,<1.0",
    ("Flask",   "1"): "Flask>=1.0,<2.0",
    ("Flask",   "2"): "Flask>=2.0,<3.0",
    ("Flask",   "3"): "Flask>=3.0,<4.0",
    ("Django",  "1"): "Django>=1.0,<2.0",
    ("Django",  "2"): "Django>=2.0,<3.0",
    ("Django",  "3"): "Django>=3.0,<4.0",
    ("Django",  "4"): "Django>=4.0,<5.0",
    ("Django",  "5"): "Django>=5.0,<6.0",
    ("FastAPI", "0"): "fastapi>=0.1,<1.0",
    ("Tornado", "1"): "tornado>=1.0,<2.0",
    ("Tornado", "2"): "tornado>=2.0,<3.0",
    ("Tornado", "3"): "tornado>=3.0,<4.0",
    ("Tornado", "4"): "tornado>=4.0,<5.0",
    ("Tornado", "5"): "tornado>=5.0,<6.0",
    ("Tornado", "6"): "tornado>=6.0,<7.0",
    ("aiohttp", "1"): "aiohttp>=1.0,<2.0",
    ("aiohttp", "2"): "aiohttp>=2.0,<3.0",
    ("aiohttp", "3"): "aiohttp>=3.0,<4.0",
    ("CherryPy", "3"):  "CherryPy>=3.0,<4.0",
    ("CherryPy", "17"): "CherryPy>=17.0,<18.0",
    ("CherryPy", "18"): "CherryPy>=18.0,<19.0",
    ("Bottle",  "0"): "bottle>=0.4,<0.14",
    ("Pyramid", "1"): "pyramid>=1.0,<2.0",
    ("Pyramid", "2"): "pyramid>=2.0,<3.0",
}

FW_EXTRA = {
    "FastAPI": ["uvicorn[standard]"],
}

# Pins for specific framework major versions to fix transitive dependency breaks.
FW_VERSION_EXTRA = {
    # Flask 0.x uses `from jinja2 import Markup, escape` (removed in Jinja2 3.1).
    # Jinja2 < 3.1 in turn imports `soft_unicode` from MarkupSafe (removed in 2.1).
    # itsdangerous 2.0 removed the `json` module that Flask 0.x imports.
    ("Flask", "0"): ["Jinja2<3.1.0", "MarkupSafe<2.1.0", "itsdangerous<2.0"],
    # Flask 1.x uses `from jinja2 import escape` (removed in Jinja2 3.1) — same chain.
    # Flask 1.x does not support Werkzeug 2.0+ (Flask 2.0 was released alongside it).
    # Flask 1.x also uses `from itsdangerous import json as _json` (removed in 2.1.0).
    ("Flask", "1"): ["Jinja2<3.1.0", "MarkupSafe<2.1.0", "Werkzeug<2.0", "itsdangerous<2.1"],
    # aiohttp 1.x/2.x leave `async-timeout` completely unversioned in their
    # own setup.py -- pip resolves it to the latest release (4.0.2), which
    # needs a newer Python than these majors target, causing a real
    # `TypeError: function() argument 1 must be code, not str` at import
    # time. Pinned to 3.0.1 (2018), contemporaneous with aiohttp 2.x's own
    # era -- confirmed working via a real docker build+run.
    ("aiohttp", "1"): ["async-timeout==3.0.1"],
    ("aiohttp", "2"): ["async-timeout==3.0.1"],
}


# ── App templates ─────────────────────────────────────────────────────────────

def flask_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
from flask import Flask, jsonify
{m['import_stmt']}

app = Flask(__name__)


@app.route("/")
def hello():
    return jsonify({{"message": "Hello World"}})


@app.route("/version")
def version():
    import importlib.metadata
    lib_version = {m['version_expr']}
    return jsonify({{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "Flask", "version": importlib.metadata.version("flask")}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
"""


def django_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DEBUG=True,
        SECRET_KEY="dev-secret-key-not-for-production",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF=__name__,
        INSTALLED_APPS=[],
    )
    django.setup()

from django.http import JsonResponse
try:
    from django.urls import path as _route
    _urlpatterns = lambda h, v: [_route("", h), _route("version", v)]
except ImportError:
    from django.conf.urls import url as _route
    _urlpatterns = lambda h, v: [_route(r"^$", h), _route(r"^version$", v)]
{m['import_stmt']}


def hello(request):
    return JsonResponse({{"message": "Hello World"}})


def version_view(request):
    lib_version = {m['version_expr']}
    return JsonResponse({{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "Django", "version": django.__version__}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }})


urlpatterns = _urlpatterns(hello, version_view)

if __name__ == "__main__":
    from django.core.management import execute_from_command_line
    execute_from_command_line(["manage.py", "runserver", "--noreload", "0.0.0.0:8000"])
"""


def fastapi_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import uvicorn
import fastapi
from fastapi import FastAPI
{m['import_stmt']}

app = FastAPI()


@app.get("/")
def hello():
    return {{"message": "Hello World"}}


@app.get("/version")
def version():
    lib_version = {m['version_expr']}
    return {{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "FastAPI", "version": fastapi.__version__}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
"""


def tornado_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import json
import importlib.metadata
import tornado.ioloop
import tornado.web
{m['import_stmt']}


class HelloHandler(tornado.web.RequestHandler):
    def get(self):
        self.write({{"message": "Hello World"}})


class VersionHandler(tornado.web.RequestHandler):
    def get(self):
        lib_version = {m['version_expr']}
        self.write({{
            "language": {{"name": "Python", "version": sys.version.split()[0]}},
            "framework": {{"name": "Tornado", "version": importlib.metadata.version("tornado")}},
            "library": {{"name": "{lib_name}", "version": str(lib_version)}},
        }})


if __name__ == "__main__":
    app = tornado.web.Application([(r"/", HelloHandler), (r"/version", VersionHandler)])
    app.listen(8000)
    tornado.ioloop.IOLoop.current().start()
"""


def aiohttp_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import importlib.metadata
from aiohttp import web
{m['import_stmt']}


async def hello(request):
    return web.json_response({{"message": "Hello World"}})


async def version(request):
    lib_version = {m['version_expr']}
    return web.json_response({{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "aiohttp", "version": importlib.metadata.version("aiohttp")}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }})


app = web.Application()
app.add_routes([web.get("/", hello), web.get("/version", version)])

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
"""


def cherrypy_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import importlib.metadata
import cherrypy
{m['import_stmt']}


class Root:
    @cherrypy.expose
    @cherrypy.tools.json_out()
    def index(self):
        return {{"message": "Hello World"}}

    @cherrypy.expose
    @cherrypy.tools.json_out()
    def version(self):
        lib_version = {m['version_expr']}
        return {{
            "language": {{"name": "Python", "version": sys.version.split()[0]}},
            "framework": {{"name": "CherryPy", "version": importlib.metadata.version("cherrypy")}},
            "library": {{"name": "{lib_name}", "version": str(lib_version)}},
        }}


if __name__ == "__main__":
    cherrypy.config.update({{"server.socket_host": "0.0.0.0", "server.socket_port": 8000}})
    cherrypy.quickstart(Root())
"""


def bottle_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import importlib.metadata
from bottle import route, run
{m['import_stmt']}


@route("/")
def hello():
    return {{"message": "Hello World"}}


@route("/version")
def version():
    lib_version = {m['version_expr']}
    return {{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "Bottle", "version": importlib.metadata.version("bottle")}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }}


if __name__ == "__main__":
    run(host="0.0.0.0", port=8000)
"""


def pyramid_app(lib_name: str) -> str:
    m = LIB_META[lib_name]
    return f"""\
import sys
import json
import importlib.metadata
from wsgiref.simple_server import make_server
from pyramid.config import Configurator
from pyramid.response import Response
{m['import_stmt']}


def hello(request):
    return Response(json.dumps({{"message": "Hello World"}}), content_type="application/json", charset="UTF-8")


def version(request):
    lib_version = {m['version_expr']}
    body = json.dumps({{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "Pyramid", "version": importlib.metadata.version("pyramid")}},
        "library": {{"name": "{lib_name}", "version": str(lib_version)}},
    }})
    return Response(body, content_type="application/json", charset="UTF-8")


if __name__ == "__main__":
    with Configurator() as config:
        config.add_route("hello", "/")
        config.add_route("version", "/version")
        config.add_view(hello, route_name="hello")
        config.add_view(version, route_name="version")
        app = config.make_wsgi_app()
    server = make_server("0.0.0.0", 8000, app)
    server.serve_forever()
"""


APP_MAKER = {
    "Flask":    flask_app,
    "Django":   django_app,
    "FastAPI":  fastapi_app,
    "Tornado":  tornado_app,
    "aiohttp":  aiohttp_app,
    "CherryPy": cherrypy_app,
    "Bottle":   bottle_app,
    "Pyramid":  pyramid_app,
}


# ── OpenSSL compatibility ─────────────────────────────────────────────────────

# Library versions below these thresholds use OpenSSL 1.x-only C APIs that were
# removed in OpenSSL 3.0.  Those builds need a Bullseye base image (OpenSSL 1.1.1)
# rather than the default Bookworm (OpenSSL 3.0).
_LIB_OPENSSL3_MIN: dict = {
    "cryptography": (36, 0),   # 36.0 first official OpenSSL 3.0 support
    "M2Crypto":     (0, 45),   # 0.45 first release without SWIG issue on glibc 2.36
}


def _needs_legacy_openssl(lib_name: str, lib_ver: str) -> bool:
    threshold = _LIB_OPENSSL3_MIN.get(lib_name)
    if threshold is None:
        return False
    parsed = _parse(lib_ver)
    return bool(parsed) and parsed < threshold


# ── Dockerfile ────────────────────────────────────────────────────────────────

# liboqs-python is a ctypes wrapper -- no C-extension build of the Python
# package itself, but the liboqs C library must be built from source and
# installed system-wide (same cmake/ninja recipe already reused for this
# project's other liboqs bindings), with LD_LIBRARY_PATH pointed at it so
# ctypes' dlopen() can find libcrypto/liboqs.so at runtime. Confirmed
# working end-to-end via a real docker build+run (a live ML-KEM-768
# keypair generated inside the container).
_LIBOQS_PYTHON_TAG = "0.15.0"

_LIBOQS_PYTHON_STAGE = (
    "RUN git clone --depth 1 --branch " + _LIBOQS_PYTHON_TAG + " \\\n"
    "    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \\\n"
    "    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \\\n"
    "       -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \\\n"
    "       -DOQS_BUILD_ONLY_LIB=ON -GNinja \\\n"
    "    && cmake --build /tmp/liboqs/build --target install \\\n"
    "    && rm -rf /tmp/liboqs && ldconfig\n"
    "ENV LD_LIBRARY_PATH=/usr/local/lib\n"
)


def make_dockerfile(python_ver: str, lib_name: str, lib_ver: str) -> str:
    if _needs_legacy_openssl(lib_name, lib_ver):
        base_image = f"python:{python_ver}-slim-bullseye"
    else:
        base_image = f"python:{python_ver}-slim"

    sys_deps = sorted({
        "build-essential", "gcc", "libffi-dev", "libssl-dev",
        *LIB_META[lib_name]["sys_deps"],
    })
    if lib_name == "liboqs-python":
        sys_deps = sorted(set(sys_deps) | {"cmake", "ninja-build", "git", "pkg-config"})
    deps_line = " \\\n    ".join(sys_deps)

    liboqs_stage = _LIBOQS_PYTHON_STAGE if lib_name == "liboqs-python" else ""

    return f"""\
FROM {base_image}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {deps_line} \\
    && rm -rf /var/lib/apt/lists/*
{liboqs_stage}
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip wheel cffi pycparser \\
    && (python -c "import setuptools" 2>/dev/null || pip install --no-cache-dir setuptools) \\
    && pip install --no-cache-dir --no-build-isolation -r requirements.txt

COPY app.py .

EXPOSE 8000

CMD ["python", "app.py"]
"""


# ── PyPI version resolution ───────────────────────────────────────────────────

_PYPI_RELEASES: dict = {}


def _ver_key(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _fetch_releases(pip_name: str) -> list:
    if pip_name in _PYPI_RELEASES:
        return _PYPI_RELEASES[pip_name]

    url = f"https://pypi.org/pypi/{pip_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        releases = sorted(
            (v for v, files in data["releases"].items()
             if re.match(r"^\d+(\.\d+)*$", v) and files),
            key=_ver_key,
        )
    except (URLError, KeyError, json.JSONDecodeError, OSError) as exc:
        print(f"  [WARN] PyPI lookup failed for {pip_name}: {exc}", flush=True)
        releases = []

    _PYPI_RELEASES[pip_name] = releases
    return releases


def _resolve(pip_name: str, registry_ver: str) -> str | None:
    if registry_ver.count(".") != 1:
        return registry_ver

    releases = _fetch_releases(pip_name)

    prefix = registry_ver + "."
    candidates = [v for v in releases if v.startswith(prefix)]
    if candidates:
        return candidates[-1]

    if registry_ver in releases:
        return registry_ver

    return None


# ── requirements.txt ──────────────────────────────────────────────────────────

def make_requirements(fw_name: str, fw_major: str,
                      lib_name: str, lib_ver: str) -> str | None:
    lines = [FW_PIP[(fw_name, fw_major)]]
    lines += FW_EXTRA.get(fw_name, [])
    lines += FW_VERSION_EXTRA.get((fw_name, fw_major), [])
    pip = LIB_META[lib_name]["pip"]
    if pip:
        exact = _resolve(pip, lib_ver)
        if exact is None:
            return None
        lines.append(f"{pip}=={exact}")
    return "\n".join(lines) + "\n"


# ── Public interface ──────────────────────────────────────────────────────────

def prefetch(lang_data: dict) -> None:
    """Pre-fetch PyPI release lists for all libraries in the registry."""
    pip_names = {
        LIB_META[lib["name"]]["pip"]
        for lib in lang_data.get("cryptography_libs", [])
        if lib["name"] in LIB_META and LIB_META[lib["name"]]["pip"] is not None
    }
    print("Fetching available versions from PyPI ...")
    for name in sorted(pip_names):
        releases = _fetch_releases(name)
        print(f"  {name}: {len(releases)} releases found")
    print()


def write_context(lang_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str, images_base: Path) -> bool:
    """Write app.py / requirements.txt / Dockerfile for one image context.

    Returns False (and removes any stale directory) when the library version
    does not exist on PyPI.
    """
    out = images_base / "python" / lang_ver / fw_name / fw_major / lib_name / lib_ver

    req = make_requirements(fw_name, fw_major, lib_name, lib_ver)
    if req is None:
        if out.exists():
            shutil.rmtree(out)
        return False

    out.mkdir(parents=True, exist_ok=True)
    (out / "app.py").write_text(APP_MAKER[fw_name](lib_name), encoding="utf-8")
    (out / "requirements.txt").write_text(req, encoding="utf-8")
    (out / "Dockerfile").write_text(
        make_dockerfile(lang_ver, lib_name, lib_ver), encoding="utf-8"
    )
    return True
