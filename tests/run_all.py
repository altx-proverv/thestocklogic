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

import os
import sys
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (label, argv). Self-tests live in their modules; this just drives them so one
# command covers everything.
# PANDAS 3 SEMANTICS, ON A PANDAS 2 VENV.
#
# The box runs pandas 3 and this venv runs 2.3.3, so an entire class of defect is a
# passing test here and a crash there: pandas 2 emits a FutureWarning and silently
# widens a column whose dtype cannot hold what is being assigned, while pandas 3
# raises TypeError. 03b published nothing for a night because of exactly that, and
# the suite was green throughout.
#
# Turning the warning into an error makes the local run behave like the box. If a
# third-party library trips this and cannot be fixed here, narrow it with a
# module-specific filter rather than removing this line.
# Weak on its own: a module-level warnings.filterwarnings("ignore") runs at import
# and overrides this, which is exactly how the pandas dtype deprecation went
# unheard for a year. tests/test_environment.py is what actually guards it, by
# asserting no pipeline module installs a blanket filter. Kept because it catches
# deprecations in modules that have no filter of their own.
os.environ.setdefault("PYTHONWARNINGS", "error::FutureWarning")

SUITES = [
    ("environment",     [sys.executable, "tests/test_environment.py"]),
    ("entry ordering", [sys.executable, "tests/test_entry_ordering.py"]),
    ("reconcile",      [sys.executable, "tests/test_reconcile.py"]),
    ("exits",          [sys.executable, "tests/test_exits.py"]),
    ("regime gate",    [sys.executable, "tests/test_regime_gate.py"]),
    ("market-hours loop", [sys.executable, "tests/test_market_hours_loop.py"]),
    ("unit files",     [sys.executable, "tests/test_unit_files.py"]),
    # A subset here: the reference implementation is the slow one, so the full
    # sweep costs ~270 ms/symbol. Run it unlimited on the box, where the data
    # has shapes this checkout does not -- recent listings, gappy series.
    ("smc equivalence", [sys.executable, "tests/test_smc_equivalence.py",
                         "--limit", "40"]),
    ("bhavcopy layouts", [sys.executable, "tests/test_bhavcopy_layouts.py"]),
    ("bhavcopy guard",  [sys.executable, "tests/test_bhavcopy_guard.py"]),
    ("exclusion block",  [sys.executable, "tests/test_exclusion_block.py"]),
    ("universe idempotency", [sys.executable, "tests/test_universe_idempotency.py"]),
    ("fundamentals",    [sys.executable, "tests/test_fundamentals.py"]),
    ("tier1 sources",   [sys.executable, "tests/test_tier1_sources.py"]),
    ("batch & report",  [sys.executable, "tests/test_batch_and_report.py"]),
    ("page claims",     [sys.executable, "tests/test_page_claims.py"]),
    ("market flash",    [sys.executable, "tests/test_market_flash.py"]),
    ("meridian iv",     [sys.executable, "tests/test_meridian_iv.py"]),
    ("signal schema",   [sys.executable, "tests/test_signal_schema.py"]),
    ("excursions",      [sys.executable, "tests/test_excursions.py"]),
    ("detection score",  [sys.executable, "tests/test_detection_scoring.py"]),
    ("outcome integrity",[sys.executable, "tests/test_outcome_integrity.py"]),
    ("gate audit",       [sys.executable, "tests/test_gate_audit.py"]),
    ("reject sample",    [sys.executable, "tests/test_reject_sample.py"]),
    ("breaker",        [sys.executable, "-m", "atlas.risk.breaker"]),
    ("position sizing", [sys.executable, "-m", "atlas.risk.position_sizing"]),
]

# THE PAGE'S OWN LOGIC, run under node. Registered conditionally rather than
# unconditionally: the production box has no node, and a suite that cannot run
# there would turn every box-side run into a failure for a reason unrelated to
# the code. Absent node the suites are SKIPPED and say so -- not silently
# dropped, which is how a test stops being a test.
if shutil.which("node"):
    SUITES += [
        ("signals batch state", ["node", "tests/signals_batch_state.js"]),
        ("signals live view",   ["node", "tests/signals_live_view.js"]),
        ("signals holidays",    ["node", "tests/signals_holidays.js"]),
    ]
else:
    SUITES += [("signals page logic (SKIPPED — node not installed)",
                [sys.executable, "-c",
                 "print('node is not installed; tests/signals_*.js did not run')"])]


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
