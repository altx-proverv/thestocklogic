"""
WHAT THE LIVE GATE WOULD HAVE SAID, MINUTE BY MINUTE, AGAINST WHAT THE STALE
ONE ACTUALLY SAID
============================================================================

The operator's condition for trusting the new Gate 1: replay recent sessions
and show, for each, what the live rule would have said at every 60s cycle,
what market.parquet's gate actually said, and HOW MANY TIMES the live rule
would have changed state during the session. The flip count is the number that
decides whether damping is needed -- and no damping is applied here or in the
live path, because that is the operator's call to make after seeing this.

HOW THE VWAP PATH IS RECONSTRUCTED, AND HOW IT DIFFERS FROM LIVE
----------------------------------------------------------------
The live gate reads the exchange's own `average_price` on the NIFTY near-month
future: one number, computed by the exchange, no arithmetic of ours. That field
is a SNAPSHOT -- it tells you the session VWAP as of now and keeps no history,
so it cannot be replayed.

So the replay reconstructs the same quantity from 1-minute futures candles:

    vwap(t) = sum(typical(i) * volume(i)) / sum(volume(i))   for i <= t
    typical(i) = (high + low + close) / 3

This is the standard running session VWAP. It is a RECONSTRUCTION, not the
exchange's figure, and the two will differ slightly: the exchange computes from
every trade, this from 1-minute aggregates, and the typical-price proxy stands
in for the per-trade average within each minute. Both measure the same thing --
the volume-weighted average price at which this contract traded today.

Where the reconstruction lands near the price, a leg can differ from what the
live gate would have seen. That is a real limit of this evidence and is why the
VWAP-leg margin is reported: a session whose flips are all on hairline VWAP
crossings is less trustworthy evidence than one whose flips are decisive.

The other two legs need no reconstruction. Previous close is the prior
session's last candle close; today's open is the 09:15 candle open. Both exact.

    python3 -m tools.replay_live_sentiment              last 5 sessions
    python3 -m tools.replay_live_sentiment --sessions 8
    python3 -m tools.replay_live_sentiment --detail 2026-10-12
"""

import sys
import argparse
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
import pandas as pd

from atlas.execution.live_sentiment import verdict_from, near_month_future
from engine.upstox_ws import BASE_URL, get_headers

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
CYCLE_START = (9, 20)      # the market-hours loop's own window
CYCLE_END   = (15, 20)


def candles(key: str, day: str) -> list:
    """1-minute candles for one instrument on one day, oldest first."""
    url = f"{BASE_URL}/historical-candle/{key}/1minute/{day}/{day}"
    r = requests.get(url, headers=get_headers(), timeout=30)
    if r.status_code != 200:
        return []
    c = r.json().get("data", {}).get("candles", [])
    return sorted(c, key=lambda x: x[0])


def session_days(n: int, end: dt.date) -> list:
    """The last n dates that actually have futures 1-minute data."""
    out, d = [], end
    while len(out) < n and (end - d).days < 40:
        iso = d.isoformat()
        if candles(FUT_KEY, iso):
            out.append(iso)
        d -= dt.timedelta(days=1)
    return sorted(out)


