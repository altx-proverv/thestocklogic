"""
BACKTEST — Test B, generator parity
===================================
Replays the pipeline over the sessions where the signal-generating code has
not changed, and compares the candidates it produces against what was actually
published.

SCOPE IS DELIBERATELY NARROW, AND WHY
-------------------------------------
The signal history runs 15 Jun - 27 Aug 2026, but the generator was replaced
inside that span:

    2026-08-08  6cc8fbe  zone-based entry + structural stops, risk-based sizing
    2026-08-08  4517191  active zones wired into 02b, zone entry into 03b
    2026-08-12  1f91310  macd fix that had put a standing +2 on every short
    2026-08-12  8db97db  MIN_SCORE gate dropped
    2026-08-13  fc8731b  the 0.30% entry gate -- the publishing gate itself

Signals before 13 Aug came from close +/-0.2% bands and a different stop model;
the fingerprint is in the data, up to 156 published on a single day against the
2-7 seen now. Replaying today's code over that period cannot reproduce them,
and a mismatch there would say nothing about this engine. So parity is asserted
only from 2026-08-13, and earlier sessions are reported as NOT COMPARABLE
rather than as a number.

WHAT A NEAR-MISS MEANS
----------------------
fc8731b landed during 13 Aug, so that session straddles the change and is
reported separately from the clean ones. A handful of differing symbols on the
13th is expected; the same on later sessions is a bug.
"""

import sys
import logging
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import store, replay

log = logging.getLogger("BACKTEST-TESTB")

# First session generated wholly by the current publishing gate.
STABLE_FROM = "2026-08-13"
BOUNDARY_SESSION = "2026-08-13"     # fc8731b landed mid-day; reported apart


def published_signals(start: str, end: str) -> pd.DataFrame:
    """What the live pipeline actually published. Read-only, via the seam."""
    rows = store.read_live_readonly(
        "signals",
        select="signal_date,symbol,direction,setup_name,entry_ref,sl,target_1",
        order="signal_date.asc,symbol.asc,direction.asc",
        extra=f"&signal_date=gte.{start}&signal_date=lte.{end}")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["direction"] = df["direction"].str.upper()
    return df


def compare_date(live: pd.DataFrame, gen: pd.DataFrame, d) -> dict:
    """Symbol-for-symbol on one session."""
    L = live[live["signal_date"] == pd.Timestamp(d)]
    G = gen[gen["signal_date"] == pd.Timestamp(d)] if len(gen) else gen

    lset = set(zip(L["symbol"], L["direction"])) if len(L) else set()
    gset = (set(zip(G["symbol"], G["direction"].str.upper()))
            if len(G) else set())

    return {
        "date": pd.Timestamp(d).date().isoformat(),
        "n_live": len(lset),
        "n_generated": len(gset),
        "matched": len(lset & gset),
        "missing": sorted(lset - gset),      # published live, not regenerated
        "extra": sorted(gset - lset),        # regenerated, never published
        "exact": lset == gset,
    }


def run(start: str = STABLE_FROM, end: str = "2026-08-27",
        symbols=None, lookback: int = replay.LOOKBACK_BARS) -> dict:
    live = published_signals(start, end)
    if live.empty:
        raise RuntimeError(f"no published signals in {start}..{end}")

    sessions = sorted(live["signal_date"].unique())
    log.info(f"{len(sessions)} published sessions in {start}..{end}, "
             f"{len(live)} signals")

    frames = replay.load_frames(symbols)
    market = replay.load_market()
    log.info(f"{len(frames)} symbols loaded")

    per_date, gen_all = [], []
    for d in sessions:
        g = replay.generate_for_date(d, frames, market, lookback=lookback)
        if len(g):
            gen_all.append(g)
        cmp = compare_date(live, g if len(g) else pd.DataFrame(columns=live.columns), d)
        per_date.append(cmp)
        log.info(f"  {cmp['date']}: live {cmp['n_live']:>3}  "
                 f"generated {cmp['n_generated']:>3}  matched {cmp['matched']:>3}"
                 f"{'  EXACT' if cmp['exact'] else ''}")

    clean = [c for c in per_date if c["date"] != BOUNDARY_SESSION]
    passed = all(c["exact"] for c in clean)
    return {"passed": passed, "per_date": per_date, "clean": clean,
            "generated": pd.concat(gen_all, ignore_index=True) if gen_all else pd.DataFrame()}


def report(r: dict) -> None:
    print("=" * 78)
    print("TEST B — GENERATOR PARITY (replay vs what was actually published)")
    print(f"  asserted from {STABLE_FROM}; earlier sessions predate the current")
    print(f"  publishing gate (fc8731b) and are NOT COMPARABLE by construction")
    print("=" * 78)
    print(f"  {'session':<12}{'live':>6}{'gen':>6}{'match':>7}   verdict")
    print("  " + "-" * 60)
    for c in r["per_date"]:
        if c["date"] == BOUNDARY_SESSION:
            v = "boundary — fc8731b landed this day, reported apart"
        elif c["exact"]:
            v = "exact"
        else:
            v = f"{len(c['missing'])} missing, {len(c['extra'])} extra"
        print(f"  {c['date']:<12}{c['n_live']:>6}{c['n_generated']:>6}"
              f"{c['matched']:>7}   {v}")

    bad = [c for c in r["clean"] if not c["exact"]]
    if bad:
        print("\n  differences on comparable sessions:")
        for c in bad[:6]:
            if c["missing"]:
                print(f"    {c['date']} published but not regenerated: {c['missing'][:6]}")
            if c["extra"]:
                print(f"    {c['date']} regenerated but not published: {c['extra'][:6]}")

    print("\n" + "=" * 78)
    print("RESULT:", "PASS — replay reproduces the published candidate set"
          if r["passed"] else
          "FAIL — replay does not reproduce what was published")
    print("=" * 78)
