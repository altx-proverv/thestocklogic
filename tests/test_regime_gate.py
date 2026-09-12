#!/usr/bin/env python3
"""
THE ENTRY GATE: BOTH CONDITIONS, OR CASH
========================================

    REGIME     close above 200DMA AND 50DMA above 200DMA
    SENTIMENT  advancing > declining today AND Nifty above its 20DMA

Either fails, no entry. This spans build_market (which computes the DMAs and
breadth into market.parquet), get_market_context (which carries them) and
regime_allows_side (which applies the policy), so it belongs here rather than
in any one module's __main__.

WHAT IT IS PROTECTING
---------------------
1. That the AND is really an AND. A gate built from two conditions is one edit
   away from being an OR, and the failure is invisible -- it just trades more.

2. That a missing input is a NO. build_market writes nifty_20dma as NaN for the
   first 20 rows of any series, and the sector_heatmap fallback cannot supply
   breadth or a 20DMA at all. If unknown were to read as permitted, the gate
   would open exactly when it knows least. That is the shape of bug this repo
   keeps hitting -- an error path returning a plausible-looking value -- so it
   is asserted rather than assumed.

3. That the REGIME/SENTIMENT split is reported. "Log which one blocked" is only
   true if the two are distinguishable downstream, so the status must differ,
   not merely the prose.

4. That build_market's `bull` still means what the gate thinks it means. The
   gate reads market_regime rather than recomputing the DMA comparison, so if
   _classify's definition drifts the gate drifts silently with it. The last
   case pins the two together.
"""

import os
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
logging.basicConfig(level=logging.CRITICAL)

from atlas.execution.atlas_entry import regime_allows_side, sentiment_ok  # noqa: E402


def ctx(regime="bull", adv=1200, dec=800, close=25000, ma20=24500,
        source="market.parquet", extreme=False):
    return {"regime": regime, "advance_count": adv, "decline_count": dec,
            "nifty_close": close, "nifty_20dma": ma20, "source": source,
            "extreme_bearish": extreme, "allow_accumulation": regime in ("bull", "sideways")}


