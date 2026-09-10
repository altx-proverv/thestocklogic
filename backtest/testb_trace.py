#!/usr/bin/env python3
"""
TEST B — single-case trace.  READ-ONLY.  Nothing here writes anything.

Runs one symbol on one session through the exact replay path, with the
try/except in structure_for_date removed, and prints the values at every
stage next to what was actually published.

    python3 backtest/testb_trace.py                    # BANKINDIA 2026-08-18
    python3 backtest/testb_trace.py BANKINDIA 2026-08-24
    python3 backtest/testb_trace.py BANKINDIA 2026-08-18 --pool  # + top-N cut

--pool regenerates the whole universe for that date (~17 min at 1875
ms/symbol) and is only needed if the symbol survives to stage 6. Stages
1-6 answer the question on their own if it dies earlier, so it is off by
default.

SUPABASE_SERVICE_KEY must be exported for the live side (stage 0). Without
it stages 1-6 still run; only the comparison against the published row is
skipped.
"""

import os
import sys
import traceback
from pathlib import Path

import pandas as pd

# Repo root: the parent of backtest/, or the cwd, or $TSL_ROOT.
_here = Path(__file__).resolve()
for _cand in (Path(os.environ.get("TSL_ROOT", "")), _here.parent.parent,
              _here.parent, Path.cwd()):
    if _cand and (_cand / "backtest").is_dir():
        ROOT = _cand
        break
else:
    sys.exit("cannot find the repo — run from the checkout or set TSL_ROOT")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from backtest import replay
from backtest.asof import slice_asof

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BANKINDIA"
DATE   = sys.argv[2] if len(sys.argv) > 2 else "2026-08-18"
POOL   = "--pool" in sys.argv
D      = pd.Timestamp(DATE)

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 60)


def head(n, title):
    print(f"\n{'='*78}\nSTAGE {n} — {title}\n{'='*78}")


def show(label, row, cols):
    have = [c for c in cols if c in row.index]
    miss = [c for c in cols if c not in row.index]
    print(f"  {label}")
    for c in have:
        print(f"    {c:<26} {row[c]!r}")
    if miss:
        print(f"    (absent: {', '.join(miss)})")


# ══════════════════════════════════════════════════════════════════
head(0, f"LIVE RECORD — what was published for {SYMBOL} on {DATE}")
# ══════════════════════════════════════════════════════════════════
live_row = None
if not os.environ.get("SUPABASE_SERVICE_KEY"):
    print("  SUPABASE_SERVICE_KEY not set — skipping the live side.")
    print("  Export it (it lives in the crontab header) to get the comparison.")
else:
    from backtest import store
    base = ("signal_date,symbol,direction,setup_name,entry_ref,entry_low,"
            "entry_high,sl,target_1,score,grade")
    try:
        # created_at matters: if the 18 Aug rows were written on 18 Aug, the
        # live run's parquet ENDED on 18 Aug and it had no future to read.
        # Lookahead on the live side then requires a later re-run, not a
        # subtly leaky indicator.
        rows = store.read_live_readonly(
            "signals", select=base + ",created_at",
            order="signal_date.asc,symbol.asc,direction.asc",
            extra=f"&signal_date=eq.{DATE}")
    except RuntimeError:
        rows = store.read_live_readonly(
            "signals", select=base,
            order="signal_date.asc,symbol.asc,direction.asc",
            extra=f"&signal_date=eq.{DATE}")
    day = pd.DataFrame(rows)
    print(f"  {len(day)} signals published on {DATE}: "
          f"{sorted(zip(day.get('symbol', []), day.get('direction', [])))}")
    mine = day[day["symbol"] == SYMBOL] if len(day) else day
    if len(mine):
        live_row = mine.iloc[0]
        print(f"\n  the {SYMBOL} row as published:")
        for k, v in live_row.items():
            print(f"    {k:<26} {v!r}")
    else:
        print(f"  !! {SYMBOL} not in the published set for {DATE}")

# ══════════════════════════════════════════════════════════════════
head(1, "THE PARQUET — does the replay universe even contain this symbol")
# ══════════════════════════════════════════════════════════════════
# load_frames() globs this directory. A symbol with no file here can never be
# regenerated, on any date, and shows up as 'missing' every time.
pq = ROOT / "data/processed/stocks" / f"{SYMBOL}.parquet"
print(f"  {pq}")
print(f"  exists: {pq.exists()}")
if not pq.exists():
    n = len(list((ROOT / 'data/processed/stocks').glob('*.parquet')))
    print(f"  universe on disk: {n} parquets")
    print("\n  >>> DIVERGES HERE: not a generator difference at all. The symbol")
    print("  >>> is absent from the replay universe, so structure_for_date")
    print("  >>> never sees it. Pure 'missing', on every date it was published.")
    sys.exit(0)

