"""
WOULD THE DERIVED STOPS BE SANE? Replay one batch. Reads only.
==============================================================

2026-10-15: 103 long candidates, not one with a stop inside
[MIN_STOP_PCT, MAX_STOP_PCT]. This replays that batch through the real
derive() and reports what the derived stops look like, how many candidates
plus signals would be ENTERABLE after every gate, and the rupee risk at
FIXED_NOTIONAL_PER_TRADE.

Uses the engine's own derive(), validate_stop() and size_for_trade() -- not a
reimplementation.

    python3 -m tools.replay_derived_stops --date 2026-10-15
"""

import sys
import argparse
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from atlas.execution.derived_stop import derive, LONG_LEVELS
from atlas.risk.position_sizing import validate_stop
from atlas.config import (MIN_STOP_PCT, MAX_STOP_PCT,
                          FIXED_NOTIONAL_PER_TRADE as NOTIONAL)
from engine.paths import ALL_ROWS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-10-15")
    a = ap.parse_args()

    df = pd.read_parquet(ALL_ROWS)
    df["date"] = pd.to_datetime(df["date"])
    day = df[df["date"] == pd.Timestamp(a.date)]
    if not len(day):
        print(f"no rows for {a.date} in {ALL_ROWS} "
              f"(it holds {df['date'].max().date()})")
        return 1
    longs = day[day["direction"] == "long"].copy()
    q = longs["qualifies"].fillna(False).astype(bool)
    has_zone = (longs["entry_low"].notna() & (longs["entry_low"] > 0)
                & longs["entry_high"].notna() & (longs["entry_high"] > 0))
    cand = longs[has_zone & ~q]
    sig = longs[has_zone & q]
    print(f"batch {a.date}: {len(longs)} long rows, "
          f"{len(sig)} signal(s), {len(cand)} candidate(s)")
    print(f"band [{MIN_STOP_PCT}, {MAX_STOP_PCT}]%   notional Rs{NOTIONAL:,.0f}")
    print()

    cols = [c for c, _ in LONG_LEVELS]
    missing_cols = [c for c in cols if c not in longs.columns]
    if missing_cols:
        print(f"!! the scored frame does not carry {missing_cols} — "
              f"falling back to the SMC parquets per symbol")
    print()

    def run(frame, label):
        rows = []
        for _, r in frame.iterrows():
            sym = r["symbol"]
            e = float(r.get("entry_ref") or 0)
            ps = float(r.get("sl") or 0)
            if not e > 0:
                continue
            lv = {c: r.get(c) for c in cols if c in frame.columns}
            d = derive(sym, e, ps, "LONG", row=(lv if lv else None))
            if d["stop"] is None:
                continue
            ok, pct, why = validate_stop(e, d["stop"], "LONG")
            qty = int(NOTIONAL // e)
            # qty 0 means the share price exceeds the whole notional; those
            # rows die at Gate 5 on size and are not enterable at any stop.
            risk = qty * abs(e - d["stop"]) if qty else None
            rows.append({"symbol": sym, "entry": e, "published": ps,
                         "published_pct": d["published_pct"],
                         "derived": d["stop"], "pct": d["stop_pct"],
                         "source": d["source"], "clamped": d["clamped"],
                         "in_band": ok, "qty": qty, "risk": risk,
                         "n_levels": len(d["candidates"])})
        return pd.DataFrame(rows)

    for frame, label in ((cand, "CANDIDATES"), (sig, "SIGNALS")):
        t = run(frame, label)
        print("=" * 92)
        print(f"{label} — {len(t)} row(s) with a computable entry")
        print("=" * 92)
        if not len(t):
            continue
        print(f"  stop INSIDE the band after derivation : "
              f"{int(t['in_band'].sum())} of {len(t)}")
        print(f"  ... and qty >= 1 at the fixed notional   : "
              f"{int((t['in_band'] & (t['qty'] >= 1)).sum())}")
        print(f"  unaffordable at Rs{NOTIONAL:,.0f} (qty 0)        : "
              f"{int((t['qty'] < 1).sum())}")
        print(f"  still outside                         : "
              f"{int((~t['in_band']).sum())}")
        print()
        print("  WHERE THE DERIVED STOP CAME FROM")
        for k, v in Counter(t["source"]).most_common():
            print(f"    {k:<18}{v:>5}")
        print()
        print("  DERIVED STOP % DISTRIBUTION")
        ib = t[t["in_band"] & (t["qty"] >= 1)]
        if len(ib):
            qs = ib["pct"].quantile([0, .25, .5, .75, 1.0])
            print(f"    min {qs[0]:.2f}%  p25 {qs[0.25]:.2f}%  "
                  f"median {qs[0.5]:.2f}%  p75 {qs[0.75]:.2f}%  "
                  f"max {qs[1.0]:.2f}%")
            for lo, hi in ((0.5, 1), (1, 2), (2, 3), (3, 4), (4, 5.001)):
                n = int(((ib["pct"] >= lo) & (ib["pct"] < hi)).sum())
                print(f"    {lo:>4.1f}-{hi:<4.1f}%  {'#' * min(n, 50)} {n}")
        print()
        print("  RUPEE RISK AT THE FIXED NOTIONAL")
        if len(ib):
            rq = ib["risk"].quantile([0, .5, 1.0])
            print(f"    min Rs{rq[0]:,.0f}   median Rs{rq[0.5]:,.0f}   "
                  f"max Rs{rq[1.0]:,.0f}")
            print(f"    as % of Rs{NOTIONAL:,.0f}: "
                  f"{100*rq[0]/NOTIONAL:.2f}% .. {100*rq[1.0]/NOTIONAL:.2f}%")
        print(f"  published-stop % before derivation: "
              f"median {t['published_pct'].median():.2f}%, "
              f"max {t['published_pct'].max():.2f}%")
        print()

    # ── the two the operator named ────────────────────────────────
    print("=" * 92)
    print("HEXT AND TATAELXSI, NAMED")
    print("=" * 92)
    import atlas.execution.exits as X
    for sym in ("HEXT", "TATAELXSI"):
        sel = longs[longs["symbol"] == sym]
        if not len(sel):
            print(f"  {sym}: not in this batch")
            continue
        r = sel.iloc[0]
        e = float(r["entry_ref"]); ps = float(r["sl"])
        lv = {c: r.get(c) for c in cols if c in longs.columns}
        d = derive(sym, e, ps, "LONG", row=(lv if lv else None))
        qty = int(NOTIONAL // e)
        risk = qty * abs(e - d["stop"]) if d["stop"] else 0
        tgt = None
        if d["stop"]:
            try:
                tgt = X.target_price(e, d["stop"], "LONG", tick=0.05)
            except Exception as ex:
                tgt = f"(needs a tick: {ex})"
        print(f"  {sym}")
        print(f"    entry_ref           {e:.2f}")
        print(f"    published stop      {ps:.2f}   "
              f"({d['published_pct']:.2f}% — outside the band)")
        print(f"    derived stop        {d['stop']:.2f}   "
              f"({d['stop_pct']:.2f}%)")
        print(f"    came from           {d['source']}"
              f"{'  (CLAMP, not structure)' if d['clamped'] else ''}")
        print(f"    levels considered   {d['candidates']}")
        print(f"    new target (2R)     {tgt}")
        print(f"    qty at Rs{NOTIONAL:,.0f}      {qty}")
        print(f"    rupee risk          Rs{risk:,.2f}  "
              f"({100*risk/NOTIONAL:.2f}% of notional)")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
