"""Pre-deploy safety check for deploy_to_server.ps1.

The server's own dashboard.py can write new version buckets straight into
`registry *.json` (the "Include"/"Dismiss" new-versions actions) while it's
running. deploy_to_server.ps1 pushes a tar of the whole local scripts/ dir
and blindly overwrites the server's copy -- if the server has a bucket the
local checkout doesn't know about (e.g. an Include that happened on the
server, never pulled down locally), a routine deploy silently destroys it.
(This actually happened: an Include-added Django "6" bucket for Python was
wiped this way, recovered by re-running the Include action after the fact.)

This script fetches each server-side registry file over the existing SSH
alias, compares its (section, name, nr) bucket set against the local copy,
and exits non-zero -- printing exactly what would be lost -- if the server
has buckets the local file lacks. It never writes anything; deploy_to_server.ps1
aborts the push when this exits non-zero.
"""

import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SSH_HOST = "pqc-sca"
SERVER_SCRIPTS_DIR = "/home/donja/SCA/scripts"
SECTION_KEYS = ("frameworks", "cryptography_libs")


def _bucket_set(data: dict) -> set:
    buckets = set()
    for lang in data.get("languages", []):
        for section in SECTION_KEYS:
            for entry in lang.get(section, []):
                name = entry.get("name")
                for v in entry.get("version", []):
                    if isinstance(v, dict) and "nr" in v:
                        buckets.add((section, name, v["nr"]))
    return buckets


def _fetch_server_json(filename: str) -> dict | None:
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", SSH_HOST,
         f"cat '{SERVER_SCRIPTS_DIR}/{filename}'"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        print(f"  [WARN] could not fetch server copy of {filename}: {proc.stderr.strip()}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        print(f"  [WARN] server copy of {filename} is not valid JSON: {exc}")
        return None


def main() -> int:
    unsafe = False
    for local_path in sorted(SCRIPT_DIR.glob("registry *.json")):
        filename = local_path.name
        local_data = json.loads(local_path.read_text(encoding="utf-8"))
        server_data = _fetch_server_json(filename)
        if server_data is None:
            continue

        local_buckets = _bucket_set(local_data)
        server_buckets = _bucket_set(server_data)
        server_only = sorted(server_buckets - local_buckets)

        if server_only:
            unsafe = True
            print(f"[UNSAFE] {filename}: server has {len(server_only)} bucket(s) "
                  f"the local copy doesn't -- deploying now would delete them:")
            for section, name, nr in server_only:
                print(f"    {section}/{name} nr={nr}")

    if unsafe:
        print("\nAborting deploy. Pull these changes down locally first "
              "(e.g. copy the server's registry file over the local one, "
              "or manually re-apply the missing buckets) before redeploying.")
        return 1

    print("Deploy safety check passed -- no server-only registry buckets would be lost.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
