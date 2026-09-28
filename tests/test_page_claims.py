#!/usr/bin/env python3
"""
OPERATOR-FACING CLAIMS MUST MATCH THE CODE
==========================================
Four claims outlived the rules they described, all by the same mechanism: every
page here is static, nothing serves config to the browser, so each figure was a
hand-maintained literal. signals.html carried a comment admitting it -- "if Rule
1 changes in config.py, change it here too. It read Rs5,000 until 28 Aug 2026 and
matched nothing." Knowing about the mechanism did not stop it producing three
more.

    Rs2,000 daily cap        zerodha_morning   — a cap that no longer exists
    Max Rs5,000 risk/trade   signals.html      — the figure was Rs3,000
    Rs1L flat capital basis  tsl-dashboard     — the view was rebuilt on real
                                                 notional in 20260821201337
    Targets at 2:1 and 3:1   signals.html      — zone_entry drops targets

This suite is the part that makes stamping worth doing: it fails the moment a
page and atlas/config.py disagree, so the fifth one cannot be committed quietly.

TWO KINDS OF CHECK, because they fail differently:
  NUMBERS   stamped spans, compared against the constants themselves
  CLAIMS    sentences that cannot be substituted -- a wrong sentence is not
            fixable by rewriting a number, so each is asserted against the
            behaviour it describes

    python3 tests/test_page_claims.py
"""

import re
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
logging.basicConfig(level=logging.CRITICAL)

import importlib.util as il                                   # noqa: E402
_sp = il.spec_from_file_location("stampcfg", ROOT / "tools/stamp_config.py")
S = il.module_from_spec(_sp)
_sp.loader.exec_module(S)


def read(name):
    p = ROOT / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def visible(src: str) -> str:
    """
    `src` with HTML and JS block comments stripped.

    A claim is what the page SHOWS. The comments here deliberately quote the
    wording they replaced -- "it said 'each sized at the Rs1L notional cap'" --
    so a naive substring check would flag the explanation as the defect and
    pressure whoever hits it into deleting the history rather than the claim.
    """
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def main() -> int:
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {label:<56}{'ok' if cond else '** FAILS **'}  {detail}")

    print("=" * 78)
    print("STAMPED NUMBERS MATCH atlas/config.py")
    print("-" * 78)
    vals = S.values()
    bad = S.drifted(vals)
    check("no stamped span disagrees with the config", not bad,
          "" if not bad else f"{len(bad)} drifted")
    for name, key, found, expected in bad:
        print(f"      {name}: {key}  page={found!r} config={expected!r}")
    total = sum(len(S.scan(ROOT / n, vals)) for n in S.PAGES)
    check("the pages actually carry stamped spans", total > 0, f"{total} span(s)")
    # the stamper must be idempotent, or --write in a deploy would churn the tree
    check("stamping is idempotent (no drift right after a check)",
          not S.drifted(vals))

    print()
    print("CLAIMS THAT CANNOT BE STAMPED, ASSERTED AGAINST BEHAVIOUR")
    print("-" * 78)
    signals = visible(read("signals.html"))
    dash    = visible(read("tsl-dashboard.html"))
    index   = visible(read("index.html"))
    wait    = visible(read("waitlist.html"))

    # 1. TARGETS. zone_entry explicitly drops target_1/target_2/rr_1/rr_2 as
    #    trade levels: "target = NONE. Winners are held and trailed." Any page
    #    promising a 2:1/3:1 target is describing a strategy that was retired.
    from engine.zone_entry import MAX_ENTRY_DIST_PCT           # noqa: F401
    ze = read("engine/zone_entry.py") or (ROOT / "engine/zone_entry.py").read_text()
    check("zone_entry still drops targets (the premise holds)",
          'for dead in ("target_1", "target_2", "rr_1", "rr_2", "sl_pct")' in ze)
    check("signals.html does not promise 2:1 / 3:1 targets",
          "Targets at 2:1" not in signals and "targets at 2:1" not in signals)
    check("signals.html does not say 'Hold until target'",
          "Hold until target" not in signals)
    # 2. THE INVENTED RATIO. (s.rr_1||2) printed "2:1 -> 3:1" even when the
    #    fields were absent -- a number produced by a fallback, shown as if it
    #    were the trade's own.
    check("no (s.rr_1||2) fallback fabricating a ratio",
          "s.rr_1||2" not in signals.replace(" ", ""))
    check("an absent rr renders as an open target instead",
          "open target" in signals)
    # 3. THE CAPITAL BASIS. v_capital_window was rebuilt on real notional.
    mig = read("migrations/20260821201337_aggregates_real_notional_peak_and_avg.sql")
    check("the capital view is built on real notional (premise holds)",
          "DROP VIEW IF EXISTS public.v_capital_window" in mig)
    check("the dashboard no longer claims a flat Rs1L basis",
          "1L notional cap" not in dash and "Rs1L notional cap" not in dash)
    # 4. THE UNIVERSE. Mechanically filtered NSE names, not a published index.
    from engine.universe import ALL_SYMBOLS
    for page, name in ((index, "index.html"), (wait, "waitlist.html")):
        check(f"{name} does not claim 'Nifty 500'",
              "Nifty 500" not in page and "NIFTY 500" not in page)
    check("the stamped universe count is the real one",
          vals["universe_count"] == str(len(ALL_SYMBOLS)),
          f"{len(ALL_SYMBOLS)} symbols")

    print()
    print("THE MODES ARE THE TWO THAT MEAN SOMETHING")
    print("-" * 78)
    from atlas.config import AGENT_MODES, HALT_MODES, DEFAULT_AGENT_MODE
    from atlas.reporting.directives import normalise_mode, handle_directive
    check("AGENT_MODES is exactly NORMAL and PAUSED",
          set(AGENT_MODES) == {"NORMAL", "PAUSED"}, str(AGENT_MODES))
    check("PAUSED is still the only halting mode",
          tuple(HALT_MODES) == ("PAUSED",))
    # A retired mode left in atlas_state must not surface as if it still existed.
    norm_ok = all(normalise_mode(g) == DEFAULT_AGENT_MODE
                  for g in ("AGGRESSIVE", "CAUTIOUS", "DEFENSIVE", "garbage",
                            "", None))
    check("a retired mode in the DB normalises to NORMAL", norm_ok)
    check("PAUSED survives normalisation", normalise_mode("PAUSED") == "PAUSED")
    # and the retired commands say so rather than falling through to "unknown"
    for cmd in ("/aggressive", "/cautious", "/defensive"):
        r = handle_directive(cmd)
        good = "NO LONGER EXISTS" in r
        ok &= good
        print(f"  {cmd + ' is answered, not ignored':<56}"
              f"{'ok' if good else '** FAILS **'}")

    print()
    print("THE DEPLOY CAN REACH THE RUNNING LISTENER")
    print("-" * 78)
    wd = read("scripts/bot_watchdog.sh")
    check("the watchdog compares the deployed SHA", "rev-parse HEAD" in wd)
    check("  and restarts when it moves", "kill -TERM" in wd)
    check("  recording the SHA only after a successful spawn",
          "FAILED to start" in wd)
    bl = read("atlas/reporting/bot_listener.py")
    check("the listener persists its Telegram offset", "save_offset" in bl)
    check("  before handling the update, not after",
          bl.index("save_offset(offset)") < bl.index("process_update(update)"))

    print("-" * 78)
    print("PAGE CLAIMS:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
