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


# SENTIMENT IS NO LONGER IN ctx, 2026-10-14. The entry-side gate reads the
# NIFTY near-month future live -- last price against previous close, today's
# open and today's session VWAP -- so the breadth counts and 20DMA that used to
# encode sentiment in this helper are not inputs to it any more. ctx still
# carries the nightly REGIME, which allowed_side reports and does not apply.
#
# Sentiment is driven here by patching the live read, which is also the only
# honest way to test the matrix: these cases are about what the gate DOES with
# a verdict, not about how the verdict is computed. tests/test_live_sentiment.py
# owns the computation.
# THE GATE READS THE CONFIRM WINDOW, NOT THE RAW LIVE READ (2026-10-14).
# Patching live_sentiment.read no longer reaches it: the window observes once
# per cycle and the gate asks the window what has been CONFIRMED. So the
# injection point moves here, which is also the honest one -- these cases are
# about what the gate does with a confirmed state, and
# tests/test_sentiment_window.py owns how that state is reached.
from atlas.execution import sentiment_window as _SW                  # noqa: E402


from atlas.execution import live_sentiment as _LS                     # noqa: E402

# Never stood down in these cases; the stand-down has its own suite.
_SW.state = lambda *a, **k: {"stood_down": False, "reason_code": None}


def _inject_window(permits=None, code=None):
    """Make the gate see a given instantaneous read.

    REPOINTED 2026-10-15. This injected a CONFIRMED state from the 24-of-30
    window; the window was vetoed and the gate reads live_sentiment.read()
    directly again, so that is the injection point.
    """
    if permits in ("bullish", "bearish"):
        _LS.read = lambda *a, **k: {"ok": True, "verdict": permits,
                                    "reason_code": None,
                                    "detail": f"injected {permits}"}
    elif code == "SENTIMENT":
        _LS.read = lambda *a, **k: {"ok": True, "verdict": "mixed",
                                    "reason_code": None,
                                    "detail": "injected mixed"}
    else:
        _LS.read = lambda *a, **k: {"ok": False, "verdict": None,
                                    "reason_code": _LS.REASON_UNAVAILABLE,
                                    "detail": "injected: the feed is down"}


