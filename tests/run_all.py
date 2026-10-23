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
import tempfile
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

# ── THE SUITE IS A DRILL, AND SAYS SO ─────────────────────────────
#
# WHY. On 2026-10-13 running this suite on the box set atlas_state.mode =
# PAUSED mid-session, because tools/verify_long_only drove a real failure
# through breaker.halt() and halt() also PATCHes shared state. 3d18fe7 fenced
# that: breaker.is_drill() refuses to touch production while drilling.
#
# The fence only works if something declares the drill, and on 2026-10-14 the
# same class of accident reappeared in a new place -- tests/test_market_hours_loop
# calls the real cycle(), cycle() advances the sentiment window, and the window
# persists to /var/lib/atlas/sentiment_window.json. Running the suite injected
# an observation into the LIVE window. Harmless this time (one "unavailable",
# which can permit nothing) and not harmless in principle: mid-session it would
# dilute a real establishment.
#
# SO THE RUNNER DECLARES IT, ONCE, FOR EVERY SUITE. Fencing each writer as it is
# discovered is a losing game -- this is the second one. A suite that wants real
# state has to opt out deliberately, which is the right way round.
os.environ["ATLAS_DRILL"] = "1"
# ── A SEEDED TICK MAP, SO LOOKUPS SUCCEED WITHOUT A BROKER ────────
#
# Tick size is per instrument as of 2026-10-16 and price_for() REFUSES rather
# than defaulting when none is known -- which is the whole point, and which
# would otherwise make every protective call in the suite raise.
#
# Production seeds this from kite.instruments("NSE") in the 08:30 job. The
# suite seeds a fixture instead: every universe symbol at 0.05 so existing
# expectations hold, plus the real ticks for the symbols that matter to the
# tick tests. A suite that needs a different tick passes tick= explicitly.
_TICKMAP = Path(tempfile.gettempdir()) / "atlas_test_tick_map.json"
os.environ.setdefault("ATLAS_TICK_MAP_FILE", str(_TICKMAP))
if os.environ["ATLAS_TICK_MAP_FILE"] == str(_TICKMAP):
    import json as _json
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    _day = _dt.now(_tz(_td(hours=5, minutes=30))).date().isoformat()
    try:
        sys.path.insert(0, str(ROOT))
        from engine.universe import SYMBOL_SECTOR_MAP as _MAP
        _seed = {s: 0.05 for s in _MAP}
    except Exception:
        _seed = {}
    _seed.update({"RELIANCE": 0.10, "ATHERENERG": 0.10, "TATAELXSI": 0.10,
                  "SONATSOFTW": 0.05, "HEXT": 0.05, "IDEA": 0.01,
                  "MRF": 5.00, "MARUTI": 1.00, "ABB": 0.50,
                  "TATASTEEL": 0.05, "X": 0.05, "TESTSYM": 0.05})
    _TICKMAP.write_text(_json.dumps({"days": {_day: _seed},
                                     "last_known": _seed}))

os.environ.setdefault(
    "ATLAS_BREAKER_RANGE_FILE",
    str(Path(tempfile.gettempdir()) / "atlas_test_breaker_range.json"))
os.environ.setdefault(
    "ATLAS_BREAKER_BLOCKED_FILE",
    str(Path(tempfile.gettempdir()) / "atlas_test_breaker_blocked.json"))
os.environ.setdefault(
    "ATLAS_SENTIMENT_STANDDOWN_FILE",
    str(Path(tempfile.gettempdir()) / "atlas_test_sentiment_standdown.json"))
# THE TWO STATE FILES ADDED 2026-10-16, redirected for the same reason as the
# three above: the suite blocks symbols and registers unprotected positions,
# and neither may touch the live files the box reads at 09:20. Both modules
# also refuse to write their canonical path while is_drill() is true, so this
# is the second of two guards.
os.environ.setdefault(
    "ATLAS_SYMBOL_BLOCK_FILE",
    str(Path(tempfile.gettempdir()) / "atlas_test_symbol_block.json"))
