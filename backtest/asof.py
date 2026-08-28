"""
BACKTEST — the as-of guard
==========================
Separate from the seam in backtest/store.py, and asserting a different
property. The seam stops a backtest WRITING into the live record. This stops a
backtest READING the future: a candidate generated for date D must not depend
on any bar after D.

WHY THIS NEEDS ITS OWN PROOF, NOT AN ARGUMENT
---------------------------------------------
The obvious optimisation is to compute indicators once over each symbol's full
history and slice the result per date. Measured, that is WRONG here, and wrong
silently. Comparing the full-history row at D against the sliced-to-D last row
on RELIANCE, 17 columns disagree:

    ob_high, ob_low, equilibrium, last_swing_high, last_swing_low,
    is_demand_ob, is_supply_ob, bearish_fvg, fvg_high, fvg_low,
    fvg_size_pct, bear_fvg_filled, active_bear_fvg_high,
    active_bear_fvg_low, bull_liq_sweep, in_discount, in_premium

Swing points are confirmed by the bars that follow them, order blocks are
identified retroactively, and FVG fill state depends on later price. So a
cached full-history frame has tomorrow's confirmation baked into today's row.
A backtest built on it would enter on zones that did not exist yet and report
a hit rate that cannot be traded.

THE TEST IS DESTRUCTIVE, NOT DECLARATIVE
----------------------------------------
plant_future() replaces every bar after D with absurd prices. If generation
for D is genuinely as-of, its output cannot move. Anything that reads past D
changes its answer, and the assertion fails. This is stronger than inspecting
call sites: it does not care HOW the future is read.
"""

import logging
import numpy as np
import pandas as pd

log = logging.getLogger("BACKTEST-ASOF")

PRICE_COLS = ("open", "high", "low", "close", "vwap", "prev_close")


def slice_asof(df: pd.DataFrame, d, date_col: str = "date") -> pd.DataFrame:
    """Bars up to and including d. The only sanctioned way to cut history."""
    if df is None or df.empty:
        return df
    out = df[df[date_col] <= pd.Timestamp(d)]
    return out.copy()


def plant_future(df: pd.DataFrame, d, factor: float = 7.3,
                 date_col: str = "date") -> pd.DataFrame:
    """
    Return df with every bar AFTER d replaced by garbage.

    Deliberately absurd rather than subtly wrong: a x7.3 price step with the
    volume blown out will move any indicator that touches it, so a guard that
    passes here is not passing by luck or rounding. Bars on or before d are
    untouched, so a correct as-of computation cannot notice the difference.
    """
    if df is None or df.empty:
        return df
    out = df.copy()
    future = out[date_col] > pd.Timestamp(d)
    if not future.any():
        return out
    for c in PRICE_COLS:
        if c in out.columns:
            out.loc[future, c] = out.loc[future, c] * factor
    if "volume" in out.columns:
        out.loc[future, "volume"] = out.loc[future, "volume"] * 1000
    # Break monotonic structure too, so a rank/percentile that peeks moves.
    if "high" in out.columns and "low" in out.columns:
        out.loc[future, "high"] = out.loc[future, "high"] * 1.5
        out.loc[future, "low"] = out.loc[future, "low"] * 0.5
    return out


def frames_equal(a: pd.DataFrame, b: pd.DataFrame, cols=None) -> list:
    """Columns whose values differ. NaN == NaN. Returns [] when identical."""
    if a is None or b is None:
        return ["<one frame is None>"]
    if len(a) != len(b):
        return [f"<row count {len(a)} vs {len(b)}>"]
    cols = cols or [c for c in a.columns if c in b.columns]
    bad = []
    for c in cols:
        va, vb = a[c].reset_index(drop=True), b[c].reset_index(drop=True)
        if pd.api.types.is_numeric_dtype(va) and pd.api.types.is_numeric_dtype(vb):
            if not np.allclose(va.astype(float), vb.astype(float),
                               rtol=1e-9, atol=1e-12, equal_nan=True):
                bad.append(c)
        else:
            if not va.astype(str).equals(vb.astype(str)):
                bad.append(c)
    return bad


def assert_asof(generate_for_date, frames: dict, market: pd.DataFrame,
                dates: list, label: str = "") -> dict:
    """
    THE ASSERTION. For each date D:

        clean   = generate_for_date(D, frames,          market)
        planted = generate_for_date(D, frames_with_future_garbage, market)

    and require the two to be identical. `generate_for_date` must accept
    (date, {symbol: df}, market_df) and return a DataFrame of candidates.

    Returns {'passed': bool, 'per_date': [...]}. Raises nothing -- the caller
    decides, because a failure here must stop a run rather than be logged.
    """
    per_date, passed = [], True

    for d in dates:
        clean = generate_for_date(d, frames, market)

        planted_frames = {s: plant_future(df, d) for s, df in frames.items()}
        planted_market = plant_future(market, d)
        planted = generate_for_date(d, planted_frames, planted_market)

        if clean is None and planted is None:
            per_date.append((d, 0, "both empty")); continue

        n_clean = 0 if clean is None else len(clean)
        n_plant = 0 if planted is None else len(planted)

        if n_clean != n_plant:
            per_date.append((d, -1, f"candidate count moved {n_clean} -> {n_plant}"))
            passed = False
            continue

        if n_clean == 0:
            per_date.append((d, 0, "no candidates either way"))
            continue

        key = ["symbol", "direction"] if "direction" in clean.columns else ["symbol"]
        a = clean.sort_values(key).reset_index(drop=True)
        b = planted.sort_values(key).reset_index(drop=True)
        diff = frames_equal(a, b)
        if diff:
            passed = False
            per_date.append((d, len(diff), f"columns moved: {diff[:6]}"))
        else:
            per_date.append((d, 0, f"{n_clean} candidates, identical"))

    return {"passed": passed, "per_date": per_date, "label": label}


def report(result: dict) -> None:
    print("=" * 72)
    print(f"AS-OF GUARD — future planted after each date {result.get('label','')}")
    print("  every bar after D multiplied by 7.3, highs x1.5, lows x0.5,")
    print("  volume x1000. As-of generation cannot notice.")
    print("=" * 72)
    for d, n, msg in result["per_date"]:
        mark = "ok  " if n == 0 else "FAIL"
        print(f"  {mark} {str(pd.Timestamp(d).date())}  {msg}")
    print("-" * 72)
    print("AS-OF:", "HOLDS — generation cannot see past D"
          if result["passed"] else "*** LEAKS — results are not tradeable ***")
