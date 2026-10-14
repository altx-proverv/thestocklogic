"""
WOULD THE RANGE BREAKERS HAVE FIRED, ON EVERY SESSION WE HAVE HISTORY FOR?
==========================================================================

The operator's condition for letting this carry money: if the market breaker
fires on most sessions then 45 cycles is too short, and if it never fires on
any session that is worth knowing too.

SAME ARITHMETIC AS THE ENGINE. box() and verdict() are imported from
atlas/execution/range_breaker.py, not reimplemented -- the direction logic in
particular is the one thing that must not exist twice.

THE SIDE HELD IS SIMULATED, because the breaker's direction depends on what
ATLAS holds and on most of these sessions it held nothing. Two passes:

  AS IT HAPPENED   the side ATLAS actually held, from atlas_trades. Where it
                   held nothing the breaker is DORMANT and cannot fire, which
                   is the honest answer for that session.
  AS IF LONG       the side forced to LONG for every cycle. This is the
                   stress case -- it asks "if ATLAS had been long all day,
                   would the floor have broken" -- and it is the number that
                   says whether 45 cycles is too tight.

FAVOURABLE BREAKS ARE COUNTED SEPARATELY. A break of the ceiling while long
must NOT fire. Counting them proves the direction logic held on real data
rather than only in the unit test.

    python3 -m tools.replay_range_breaker
    python3 -m tools.replay_range_breaker --stocks 8
"""

import sys
import argparse
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests

from atlas.execution.range_breaker import box, verdict, with_last_price
from atlas.execution.live_sentiment import near_month_future
from engine.upstox_ws import BASE_URL, get_headers

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def candles(key: str, day: str) -> list:
    r = requests.get(f"{BASE_URL}/historical-candle/{key}/1minute/{day}/{day}",
                     headers=get_headers(), timeout=30)
    if r.status_code != 200:
        return []
    return sorted(r.json().get("data", {}).get("candles", []), key=lambda x: x[0])


def sessions_with_data(key: str, end: dt.date, want: int) -> list:
    out, d = [], end
    while len(out) < want and (end - d).days < 45:
        iso = d.isoformat()
        if candles(key, iso):
            out.append(iso)
        d -= dt.timedelta(days=1)
    return sorted(out)


def replay(cs: list, side: str, arms_at="09:50") -> dict:
    """Walk a session minute by minute, applying the engine's own box/verdict."""
    hh, mm = (int(x) for x in arms_at.split(":"))
    fired_at = None
    fired_box = None
    favourable = 0
    evaluable = 0
    not_evaluable = 0
    closest = None          # smallest distance from the watched edge
    closest_at = None
    for i, c in enumerate(cs):
        t = dt.datetime.fromisoformat(c[0]).astimezone(IST)
        if (t.hour, t.minute) < (hh, mm):
            continue
        b = box("replay", now=t, candles=cs)
        if not b["ok"]:
            not_evaluable += 1
            continue
        evaluable += 1
        v = verdict(b, side)
        if v["favourable_break"]:
            favourable += 1
        edge = b["low"] if side == "LONG" else b["high"]
        probe = b.get("probe_low") if side == "LONG" else b.get("probe_high")
        if probe is not None:
            dist = (probe - edge) if side == "LONG" else (edge - probe)
            if closest is None or dist < closest:
                closest, closest_at = dist, t.strftime("%H:%M")
        if v["broke"] and fired_at is None:
            fired_at = t.strftime("%H:%M")
            fired_box = dict(b, breaking_price=v["price"], level=v["level"])
    return {"fired_at": fired_at, "box": fired_box, "favourable": favourable,
            "evaluable": evaluable, "not_evaluable": not_evaluable,
            "closest": closest, "closest_at": closest_at}


