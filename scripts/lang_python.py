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


APP_MAKER = {
    "Flask":   flask_app,
    "Django":  django_app,
    "FastAPI": fastapi_app,
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

def make_dockerfile(python_ver: str, lib_name: str, lib_ver: str) -> str:
    if _needs_legacy_openssl(lib_name, lib_ver):
        base_image = f"python:{python_ver}-slim-bullseye"
    else:
        base_image = f"python:{python_ver}-slim"

    sys_deps = sorted({
        "build-essential", "gcc", "libffi-dev", "libssl-dev",
        *LIB_META[lib_name]["sys_deps"],
    })
    deps_line = " \\\n    ".join(sys_deps)
    return f"""\
FROM {base_image}

RUN apt-get update && apt-get install -y --no-install-recommends \\
    {deps_line} \\
    && rm -rf /var/lib/apt/lists/*

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
