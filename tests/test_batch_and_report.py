#!/usr/bin/env python3
"""
THE TWO NUMBERS THAT LIED FOR EIGHTEEN DAYS
===========================================
03b crashed on cleanup for eighteen sessions, the &&-joined chain never reached
06_push, and nothing reaching Supabase meant the loop ran a full session every
day against no batch. Two readouts should have caught it and neither could:

  1. THE DAILY REPORT PRINTED `batch —` AND MOVED ON. An em-dash in a field is
     not an alarm; it reads as "nothing to report", which is the opposite of what
     it meant. Every other number in that report was describing an engine with
     nothing to work with, and the report said so nowhere.

  2. THE SIGNAL ENGINE REPORT'S PERCENTAGES WERE STRUCTURALLY FIXED. They were
     computed from `scored`, which by then holds ONLY qualifying rows, so it read
     "Qualifying: N (100.0%)", "Disqualified: 0" and an empty disqualification
     breakdown on every run the engine has ever done. A number that can only ever
     say one thing is worse than no number, because it reads as a healthy result.

Offline: no network, no Supabase, no box.

    python3 tests/test_batch_and_report.py
"""

import sys
import logging
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
logging.basicConfig(level=logging.CRITICAL)

import pandas as pd                                   # noqa: E402
from atlas.reporting import engine_report as R        # noqa: E402
from trading_calendar import prev_trading_day         # noqa: E402