os.environ.setdefault(
    "ATLAS_UNPROTECTED_FILE",
    str(Path(tempfile.gettempdir()) / "atlas_test_unprotected.json"))

SUITES = [
    ("environment",     [sys.executable, "tests/test_environment.py"]),
    ("entry ordering", [sys.executable, "tests/test_entry_ordering.py"]),
    ("reconcile",      [sys.executable, "tests/test_reconcile.py"]),
    ("exits",          [sys.executable, "tests/test_exits.py"]),
    # The 2026-10-07 rebuild: order types, fallback independence, square-off
    # timing, the halt guards, and the two operator-facing strings.
    ("exit path rebuild", [sys.executable, "tests/test_exit_path_rebuild.py"]),
    ("universe publish gate", [sys.executable,
                               "tests/test_universe_publish_gate.py"]),
    ("switch inventory", [sys.executable, "tests/test_switch_inventory.py"]),
    ("live sentiment", [sys.executable, "tests/test_live_sentiment.py"]),
    ("sentiment stand-down", [sys.executable,
                              "tests/test_sentiment_standdown.py"]),
    ("range breaker", [sys.executable, "tests/test_range_breaker.py"]),
    ("feed freshness", [sys.executable, "tests/test_feed_freshness.py"]),
    ("bhavcopy dates", [sys.executable, "tests/test_bhavcopy_dates.py"]),
    ("price basis guard", [sys.executable, "tests/test_price_basis_guard.py"]),
    ("calendar source", [sys.executable, "tests/test_calendar_source.py"]),
    ("tick sizes", [sys.executable, "tests/test_tick_sizes.py"]),
    ("session report", [sys.executable, "tests/test_session_report.py"]),
    ("unprotected entry", [sys.executable, "tests/test_unprotected_entry.py"]),
    ("home page",       [sys.executable, "tests/test_home_page.py"]),
    ("page language",   [sys.executable, "tests/test_page_language.py"]),
    ("tape vocabulary", [sys.executable, "tests/test_tape_vocabulary.py"]),
    ("tab bar shared", [sys.executable, "tests/test_tab_bar_shared.py"]),
    ("gates intact",    [sys.executable, "tests/test_gates_intact.py"]),
    # The two paths that bypassed the gates: the accumulation undo leaving
    # later gate failures unapplied, and `oversized` computed and ignored.
    ("gate bypass",     [sys.executable, "tests/test_gate_bypass.py"]),
    ("page cannot write breaker",
                        [sys.executable, "tests/test_page_cannot_write_breaker.py"]),
    ("sizing",           [sys.executable, "tests/test_sizing.py"]),
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
    ("page styles",      [sys.executable, "tests/test_page_styles.py"]),
    ("push payloads",    [sys.executable, "tests/test_push_payloads.py"]),
    ("learning loop",    [sys.executable, "tests/test_learning.py"]),
    ("migrations",       [sys.executable, "tests/test_migrations.py"]),
    ("crontab",          [sys.executable, "tests/test_crontab.py"]),
    # TSL Flash Radar R1: the Hot 10, displacement, and the hard rules on
    # cause attribution and action language.
    ("radar",            [sys.executable, "tests/test_radar.py"]),
    # The long-only proof and the GTT-failure-halts demonstration. In the suite
    # rather than run by hand: both are claims the operator asked to see held,
    # and a claim nothing re-checks is a claim that decays.
    ("short path",      [sys.executable, "-m", "tools.verify_short_path"]),
    # The day-0/day-1 handover and the move-up-only stop guard. In the suite
    # because both are claims the operator asked to see held, and the stop
    # guard in particular is the one thing in the engine that must never
    # regress quietly.
    ("day0 and trail",   [sys.executable, "-m", "tools.verify_day0_and_trail"]),
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
