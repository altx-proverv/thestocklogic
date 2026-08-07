"""
ATLAS / TSL — Active Zone Propagation
=====================================
ROOT CAUSE THIS FIXES
---------------------
02b_smc_signals.detect_order_blocks() writes ob_high/ob_low ONLY on the candle
that forms the order block. detect_fvgs() does the same for fvg_high/fvg_low.
Neither is carried forward. Only the BOOLEANS (near_demand_ob, price_in_bull_fvg)
propagate.

03b_score.py then does:
    has_ob = ob_entry_high.notna() & ob_entry_low.notna()
    entry_ref = ob_entry_high.where(has_ob, close)

has_ob is False on nearly every row -> entry falls back to CLOSE. The zone-entry
logic in 03b was correct all along; it was simply never reachable because the
zone COORDINATES were dropped one step upstream.

Result: signals labelled "FVG + Demand OB Confluence" priced off previous close,
with a fixed 2% stop and 2R/3R targets that are pure multiplication.

WHAT THIS ADDS
--------------
Forward-filled columns carrying the NEAREST UNMITIGATED zone as of each candle:
    active_demand_ob_high / active_demand_ob_low
    active_supply_ob_high / active_supply_ob_low
    active_bull_fvg_high  / active_bull_fvg_low
    active_bear_fvg_high  / active_bear_fvg_low
    active_zone_source    ("demand_ob" | "bull_fvg" | "supply_ob" | "bear_fvg" | "")
    active_zone_high / active_zone_low   <- resolved by priority ladder
    zone_age_days                        <- candles since the zone formed
    zone_dist_pct                        <- distance from close to zone edge

NO LOOKAHEAD: row i only ever sees zones formed at index <= i.

INTEGRATION
-----------
In 02b_smc_signals.py, after detect_order_blocks() and detect_fvgs():

    from engine.active_zones import add_active_zones
    df = add_active_zones(df)

Then in 03b_score.py, change the two source columns:
    ob_high_col -> df["active_zone_high"]
    ob_low_col  -> df["active_zone_low"]
and stop:
    sl_long  -> df["last_swing_low"]   (already forward-filled, no change needed)
    sl_short -> df["last_swing_high"]

ZONE PRIORITY (first match wins), per the agreed ladder:
    demand OB -> bullish FVG   (longs)
    supply OB -> bearish FVG   (shorts)
VWAP / volume-profile POC are intraday constructs and are NOT available from
daily bhavcopy data. They belong in the live price watcher, not here.
"""

import numpy as np
import pandas as pd

# A zone older than this is stale — price has had too long to invalidate it.
MAX_ZONE_AGE = 60          # trading days
# Ignore zones price has already travelled far past.
MAX_ZONE_DIST_PCT = 15.0   # percent


def _mitigated(zone_low: float, zone_high: float, lows, highs, is_demand: bool) -> bool:
    """A demand zone is mitigated once price trades back down through its low.
    A supply zone is mitigated once price trades up through its high."""
    if is_demand:
        return bool((lows < zone_low).any())
    return bool((highs > zone_high).any())


def _collect(df: pd.DataFrame, flag_col: str, hi_col: str, lo_col: str) -> list:
    """All zones in formation order: [{idx, high, low}]."""
    if flag_col not in df.columns:
        return []
    out = []
    flags = df[flag_col].fillna(False).to_numpy()
    his   = df[hi_col].to_numpy() if hi_col in df.columns else np.full(len(df), np.nan)
    los   = df[lo_col].to_numpy() if lo_col in df.columns else np.full(len(df), np.nan)
    for i in range(len(df)):
        if flags[i] and not np.isnan(his[i]) and not np.isnan(los[i]):
            out.append({"idx": i, "high": float(his[i]), "low": float(los[i])})
    return out