def main() -> int:
    ok = True

    def check(label, got, want, extra=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<50}{str(got):<8}"
              f"{'ok' if good else f'** want {want} **'}  {extra}")

    DAY = date(2026, 9, 28)          # a Monday
    prev = prev_trading_day(DAY)     # the batch the loop should be holding

    print("=" * 78)
    print(f"A MISSING OR STALE BATCH MUST RAISE  (day {DAY}, last session {prev})")
    print("-" * 78)
    # Every shape the field has actually taken, including the em-dash the report
    # itself substitutes for an absent value.
    for label, bd in (("batch_date absent", None),
                      ("batch_date empty", ""),
                      ("the em-dash the report prints", "—"),
                      ("the string 'none'", "none"),
                      ("unparseable date", "2026-13-99")):
        got, lines = R.batch_health(DAY, {"batch_date": bd})
        check(label + " -> raises", got, False)
        ok &= bool(lines) and lines[0].startswith("🔴")
    # the frozen batch from the real incident
    got, lines = R.batch_health(DAY, {"batch_date": "2026-09-10"})
    check("the real incident: batch frozen at 2026-09-10", got, False)
    good = any("18 day" in x for x in lines)
    ok &= good
    print(f"  {'and it states the age in days':<50}"
          f"{'ok' if good else '** the age is not stated **'}")
    # ONE missed night must be caught that night. This is deliberately stricter
    # than the loop's MAX_BATCH_AGE_DAYS=5, which decides whether to TRADE a
    # batch. Reporting late is the failure mode being fixed here.
    check("one session older than the last -> raises",
          R.batch_health(DAY, {"batch_date": prev_trading_day(prev).isoformat()})[0],
          False, "stricter than the 5-day trade guard, on purpose")

    print()
    print("A CURRENT BATCH MUST NOT RAISE")
    print("-" * 78)
    check("the last trading session (the normal case)",
          R.batch_health(DAY, {"batch_date": prev.isoformat()})[0], True)
    check("same-day batch", R.batch_health(DAY, {"batch_date": DAY.isoformat()})[0],
          True)
    # a full timestamp, which is how Supabase hands dates back
    check("an ISO timestamp, not just a date",
          R.batch_health(DAY, {"batch_date": prev.isoformat() + "T18:42:00"})[0],
          True)

    print()
    print("THE RAISE REACHES THE TOP OF THE REPORT, NOT A FIELD IN THE MIDDLE")
    print("-" * 78)
    sess = {"mode": "SHADOW", "batch_date": None, "cycle_n": 360, "zones": 0}
    body = R.compose(DAY, sess, True, [{"symbol": "X", "status": "SKIPPED"}])
    lines = [x for x in body.split("\n") if x.strip()]
    pos = next((i for i, x in enumerate(lines) if "NO SIGNAL BATCH" in x), None)
    good = pos is not None and pos <= 3
    ok &= good
    print(f"  {'the alarm is within the first 3 lines':<50}"
          f"{str(pos):<8}{'ok' if good else '** buried or absent **'}")
    good = "batch      —" not in body or pos is not None
    ok &= good
    print(f"  {'an em-dash alone is never the whole story':<50}"
          f"{'ok' if good else '** SILENT **'}")

    print()
    print("THE ENGINE REPORT COUNTS EVERY SCORED ROW, NOT THE SURVIVORS")
    print("-" * 78)
    # tally() runs on the FULL batch, before it is reduced to qualifying rows.
    # Feed it a frame where most rows are disqualified and one qualifies: if the
    # counts came from the survivors, this would read 1 of 1 = 100%.
    import importlib.util as il
    sp = il.spec_from_file_location("s3b", ROOT / "engine/03b_score.py")
    s3b = il.module_from_spec(sp)
    sp.loader.exec_module(s3b)

    df = pd.DataFrame({
        "is_warmup":         [False] * 10,
        "qualifies":         [True] + [False] * 9,
        "disqualified":      [False] + [True] * 9,
        "disqualify_reason": [None] + ["no active zone"] * 5 + ["very_low_volume"] * 4,
    })
    stats = {"live": 0, "qual": 0, "dq": 0}
    from collections import Counter
    reasons = Counter()
    s3b.tally(df, stats, reasons)
    check("10 scored rows are all counted", stats["live"], 10)
    check("  1 qualifying", stats["qual"], 1)
    check("  9 disqualified", stats["dq"], 9)
    check("  the breakdown survives batching", reasons["no active zone"], 5)
    check("  and the second reason too", reasons["very_low_volume"], 4)
    pct = stats["qual"] / max(stats["live"], 1) * 100
    good = abs(pct - 10.0) < 0.01
    ok &= good
    print(f"  {'qualifying rate is 10%, not the 100% a':<50}"
          f"{pct:<8.1f}{'ok' if good else '** WRONG **'}")
    print(f"  {'  survivors-only frame would report':<50}")

    # warmup rows are excluded from the denominator, as before
    dfw = pd.DataFrame({
        "is_warmup":         [True] * 5 + [False] * 5,
        "qualifies":         [False] * 10,
        "disqualified":      [True] * 10,
        "disqualify_reason": ["warmup"] * 5 + ["no active zone"] * 5,
    })
    stats2 = {"live": 0, "qual": 0, "dq": 0}
    r2 = Counter()
    s3b.tally(dfw, stats2, r2)
    check("warmup rows stay out of the denominator", stats2["live"], 5)
    check("  and out of the breakdown", r2["warmup"], 0)

    # the accounting identity the report now asserts
    check("qual + dq accounts for every live row",
          stats["qual"] + stats["dq"], stats["live"])

    print()
    print("A DIRECTION THAT PUBLISHES NOTHING EXPLAINS ITSELF")
    print("-" * 78)
    # Shorts stopped reaching the signals table on 13 Aug 2026 when
    # MAX_ENTRY_DIST_PCT went 8.0 -> 0.30. Six weeks of long-only output looked
    # exactly like a market with no short setups, and establishing that it was
    # not took a manual trace -- every rejection was already computed per row,
    # nothing aggregated it by side.
    from engine.zone_entry import is_zone_reject
    check("a zone reject is attributed to the zone gate",
          is_zone_reject("stop 8.08% too wide (max 7.0%)"), True)
    check("  and an unreachable entry too",
          is_zone_reject("entry 5.01% away (max 0.3%) -- unreachable"), True)
    check("a disqualifier-block reason is not",
          is_zone_reject("no_recent_bos_choch"), False)
    check("  nor a screen reject", is_zone_reject("very_low_volume"), False)

    # THE TWO ZEROES MEAN OPPOSITE THINGS and must not read alike: rejected AT
    # the gate is a valid setup that was unreachable; rejected BEFORE it is no
    # setup at all.
    at_gate = s3b.explain_empty_side(
        "short", Counter({"live": 185185, "qual": 0, "zone_rejected": 25}),
        Counter({"stop N% too wide (max N%)": 19306}))
    ok_ = bool(at_gate) and "reached the zone gate" in at_gate[1]
    ok &= ok_
    print(f"  {'all short setups failed AT the gate':<50}"
          f"{'ok' if ok_ else '** WRONG **'}")
    before = s3b.explain_empty_side(
        "short", Counter({"live": 1000, "qual": 0, "zone_rejected": 0}),
        Counter({"no_recent_bos_choch": 900}))
    ok_ = bool(before) and "rejected earlier" in before[1]
    ok &= ok_
    print(f"  {'none reached the gate at all':<50}"
          f"{'ok' if ok_ else '** WRONG **'}")
    ok_ = bool(at_gate) and "Not an absence of setups" in at_gate[0]
    ok &= ok_
    print(f"  {'the headline denies it was a quiet market':<50}"
          f"{'ok' if ok_ else '** SILENT **'}")
    ok_ = any("19,306" in l for l in at_gate)
    ok &= ok_
    print(f"  {'and names the dominant reason with its count':<50}"
          f"{'ok' if ok_ else '** UNNAMED **'}")
    # a side that DID publish says nothing -- no noise on a healthy run
    ok_ = s3b.explain_empty_side("long", Counter({"live": 100, "qual": 5}),
                                 Counter()) == []
    ok &= ok_
    print(f"  {'a side that published stays silent':<50}"
          f"{'ok' if ok_ else '** NOISY **'}")

    print()
    print("THE ZONE FAMILY FOLLOWS THE TRADE DIRECTION, NOT THE ROW'S TREND")
    print("-" * 78)
    # active_zones resolves active_zone_high/low from structure_trend: demand ->
    # bull_fvg on anything that is not a downtrend. zone_entry used to read that,
    # so on an UPTREND row the SHORT pass received a DEMAND zone -- below price,
    # when a short's zone must be above it. 57.7% of short candidates and 21.8%
    # of long candidates were handed the wrong family, and a wrong-family row
    # passed validation 0.00% / 0.02% of the time: a guaranteed rejection that
    # reported itself as "stop too wide".
    from engine.zone_entry import compute_zone_entries
    # One uptrend row carrying BOTH families. A demand zone below price and a
    # supply zone above it, so whichever is picked is unambiguous.
    row = {
        "close": 100.0, "structure_trend": "uptrend",
        "active_demand_ob_high": 96.0, "active_demand_ob_low": 94.0,
        "active_bull_fvg_high": 95.0,  "active_bull_fvg_low": 93.0,
        "active_supply_ob_high": 107.0, "active_supply_ob_low": 104.0,
        "active_bear_fvg_high": 108.0,  "active_bear_fvg_low": 105.0,
        "last_swing_low": 92.0, "last_swing_high": 109.0,
        # what the OLD code read: the trend-resolved pair, i.e. the demand zone
        "active_zone_high": 96.0, "active_zone_low": 94.0,
        "active_zone_source": "demand_ob",
    }
    for direction, want_src, want_side in (("long", "demand_ob", "below"),
                                           ("short", "supply_ob", "above")):
        d = pd.DataFrame([dict(row, direction=direction)])
        out = compute_zone_entries(d)
        src = out["entry_zone_source"].iloc[0]
        e = float(out["entry_ref"].iloc[0])
        sl = float(out["sl"].iloc[0])
        side = "above" if e > row["close"] else "below"
        good = src == want_src and side == want_side
        ok &= good
        print(f"  {direction + ' uses the ' + want_src + ' family':<50}"
              f"{src:<12}{'ok' if good else '** ' + src + ' **'}")
        print(f"      entry {e:.2f} ({side} price {row['close']:.0f}), stop {sl:.2f}")
        # and the stop must sit on the protective side of the entry
        prot = (sl < e) if direction == "long" else (sl > e)
        ok &= prot
        print(f"      {'stop on the protective side':<44}"
              f"{'ok' if prot else '** WRONG SIDE **'}")

    # A wrong-side zone is now NAMED rather than surfacing as a stop-width number.
    ws = pd.DataFrame([dict(row, direction="short",
                            active_supply_ob_high=float("nan"),
                            active_supply_ob_low=float("nan"),
                            active_bear_fvg_high=96.0, active_bear_fvg_low=94.0)])
    out = compute_zone_entries(ws)
    r = str(out["reject_reason"].iloc[0])
    good = "wrong side" in r
    ok &= good
    print(f"  {'a supply zone below price is named, not mis-blamed':<50}"
          f"{'ok' if good else '** ' + r[:24] + ' **'}")

    # Fail CLOSED when the families are absent: falling back to the trend-resolved
    # column would silently reproduce the bug on an older parquet.
    bare = pd.DataFrame([{"close": 100.0, "direction": "short",
                          "active_zone_high": 96.0, "active_zone_low": 94.0,
                          "last_swing_high": 109.0, "last_swing_low": 92.0}])
    out = compute_zone_entries(bare)
    r = str(out["reject_reason"].iloc[0])
    good = (not bool(out["entry_valid"].iloc[0])) and "re-run 02b" in r
    ok &= good
    print(f"  {'missing families reject, never fall back':<50}"
          f"{'ok' if good else '** ' + r[:24] + ' **'}")

    print()
    print("THE PUBLISHER DOES NOT DECIDE WHAT IS TRADEABLE")
    print("-" * 78)
    # 06_push used to drop longs in a bearish regime and shorts in a bullish one.
    # On 2026-09-29 it logged "6 long / 0 short" and then "No qualifying signals":
    # six findings withheld from the product because the AGENT would not have
    # traded them. Asserted at source because push_signals needs a service key and
    # a network, and the property worth protecting is that the suppression does not
    # come back -- it was removed once before and reappeared.
    push = (ROOT / "engine/06_push_supabase.py").read_text(encoding="utf-8")
    code = "\n".join(l for l in push.splitlines()
                      if not l.lstrip().startswith("#"))
    for pat in ('day["direction"] != "long"', "day['direction'] != 'long'",
                'day["direction"] != "short"', "day['direction'] != 'short'"):
        gone = pat not in code
        ok &= gone
        print(f"  {'no suppression: ' + pat:<52}"
              f"{'ok' if gone else '** REINSTATED **'}")
    kept = "_n_long" in code and "_n_short" in code
    ok &= kept
    print(f"  {'the by-direction composition is still logged':<52}"
          f"{'ok' if kept else '** LOST **'}")

    # And the gate that DOES decide is unchanged and still refuses both sides
    # outside a bull regime. This is the reason removing the filter is safe.
    from atlas.execution.atlas_entry import regime_allows_side
    bear = {"regime": "bear", "extreme_bearish": False, "source": "market.parquet",
            "advance_count": 1500, "decline_count": 500,
            "nifty_close": 21000, "nifty_20dma": 20500}
    for side in ("LONG", "SHORT"):
        allowed, why, _ = regime_allows_side(bear, side)
        ok &= not allowed
        print(f"  {'entry gate still refuses a ' + side + ' in a bear regime':<52}"
              f"{'ok' if not allowed else '** ALLOWED **'}")

    print("-" * 78)
    print("BATCH & REPORT:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
