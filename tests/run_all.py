#!/usr/bin/env python3
"""
Run every harness in tests/, plus the in-module self-tests that assert
something a caller depends on.

Each harness runs in its own interpreter. They stub module internals -- a
module patched by one test must not stay patched for the next -- and a
subprocess is the cheap way to guarantee that without a framework.

Exits non-zero if anything fails, so this works unchanged from a shell, from
cron, or from CI.
"""

import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, argv). Self-tests live in their modules; this just drives them so one
# command covers everything.
SUITES = [
    ("entry ordering", [sys.executable, "tests/test_entry_ordering.py"]),
    ("reconcile",      [sys.executable, "tests/test_reconcile.py"]),
    ("breaker",        [sys.executable, "-m", "atlas.risk.breaker"]),
    ("position sizing", [sys.executable, "-m", "atlas.risk.position_sizing"]),
]


def main() -> int:
    failed = []
    for label, argv in SUITES:
        # flush before handing stdout to the child, or our headers buffer and
        # land after the output they are supposed to introduce.
        print("=" * 78, flush=True)
        print(f"── {label}", flush=True)
        print("=" * 78, flush=True)
        r = subprocess.run(argv, cwd=ROOT)
        if r.returncode != 0:
            failed.append(label)
        print()

    print("=" * 78)
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(SUITES)} suites passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