def replay_day(day: str, prev_day: str) -> dict:
    """Minute-by-minute verdicts for one session."""
    fut = candles(FUT_KEY, day)
    if not fut:
        return {"day": day, "error": "no futures candles"}
    prev = candles(FUT_KEY, prev_day) if prev_day else []
    if not prev:
        return {"day": day, "error": f"no previous session ({prev_day})"}
    prev_close = float(prev[-1][4])
    day_open = float(fut[0][1])

    rows, cum_pv, cum_v = [], 0.0, 0.0
    for c in fut:
        ts = dt.datetime.fromisoformat(c[0]).astimezone(IST)
        o, h, l, cl, v = float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])
        typical = (h + l + cl) / 3.0
        cum_pv += typical * v
        cum_v  += v
        if cum_v <= 0:
            continue
        vwap = cum_pv / cum_v
        mins = ts.hour * 60 + ts.minute
        if not (CYCLE_START[0] * 60 + CYCLE_START[1] <= mins
                < CYCLE_END[0] * 60 + CYCLE_END[1]):
            continue
        rows.append({
            "t": ts.strftime("%H:%M"), "last": cl, "prev_close": prev_close,
            "open": day_open, "vwap": vwap,
            "verdict": verdict_from(cl, prev_close, day_open, vwap),
            "d_prev": cl - prev_close, "d_open": cl - day_open,
            "d_vwap": cl - vwap,
        })
    if not rows:
        return {"day": day, "error": "no candles inside the cycle window"}

    df = pd.DataFrame(rows)
    flips = int((df["verdict"] != df["verdict"].shift()).sum() - 1)
    # THE OPEN IS NOT THE DAY. At 09:20 the session VWAP is five minutes old
    # and moves fast, so early flips say more about a forming average than
    # about the market changing its mind. Split so the operator can see which
    # kind of instability they are being asked to judge.
    first15 = df[df["t"] <= "09:35"]
    rest = df[df["t"] > "09:35"]
    flips_open = (int((first15["verdict"] != first15["verdict"].shift()).sum() - 1)
                  if len(first15) else 0)
    flips_rest = (int((rest["verdict"] != rest["verdict"].shift()).sum() - 1)
                  if len(rest) else 0)
    # THE FLIPS THAT CHANGE WHAT IS PERMITTED. With ALLOW_SHORT_ENTRIES False,
    # bearish and mixed are the same instruction: cash. A bearish<->mixed
    # oscillation is noise in the label and no change in behaviour, so counting
    # it alongside a real bullish crossing overstates how unstable the GATE is
    # as opposed to the verdict string. Both are reported; this is the one that
    # moves money.
    permits = df["verdict"].eq("bullish")
    flips_actionable = int((permits != permits.shift()).sum() - 1)
    runs = []
    cur, n = df["verdict"].iloc[0], 0
    for v in df["verdict"]:
        if v == cur:
            n += 1
        else:
            runs.append((cur, n)); cur, n = v, 1
    runs.append((cur, n))
    counts = df["verdict"].value_counts().to_dict()
    # how close the VWAP leg ran: a flip on a 0.5-point margin is weaker
    # evidence than one on 30 points, and the reconstruction's error lives here
    near_vwap = int((df["d_vwap"].abs() < 5.0).sum())
    return {
        "day": day, "cycles": len(df), "flips": flips,
        "flips_open": max(flips_open, 0), "flips_rest": max(flips_rest, 0),
        "flips_actionable": max(flips_actionable, 0),
        "bullish": counts.get("bullish", 0), "bearish": counts.get("bearish", 0),
        "mixed": counts.get("mixed", 0),
        "open_verdict": df["verdict"].iloc[0], "close_verdict": df["verdict"].iloc[-1],
        "runs": runs, "near_vwap_cycles": near_vwap,
        "longest_run": max(n for _, n in runs),
        "prev_close": prev_close, "open": day_open,
        "frame": df,
    }


def stale_gate_said(day: str) -> tuple:
    """What market.parquet's gate said for that session. (state, detail)

    THE SAME ARITHMETIC THE OLD GATE USED: advances > declines AND close above
    the 20DMA.

    STRICTLY BEFORE THE SESSION, which is the whole point. build_market writes
    the row for day D at 18:35 IST on day D, so a loop running intraday on D
    can only ever have read D-1 or older. Taking the row for D itself -- which
    a replay can see and the live loop could not -- would compare the live rule
    against a version of the old gate that never existed, and it is the
    flattering version: it has the day's own closing breadth in it.
    """
    p = Path("data/processed/market.parquet")
    if not p.exists():
        return "unknown", "market.parquet absent"
    m = pd.read_parquet(p)
    m["date"] = pd.to_datetime(m["date"])
    upto = m[m["date"] < pd.Timestamp(day)]
    if upto.empty:
        return "unknown", f"no row at or before {day}"
    row = upto.sort_values("date").iloc[-1]
    asof = pd.Timestamp(row["date"]).date().isoformat()
    lag = (dt.date.fromisoformat(day) - dt.date.fromisoformat(asof)).days
    adv, dec = row.get("advance_count"), row.get("decline_count")
    close, ma20 = row.get("nifty_close"), row.get("nifty_20dma")
    if any(pd.isna(x) for x in (adv, dec, close, ma20)):
        return "unknown", f"row {asof} incomplete"
    up, above = adv > dec, close > ma20
    state = ("bullish" if (up and above) else
             "bearish" if ((not up) and (not above)) else "mixed")
    return state, (f"row {asof} ({lag}d behind): breadth {adv:.0f}/{dec:.0f}, "
                   f"close {close:.0f} vs 20DMA {ma20:.0f}")