def _active_series(df: pd.DataFrame, zones: list, is_demand: bool):
    """For each row, the nearest still-valid zone on the correct side of price."""
    n     = len(df)
    highs = df["high"].to_numpy()
    lows  = df["low"].to_numpy()
    close = df["close"].to_numpy()

    a_hi  = np.full(n, np.nan)
    a_lo  = np.full(n, np.nan)
    a_age = np.full(n, np.nan)

    if not zones:
        return a_hi, a_lo, a_age

    for i in range(n):
        best = None
        best_dist = np.inf
        c = close[i]
        if not c or np.isnan(c):
            continue

        for z in zones:
            if z["idx"] > i:          # no lookahead
                break
            age = i - z["idx"]
            if age > MAX_ZONE_AGE:
                continue

            # demand must sit at/below price; supply at/above
            if is_demand and z["high"] > c:
                continue
            if not is_demand and z["low"] < c:
                continue

            # discard if price already violated the zone after it formed
            seg = slice(z["idx"] + 1, i + 1)
            if z["idx"] + 1 <= i and _mitigated(z["low"], z["high"],
                                                lows[seg], highs[seg], is_demand):
                continue

            edge = z["high"] if is_demand else z["low"]
            dist = abs(c - edge) / c * 100.0
            if dist > MAX_ZONE_DIST_PCT:
                continue

            if dist < best_dist:
                best, best_dist = z, dist

        if best is not None:
            a_hi[i]  = best["high"]
            a_lo[i]  = best["low"]
            a_age[i] = i - best["idx"]

    return a_hi, a_lo, a_age


def add_active_zones(df: pd.DataFrame) -> pd.DataFrame:
    """Adds forward-filled active zone columns. Expects 02b's OB/FVG columns."""
    if df is None or df.empty:
        return df

    df = df.sort_values("date").reset_index(drop=True)

    demand = _collect(df, "is_demand_ob", "ob_high", "ob_low")
    supply = _collect(df, "is_supply_ob", "ob_high", "ob_low")
    bfvg   = _collect(df, "bullish_fvg",  "fvg_high", "fvg_low")
    sfvg   = _collect(df, "bearish_fvg",  "fvg_high", "fvg_low")

    d_hi, d_lo, d_age = _active_series(df, demand, is_demand=True)
    s_hi, s_lo, s_age = _active_series(df, supply, is_demand=False)
    bf_hi, bf_lo, bf_age = _active_series(df, bfvg, is_demand=True)
    sf_hi, sf_lo, sf_age = _active_series(df, sfvg, is_demand=False)

    df["active_demand_ob_high"] = d_hi
    df["active_demand_ob_low"]  = d_lo
    df["active_supply_ob_high"] = s_hi
    df["active_supply_ob_low"]  = s_lo
    df["active_bull_fvg_high"]  = bf_hi
    df["active_bull_fvg_low"]   = bf_lo
    df["active_bear_fvg_high"]  = sf_hi
    df["active_bear_fvg_low"]   = sf_lo

    # Resolve by priority ladder, respecting the row's own trend direction.
    trend = df.get("structure_trend", pd.Series([""] * len(df))).fillna("").to_numpy()
    n = len(df)
    z_hi  = np.full(n, np.nan)
    z_lo  = np.full(n, np.nan)
    z_age = np.full(n, np.nan)
    z_src = np.array([""] * n, dtype=object)

    for i in range(n):
        bearish = str(trend[i]).lower() == "downtrend"
        if bearish:
            ladder = [("supply_ob", s_hi[i], s_lo[i], s_age[i]),
                      ("bear_fvg",  sf_hi[i], sf_lo[i], sf_age[i])]
        else:
            ladder = [("demand_ob", d_hi[i], d_lo[i], d_age[i]),
                      ("bull_fvg",  bf_hi[i], bf_lo[i], bf_age[i])]
        for name, hi, lo, age in ladder:
            if not np.isnan(hi) and not np.isnan(lo):
                z_hi[i], z_lo[i], z_age[i], z_src[i] = hi, lo, age, name
                break

    df["active_zone_high"]  = np.round(z_hi, 2)
    df["active_zone_low"]   = np.round(z_lo, 2)
    df["active_zone_source"] = z_src
    df["zone_age_days"]     = z_age

    close = df["close"].to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        edge = np.where(np.isnan(z_hi), np.nan, (z_hi + z_lo) / 2.0)
        df["zone_dist_pct"] = np.round(np.abs(close - edge) / close * 100.0, 2)

    return df


def zone_coverage_report(df: pd.DataFrame) -> dict:
    """How often a usable zone exists. Run this before and after the patch —
    'before' should be near 0%."""
    n = len(df)
    have = int(df["active_zone_high"].notna().sum()) if "active_zone_high" in df else 0
    old  = int(df["ob_high"].notna().sum()) if "ob_high" in df else 0
    return {
        "rows": n,
        "rows_with_active_zone": have,
        "active_zone_pct": round(have / n * 100, 1) if n else 0,
        "rows_with_raw_ob_level": old,
        "raw_ob_pct": round(old / n * 100, 1) if n else 0,
    }
