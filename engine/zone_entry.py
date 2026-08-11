"""
TSL — Zone-Based Entry & Structural Stop
========================================
Replaces the entry/stop/target block in 03b_score.py (approx lines 330-406).

WHAT CHANGED AND WHY
--------------------
OLD:  entry_ref = close (the OB branch existed but never fired -- ob_high/ob_low
      were NaN on almost every row because 02b wrote them only on the formation
      candle and never carried them forward)
      entry_low/high = close * 0.998 / 1.002   (a 0.4% tolerance band)
      sl = close * (1 - clip(atr_pct, MIN, MAX))
      target_1/2 = entry +/- 2R / 3R

      Consequence: signals named "FVG + Demand OB Confluence" were priced off
      previous close with a fixed 2% stop and targets that were multiplication,
      not analysis. Entry rarely activated -- price gapped past it or opened
      away from it.

NEW:  entry  = the zone edge price must retrace INTO
                 LONG  -> active_zone_high (retracement DOWN into demand)
                 SHORT -> active_zone_low  (retracement UP into supply)
      band   = [active_zone_low, active_zone_high] -- the real zone, not a
               percentage wrapper. Width comes from structure.
      stop   = last_swing_low (LONG) / last_swing_high (SHORT), forced outside
               the zone so a fill that immediately fails is cleanly invalidated
      qty    = risk-based, Rs3,000 budget, Rs1L notional cap, floor to mult of 5
      target = NONE. Winners are held and trailed. rr_1/rr_2/sl_pct/target_1/
               target_2 are dropped -- meaningless with an open target.

STOP-DISTANCE BAND acts as a QUALITY FILTER, not just a guard:
  < 1.5%  reject -- inside noise, and the notional cap would bind and break
                    the Rs3k risk standardisation
  > 7.0%  reject -- position too small for costs to be worth it
Band verified against live data (40 symbols): distances cluster 2-9%, median
~5.3%. 1.5-7% passes 16/26 usable setups.

INTEGRATION (03b_score.py)
--------------------------
Replace the entry/sl/target block with:

    from engine.zone_entry import compute_zone_entries
    df = compute_zone_entries(df)

Requires active_zone_high / active_zone_low / active_zone_source from
engine.active_zones.add_active_zones(), and last_swing_low / last_swing_high
which 02b already forward-fills.
"""

import numpy as np
import pandas as pd

RISK_PER_TRADE = 3000.0
MAX_NOTIONAL   = 100000.0
QTY_MULTIPLE   = 5
MIN_STOP_PCT   = 1.5     # percent
MAX_STOP_PCT   = 7.0     # percent
STOP_BUFFER    = 0.002   # push stop just outside the zone edge

# Reject a zone price has already run too far from -- it will not be revisited
# intraday, so the signal is untradeable even though the zone is technically live.
MAX_ENTRY_DIST_PCT = 8.0


def _floor_to_multiple(n: float, m: int = QTY_MULTIPLE) -> int:
    if not np.isfinite(n) or n <= 0:
        return 0
    return int(np.floor(n / m) * m)