def main() -> int:
    global FUT_KEY
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--detail", default="")
    ap.add_argument("--end", default=dt.datetime.now(IST).date().isoformat())
    a = ap.parse_args()

    fut = near_month_future()
    FUT_KEY = fut["key"]
    print(f"contract: {fut['symbol']}  {fut['key']}  expiry {fut['expiry']}")
    print(f"VWAP leg: reconstructed from 1-minute futures candles "
          f"(cum typical*vol / cum vol). The LIVE gate uses the exchange's own "
          f"average_price; see this module's docstring for the difference.")
    print()

    end = dt.date.fromisoformat(a.end)
    days = session_days(a.sessions + 1, end)
    if len(days) < 2:
        print("not enough sessions with intraday data")
        return 1

    results = []
    for i in range(1, len(days)):
        r = replay_day(days[i], days[i - 1])
        if "error" in r:
            print(f"  {days[i]}: {r['error']}")
            continue
        r["stale"], r["stale_detail"] = stale_gate_said(days[i])
        results.append(r)

    print("=" * 118)
    print("WHAT THE LIVE RULE WOULD HAVE SAID, PER 60s CYCLE, vs THE STALE GATE")
    print("=" * 118)
    print(f"{'SESSION':<12}{'CYCLES':>7}{'FLIPS':>6}{'->09:35':>8}{'09:35->':>8}{'LONG-GATE':>10}"
          f"{'BULL':>6}{'BEAR':>6}{'MIXED':>7}{'AT OPEN':>9}{'AT CLOSE':>10}"
          f"{'LONGEST':>9}{'STALE GATE SAID':>17}")
    print("-" * 118)
    for r in results:
        print(f"{r['day']:<12}{r['cycles']:>7}{r['flips']:>6}{r['flips_open']:>8}"
              f"{r['flips_rest']:>8}{r['flips_actionable']:>10}{r['bullish']:>6}{r['bearish']:>6}"
              f"{r['mixed']:>7}{r['open_verdict']:>9}{r['close_verdict']:>10}"
              f"{r['longest_run']:>9}{r['stale']:>17}")
    print("-" * 118)
    if results:
        f = [r["flips"] for r in results]
        fo = [r["flips_open"] for r in results]
        fr = [r["flips_rest"] for r in results]
        print(f"FLIPS per session       : min {min(f)}  median "
              f"{sorted(f)[len(f)//2]}  max {max(f)}  mean {sum(f)/len(f):.1f}")
        print(f"  of which before 09:35 : {sum(fo)} of {sum(f)} total "
              f"({100.0*sum(fo)/max(sum(f),1):.0f}%) — the VWAP is minutes old there")
        print(f"  after 09:35            : min {min(fr)}  median "
              f"{sorted(fr)[len(fr)//2]}  max {max(fr)}  mean {sum(fr)/len(fr):.1f}")
        fa = [r["flips_actionable"] for r in results]
        print(f"LONG-GATE flips (in/out of bullish — the only ones that change "
              f"what ATLAS may do while shorts are off):")
        print(f"                          min {min(fa)}  median "
              f"{sorted(fa)[len(fa)//2]}  max {max(fa)}  mean {sum(fa)/len(fa):.1f}"
              f"  total {sum(fa)} over {len(results)} session(s)")
        agree = sum(1 for r in results
                    if r["stale"] == max(("bullish", r["bullish"]),
                                         ("bearish", r["bearish"]),
                                         ("mixed", r["mixed"]),
                                         key=lambda t: t[1])[0])
        print(f"sessions where the stale gate matched the live rule's dominant "
              f"state: {agree}/{len(results)}")
        nv = sum(r["near_vwap_cycles"] for r in results)
        tc = sum(r["cycles"] for r in results)
        print(f"cycles within 5 points of the VWAP leg: {nv}/{tc} "
              f"({100.0*nv/tc:.1f}%) — where the reconstruction is least "
              f"certain")
    print()
    print("STATE RUNS PER SESSION (consecutive cycles in one state)")
    print("-" * 118)
    for r in results:
        runs = "  ".join(f"{v}x{n}" for v, n in r["runs"])
        print(f"  {r['day']}  {runs[:104]}")
    print()
    print("THE STALE GATE'S OWN READING, with its lag")
    print("-" * 118)
    for r in results:
        print(f"  {r['day']}  {r['stale']:<9} {r['stale_detail']}")

    if a.detail:
        d = next((r for r in results if r["day"] == a.detail), None)
        if not d:
            print(f"\nno replay for {a.detail}")
            return 0
        print()
        print("=" * 118)
        print(f"CYCLE DETAIL — {a.detail}  (every 15th cycle, and every flip)")
        print("=" * 118)
        df = d["frame"].reset_index(drop=True)
        show = set(range(0, len(df), 15))
        show |= {i for i in range(1, len(df))
                 if df["verdict"].iloc[i] != df["verdict"].iloc[i - 1]}
        print(f"{'TIME':<7}{'LAST':>10}{'vs PREV':>10}{'vs OPEN':>10}"
              f"{'vs VWAP':>10}{'VERDICT':>10}   FLIP")
        prev_v = None
        for i in sorted(show):
            row = df.iloc[i]
            flip = "  <-- flip" if prev_v and row["verdict"] != prev_v else ""
            print(f"{row['t']:<7}{row['last']:>10.2f}{row['d_prev']:>+10.2f}"
                  f"{row['d_open']:>+10.2f}{row['d_vwap']:>+10.2f}"
                  f"{row['verdict']:>10}{flip}")
            prev_v = row["verdict"]
    return 0


if __name__ == "__main__":
    sys.exit(main())
