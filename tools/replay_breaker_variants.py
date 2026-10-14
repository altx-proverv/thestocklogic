"""
FOUR COMBINATIONS, EVERY SESSION: WHICH BOX AND WHICH BREAK TEST?
=================================================================

  rolling box + wick        what 82e8470 deployed
  rolling box + close
  fixed opening range + wick
  fixed opening range + close    what is deployed now

THE TARGET IS EXPLICIT, set by the operator from what the data already says:
  SHOULD FIRE  : 2026-10-07, 2026-10-08, 2026-10-12  (bearish; long was wrong)
  SHOULD BE SILENT: 2026-10-06, 2026-10-09, 2026-10-13  (read bullish nearly
                   every cycle)

THE SHIPPED COMBINATION IS RUN THROUGH THE ENGINE'S OWN FUNCTIONS --
range_breaker.opening_range() and range_breaker.verdict() -- so the column that
matters cannot drift from what runs. The other three are implemented here, for
comparison only, and are deliberately NOT importable by the engine.

All passes are AS IF LONG ALL DAY: the stress case, and the one that answers
"would this have stopped a good day".
"""

import sys
import argparse
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from atlas.execution.range_breaker import opening_range, verdict
from atlas.execution.live_sentiment import near_month_future
from engine.upstox_ws import BASE_URL, get_headers

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
OR_START, OR_END = (9, 15), (10, 0)
ROLL = 45

SHOULD_FIRE = {"2026-10-07", "2026-10-08", "2026-10-12"}
SHOULD_BE_SILENT = {"2026-10-06", "2026-10-09", "2026-10-13"}


def candles(key, day):
    r = requests.get(f"{BASE_URL}/historical-candle/{key}/1minute/{day}/{day}",
                     headers=get_headers(), timeout=30)
    if r.status_code != 200:
        return []
    return sorted(r.json().get("data", {}).get("candles", []), key=lambda x: x[0])


def hm(ts):
    t = dt.datetime.fromisoformat(ts).astimezone(IST)
    return t.hour, t.minute


def _or_levels(cs):
    win = [c for c in cs if OR_START <= hm(c[0]) < OR_END]
    if not win:
        return None
    return max(float(c[2]) for c in win), min(float(c[3]) for c in win), len(win)


def fire_time(cs, box_kind, test_kind) -> tuple:
    """(fired_at, detail, closest) for one combination, as if LONG all day."""
    lv = _or_levels(cs)
    closest = None
    for i, c in enumerate(cs):
        t = dt.datetime.fromisoformat(c[0]).astimezone(IST)
        if (t.hour, t.minute) < OR_END:
            continue
        if box_kind == "fixed":
            if lv is None:
                continue
            hi, lo, n = lv
            first, last = "09:15", "09:59"
        else:
            prior = cs[:i]
            if len(prior) < ROLL:
                continue
            w = prior[-ROLL:]
            hi = max(float(x[2]) for x in w)
            lo = min(float(x[3]) for x in w)
            n = ROLL
            first, last = str(w[0][0])[11:16], str(w[-1][0])[11:16]
        # the probe: a wick uses this minute's low, a close uses the last
        # COMPLETED minute's close
        if test_kind == "wick":
            probe = float(c[3])
            at = t.strftime("%H:%M")
        else:
            if i == 0:
                continue
            probe = float(cs[i - 1][4])
            at = str(cs[i - 1][0])[11:16]
        d = probe - lo
        if closest is None or d < closest:
            closest = d
        if probe < lo:
            return (at, f"range {lo:.2f}-{hi:.2f} ({n} candles {first}-{last}), "
                        f"probe {probe:.2f}", closest)
    return (None, None, closest)