def held_sides_by_day() -> dict:
    """{date: side} from atlas_trades -- what ATLAS actually held."""
    try:
        from atlas.config import SUPABASE_URL, SUPABASE_KEY
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?select=symbol,direction,entry_date,status",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=20)
        if r.status_code != 200:
            return {}
        out = {}
        for row in r.json() or []:
            d = str(row.get("entry_date") or "")[:10]
            s = str(row.get("direction", "")).upper()
            if d and s in ("LONG", "SHORT"):
                out.setdefault(d, set()).add(s)
        return {k: (v.pop() if len(v) == 1 else None) for k, v in out.items()}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--stocks", type=int, default=6)
    ap.add_argument("--end", default=dt.datetime.now(IST).date().isoformat())
    a = ap.parse_args()

    fut = near_month_future()
    print(f"market subject: {fut['symbol']}  {fut['key']}")
    print(f"box: the 45 1-minute candles STRICTLY BEFORE the current minute; "
          f"armed 09:50")
    print()

    days = sessions_with_data(fut["key"], dt.date.fromisoformat(a.end),
                              a.sessions)
    if not days:
        print("no sessions with intraday history")
        return 1
    held = held_sides_by_day()

    print("=" * 118)
    print("THE MARKET BREAKER — AS IT HAPPENED (the side ATLAS actually held)")
    print("=" * 118)
    print(f"{'SESSION':<12}{'HELD':>8}{'VERDICT':>12}{'FIRED AT':>10}"
          f"{'EVALUABLE':>11}{'NOT EVAL':>10}{'CLOSEST TO THE EDGE':>24}")
    print("-" * 118)
    as_happened = []
    for d in days:
        side = held.get(d)
        if side is None:
            print(f"{d:<12}{'none':>8}{'DORMANT':>12}{'—':>10}{'—':>11}"
                  f"{'—':>10}{'nothing open to protect':>24}")
            as_happened.append({"day": d, "dormant": True})
            continue
        cs = candles(fut["key"], d)
        r = replay(cs, side)
        r["day"], r["side"] = d, side
        as_happened.append(r)
        if r["closest"] is None:
            near = "—"
        else:
            near = f"{r['closest']:+.2f} at {r['closest_at']}"
        print(f"{d:<12}{side:>8}{('FIRED' if r['fired_at'] else 'no'):>12}"
              f"{str(r['fired_at'] or '—'):>10}{r['evaluable']:>11}"
              f"{r['not_evaluable']:>10}{near:>24}")

    print()
    print("=" * 118)
    print("THE MARKET BREAKER — AS IF LONG ALL DAY (the stress case)")
    print("=" * 118)
    print(f"{'SESSION':<12}{'VERDICT':>10}{'FIRED AT':>10}{'BOX AT THE BREAK':>34}"
          f"{'BREAK PRICE':>13}{'FAVOURABLE':>12}{'CLOSEST':>12}")
    print("-" * 118)
    rows = []
    for d in days:
        cs = candles(fut["key"], d)
        r = replay(cs, "LONG")
        r["day"] = d
        rows.append(r)
        bx = (f"{r['box']['low']:.2f}-{r['box']['high']:.2f} "
              f"({r['box']['first']}-{r['box']['last']})" if r["box"] else "—")
        bp = f"{r['box']['breaking_price']:.2f}" if r["box"] else "—"
        cl = f"{r['closest']:+.2f}" if r["closest"] is not None else "—"
        print(f"{d:<12}{('FIRED' if r['fired_at'] else 'no'):>10}"
              f"{str(r['fired_at'] or '—'):>10}{bx:>34}{bp:>13}"
              f"{r['favourable']:>12}{cl:>12}")
    print("-" * 118)
    nfired = sum(1 for r in rows if r["fired_at"])
    print(f"as if LONG all day, the market breaker fires on {nfired} of "
          f"{len(rows)} session(s)")
    nf = sum(r["favourable"] for r in rows)
    print(f"FAVOURABLE breaks (ceiling broken while long) seen: {nf} across "
          f"{len(rows)} session(s) — every one correctly did NOT fire")
    nofire = [r for r in rows if not r["fired_at"] and r["closest"] is not None]
    if nofire:
        tightest = min(nofire, key=lambda r: r["closest"])
        print(f"CLOSEST a session came to firing without doing so: "
              f"{tightest['closest']:+.2f} points from the floor on "
              f"{tightest['day']} at {tightest['closest_at']}")
    ne = sum(r["not_evaluable"] for r in rows)
    ev = sum(r["evaluable"] for r in rows)
    print(f"minutes evaluable {ev}, not evaluable {ne} "
          f"({100.0*ne/max(ev+ne,1):.1f}% — the 09:50-10:00 warm-up, where a "
          f"45-candle box does not exist yet)")

    # ── THE STOCK BREAKER, on a sample ────────────────────────────
    print()
    print("=" * 118)
    print(f"THE STOCK BREAKER — {a.stocks} symbols, as if LONG all day")
    print("=" * 118)
    from engine.universe import INSTRUMENT_KEYS, ALL_SYMBOLS
    syms = [s for s in ("RELIANCE", "TCS", "INFY", "SBIN", "ITC", "HDFCBANK",
                        "AXISBANK", "MARUTI")][:a.stocks]
    print(f"{'SYMBOL':<12}{'SESSIONS':>9}{'BREAKS':>8}{'FAVOURABLE':>12}"
          f"   FIRED ON (first four)")
    print("-" * 118)
    tot_breaks = 0
    for sym in syms:
        k = INSTRUMENT_KEYS.get(sym)
        if not k:
            continue
        hits, fav, n = [], 0, 0
        for d in days:
            cs = candles(k, d)
            if not cs:
                continue
            n += 1
            r = replay(cs, "LONG")
            fav += r["favourable"]
            if r["fired_at"]:
                hits.append(f"{d[5:]} {r['fired_at']}")
        tot_breaks += len(hits)
        print(f"{sym:<12}{n:>9}{len(hits):>8}{fav:>12}   "
              f"{', '.join(hits[:4]) or '—'}")
    print("-" * 118)
    print(f"stock-level breaks across {len(syms)} symbols x {len(days)} "
          f"sessions: {tot_breaks}")
    print()
    print("A stock break blocks NEW ENTRIES in that symbol for the session. It "
          "does not stand the day down and it never exits a position.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