def compute_zone_entries(df: pd.DataFrame) -> pd.DataFrame:
    """Adds zone-based entry, structural stop, and risk-based qty. Rows that
    fail validation get entry_valid=False and a reject_reason -- they are kept
    (not dropped) so the reject distribution stays visible for tuning."""
    if df is None or df.empty:
        return df

    n = len(df)
    close = df["close"].to_numpy(dtype=float)

    def col(name, default=np.nan):
        return (df[name].to_numpy(dtype=float) if name in df.columns
                else np.full(n, default, dtype=float))

    z_hi = col("active_zone_high")
    z_lo = col("active_zone_low")
    sw_lo = col("last_swing_low")
    sw_hi = col("last_swing_high")

    direction = (df["direction"].astype(str).str.lower().to_numpy()
                 if "direction" in df.columns else np.array(["long"] * n))

    entry      = np.full(n, np.nan)
    entry_low  = np.full(n, np.nan)
    entry_high = np.full(n, np.nan)
    stop       = np.full(n, np.nan)
    stop_pct   = np.full(n, np.nan)
    qty        = np.zeros(n, dtype=int)
    risk_act   = np.full(n, np.nan)
    notional   = np.full(n, np.nan)
    entry_dist = np.full(n, np.nan)
    valid      = np.zeros(n, dtype=bool)
    reason     = np.array([""] * n, dtype=object)

    for i in range(n):
        c = close[i]
        is_long = direction[i] != "short"

        if not np.isfinite(c) or c <= 0:
            reason[i] = "no close"
            continue
        if not np.isfinite(z_hi[i]) or not np.isfinite(z_lo[i]):
            reason[i] = "no active zone"
            continue

        # ENTRY = the edge price must retrace into
        e = z_hi[i] if is_long else z_lo[i]
        lo, hi = min(z_lo[i], z_hi[i]), max(z_lo[i], z_hi[i])

        # STOP = swing extreme, forced outside the zone
        if is_long:
            raw = sw_lo[i]
            s = min(raw, lo) if np.isfinite(raw) else lo
            s = s * (1 - STOP_BUFFER)
        else:
            raw = sw_hi[i]
            s = max(raw, hi) if np.isfinite(raw) else hi
            s = s * (1 + STOP_BUFFER)

        if not np.isfinite(s) or s <= 0:
            reason[i] = "no structural stop"
            continue
        if is_long and s >= e:
            reason[i] = "stop not below entry"
            continue
        if not is_long and s <= e:
            reason[i] = "stop not above entry"
            continue

        sp = abs(e - s) / e * 100.0
        d  = abs(c - e) / c * 100.0

        entry[i], entry_low[i], entry_high[i] = round(e, 2), round(lo, 2), round(hi, 2)
        stop[i], stop_pct[i], entry_dist[i] = round(s, 2), round(sp, 2), round(d, 2)

        if sp < MIN_STOP_PCT:
            reason[i] = f"stop {sp:.2f}% too tight (min {MIN_STOP_PCT}%)"
            continue
        if sp > MAX_STOP_PCT:
            reason[i] = f"stop {sp:.2f}% too wide (max {MAX_STOP_PCT}%)"
            continue
        if d > MAX_ENTRY_DIST_PCT:
            reason[i] = f"entry {d:.2f}% away (max {MAX_ENTRY_DIST_PCT}%) -- unreachable"
            continue

        risk_per_share = abs(e - s)
        q = _floor_to_multiple(min(RISK_PER_TRADE / risk_per_share, MAX_NOTIONAL / e))
        if q < QTY_MULTIPLE:
            reason[i] = f"qty {q} below min {QTY_MULTIPLE} -- too expensive for Rs{RISK_PER_TRADE:,.0f}"
            continue

        qty[i]      = q
        risk_act[i] = round(q * risk_per_share, 2)
        notional[i] = round(q * e, 2)
        valid[i]    = True
        reason[i]   = "ok"

    df = df.copy()
    df["entry_ref"]     = np.round(entry, 2)
    df["entry_low"]     = np.round(entry_low, 2)
    df["entry_high"]    = np.round(entry_high, 2)
    df["sl"]            = np.round(stop, 2)
    df["stop_pct"]      = np.round(stop_pct, 2)
    df["entry_dist_pct"] = np.round(entry_dist, 2)
    df["qty"]           = qty
    df["risk_inr"]      = np.round(risk_act, 2)
    df["notional"]      = np.round(notional, 2)
    df["entry_valid"]   = valid
    df["reject_reason"] = reason
    df["product"]       = np.where(direction == "short", "MIS", "CNC")

    # Explicitly retired as TRADE levels -- open target, trailing exit.
    # A fixed 2R/3R yardstick still exists for MEASUREMENT only; it is applied
    # at push/scoring time, never here. See measurement_targets() below.
    for dead in ("target_1", "target_2", "rr_1", "rr_2", "sl_pct"):
        if dead in df.columns:
            df.drop(columns=[dead], inplace=True)

    return df


# Measurement yardstick. NOT trade levels.
#
# The live strategy holds with an open target and trails winners, so
# compute_zone_entries() drops target_1/target_2 above. But scoring signal
# accuracy needs SOME fixed reference, otherwise every outcome is "OPEN"
# forever and there is no accuracy record at all.
#
# 06_push_supabase.py already documented the intent -- "2R / 3R off the
# structural stop, not off the previous close" -- but nothing computed it once
# the columns were dropped, so target_1 pushed as NULL and both consumers
# (update_outcomes.py, trade_review.py) skipped every signal on their
# `t1 <= 0 -> SKIP` guard. Defined here, in one place, so the two consumers and
# the push agree on what is being measured.
#
# ATLAS never reads these. atlas/signal/market_open.py builds its signal dict
# without target_1/target_2 and atlas_entry.enter_trade() sizes off the
# structural stop alone.
MEASURE_R1 = 2.0
MEASURE_R2 = 3.0


def measurement_targets(entry: float, stop: float, direction: str = "long") -> tuple:
    """(target_1, target_2) at 2R/3R off the structural stop, for accuracy
    measurement only. Returns (None, None) if the geometry is unusable."""
    try:
        e, s = float(entry), float(stop)
    except (TypeError, ValueError):
        return None, None
    if not np.isfinite(e) or not np.isfinite(s) or e <= 0 or s <= 0:
        return None, None

    is_long = str(direction or "long").strip().lower() != "short"
    r = (e - s) if is_long else (s - e)
    if r <= 0:                     # stop on the wrong side of entry
        return None, None

    if is_long:
        return round(e + MEASURE_R1 * r, 2), round(e + MEASURE_R2 * r, 2)
    return round(e - MEASURE_R1 * r, 2), round(e - MEASURE_R2 * r, 2)


def reject_report(df: pd.DataFrame) -> dict:
    """Distribution of reject reasons -- watch this when tuning the band."""
    if "reject_reason" not in df.columns:
        return {}
    counts = df["reject_reason"].value_counts().to_dict()
    total = len(df)
    return {"total": total,
            "valid": int(df["entry_valid"].sum()),
            "valid_pct": round(df["entry_valid"].sum() / total * 100, 1) if total else 0,
            "reasons": counts}