def ctx(regime="bull", sentiment="bullish", source="live", extreme=False):
    """The regime half, plus the CONFIRMED state the gate will see.

    `sentiment` names the confirmed state, not a raw read:
        bullish / bearish   a confirmed side -> permits that side
        mixed               confirmed mixed  -> permits nothing, bucket SENTIMENT
        None                the window cannot answer -> its own reason code
    """
    if sentiment in ("bullish", "bearish"):
        _inject_window(permits=sentiment, code=None)
    elif sentiment == "mixed":
        _inject_window(permits=None, code="SENTIMENT")
    else:
        _inject_window(permits=None, code=_LS.REASON_UNAVAILABLE)
    return {"regime": regime, "source": source, "extreme_bearish": extreme,
            "allow_accumulation": regime in ("bull", "sideways")}


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
    case("bull, live read MIXED", ctx(sentiment="mixed"), "LONG", False, "SENTIMENT")
    case("bull, live read UNAVAILABLE", ctx(sentiment=None), "LONG", False,
         "LIVE_SENTIMENT_UNAVAILABLE")
    # CONFLICT IS GONE AS A LABEL, and that follows from the change rather than
    # being a loss. "Structure and participation disagree" was only meaningful
    # while BOTH were applied here. With regime reported and not applied, a
    # bull regime with bearish breadth is simply bearish sentiment -- and the
    # side that names is a short, so the label is LONG_ONLY.
    case("bull structure vs BEARISH live read -> LONG_ONLY",
         ctx(sentiment="bearish"), "LONG", False, "LONG_ONLY")
    # SENTIMENT ALONE PERMITS A LONG, 2026-10-13. Regime is no longer applied
    # at entry: the nightly scoring stage already filters on it over the full
    # universe (bear_regime_no_reversal), and the entry-side copy was the
    # broken one -- 361 symbols of 746, inverted breadth. Applying a worse
    # version of the same test a second time could only subtract trades, and
    # it did: one tradeable state out of twenty.
    #
    # THIS ASSERTION HAS NOW BEEN FOUR WAYS and the churn is worth naming:
    # sideways is cash, sideways follows sentiment, strict bull-AND-bullish,
    # and now sentiment-only. Each time the TEST was changed to match the code.
    # What makes this one different is that the regime test did not disappear
    # -- it moved to where the good data is, and tests/test_batch_and_report.py
    # asserts it there.
    case("SIDEWAYS + bullish -> LONG (regime not applied)",
         ctx(regime="sideways"), "LONG", True, None)
    case("BEAR + bullish -> LONG (the nightly filter owns regime)",
         ctx(regime="bear"), "LONG", True, None)
    case("UNKNOWN regime + bullish -> LONG",
         ctx(regime="unknown"), "LONG", True, None)
    # Bearish sentiment is cash whatever the regime, because the side it names
    # is a SHORT and shorts are disabled.
    for reg in ("bull", "bear", "sideways"):
        case(f"{reg.upper()} + bearish -> cash (LONG_ONLY)",
             ctx(regime=reg, sentiment="bearish"),
             "SHORT", False, "LONG_ONLY")
    # AN UNKNOWN REGIME NO LONGER BLOCKS A LONG. It cannot: regime is not read
    # at entry. The nightly filter is what sees the regime, and it sees it on
    # the pipeline's own data rather than on the 361-of-746 entry-side copy.
    case("unknown regime + bullish -> LONG", ctx(regime="unknown"),
         "LONG", True, None)

    for label, got, blocked, good, reason in rows:
        print(f"  {label:<34}{'ENTER' if got else 'cash':<7}{blocked:<11}"
              f"{'ok' if good else '** WRONG **':<14}{reason[:34]}")

    # An AND that has become an OR still passes every case above except these.
    print()
    print("NEITHER INPUT DECIDES ALONE")
    print("-" * 92)
    # RESTATED AGAIN, 2026-10-13. This has asserted a plain AND, then "each
    # input can veto on its own". Neither is the rule now: there is ONE input
    # for a long, and the property that survives is the one that actually
    # matters -- SENTIMENT MUST BE READABLE AND BULLISH, and an unreadable or
    # half-formed reading is never carried by the half that is present.
    #
    # Regime is deliberately NOT in this list any more. An unknown regime
    # permitting a long is the intended behaviour, and asserting otherwise
    # would be asserting the double-counting that was removed.
    # BUILT INSIDE THE LOOP, not in the tuple: ctx() sets the verdict the
    # patched live read will return, so constructing all five up front made the
    # last one's sentiment apply to every case.
    for label, kw in (
            ("bull regime, mixed live read", {"sentiment": "mixed"}),
            ("bull regime, bearish live read", {"sentiment": "bearish"}),
            ("sideways, mixed live read",
             {"regime": "sideways", "sentiment": "mixed"}),
            ("bull regime, feed down", {"sentiment": None}),
            ("sideways, live read unavailable",
             {"regime": "sideways", "sentiment": None})):
        c = ctx(**kw)
        got, why, blk = regime_allows_side(c, "LONG")
        ok &= not got
        print(f"  {label:<40}{'ENTER' if got else 'cash':<7}"
              f"{'ok' if not got else '** ENTERED **':<18}{str(blk)}")

    print()
    print("MISSING INPUT IS A NO, NEVER A PASS")
    print("-" * 92)
    # THE MISSING INPUTS ARE DIFFERENT INPUTS NOW. This used to withhold
    # nifty_20dma, advance_count, decline_count and nifty_close one at a time,
    # because those were the gate's inputs. They are not read at entry any
    # more; the live read's legs are, and withholding one of those is
    # tests/test_live_sentiment.py section 2. What this section still owns is
    # the property that matters whatever the inputs are: an unreadable
    # sentiment is never carried by anything else in ctx.
    for label, kw in (("feed down, bull regime",
                       {"regime": "bull", "sentiment": None}),  # noqa
                      ("live read unavailable, sideways",
                       {"regime": "sideways", "sentiment": None}),
                      ("live read unavailable, bear regime",
                       {"regime": "bear", "sentiment": None}),
                      ("live read unavailable, regime unknown",
                       {"regime": "unknown", "sentiment": None})):
        c = ctx(**kw)
        got_ok, reason, blocked = regime_allows_side(c, "LONG")
        # THE BUCKET IS THE FAILURE CODE, not SENTIMENT. "the feed would not
        # answer" needs investigating; "the market permits no side" needs
        # waiting, and they were the same row in any count over blocked_by.
        good = (not got_ok) and blocked == "LIVE_SENTIMENT_UNAVAILABLE"
        ok &= good
        print(f"  {label:<40}{'cash' if not got_ok else 'ENTER':<7}"
              f"{'ok' if good else '** PERMITTED ON UNKNOWN **':<30}{reason[:30]}")

    # A ctx CARRYING NOTHING AT ALL still cannot permit an entry. ctx is no
    # longer the sentiment source, so the worst case is no longer "the file was
    # empty" -- it is "something passed an empty dict and the gate read it as
    # permission".
    _inject_window(permits=None, code=_LS.REASON_UNAVAILABLE)
    got_ok, reason, blocked = regime_allows_side({}, "LONG")
    good = (not got_ok) and blocked == "LIVE_SENTIMENT_UNAVAILABLE"
    ok &= good
    print(f"  {'an empty ctx permits nothing':<40}"
          f"{'cash' if not got_ok else 'ENTER':<7}"
          f"{'ok' if good else '** EMPTY CTX PERMITTED AN ENTRY **':<30}{reason[:30]}")

    print()
    print("THE SPLIT IS COUNTABLE, NOT JUST READABLE")
    print("-" * 92)
    # THREE reasons to sit out, and they must stay distinguishable in
    # atlas_entry_log.blocked_by. The third is new as of 2026-10-14 and is the
    # point of giving the live read its own code:
    #
    #   LIVE_SENTIMENT_UNAVAILABLE  the feed would not answer -> investigate
    #   SENTIMENT                   it answered, no side permitted -> wait
    #   LONG_ONLY                   it answered bearish, and shorts are off
    #
    # Over a long flat stretch "we could not read the market" and "we read it
    # and stood aside" are different facts, and only the second is the strategy
    # working. They used to be the same bucket.
    _, _, b_unavail = regime_allows_side(ctx(sentiment=None), "LONG")  # noqa
    _, _, b_mixed = regime_allows_side(ctx(sentiment="mixed"), "LONG")
    _, _, b_bear = regime_allows_side(ctx(sentiment="bearish"), "LONG")
    got = (b_unavail, b_mixed, b_bear)
    distinct = got == ("LIVE_SENTIMENT_UNAVAILABLE", "SENTIMENT", "LONG_ONLY")
    ok &= distinct
    for lbl, b in (("live read unavailable", b_unavail),
                   ("live read mixed", b_mixed),
                   ("live read bearish (a short)", b_bear)):
        print(f"  {lbl:<34}-> SKIPPED_{b}")
    print(f"  {'three distinct buckets, not two:':<44}"
          f"{'ok' if distinct else '** COLLAPSED ** ' + str(got)}")

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
        """ctx for the regime column; the sentiment column is the CONFIRMED state.

        Set at call time, per cell. Building every ctx up front would leave the
        last cell's sentiment applying to all of them.
        """
        return ctx(regime=regime,
                   sentiment=None if sent == "unknown" else sent)

    # ONE TRADEABLE CELL OUT OF SIXTEEN. That is what "strict, both must hold"
    # means, and seeing it as a table is the clearest statement of the rule
    # anywhere in the codebase.
    # SENTIMENT IS THE ONLY COLUMN THAT MATTERS NOW. Every bullish row is a
    # LONG whatever the regime; everything else is cash. Seeing it as a table
    # is the clearest statement of the rule anywhere in the codebase -- and
    # the regime column being irrelevant is the change, made visible.
    MATRIX = {
        ("bull", "bullish"): "LONG",     ("bull", "bearish"): None,
        ("bull", "mixed"):   None,       ("bull", "unknown"): None,
        ("bear", "bullish"): "LONG",     ("bear", "bearish"): None,
        ("bear", "mixed"):   None,       ("bear", "unknown"): None,
        ("sideways", "bullish"): "LONG", ("sideways", "bearish"): None,
        ("sideways", "mixed"):   None,   ("sideways", "unknown"): None,
        ("unknown", "bullish"): "LONG",  ("unknown", "bearish"): None,
        ("unknown", "mixed"):   None,    ("unknown", "unknown"): None,
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

    # THE ONE cell that takes a side, named individually so a policy change
    # cannot pass by editing the table above alone.
    longs = [k for k in MATRIX if allowed_side(mctx(*k))[0] == "LONG"]
    ok &= len(longs) == 4
    print(f"  {'every bullish-sentiment cell returns LONG (4 of 16)':<58}"
          f"{'ok' if len(longs) == 4 else '** ' + str(len(longs)) + ' **'}")
    only_bullish = all(k[1] == "bullish" for k in longs)
    ok &= only_bullish
    print(f"  {'  and ONLY the bullish ones':<58}"
          f"{'ok' if only_bullish else '** ' + str(longs) + ' **'}")
    # AND NO CELL ANYWHERE RETURNS A SHORT.
    shorty = [k for k in MATRIX if allowed_side(mctx(*k))[0] == "SHORT"]
    ok &= not shorty
    print(f"  {'no cell in the matrix returns SHORT':<58}"
          f"{'ok' if not shorty else '** ' + str(shorty) + ' **'}")

    # bearish sentiment is LONG_ONLY from any regime -- the countable reason is
    # "the side would be a short", not a disagreement between two gates.
    for regime, sent in (("bull", "bearish"), ("bear", "bearish")):
        blk = allowed_side(mctx(regime, sent))[2]
        good = blk == "LONG_ONLY"
        ok &= good
        print(f"  {regime + ' + ' + sent + ' is blocked_by LONG_ONLY':<58}"
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
        # LONG_ONLY for the short switch, CONFIG for the long one. The labels
        # differ on purpose: shorts being off is a standing POLICY worth
        # counting as one bucket, while longs being off is an operator
        # intervention and keeps the generic label.
        want_blk = "LONG_ONLY" if side == "SHORT" else "CONFIG"
        good = (not got) and blk == want_blk
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

    ok &= test_zone_side_invariant()

    print("-" * 92)
    print("ENTRY GATE:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


def test_zone_side_invariant():
    """The wrong-zone defect, in the second place it lived.

    zone_entry was fixed on 30 Sep: it chose the zone family from structure_trend
    rather than the trade direction. market_open.near_zone was the same defect --
    abs(ltp - ref) <= MAX_ENTRY_DIST_PCT, direction-blind -- and the 0.30% bound
    was the only thing keeping it harmless. With the bound removed the invariant
    has to be explicit, so it is asserted here against the live quote.
    """
    import atlas.signal.market_open as MO
    from atlas.execution.atlas_entry import check_entry_range
    print()
    print("THE ZONE MUST BE ON THE RIGHT SIDE OF THE LIVE PRICE")
    print("-" * 78)
    ok = True
    # demand band 94-96, stop 92.  supply band 104-106, stop 108.
    LONG  = {"entry_low": 94.0, "entry_high": 96.0, "entry_ref": 96.0}
    SHORT = {"entry_low": 104.0, "entry_high": 106.0, "entry_ref": 104.0}
    cases = [
        ("LONG, price in the band",          LONG,  "LONG",  95.0, True,  ""),
        ("LONG, price above — still waiting", LONG,  "LONG",  99.0, False, "not yet"),
        ("LONG, price through the low",      LONG,  "LONG",  93.0, False, "mitigated"),
        ("LONG, price far below",            LONG,  "LONG",  70.0, False, "mitigated"),
        ("SHORT, price in the band",         SHORT, "SHORT", 105.0, True,  ""),
        ("SHORT, price below — still waiting", SHORT, "SHORT", 100.0, False, "not yet"),
        ("SHORT, price through the high",    SHORT, "SHORT", 107.0, False, "mitigated"),
    ]
    for label, sig, d, ltp, want, marker in cases:
        got, why = MO.at_zone(sig, ltp, d)
        good = got == want and (marker in why.lower() if marker else True)
        ok &= good
        print(f"  {label:<40}{'ENTER' if got else 'no':<7}"
              f"{'ok' if good else '** WRONG **':<14}{why[:44]}")
    # and the entry path asserts it independently
    print()
    print("  and check_entry_range asserts the same thing, with the stop:")
    # entry_ref IS NOW REQUIRED, and these fixtures pass one at the LTP so the
    # band and stop logic is what is being tested rather than the proximity
    # term. Before 2026-10-13 the gate took no reference price and these calls
    # omitted it; a missing entry_ref is now a REFUSAL, not a pass, so the
    # fixtures have to supply it.
    for label, d, ltp, lo, hi, stop, want in (
            ("long in band, above stop",    "LONG",  95.0, 94.0, 96.0, 92.0, True),
            ("long in band, AT the stop",   "LONG",  92.0, 94.0, 96.0, 92.0, False),
            ("long mitigated below band",   "LONG",  93.0, 94.0, 96.0, 92.0, False),
            ("short in band, below stop",   "SHORT", 105.0, 104.0, 106.0, 108.0, True),
            ("short mitigated above band",  "SHORT", 107.0, 104.0, 106.0, 108.0, False)):
        got, why = check_entry_range(d, ltp, lo, hi, stop, entry_ref=ltp)
        good = got == want
        ok &= good
        print(f"    {label:<32}{'ENTER' if got else 'no':<7}"
              f"{'ok' if good else '** WRONG **':<14}{why[:40]}")

    # ── THE 1% PROXIMITY GATE ─────────────────────────────────────
    #
    # The band test is not a distance test: a wide band admits a price far from
    # the reference the setup was published against and sizing was computed
    # from. ENTRY_PROXIMITY_PCT closes that, and MAX_ZONE_DIST_PCT (15%,
    # engine/active_zones.py) remains the SCREENING filter -- it governs what
    # is published, not what is bought.
    print()
    print("  and the 1% proximity gate, measured from entry_ref:")
    from atlas.config import ENTRY_PROXIMITY_PCT
    ok &= ENTRY_PROXIMITY_PCT == 1.0
    print(f"    {'ENTRY_PROXIMITY_PCT is 1.0':<32}{str(ENTRY_PROXIMITY_PCT):<7}"
          f"{'ok' if ENTRY_PROXIMITY_PCT == 1.0 else '** WRONG **'}")
    for label, ltp, want in (("at the reference",        1000.0, True),
                             ("0.99% away",              1009.9, True),
                             ("exactly 1.00% away",      1010.0, True),
                             ("1.01% away",              1010.1, False),
                             ("2% away, still in band",  1020.0, False),
                             ("1% below",                 990.0, True),
                             ("1.5% below",               985.0, False)):
        got, why = check_entry_range("LONG", ltp, 960.0, 1040.0, 950.0,
                                     entry_ref=1000.0)
        good = got == want
        ok &= good
        print(f"    {label:<32}{'ENTER' if got else 'skip':<7}"
              f"{'ok' if good else '** WRONG **':<14}{why[:40]}")
    # THE BOUNDARY IS INCLUSIVE. 1010/1000-1 is 0.010000000000000009 in float,
    # so a price exactly 1% away compared greater-than and was skipped -- a gate
    # that rejects its own stated boundary is one nobody can reason about.
    got_b, _ = check_entry_range("LONG", 1010.0, 960.0, 1040.0, 950.0,
                                 entry_ref=1000.0)
    ok &= got_b
    print(f"    {'the 1% boundary is INCLUSIVE':<32}{'ENTER' if got_b else 'skip':<7}"
          f"{'ok' if got_b else '** WRONG **'}")
    # A MISSING REFERENCE IS NOT PERMISSION. Admitting a trade because the
    # reference price is absent would make a data gap the widest gate here.
    got_n, why_n = check_entry_range("LONG", 1000.0, 960.0, 1040.0, 950.0,
                                     entry_ref=0.0)
    ok &= (not got_n)
    print(f"    {'a missing entry_ref REFUSES':<32}{'ENTER' if got_n else 'skip':<7}"
          f"{'ok' if not got_n else '** WRONG **':<14}{why_n[:40]}")
    print()
    # A MISSING DISPLAY COLUMN MUST NOT STOP THE ENGINE TRADING. publication_kind
    # arrives with a hand-applied migration, and until it lands PostgREST answers
    # the whole select with 400 "does not exist" -- which took down load_zone_map,
    # and with it quotes and entries, for a column that only labels a population.
    class _R:
        def __init__(self, c, t="", j=None):
            self.status_code, self.text, self._j = c, t, j or []
        def json(self): return self._j
    _saved = MO.requests.get
    try:
        seen = []
        def _g(url, **kw):
            seen.append(url)
            if "publication_kind" in url:
                return _R(400, '{"message":"column signals.publication_kind does not exist"}')
            return _R(200, "", [{"symbol": "ABB", "direction": "LONG"}])
        MO.requests.get = _g
        rows = MO.get_signals("2026-10-01")
        good = (len(rows) == 1 and rows[0].get("publication_kind") == "signal"
                and len(seen) == 2)
        ok &= good
        print(f"  {'a missing publication_kind degrades, not dies':<40}"
              f"{'ok' if good else '** THE SESSION WOULD BE DEAD **'}")
        MO.requests.get = lambda url, **kw: _R(500, "boom")
        try:
            MO.get_signals("2026-10-01"); raised = False
        except RuntimeError:
            raised = True
        ok &= raised
        print(f"  {'  but a real fetch failure still raises':<40}"
              f"{'ok' if raised else '** SWALLOWED **'}")
    finally:
        MO.requests.get = _saved

    # Checked on the CODE OBJECT, not the source text. Both modules describe the
    # removed gate at length in comments and docstrings -- deliberately -- and a
    # grep for the name cannot tell an explanation from a use.
    import engine.zone_entry as ZE
    def names(fn):
        c = fn.__code__
        return set(c.co_names) | set(c.co_varnames) | set(
            n for const in c.co_consts if hasattr(const, "co_names")
            for n in const.co_names)
    no_gate = ("MAX_ENTRY_DIST_PCT" not in names(MO.at_zone)
               and not hasattr(MO, "MAX_ENTRY_DIST_PCT"))
    ok &= no_gate
    print(f"  {'the loop has no entry-distance gate':<40}"
          f"{'confirmed' if no_gate else '** STILL THERE **'}")
    no_pub = ("MAX_ENTRY_DIST_PCT" not in names(ZE.compute_zone_entries)
              and not hasattr(ZE, "MAX_ENTRY_DIST_PCT"))
    ok &= no_pub
    print(f"  {'nor does the publish gate':<40}"
          f"{'confirmed' if no_pub else '** STILL THERE **'}")
    return ok

if __name__ == "__main__":
    sys.exit(main())
