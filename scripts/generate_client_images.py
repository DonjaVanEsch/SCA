#!/usr/bin/env python3
"""
Generate outbound-call client image contexts from a language registry JSON's
"http_clients" section (see the client-fingerprinting experiment described
in registry python.json's "_comment_http_clients").

Output structure:
  images_clients/<lang>/<lang_ver>/<http_client_name>/<http_client_ver>/

Mirrors generate_images.py's own structure and compatibility helpers, but
over the simpler 2D (language x http_client) matrix -- no framework/library
cross product. Language-specific logic lives in lang_<id>_client.py modules,
matching the lang_<id>.py convention used for the server-side generator.

Usage:
  python generate_client_images.py [--lang python]
"""

import argparse
import importlib
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR         = Path(__file__).parent
CLIENT_IMAGES_BASE = SCRIPT_DIR.parent / "images_clients"

sys.path.insert(0, str(SCRIPT_DIR))
from generate_images import _included  # noqa: E402 (language-agnostic, reused as-is)

_REGISTRY_FILES = {
    "python": SCRIPT_DIR / "registry python.json",
    "node":   SCRIPT_DIR / "registry node.json",
    "php":    SCRIPT_DIR / "registry php.json",
    "java":   SCRIPT_DIR / "registry java.json",
    "dotnet": SCRIPT_DIR / "registry dotnet.json",
}


def _load_client_module(lang_id: str):
    try:
        return importlib.import_module(f"lang_{lang_id}_client")
    except ModuleNotFoundError:
        return None


def generate(lang_id: str) -> None:
    registry_path = _REGISTRY_FILES[lang_id]
    lang_mod = _load_client_module(lang_id)
    if lang_mod is None:
        print(f"No lang_{lang_id}_client.py yet -- skipping {lang_id}.")
        return

    data = json.loads(registry_path.read_text(encoding="utf-8"))
    lang_obj = data["languages"][0]
    http_clients = lang_obj.get("http_clients", [])
    if not http_clients:
        print(f"No 'http_clients' section in registry {lang_id}.json yet.")
        return

    lang_versions = [v["nr"] for v in lang_obj.get("versions", []) if v.get("include", True)]
    print(f"{lang_id} client versions to build: {lang_versions}")

    generated = 0
    skipped   = 0

    for lang_ver in lang_versions:
        for hc in http_clients:
            hc_name = hc["name"]
            versions = hc.get("version")

            if versions == "built-in":
                hc_ver_list = [{"nr": "builtin", "compatibility": None}]
            else:
                hc_ver_list = versions or []

            for hv in hc_ver_list:
                hc_ver = hv.get("nr", "")
                # `available: false` marks a version as a historical
                # reference row (known real ceiling/impossibility) -- never
                # actually generated, same convention as generate_images.py.
                if not hv.get("available", True) or not _included(lang_ver, hv.get("compatibility")):
                    out = (CLIENT_IMAGES_BASE / lang_id / lang_ver / hc_name / hc_ver)
                    if out.exists():
                        shutil.rmtree(out)
                    skipped += 1
                    continue

                # The raw registry bucket (e.g. "0.48"), NOT a pre-resolved
                # exact patch version, is what names the directory/DB row --
                # matching generate_images.py's own convention for the
                # server side. Resolution to an exact pip-installable
                # version happens INSIDE write_client_context()'s own
                # make_requirements(), same as lang_python.py's
                # make_requirements() does for server-side libraries. Doing
                # it here instead would put the resolved version in the
                # directory name, which load_registry() never sees (it
                # stores the raw bucket), silently breaking DB sync.
                if lang_mod.write_client_context(lang_ver, hc_name, hc_ver, CLIENT_IMAGES_BASE):
                    generated += 1
                else:
                    print(f"  [SKIP] {hc_name} {hc_ver} not resolvable for {lang_id} {lang_ver}")
                    skipped += 1

    print(f"\nGenerated : {generated} client image context(s)")
    print(f"Skipped   : {skipped} incompatible/unresolvable combination(s)")
    print(f"Location  : {CLIENT_IMAGES_BASE}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", choices=list(_REGISTRY_FILES), default="python",
                        help="Language ID to generate (default: python).")
    args = parser.parse_args()
    generate(args.lang)


if __name__ == "__main__":
    main()
