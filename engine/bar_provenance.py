#!/usr/bin/env python3
"""
Was this bar revised at source, or corrupted by 01b?  READ-ONLY.

    python3 engine/bar_provenance.py BANKINDIA 2026-08-18
    python3 engine/bar_provenance.py BANKINDIA 2026-08-18 --field close

WHY THIS IS DECIDABLE AT ALL
----------------------------
download_all() skips any date whose CSV is already on disk:

    if fpath.exists():
        results[d] = True
        continue

So data/raw/bhavcopy holds the file as first fetched, and it is an independent
record of what NSE said at download time. Re-parsing it and comparing against
the parquet separates the two causes cleanly:

    CSV parses to the parquet's CURRENT value
        the parquet faithfully reflects its source. The change entered through
        the source -- the CSV was deleted and re-fetched and NSE served a
        revised file -- or through something that writes AFTER parsing, which
        in this pipeline means the delivery injection.

    CSV parses to the value that was PUBLISHED
        the source never moved. 01b changed it on the way to disk: the
        delivery injection, or a change to parse_bhavcopy_csv's column mapping
        between the two runs.

Neither answer changes whether it recurs -- build_parquets rebuilt each file
whole from re-parsed CSVs and never compared against what it was overwriting,
so any difference became truth silently. That is fixed. This tells you which
door it came through, which is what decides whether the fix is sufficient.
"""

import sys
import importlib.util
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "BANKINDIA"
DATE   = sys.argv[2] if len(sys.argv) > 2 else "2026-08-18"
FIELD  = (sys.argv[sys.argv.index("--field") + 1]
          if "--field" in sys.argv else None)
D      = pd.Timestamp(DATE)

spec = importlib.util.spec_from_file_location(
    "b01", ROOT / "engine/01b_download_bhavcopy.py")
b01 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b01)

COLS = ["open", "high", "low", "close", "prev_close",
        "volume", "delivery_qty", "delivery_pct", "trades"]

print("=" * 74)
print(f"PROVENANCE — {SYMBOL} @ {DATE}")
print("=" * 74)

# ── the parquet ───────────────────────────────────────────────────
pq = ROOT / "data/processed/stocks" / f"{SYMBOL}.parquet"
if not pq.exists():
    sys.exit(f"no parquet for {SYMBOL} — nothing to compare")
df = pd.read_parquet(pq)
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)
row = df[df["date"] == D]
if row.empty:
    sys.exit(f"{SYMBOL} has no bar on {DATE} (last bar {df['date'].iloc[-1].date()})")
row = row.iloc[0]
print(f"  parquet   {pq}")
print(f"            mtime {pd.Timestamp(pq.stat().st_mtime, unit='s'):%Y-%m-%d %H:%M}")

# ── the cached CSV, re-parsed through the real parser ─────────────
d = datetime.strptime(DATE, "%Y-%m-%d").date()
csv = ROOT / "data/raw/bhavcopy" / b01.csv_filename(d)
print(f"  raw CSV   {csv}")
if not csv.exists():
    print("            ABSENT — it was deleted, which is itself the finding:")
    print("            download_all() only re-fetches a date whose CSV is gone,")
    print("            so a deleted file is how a revised bhavcopy gets in.")
    sys.exit(2)
print(f"            mtime {pd.Timestamp(csv.stat().st_mtime, unit='s'):%Y-%m-%d %H:%M}")

src = b01.parse_bhavcopy_csv(csv, d)
src = src[src["symbol"] == SYMBOL]
if src.empty:
    sys.exit(f"{SYMBOL} not present in {csv.name} (not EQ series that day?)")
src = src.iloc[0]

# ── field by field ────────────────────────────────────────────────
print(f"\n  {'field':<14}{'parquet':>16}{'re-parsed CSV':>18}   verdict")
print("  " + "-" * 66)
differ = []
for c in ([FIELD] if FIELD else COLS):
    if c not in df.columns:
        continue
    pv, sv = row.get(c), src.get(c)
    same = (pd.isna(pv) and pd.isna(sv)) or (
        pd.notna(pv) and pd.notna(sv) and abs(float(pv) - float(sv)) < 1e-9)
    if not same:
        differ.append((c, pv, sv))
    print(f"  {c:<14}{pv!r:>16}{sv!r:>18}   {'match' if same else '<-- DIFFERS'}")

print()
if not differ:
    print("  The parquet agrees with the CSV as first downloaded, field for field.")
    print("  Nothing has been corrupted between source and disk. If this bar still")
    print("  disagrees with what was published, the published value came from a")
    print("  DIFFERENT source state — a bhavcopy revised before the download that")
    print("  produced this CSV, or a bar the live run read before NSE settled it.")
else:
    print(f"  {len(differ)} field(s) differ between the CSV and the parquet built")
    print("  from it. The source did not move; 01b changed the value on the way")
    print("  to disk. Two writers can do that:")
    print("    - the delivery injection, which wrote latest_day's delivery_pct")
    print("      onto the symbol's LAST ROW whatever date that row held")
    print("    - a change to parse_bhavcopy_csv's column mapping between runs")
    print("      (git log -p -- engine/01b_download_bhavcopy.py)")

# ── the injection specifically ────────────────────────────────────
is_last = df["date"].iloc[-1] == D
print(f"\n  is this the symbol's last bar? {is_last}")
if is_last:
    print("    Relevant: the old delivery injection targeted df.index[-1]")
    print("    unconditionally. If the run's latest_day was a LATER session than")
    print("    this bar, this row was given another session's delivery_pct on")
    print("    every run. Compare delivery_pct above — a mismatch there, on the")
    print("    last bar, is that bug and not a revision.")
else:
    print("    The injection only ever wrote to the last row, so it cannot")
    print("    explain a change on this bar.")
