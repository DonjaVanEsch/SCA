"""Wipe every build/test/fingerprint result and batch (run), server AND
client side, as if nothing had ever been built or tested. Leaves the
reference tables, images/client_images themselves (incl. ignored flags),
and the registry/version_overrides completely untouched -- this only
clears RESULTS, not the source-of-truth matrix those results are about.

Deletes in FK-safe order (children before the `runs` parent, since
run_id has no ON DELETE CASCADE): build_results, test_results,
fingerprints, client_build_results, client_test_results,
client_fingerprints, then runs.

Usage: python scripts/reset_test_data.py --yes
(the --yes flag is required so this can never fire by accident, e.g. from
a copy-pasted command missing its intended argument)
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent))

import db  # noqa: E402 (db.py lives at the project root, see generate_images.py)

_TABLES = [
    "build_results",
    "test_results",
    "fingerprints",
    "client_build_results",
    "client_test_results",
    "client_fingerprints",
    "runs",
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true",
                         help="Actually perform the reset (otherwise just reports counts).")
    args = parser.parse_args()

    db.init_db()
    with db._connect() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _TABLES}

    print("Rows that would be deleted:")
    for t, n in counts.items():
        print(f"  {t}: {n:,}")

    if not args.yes:
        print("\nDry run only -- re-run with --yes to actually delete.")
        return

    with db._connect() as conn:
        for t in _TABLES:
            conn.execute(f"DELETE FROM {t}")

    print("\nDone -- all build/test/fingerprint results and batches cleared.")


if __name__ == "__main__":
    main()