def main() -> int:
    ok = True
    rows = []

    def case(label, c, direction, want_ok, want_block):
        nonlocal ok
        got_ok, reason, blocked = regime_allows_side(c, direction)
        good = (got_ok == want_ok) and (blocked == want_block)
        ok &= good
        rows.append((label, got_ok, blocked or "-", good, reason))

    print("BOTH CONDITIONS REQUIRED")
    print("-" * 92)
    case("bull + breadth + above 20DMA", ctx(), "LONG", True, None)
    case("bull, breadth NEGATIVE", ctx(adv=700, dec=1300), "LONG", False, "SENTIMENT")
    case("bull, Nifty BELOW 20DMA", ctx(close=24000, ma20=24500), "LONG", False, "SENTIMENT")
    case("bull, both sentiment legs fail", ctx(adv=700, dec=1300, close=24000, ma20=24500),
         "LONG", False, "SENTIMENT")
    case("SIDEWAYS, sentiment fine", ctx(regime="sideways"), "LONG", False, "REGIME")
    case("BEAR, sentiment fine", ctx(regime="bear"), "LONG", False, "REGIME")
    case("unknown regime", ctx(regime="unknown"), "LONG", False, "REGIME")

    for label, got, blocked, good, reason in rows:
        print(f"  {label:<34}{'ENTER' if got else 'cash':<7}{blocked:<11}"
              f"{'ok' if good else '** WRONG **':<14}{reason[:34]}")

    # An AND that has become an OR still passes every case above except these.
    print()
    print("THE AND IS AN AND")
    print("-" * 92)
    regime_only, _, _ = regime_allows_side(ctx(adv=700, dec=1300), "LONG")
    sent_only, _, _ = regime_allows_side(ctx(regime="sideways"), "LONG")
    anded = (not regime_only) and (not sent_only)
    ok &= anded
    print(f"  {'regime passes, sentiment fails':<34}"
          f"{'ENTER' if regime_only else 'cash':<7}"
          f"{'ok' if not regime_only else '** OR, not AND **'}")
    print(f"  {'sentiment passes, regime fails':<34}"
          f"{'ENTER' if sent_only else 'cash':<7}"
          f"{'ok' if not sent_only else '** OR, not AND **'}")

    print()
    print("MISSING INPUT IS A NO, NEVER A PASS")
    print("-" * 92)
    for label, kw in (("nifty_20dma is NaN (series warm-up)", {"ma20": None}),
                      ("advance_count absent", {"adv": None}),
                      ("decline_count absent", {"dec": None}),
                      ("nifty_close absent", {"close": None})):
        c = ctx(**kw)
        got_ok, reason, blocked = regime_allows_side(c, "LONG")
        good = (not got_ok) and blocked == "SENTIMENT"
        ok &= good
        print(f"  {label:<40}{'cash' if not got_ok else 'ENTER':<7}"
              f"{'ok' if good else '** PERMITTED ON UNKNOWN **':<30}{reason[:30]}")

    # The fallback source carries a direction string and nothing else.
    c = {"regime": "bull", "source": "sector_heatmap (fallback)",
         "advance_count": None, "decline_count": None,
         "nifty_close": None, "nifty_20dma": None, "extreme_bearish": False}
    got_ok, reason, blocked = regime_allows_side(c, "LONG")
    good = (not got_ok) and blocked == "SENTIMENT"
    ok &= good
    print(f"  {'sector_heatmap fallback, regime bull':<40}"
          f"{'cash' if not got_ok else 'ENTER':<7}"
          f"{'ok' if good else '** FALLBACK PERMITTED AN ENTRY **':<30}{reason[:30]}")

    print()
    print("THE SPLIT IS COUNTABLE, NOT JUST READABLE")
    print("-" * 92)
    _, _, b_regime = regime_allows_side(ctx(regime="sideways"), "LONG")
    _, _, b_sent = regime_allows_side(ctx(adv=700, dec=1300), "LONG")
    distinct = b_regime == "REGIME" and b_sent == "SENTIMENT" and b_regime != b_sent
    ok &= distinct
    print(f"  regime block -> SKIPPED_{b_regime}")
    print(f"  sentiment block -> SKIPPED_{b_sent}")
    print(f"  {'distinguishable in atlas_entry_log.status:':<44}"
          f"{'ok' if distinct else '** COLLAPSED **'}")

    # build_market owns `bull`; the gate trusts it. Pin them together so a
    # drift in _classify cannot silently change what the gate means.
    print()
    print("build_market's `bull` STILL MEANS close>200DMA AND 50DMA>200DMA")
    print("-" * 92)
    try:
        import importlib.util
        import pandas as pd
        spec = importlib.util.spec_from_file_location(
            "bm", ROOT / "engine/build_market.py")
        bm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bm)

        # 260 sessions: a long climb, so the last row is unambiguously bull.
        n = 260
        df = pd.DataFrame({
            "date": pd.bdate_range("2025-01-01", periods=n),
            "nifty_close": [100.0 + i for i in range(n)],
            "vix_close": [12.0] * n,
        })
        out = bm._classify(df.copy())
        last = out.iloc[-1]
        agrees = (
            last["market_regime"] == "bull"
            and last["nifty_close"] > last["nifty_200dma"]
            and last["nifty_50dma"] > last["nifty_200dma"]
            and "nifty_20dma" in out.columns
            and pd.notna(last["nifty_20dma"])
        )
        ok &= agrees
        print(f"  rising series -> regime={last['market_regime']}, "
              f"close {last['nifty_close']:.0f} > 200DMA {last['nifty_200dma']:.0f}, "
              f"50DMA {last['nifty_50dma']:.0f} > 200DMA")
        print(f"  nifty_20dma written: {pd.notna(last['nifty_20dma'])} "
              f"({last['nifty_20dma']:.1f})")
        print(f"  {'definitions agree:':<44}{'ok' if agrees else '** DRIFTED **'}")
    except Exception as e:
        ok = False
        print(f"  ** could not verify against build_market: {type(e).__name__}: {e}")

    print("-" * 92)
    print("ENTRY GATE:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
