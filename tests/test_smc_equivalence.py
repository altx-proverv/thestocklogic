#!/usr/bin/env python3
"""
THE FAST DETECTORS MUST MATCH THE REFERENCE ONES, BAR FOR BAR
=============================================================
detect_order_blocks and detect_fvgs were rewritten to read and write numpy
arrays instead of doing `df["close"].iloc[i]` per bar and
`df.loc[df.index[i], col] = True` per hit. That was 67% of
compute_smc_signals -- 275 of 409 ms per symbol -- and both are ~22x faster
rewritten.

The algorithms are unchanged: same iteration order, same comparisons, same
state. So the outputs should be BIT-identical, not approximately equal, and
this asserts exactly that -- rtol=0, atol=0, across every column each detector
writes, for every parquet on disk.

WHY IT IS A HARNESS AND NOT A ONE-OFF
-------------------------------------
These two functions decide what gets published: near_demand_ob and
price_in_bull_fvg feed the smc score and the no_smc_signal disqualifier, so a
one-bar disagreement can add or remove a real signal. A rewrite verified once,
on one machine, against one vintage of data, is verified for that machine and
that vintage.

This checkout has 243 symbols of ~850 clean bars. The box has 495 including
recent listings with short history and series with gaps, which is a different
shape -- so run it THERE before trusting the conversion, and again after any
edit to either implementation.

    python3 tests/test_smc_equivalence.py             # every parquet on disk
    python3 tests/test_smc_equivalence.py --limit 40  # a quick subset
    python3 tests/test_smc_equivalence.py --timing    # also report the speedup

It is deliberately slow: the reference implementation is the slow one, and
there is no way to check it without running it.
"""

import sys
import glob
import time
import argparse
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("smc", ROOT / "engine/02b_smc_signals.py")
smc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smc)

OB_COLS = ("is_demand_ob", "is_supply_ob", "ob_high", "ob_low",
           "ob_mitigated", "near_demand_ob", "near_supply_ob")
FVG_COLS = ("bullish_fvg", "bearish_fvg", "fvg_high", "fvg_low",
            "fvg_size_pct", "price_in_bull_fvg", "price_in_bear_fvg")

PAIRS = (("order blocks", smc._detect_order_blocks_reference,
          smc.detect_order_blocks, OB_COLS),
         ("FVGs", smc._detect_fvgs_reference, smc.detect_fvgs, FVG_COLS))


def compare(a: pd.DataFrame, b: pd.DataFrame, cols) -> list:
    """Columns that differ at all. NaN equals NaN; nothing else is tolerated."""
    bad = []
    for c in cols:
        if c not in a.columns or c not in b.columns:
            bad.append(f"{c}<missing>")
            continue
        x, y = a[c], b[c]
        if x.dtype == bool or y.dtype == bool:
            if not (x.astype(bool) == y.astype(bool)).all():
                bad.append(c)
        elif not np.allclose(x.astype(float), y.astype(float),
                             rtol=0, atol=0, equal_nan=True):
            bad.append(c)
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timing", action="store_true")
    ap.add_argument("--stocks-dir", default=str(ROOT / "data/processed/stocks"))
    a = ap.parse_args()

    files = sorted(glob.glob(str(Path(a.stocks_dir) / "*.parquet")))
    if a.limit:
        files = files[:a.limit]
    if not files:
        print(f"no parquets under {a.stocks_dir} — nothing to compare")
        return 1

    print("SMC DETECTOR EQUIVALENCE — fast path vs reference, rtol=0 atol=0")
    print(f"  {len(files)} symbol(s) from {a.stocks_dir}")
    print("-" * 78)

    ok = 0
    checked = 0
    skipped = 0
    failures = {}
    t_ref = t_fast = 0.0
    bars_min = bars_max = None

    for f in files:
        sym = Path(f).stem
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
        except Exception as e:
            print(f"  SKIP {sym}: unreadable ({e})")
            skipped += 1
            continue
        # Short and gappy series are the interesting ones -- a recent listing is
        # exactly the shape this checkout does not have. Do not skip them.
        if len(df) < 10:
            skipped += 1
            continue
        bars_min = len(df) if bars_min is None else min(bars_min, len(df))
        bars_max = len(df) if bars_max is None else max(bars_max, len(df))

        checked += 1
        clean = True
        for label, ref, fast, cols in PAIRS:
            t = time.time(); A = ref(df.copy());  t_ref += time.time() - t
            t = time.time(); B = fast(df.copy()); t_fast += time.time() - t
            bad = compare(A, B, cols)
            if bad:
                clean = False
                failures.setdefault(label, []).append((sym, len(df), bad))
        if clean:
            ok += 1

    print(f"  bars per symbol: {bars_min} to {bars_max}")
    print(f"  identical: {ok}/{checked}" + (f"   ({skipped} skipped)" if skipped else ""))

    if failures:
        print()
        for label, rows in failures.items():
            print(f"  {label}: {len(rows)} symbol(s) differ")
            for sym, n, bad in rows[:12]:
                print(f"    {sym:<14} {n:>5} bars   {', '.join(bad)}")
            if len(rows) > 12:
                print(f"    ... and {len(rows)-12} more")

    if a.timing and checked:
        print()
        print(f"  reference {t_ref/checked*1000:>7.1f} ms/symbol")
        print(f"  fast      {t_fast/checked*1000:>7.1f} ms/symbol"
              f"   ({t_ref/max(t_fast,1e-9):.1f}x)")

    print("-" * 78)
    good = not failures and checked > 0
    print("SMC EQUIVALENCE:", "identical" if good else "*** DIVERGED ***")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
