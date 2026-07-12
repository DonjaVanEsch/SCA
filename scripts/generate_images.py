#!/usr/bin/env python3
"""
Generate Docker image contexts from a language registry JSON.

Output structure:
  images/<lang>/<lang_ver>/<framework>/<fw_ver>/<lib_name>/<lib_ver>/

Language-specific logic (app templates, package resolution, Dockerfile
generation) lives in lang_<id>.py modules alongside this file.

Usage:
  python generate_images.py [--lang python]

Options:
  --lang LANG   Language ID to generate (default: python).
                A matching 'lang_<LANG>.py' and 'registry <LANG>.json'
                must exist in the same directory as this script.
  --test        (handled by manager.py)
  --remove      (handled by manager.py)
"""

import argparse
import importlib
import json
import re
import shutil
import sys
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent
IMAGES_BASE = SCRIPT_DIR.parent / "images"


# ── Compatibility helpers (language-agnostic) ─────────────────────────────────

def _parse(s: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", s))


def is_compatible(lang_ver: str, compat_list: list) -> bool:
    """Return True if lang_ver satisfies at least one entry in compat_list.

    Handles both Python-style (2.x vs 3.x major split) and Go-style (all 1.x)
    version ranges:
      "3.9+"      – lang_ver >= 3.9
      "3.6-3.9"   – 3.6 <= lang_ver <= 3.9
      "1.21+"     – lang_ver >= 1.21
    When compat_list contains both 2.x and 3.x ranges, only ranges whose major
    version matches lang_ver are evaluated.
    """
    ver = _parse(lang_ver)
    ver_major = ver[0]

    same, other = [], []
    for raw in compat_list:
        c = raw.strip().replace(".x", ".0")
        head = re.split(r"[+\-]", c)[0]
        (same if _parse(head)[0] == ver_major else other).append(c)

    if not same:
        return False

    for c in same:
        if c.endswith("+"):
            if ver >= _parse(c[:-1]):
                return True
        elif "-" in c:
            lo, hi = c.split("-", 1)
            if _parse(lo) <= ver <= _parse(hi):
                return True
        else:
            exact = _parse(c)
            if ver[: len(exact)] == exact:
                return True
    return False


def _included(lang_ver: str, compat) -> bool:
    """Decide whether lang_ver passes a compatibility specification.

    - compat is None  → no restriction, always include
    - compat is []    → no validated entries, always skip
    - compat is [..] → evaluate with is_compatible()
    """
    if compat is None:
        return True
    if not compat:
        return False
    return is_compatible(lang_ver, compat)


# ── Generic main loop ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Docker image contexts from a language registry."
    )
    parser.add_argument(
        "--lang", default="python",
        help="Language ID to generate (default: python).",
    )
    parser.add_argument(
        "--version", default=None,
        help="Only regenerate this language version (e.g. 1.2). Default: all included versions.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Write into this directory instead of ../images (e.g. for a dry-run "
             "diff against the live tree without touching it).",
    )
    args = parser.parse_args()
    lang_id = args.lang.lower()
    images_base = Path(args.out) if args.out else IMAGES_BASE

    # Make lang_<id>.py importable from the same directory as this script.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        lang = importlib.import_module(f"lang_{lang_id}")
    except ModuleNotFoundError:
        print(f"ERROR: No language module found for '{lang_id}' "
              f"(expected {SCRIPT_DIR / f'lang_{lang_id}.py'})")
        sys.exit(1)

    registry_path = SCRIPT_DIR / lang.REGISTRY_FILE
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    lang_data = next(
        (l for l in registry["languages"] if l["id"] == lang_id), None
    )
    if lang_data is None:
        print(f"ERROR: Language '{lang_id}' not found in {registry_path.name}")
        sys.exit(1)
    if not lang_data.get("include", True):
        print(f"'{lang_id}' is marked include:false in {registry_path.name} -- skipping.")
        return

    included_versions = [v["nr"] for v in lang_data["versions"] if v.get("include")]
    if args.version:
        if args.version not in included_versions:
            print(f"ERROR: version '{args.version}' not found (or not included) "
                  f"in {registry_path.name}")
            sys.exit(1)
        included_versions = [args.version]
    print(f"{lang_id} versions to build: {included_versions}")

    lang.prefetch(lang_data)

    count = skipped = not_available = 0

    for lang_ver in included_versions:
        for fw in lang_data["frameworks"]:
            # Frameworks marked include:false are skipped entirely (e.g. Martini).
            if not fw.get("include", True):
                continue

            fw_name     = fw["name"]
            fw_versions = fw.get("version")

            # Built-in framework (e.g. net/http in Go): treat as single
            # unversioned entry with no compatibility restriction.
            if fw_versions == "built-in":
                fw_ver_list = [{"nr": "builtin"}]
            else:
                fw_ver_list = fw_versions

            for fw_ver in fw_ver_list:
                fw_major = fw_ver["nr"]
                fw_compat = fw_ver.get("compatibility")  # None → no restriction

                if not _included(lang_ver, fw_compat):
                    fw_dir = images_base / lang_id / lang_ver / fw_name / fw_major
                    if fw_dir.exists():
                        shutil.rmtree(fw_dir)
                    skipped += 1
                    continue

                for lib in lang_data["cryptography_libs"]:
                    lib_name     = lib["name"]
                    lib_versions = lib.get("version")

                    if lib_versions == "built-in":
                        # Optional lib-level compatibility (e.g. crypto/mlkem → 1.24+).
                        lib_compat = lib.get("compatibility")
                        if not _included(lang_ver, lib_compat):
                            out = (images_base / lang_id / lang_ver
                                   / fw_name / fw_major / lib_name / "builtin")
                            if out.exists():
                                shutil.rmtree(out)
                            skipped += 1
                            continue
                        if lang.write_context(
                            lang_ver, fw_name, fw_major, lib_name, "builtin", images_base
                        ):
                            count += 1
                        continue

                    for lv in lib_versions:
                        lib_ver_nr = lv["nr"]
                        out = (images_base / lang_id / lang_ver
                               / fw_name / fw_major / lib_name / lib_ver_nr)

                        if not lv.get("available", True):
                            if out.exists():
                                shutil.rmtree(out)
                            skipped += 1
                            continue

                        lv_compat = lv.get("compatibility")
                        if not _included(lang_ver, lv_compat):
                            if out.exists():
                                shutil.rmtree(out)
                            skipped += 1
                            continue

                        if lang.write_context(
                            lang_ver, fw_name, fw_major, lib_name, lib_ver_nr, images_base
                        ):
                            count += 1
                        else:
                            not_available += 1

    print(f"Generated    : {count} image contexts")
    print(f"Skipped      : {skipped} incompatible combinations")
    if not_available:
        print(f"Not available: {not_available} version(s) removed/skipped")
    print(f"Location     : {images_base}")


if __name__ == "__main__":
    main()
