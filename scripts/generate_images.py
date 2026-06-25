#!/usr/bin/env python3
"""
Generate Docker image contexts from 'registry python.json'.

Output structure:
  images/python/<py_ver>/<framework>/<fw_major>/<lib_name>/<lib_ver>/
    app.py
    requirements.txt
    Dockerfile
"""

import json
import re
import shutil
import urllib.request
from pathlib import Path
from urllib.error import URLError

SCRIPT_DIR = Path(__file__).parent
REGISTRY_PATH = SCRIPT_DIR / "registry python.json"
IMAGES_BASE = SCRIPT_DIR.parent / "images"


# ── Compatibility helpers ─────────────────────────────────────────────────────

def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


def is_compatible(python_nr: str, compat_list: list) -> bool:
    """Return True if python_nr satisfies at least one compatibility string.

    When a list contains both 2.x and 3.x ranges, only the ranges whose
    major version matches python_nr are evaluated — this prevents '2.7+'
    from falsely matching Python 3.9 when '3.0-3.3' is also present.
    """
    py = _parse(python_nr)
    py_major = py[0]

    same, other = [], []
    for raw in compat_list:
        c = raw.strip().replace(".x", ".0")
        head = re.split(r"[+\-]", c)[0]
        (same if _parse(head)[0] == py_major else other).append(c)

    # Libraries that only list 2.x ranges don't support Python 3.x.
    # (Packages that genuinely support both always include an explicit 3.x range.)
    if not same:
        return False

    for c in same:
        if c.endswith("+"):
            if py >= _parse(c[:-1]):
                return True
        elif "-" in c:
            lo, hi = c.split("-", 1)
            if _parse(lo) <= py <= _parse(hi):
                return True
        else:
            exact = _parse(c)
            if py[: len(exact)] == exact:
                return True
    return False


# ── Library / framework metadata ─────────────────────────────────────────────

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
        "version_expr": "Crypto.__version__",
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
    ("Flask", "0"): ["Jinja2<3.1.0", "MarkupSafe<2.1.0"],
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
    import flask
    lib_version = {m['version_expr']}
    return jsonify({{
        "language": {{"name": "Python", "version": sys.version.split()[0]}},
        "framework": {{"name": "Flask", "version": flask.__version__}},
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
from django.urls import path
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


urlpatterns = [
    path("", hello),
    path("version", version_view),
]

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
    "M2Crypto":     (0, 45),   # 0.45 first release without fd_set/__fds_bits SWIG issue on glibc 2.36
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

_PYPI_RELEASES: dict = {}   # pip_name -> sorted list of release version strings


def _ver_key(v: str) -> tuple:
    """'3.10.4' -> (3, 10, 4)  for numeric sorting."""
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def _fetch_releases(pip_name: str) -> list:
    """Return all stable release version strings for pip_name from PyPI (cached)."""
    if pip_name in _PYPI_RELEASES:
        return _PYPI_RELEASES[pip_name]

    url = f"https://pypi.org/pypi/{pip_name}/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        # Keep only versions that are purely numeric (no pre/post/dev suffixes)
        # and whose release files are not empty (yanked releases have empty lists)
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
    """Resolve a registry version to the highest matching PyPI release.

    - '3.10'  -> highest available '3.10.x' (e.g. '3.10.4')
    - '47.0'  -> highest available '47.0.x' (e.g. '47.0.0')
    - '0.11'  -> None if neither '0.11' nor '0.11.x' exist on PyPI
    - '1.6.2' -> '1.6.2' (already fully specified, returned unchanged)

    Returns None when the version (or any patch of it) cannot be found on
    PyPI at all — the caller should skip generating that image context.
    """
    if registry_ver.count(".") != 1:
        return registry_ver  # 3-component version: trust the registry

    releases = _fetch_releases(pip_name)

    prefix = registry_ver + "."
    candidates = [v for v in releases if v.startswith(prefix)]
    if candidates:
        return candidates[-1]  # highest X.Y.z

    # No X.Y.z found; accept X.Y itself if it's a real published release.
    if registry_ver in releases:
        return registry_ver

    return None  # version does not exist on PyPI


# ── requirements.txt ──────────────────────────────────────────────────────────

def make_requirements(fw_name: str, fw_major: str,
                      lib_name: str, lib_ver: str) -> str | None:
    """Return requirements.txt content, or None if the library version is
    not available on PyPI."""
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


# ── File writer ───────────────────────────────────────────────────────────────

def write_context(py_ver: str, fw_name: str, fw_major: str,
                  lib_name: str, lib_ver: str) -> bool:
    """Write app.py / requirements.txt / Dockerfile for one context.

    Returns False (and removes any stale directory) when the library version
    does not exist on PyPI.
    """
    out = (IMAGES_BASE / "python" / py_ver
           / fw_name / fw_major / lib_name / lib_ver)

    req = make_requirements(fw_name, fw_major, lib_name, lib_ver)
    if req is None:
        if out.exists():
            shutil.rmtree(out)
        return False

    out.mkdir(parents=True, exist_ok=True)
    (out / "app.py").write_text(APP_MAKER[fw_name](lib_name), encoding="utf-8")
    (out / "requirements.txt").write_text(req, encoding="utf-8")
    (out / "Dockerfile").write_text(
        make_dockerfile(py_ver, lib_name, lib_ver), encoding="utf-8"
    )
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    lang = next(l for l in registry["languages"] if l["id"] == "python")

    included = [v["nr"] for v in lang["versions"] if v.get("include")]
    print(f"Python versions to build: {included}")

    # Pre-fetch PyPI releases for all libraries that need resolution so the
    # cache is warm and progress messages appear before file generation starts.
    pip_names = {
        meta["pip"]
        for meta in LIB_META.values()
        if meta["pip"] is not None
    }
    print("Fetching available versions from PyPI ...")
    for name in sorted(pip_names):
        releases = _fetch_releases(name)
        print(f"  {name}: {len(releases)} releases found")
    print()

    count = 0
    skipped = 0
    not_on_pypi = 0

    for py_ver in included:
        for fw in lang["frameworks"]:
            fw_name = fw["name"]
            for fw_ver in fw["version"]:
                fw_major = fw_ver["nr"]
                if not is_compatible(py_ver, fw_ver.get("compatibility", [])):
                    skipped += 1
                    continue

                for lib in lang["cryptography_libs"]:
                    lib_name = lib["name"]

                    if lib.get("version") == "built-in":
                        write_context(py_ver, fw_name, fw_major, lib_name, "builtin")
                        count += 1
                        continue

                    for lv in lib.get("version", []):
                        lib_ver = lv["nr"]
                        out = (IMAGES_BASE / "python" / py_ver
                               / fw_name / fw_major / lib_name / lib_ver)

                        if not lv.get("available", True):
                            if out.exists():
                                shutil.rmtree(out)
                            skipped += 1
                            continue

                        compat = lv.get("compatibility", [])
                        if not compat or not is_compatible(py_ver, compat):
                            if out.exists():
                                shutil.rmtree(out)
                            skipped += 1
                            continue

                        if write_context(py_ver, fw_name, fw_major, lib_name, lib_ver):
                            count += 1
                        else:
                            not_on_pypi += 1

    print(f"Generated : {count} image contexts")
    print(f"Skipped   : {skipped} incompatible combinations")
    if not_on_pypi:
        print(f"Not on PyPI: {not_on_pypi} version(s) removed/skipped")
    print(f"Location  : {IMAGES_BASE}")


if __name__ == "__main__":
    main()