raw = pd.read_parquet(pq)
raw["date"] = pd.to_datetime(raw["date"])
raw = raw.sort_values("date").reset_index(drop=True)
print(f"  rows: {len(raw)}   {raw['date'].min().date()} -> {raw['date'].max().date()}")
print(f"  file mtime: {pd.Timestamp(pq.stat().st_mtime, unit='s')}")
print(f"  columns: {sorted(raw.columns)}")

on_d = raw[raw["date"] == D]
print(f"\n  bar for {DATE}: {'YES' if len(on_d) else 'NO'}")
if len(on_d):
    r = on_d.iloc[0]
    print(f"    open {r['open']}  high {r['high']}  low {r['low']}  "
          f"close {r['close']}  prev_close {r.get('prev_close')}")
    print(f"    volume {r['volume']}  delivery_pct {r.get('delivery_pct')}")

# possibility (a): has the file been rewritten since publication?
if live_row is not None and len(on_d):
    c = float(on_d.iloc[0]["close"])
    e = float(live_row["entry_ref"]) if pd.notna(live_row["entry_ref"]) else float("nan")
    s = float(live_row["sl"]) if pd.notna(live_row["sl"]) else float("nan")
    print(f"\n  published entry_ref {e}  sl {s}   vs today's close {c}")
    if pd.notna(e) and c > 0:
        gap = abs(e - c) / c * 100
        print(f"  entry_ref is {gap:.2f}% from today's close for that session")
        if gap > 25:
            print("  >>> the published price is nowhere near today's bar. The series")
            print("  >>> has been rewritten since publication (corporate action or a")
            print("  >>> re-download). That is possibility (a): the replay is correct")
            print("  >>> about today's data and cannot reproduce yesterday's.")

# ══════════════════════════════════════════════════════════════════
head(2, "THE TWO SILENT GUARDS in structure_for_date")
# ══════════════════════════════════════════════════════════════════
h = slice_asof(raw, D)
print(f"  bars <= {DATE}: {len(h)}   (replay MIN_BARS={replay.MIN_BARS}, live 02b skips at <50)")
if len(h) < replay.MIN_BARS:
    print("\n  >>> DIVERGES HERE: dropped by MIN_BARS. Live 02b's threshold is 50,")
    print("  >>> the replay's is 120, so a symbol with 50-119 bars is published")
    print("  >>> live and can never be regenerated. Missing-only, every date.")
    sys.exit(0)

last = h["date"].iloc[-1]
print(f"  last bar <= {DATE}: {last.date()}   matches session: {last == D}")
if last != D:
    print("\n  >>> DIVERGES HERE: 'symbol did not trade on d'. The bar the live run")
    print("  >>> published from is not in today's parquet.")
    sys.exit(0)

# ══════════════════════════════════════════════════════════════════
head(3, "ZONE DETECTION — 02b + active_zones, try/except REMOVED")
# ══════════════════════════════════════════════════════════════════
# structure_for_date swallows anything raised here at log.debug and continues,
# so a per-symbol exception is invisible in a Test B log written at INFO.
smc, score = replay._stages()
market = replay.load_market()
m = slice_asof(market, D)
print(f"  market rows <= {DATE}: {len(m)}")

from engine.active_zones import add_active_zones
from engine.zone_entry import compute_zone_entries

try:
    struct = smc.compute_smc_signals(h.copy(), m)
    struct = add_active_zones(struct)
    out = compute_zone_entries(struct)
except Exception:
    print("\n  >>> DIVERGES HERE: the replay path RAISES for this symbol.")
    print("  >>> structure_for_date catches this at log.debug and drops the")
    print("  >>> symbol silently — which is why the Test B log shows a missing")
    print("  >>> signal and no error. Traceback:\n")
    traceback.print_exc()
    sys.exit(1)

row = out.iloc[-1]
print(f"  computed {len(out)} rows, last is {row['date'].date()}")
show("zone state as-of the session:", row, [
    "close", "structure_trend",
    "active_zone_source", "active_zone_high", "active_zone_low",
    "zone_age_days", "zone_dist_pct",
    "active_demand_ob_high", "active_demand_ob_low",
    "active_supply_ob_high", "active_supply_ob_low",
    "active_bull_fvg_high", "active_bull_fvg_low",
    "active_bear_fvg_high", "active_bear_fvg_low",
    "last_swing_high", "last_swing_low",
])
if pd.isna(row.get("active_zone_high")):
    print("\n  >>> NO ACTIVE ZONE as-of this session. Every downstream stage will")
    print("  >>> reject with 'no active zone'. If the live run published an")
    print("  >>> entry_ref anyway, the zone it used did not exist yet as of D —")
    print("  >>> possibility (b), and the replay is the one telling the truth.")

