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
    # bull structure against bearish participation is a CONFLICT, not a sentiment
    # failure: attributing a disagreement to one half would be arbitrary, and the
    # split is what makes a long flat stretch legible.
    case("bull structure vs bearish participation",
         ctx(adv=700, dec=1300, close=24000, ma20=24500),
         "LONG", False, "CONFLICT")
    # SIDEWAYS NOW FOLLOWS SENTIMENT. This asserted cash, which was the rule
    # until the direction matrix landed: a sideways regime takes the side
    # sentiment names rather than standing aside.
    case("SIDEWAYS + bullish sentiment -> long", ctx(regime="sideways"),
         "LONG", True, None)
    case("SIDEWAYS + bearish sentiment -> short",
         ctx(regime="sideways", adv=700, dec=1300, close=24000),
         "SHORT", True, None)
    # bear structure with bullish participation is the mirror conflict.
    case("bear structure vs bullish participation", ctx(regime="bear"),
         "LONG", False, "CONFLICT")
    # and the side a bear regime DOES take, when participation agrees
    case("bear + bearish sentiment -> short",
         ctx(regime="bear", adv=700, dec=1300, close=24000), "SHORT", True, None)
    case("unknown regime", ctx(regime="unknown"), "LONG", False, "REGIME")

    for label, got, blocked, good, reason in rows:
        print(f"  {label:<34}{'ENTER' if got else 'cash':<7}{blocked:<11}"
              f"{'ok' if good else '** WRONG **':<14}{reason[:34]}")

    # An AND that has become an OR still passes every case above except these.
    print()
    print("NEITHER INPUT DECIDES ALONE")
    print("-" * 92)
    # This asserted a plain AND, with "regime fails" represented by SIDEWAYS.
    # Sideways is a valid cell now -- it follows sentiment -- so the property has
    # to be restated: each input can still veto on its own, and an UNREADABLE one
    # always does. A missing half is never carried by the half that is present.
    for label, c in (
            ("bull regime, mixed sentiment", ctx(adv=700, dec=1300)),
            ("bull regime, bearish sentiment", ctx(adv=700, dec=1300, close=24000)),
            ("bullish breadth, regime unknown", ctx(regime="unknown")),
            ("bull regime, sentiment unreadable",
             ctx(adv=None, dec=None, close=None, ma20=None)),
            ("sideways, sentiment unreadable",
             ctx(regime="sideways", adv=None, dec=None, close=None, ma20=None))):
        got, why, blk = regime_allows_side(c, "LONG")
        ok &= not got
        print(f"  {label:<40}{'ENTER' if got else 'cash':<7}"
              f"{'ok' if not got else '** ENTERED **':<18}{str(blk)}")

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
    # THREE reasons to sit out now, and they must stay distinguishable in
    # atlas_entry_log.status: no side from the structure, participation unclear or
    # unreadable, and the two pointing opposite ways. Over a long flat stretch the
    # split between the last two is the interesting number -- "the trend is there
    # and nobody is buying it" is not "there is no trend".
    _, _, b_regime = regime_allows_side(ctx(regime="unknown"), "LONG")
    _, _, b_sent   = regime_allows_side(
        ctx(adv=None, dec=None, close=None, ma20=None), "LONG")
    # close must drop below the 20DMA too, or this is MIXED rather than bearish --
    # breadth alone is not a bearish reading, which is the point of three values.
    _, _, b_conf   = regime_allows_side(ctx(adv=700, dec=1300, close=24000), "LONG")
    got = (b_regime, b_sent, b_conf)
    distinct = got == ("REGIME", "SENTIMENT", "CONFLICT") and len(set(got)) == 3
    ok &= distinct
    for lbl, b in (("regime gives no side", b_regime),
                   ("sentiment unreadable", b_sent),
                   ("the two disagree", b_conf)):
        print(f"  {lbl:<34}-> SKIPPED_{b}")
    print(f"  {'three distinct statuses:':<44}"
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

    print()
    print("THE DIRECTION MATRIX: ONE SIDE, OR CASH")
    print("-" * 92)
    # Shorts are REACHABLE now. The old section here asserted the opposite and was
    # right to at the time: the mandate was long-only, ALLOW_SHORT_ENTRIES was
    # False, and a sweep of all 32 combinations proved no short could pass. The
    # policy changed, so the assertion has to change with it -- what must not
    # change is that the sweep is exhaustive rather than a sample.
    from atlas.execution.atlas_entry import allowed_side

    def mctx(regime, sent):
        if sent == "bullish":   a_, d_, c_, m_ = 1200, 800, 25000, 24500
        elif sent == "bearish": a_, d_, c_, m_ = 800, 1200, 24000, 24500
        elif sent == "mixed":   a_, d_, c_, m_ = 1200, 800, 24000, 24500
        else:                   a_, d_, c_, m_ = None, None, None, None
        return {"regime": regime, "advance_count": a_, "decline_count": d_,
                "nifty_close": c_, "nifty_20dma": m_,
                "source": "market.parquet", "extreme_bearish": False}

    MATRIX = {
        ("bull", "bullish"): "LONG",   ("bull", "bearish"): None,
        ("bull", "mixed"):   None,     ("bull", "unknown"): None,
        ("bear", "bullish"): None,     ("bear", "bearish"): "SHORT",
        ("bear", "mixed"):   None,     ("bear", "unknown"): None,
        ("sideways", "bullish"): "LONG", ("sideways", "bearish"): "SHORT",
        ("sideways", "mixed"):   None,   ("sideways", "unknown"): None,
        ("unknown", "bullish"): None,  ("unknown", "bearish"): None,
        ("unknown", "mixed"):   None,  ("unknown", "unknown"): None,
    }
    bad = []
    for (regime, sent), want in MATRIX.items():
        c = mctx(regime, sent)
        got, why, blk = allowed_side(c)
        lo = regime_allows_side(c, "LONG")[0]
        sh = regime_allows_side(c, "SHORT")[0]
        if got != want or lo != (want == "LONG") or sh != (want == "SHORT"):
            bad.append((regime, sent, want, got, lo, sh))
        # NEVER BOTH SIDES on one evaluation. A matrix returning a single value
        # makes this structural, but it is the property that matters most: two
        # opposing positions opened on the same cycle is not a hedge, it is the
        # gate having no opinion.
        if lo and sh:
            bad.append((regime, sent, want, "BOTH", lo, sh))
    ok &= not bad
    print(f"  {'all 16 regime x sentiment cells match the matrix':<58}"
          f"{'ok' if not bad else '** ' + str(len(bad)) + ' WRONG **'}")
    for r_, s_, want, got, lo, sh in bad[:6]:
        print(f"      {r_}+{s_}: want {want}, got {got} (long={lo} short={sh})")

    # the four cells that DO take a side, named individually so a policy change
    # cannot pass by editing the table above alone
    for regime, sent, side in (("bull", "bullish", "LONG"),
                               ("bear", "bearish", "SHORT"),
                               ("sideways", "bullish", "LONG"),
                               ("sideways", "bearish", "SHORT")):
        got = allowed_side(mctx(regime, sent))[0]
        good = got == side
        ok &= good
        print(f"  {regime + ' + ' + sent + ' -> ' + side:<58}"
              f"{'ok' if good else '** ' + str(got) + ' **'}")

    # a disagreement is its own countable status, not attributed to either half
    for regime, sent in (("bull", "bearish"), ("bear", "bullish")):
        blk = allowed_side(mctx(regime, sent))[2]
        good = blk == "CONFLICT"
        ok &= good
        print(f"  {regime + ' vs ' + sent + ' is blocked_by CONFLICT':<58}"
              f"{'ok' if good else '** ' + str(blk) + ' **'}")

    # the master switches still override the matrix, checked before any market
    # reading, so a side can be taken off the table without reasoning about state
    import atlas.execution.atlas_entry as AE
    for flag, side in (("ALLOW_SHORT_ENTRIES", "SHORT"),
                       ("ALLOW_LONG_ENTRIES", "LONG")):
        regime, sent = ("bear", "bearish") if side == "SHORT" else ("bull", "bullish")
        saved = getattr(AE, flag)
        setattr(AE, flag, False)
        try:
            got, why, blk = regime_allows_side(mctx(regime, sent), side)
        finally:
            setattr(AE, flag, saved)
        good = (not got) and blk == "CONFIG"
        ok &= good
        print(f"  {flag + '=False blocks its side outright':<58}"
              f"{'ok' if good else '** ALLOWED **'}")

    print()
    print("AND NO REAL MARKET ROW CAN CLAIM bull AND extreme_bearish AT ONCE")
    print("-" * 92)
    # The one context that WOULD pass is regime="bull" with extreme_bearish=True.
    # Unreachability therefore rests on build_market never emitting that pair, so
    # the invariant is asserted at the classifier rather than trusted.
    #
    #   extreme_bearish requires `broken`  (close < 200DMA x (1 - NEUTRAL_BAND))
    #   `broken` sets market_regime = "bear" AFTER the bull assignment
    #   so extreme_bearish implies regime == "bear", never "bull"
    contradiction = regime_allows_side(
        ctx(regime="bull", extreme=True), "SHORT")[0]
    print(f"  {'(a bull + extreme_bearish context WOULD pass the gate)':<58}"
          f"{'yes' if contradiction else 'no'}  — so the classifier must never emit it")
    try:
        import numpy as np, pandas as pd
        import importlib.util as il
        _sp = il.spec_from_file_location("bm", ROOT / "engine/build_market.py")
        bm = il.module_from_spec(_sp)
        _sp.loader.exec_module(bm)
        # A 400-day series that sweeps from a strong uptrend into a deep bear, so
        # every regime and the hedge flag all actually occur in one frame.
        n = 400
        trend = np.concatenate([np.linspace(18000, 26000, 250),
                                np.linspace(26000, 17000, n - 250)])
        df = pd.DataFrame({
            "date": pd.date_range("2024-01-01", periods=n, freq="D"),
            "nifty_close": trend,
            "vix_close": np.concatenate([np.full(250, 12.0),
                                         np.full(n - 250, 26.0)]),
        })
        out = bm._classify(df)
        both = out[(out["market_regime"] == "bull") & out["extreme_bearish"]]
        seen = sorted(out["market_regime"].unique())
        eb = int(out["extreme_bearish"].sum())
        ok &= both.empty and eb > 0 and "bull" in seen and "bear" in seen
        print(f"  {'classifier emits bull + extreme_bearish together':<58}"
              f"{len(both)} row(s)  {'ok' if both.empty else '** INVARIANT BROKEN **'}")
        print(f"      swept {n} sessions: regimes {seen}, "
              f"extreme_bearish on {eb} of them")
        if eb == 0 or "bull" not in seen:
            print("      ** the sweep did not exercise both states — "
                  "the invariant above is untested **")
    except Exception as e:
        ok = False
        print(f"  ** could not exercise build_market._classify: {e}")

    print("-" * 92)
    print("ENTRY GATE:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