def shipped_fire_time(cs) -> tuple:
    """fixed + close, through the ENGINE's own opening_range()/verdict()."""
    for i, c in enumerate(cs):
        t = dt.datetime.fromisoformat(c[0]).astimezone(IST)
        if (t.hour, t.minute) < OR_END:
            continue
        b = opening_range("replay", now=t, candles=cs, use_cache=False)
        if not b["ok"]:
            continue
        v = verdict(b, "LONG")
        if v["broke"]:
            return (b["probe_minute"],
                    f"range {b['low']:.2f}-{b['high']:.2f}, close {v['price']:.2f}")
    return (None, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--stocks", type=int, default=6)
    a = ap.parse_args()

    fut = near_month_future()
    print(f"market subject: {fut['symbol']}  {fut['key']}")
    print(f"all passes AS IF LONG ALL DAY; opening range "
          f"{OR_START[0]:02d}:{OR_START[1]:02d}-{OR_END[0]:02d}:{OR_END[1]:02d}, "
          f"rolling box {ROLL} candles")
    print()

    days, d = [], dt.date.today()
    while len(days) < a.sessions and (dt.date.today() - d).days < 45:
        iso = d.isoformat()
        if candles(fut["key"], iso):
            days.append(iso)
        d -= dt.timedelta(days=1)
    days.sort()

    COMBOS = [("rolling", "wick", "roll+wick  (82e8470)"),
              ("rolling", "close", "roll+close"),
              ("fixed", "wick", "fixed+wick"),
              ("fixed", "close", "FIXED+CLOSE (now)")]

    print("=" * 112)
    print("THE MARKET BREAKER — FOUR COMBINATIONS")
    print("=" * 112)
    print(f"{'SESSION':<12}{'TARGET':>9}" + "".join(f"{c[2]:>22}" for c in COMBOS))
    print("-" * 112)
    results = {c[2]: {} for c in COMBOS}
    closest = {c[2]: {} for c in COMBOS}
    for day in days:
        cs = candles(fut["key"], day)
        tgt = ("FIRE" if day in SHOULD_FIRE else
               "SILENT" if day in SHOULD_BE_SILENT else "—")
        cells = []
        for bk, tk, label in COMBOS:
            at, det, cl = fire_time(cs, bk, tk)
            results[label][day] = at
            closest[label][day] = cl
            cells.append(f"{at}" if at else "silent")
        print(f"{day:<12}{tgt:>9}" + "".join(f"{c:>22}" for c in cells))
    print("-" * 112)

    print()
    print("=" * 112)
    print("AGAINST THE TARGET")
    print("=" * 112)
    print(f"{'COMBINATION':<24}{'FIRED ON SHOULD-FIRE':>22}"
          f"{'SILENT ON SHOULD-BE-SILENT':>28}{'TOTAL FIRED':>13}{'HITS TARGET':>13}")
    print("-" * 112)
    for _, _, label in COMBOS:
        r = results[label]
        got_fire = sum(1 for d in SHOULD_FIRE if r.get(d))
        got_silent = sum(1 for d in SHOULD_BE_SILENT if not r.get(d))
        total = sum(1 for d in r if r[d])
        hit = (got_fire == len(SHOULD_FIRE & set(days)) and
               got_silent == len(SHOULD_BE_SILENT & set(days)))
        print(f"{label:<24}{f'{got_fire}/{len(SHOULD_FIRE & set(days))}':>22}"
              f"{f'{got_silent}/{len(SHOULD_BE_SILENT & set(days))}':>28}"
              f"{f'{total}/{len(days)}':>13}{('YES' if hit else 'no'):>13}")
    print("-" * 112)

    print()
    print("HOW FAR INSIDE THE FLOOR PRICE GOT ON THE SILENT SESSIONS")
    print("(fixed+close; a small number is a near miss, a large one is comfortable)")
    print("-" * 112)
    label = "FIXED+CLOSE (now)"
    for day in days:
        if results[label].get(day):
            continue
        cl = closest[label].get(day)
        print(f"  {day}  closest close came to the floor: "
              f"{cl:+.2f} points" if cl is not None else f"  {day}  —")

    print()
    print("=" * 112)
    print("CROSS-CHECK: the shipped path through the ENGINE's own functions")
    print("=" * 112)
    for day in days:
        cs = candles(fut["key"], day)
        at, det = shipped_fire_time(cs)
        mine = results["FIXED+CLOSE (now)"].get(day)
        agree = (at or None) == (mine or None)
        print(f"  {day}  engine: {str(at or 'silent'):<8} harness: "
              f"{str(mine or 'silent'):<8} {'agree' if agree else '** DIVERGE **'}"
              f"  {det or ''}")

    # ── stocks ────────────────────────────────────────────────────
    print()
    print("=" * 112)
    print(f"THE STOCK BREAKER — fixed+close, {a.stocks} symbols")
    print("=" * 112)
    from engine.universe import INSTRUMENT_KEYS
    syms = ["RELIANCE", "TCS", "INFY", "SBIN", "ITC", "HDFCBANK"][:a.stocks]
    print(f"{'SYMBOL':<12}{'SESSIONS':>9}{'roll+wick':>11}{'FIXED+CLOSE':>13}"
          f"   FIRED ON (fixed+close)")
    print("-" * 112)
    tot_old = tot_new = 0
    for sym in syms:
        k = INSTRUMENT_KEYS.get(sym)
        if not k:
            continue
        n = old = new = 0
        hits = []
        for day in days:
            cs = candles(k, day)
            if not cs:
                continue
            n += 1
            if fire_time(cs, "rolling", "wick")[0]:
                old += 1
            at = fire_time(cs, "fixed", "close")[0]
            if at:
                new += 1
                hits.append(f"{day[5:]} {at}")
        tot_old += old
        tot_new += new
        print(f"{sym:<12}{n:>9}{old:>11}{new:>13}   "
              f"{', '.join(hits[:5]) or '—'}")
    print("-" * 112)
    print(f"stock-level breaks: roll+wick {tot_old}, fixed+close {tot_new}")

    print()
    print("=" * 112)
    print("THE CALL COST")
    print("=" * 112)
    print("  ROLLING: the box moved every cycle, so every symbol was refetched")
    print("    every cycle. 6.2 candidates + 1 position + the future = 8 calls")
    print("    x 360 cycles = ~2,880 a session.")
    print("  FIXED: the opening range is computed ONCE at 10:00 and cached to")
    print("    disk, so after that a symbol is fetched only to read the latest")
    print("    completed close.")
    print("    the range itself:  1 call per distinct symbol per session")
    print(f"    at {a.stocks} candidate symbols + 1 position + the future = "
          f"{a.stocks + 2} range calls a session")
    print("    the probe close: still 1 call per symbol per cycle it is")
    print("    evaluated, because the latest close has to come from somewhere")
    print("  -> the saving is the RANGE arithmetic, not the fetch: the box is")
    print("     no longer rebuilt 360 times a day per symbol. The honest")
    print("     number for calls is unchanged unless the probe is also cached,")
    print("     which it must not be -- it is the thing being measured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