show("disqualifier inputs (all recomputed as-of, none read from the parquet):", row, [
    "no_trade_zone", "rvol", "atr_pct", "adx", "adx_ranging",
    "weekly_bullish", "weekly_bearish", "recent_bos_choch",
    "market_regime", "pct_from_52w_high", "delivery_pct",
    "institutional_buying", "high_delivery",
])

# ══════════════════════════════════════════════════════════════════
head(4, "SCORING + DISQUALIFIER BLOCK + ENTRY GATE, both directions")
# ══════════════════════════════════════════════════════════════════
combined = out.iloc[[-1]].copy()
combined["symbol"] = SYMBOL

symbol_sector = score.load_symbol_sector()
bias = replay.sector_bias_asof(D)
print(f"  sector: {symbol_sector.get(SYMBOL, 'OTHER')}   "
      f"as-of sector bias: {bias.get(symbol_sector.get(SYMBOL, 'OTHER'), 'avoid')}")
print(f"  (live 03b reads sector_momentum.parquet — a nightly snapshot — where")
print(f"   the replay recomputes as-of. Affects total_score only: there is no")
print(f"   MIN_SCORE gate, qualifies = ~disqualified.)")

for direction in ("long", "short"):
    print(f"\n  ── {direction.upper()} " + "─" * 60)
    s = score.process_direction(combined.copy(), direction, bias, symbol_sector)
    r = s.iloc[0]
    show("", r, [
        "disqualified", "disqualify_reason", "qualifies",
        "is_accumulation", "accumulation_score",
        "total_score", "grade", "setup_name",
        "entry_valid", "reject_reason",
        "entry_ref", "entry_low", "entry_high", "sl",
        "stop_pct", "entry_dist_pct", "qty", "risk_inr", "notional",
    ])
    if bool(r.get("qualifies")):
        print(f"    => QUALIFIES, {r.get('entry_dist_pct')}% from entry "
              f"(gate is {compute_zone_entries.__globals__['MAX_ENTRY_DIST_PCT']}%)")
    else:
        print(f"    => REJECTED: {r.get('disqualify_reason') or r.get('reject_reason')}")
        # Read the entry_* values above with care on a rejected row.
        # structure_for_date runs compute_zone_entries BEFORE a direction
        # column exists, so that pass sizes everything as a LONG, and
        # process_direction only overwrites those columns for rows that
        # qualify. On a rejected short they are therefore the long-side
        # numbers, not this direction's. Output parity is unaffected -- a
        # rejected row is never published -- but the numbers are not this
        # direction's answer.
        if direction == "short":
            print("       (entry_ref/sl/stop_pct above are from the "
                  "direction-less long pass, not the short)")

    if live_row is not None and str(live_row["direction"]).lower() == direction:
        print("\n    published vs regenerated, side by side:")
        for c in ("entry_ref", "entry_low", "entry_high", "sl"):
            lv = live_row.get(c)
            gv = r.get(c)
            flag = ""
            try:
                if pd.notna(lv) and pd.notna(gv) and abs(float(lv) - float(gv)) > 0.01:
                    flag = "   <-- DIFFERS"
            except Exception:
                pass
            print(f"      {c:<12} live {lv!r:>12}   replay {gv!r:>12}{flag}")

# ══════════════════════════════════════════════════════════════════
if POOL:
    head(5, "THE TOP-N CUT — the whole date's qualifying pool")
    frames = replay.load_frames()
    print(f"  {len(frames)} symbols; regenerating {DATE} in full, this is the slow part")
    g = replay.generate_for_date(D, frames, market)
    print(f"\n  replay published {len(g)} candidates:")
    if len(g):
        print(g[["symbol", "direction", "entry_dist_pct", "entry_ref", "sl",
                 "stop_pct", "total_score"]].to_string(index=False))
    print(f"\n  cut is top {score.TOP_N_LONG} long / {score.TOP_N_SHORT} short "
          f"by entry_dist_pct — it only binds when the pool exceeds it.")
    if len(g) and SYMBOL not in set(g["symbol"]):
        print(f"  >>> {SYMBOL} qualified but lost the cut: compare its")
        print(f"  >>> entry_dist_pct above against the worst kept value here.")
else:
    print(f"\n{'='*78}\nSTAGE 5 — top-N cut not run (pass --pool). Only relevant if the")
    print("symbol reached stage 4 QUALIFIES on the direction that was published.")
    print("=" * 78)
