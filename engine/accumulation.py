"""
TSL — Accumulation Screen
==========================
Direction-aware correction to 03b's disqualifier block, plus an accumulation
footprint score for ranking.

WHY THIS EXISTS
---------------
03b's disqualifiers were written for momentum/swing trading. Two of them reject
the exact setups an accumulation strategy is built to find:

  line  89   rvol < 0.5      -> "very_low_volume"
             Quiet tape is the ENTRY SIGNAL. Retail has stopped watching.

  line 111   adx_ranging==1  -> "adq_ranging_market"
             Consolidation is what accumulation LOOKS like. A stock basing
             while institutions take delivery is the thesis, not a reject.

Both are undone for LONGS only. Shorts are intraday hedges and keep the
original momentum logic, where those disqualifiers are correct.

THE FOOTPRINT
-------------
Low volume + high delivery is the strongest combination available in this
dataset for the thing being looked for: somebody is taking delivery while
nobody is trading. Neither leg means much alone.

  discount        5-20% off the 52-week high. Off enough to matter, not broken.
  in_discount     below the equilibrium of its own range
  quiet           rvol at or below average
  delivery        delivery_pct above threshold -- real buying, not churn
  institutional   institutional_buying / high_delivery flags from 02b
  basing          adx_ranging, or weekly trend not down
  structure       an active zone exists to rest a GTT at

MODE
----
REQUIRE_ACCUMULATION_FOR_LONGS = False by default. The screen TAGS rows
(is_accumulation, accumulation_score) without filtering, so the distribution
can be inspected against real data before it gates anything. Flip to True once
the counts look right.
"""

import numpy as np
import pandas as pd

# Undone for longs. These are momentum-era rejects.
ACCUMULATION_KILLERS = ("very_low_volume", "adq_ranging_market")

DISCOUNT_MIN_PCT = 5.0     # at least this far off the 52w high
DISCOUNT_MAX_PCT = 20.0    # but not a broken chart
MAX_RVOL         = 1.0     # at or below average volume
MIN_DELIVERY_PCT = 50.0    # delivery-based buying

REQUIRE_ACCUMULATION_FOR_LONGS = False


def _col(df, name, default):
    if name in df.columns:
        return df[name].fillna(default)
    return pd.Series(default, index=df.index)


def apply_accumulation_screen(df: pd.DataFrame,
                              require: bool = REQUIRE_ACCUMULATION_FOR_LONGS) -> pd.DataFrame:
    """Call AFTER 03b's disqualifier block, BEFORE scoring."""
    if df is None or df.empty:
        return df

    direction = (df["direction"].astype(str).str.lower()
                 if "direction" in df.columns
                 else pd.Series("long", index=df.index))
    is_long = direction == "long"

    # ── 1. Undo the momentum-era rejects, longs only ──────────────
    if "disqualify_reason" in df.columns:
        undo = (is_long
                & df["disqualified"].fillna(False)
                & df["disqualify_reason"].isin(ACCUMULATION_KILLERS))
        n_undo = int(undo.sum())
        df.loc[undo, "disqualified"]      = False
        df.loc[undo, "disqualify_reason"] = ""
        df.attrs["accumulation_undone"] = n_undo

    # ── 2. Footprint components ───────────────────────────────────
    off_high  = _col(df, "pct_from_52w_high", 0.0)      # negative = below high
    rvol      = _col(df, "rvol", 1.0)
    delivery  = _col(df, "delivery_pct", 0.0)
    inst_buy  = _col(df, "institutional_buying", 0).astype(float)
    hi_del    = _col(df, "high_delivery", 0).astype(float)
    in_disc   = _col(df, "in_discount", 0).astype(float)
    ranging   = _col(df, "adx_ranging", 0).astype(float)
    wk_bear   = _col(df, "weekly_bearish", 0).astype(float)
    zone_hi   = _col(df, "active_zone_high", np.nan)

    discount = (off_high <= -DISCOUNT_MIN_PCT) & (off_high >= -DISCOUNT_MAX_PCT)
    quiet    = rvol <= MAX_RVOL
    deliv_ok = delivery >= MIN_DELIVERY_PCT
    basing   = (ranging == 1) | (wk_bear == 0)
    has_zone = zone_hi.notna()

    # Core thesis: quiet tape AND delivery buying AND available at a discount.
    core = discount & quiet & deliv_ok
    df["is_accumulation"] = (is_long & core & has_zone).fillna(False)

    # ── 3. Footprint score, for RANKING only (never a gate) ───────
    # Weighted toward the combination that carries the signal, not any one leg.
    score = (
        (quiet & deliv_ok).astype(float) * 30.0   # the combination itself
        + discount.astype(float)         * 20.0
        + in_disc                        * 15.0
        + inst_buy                       * 15.0
        + hi_del                         * 10.0
        + basing.astype(float)           * 10.0
    )
    df["accumulation_score"] = np.where(is_long, score.round(1), np.nan)

    # ── 4. Optional enforcement ───────────────────────────────────
    if require:
        fail = (is_long
                & ~df["is_accumulation"]
                & ~df["disqualified"].fillna(False))
        df.loc[fail, "disqualified"]      = True
        df.loc[fail, "disqualify_reason"] = "not_accumulation_setup"

    return df


def screen_report(df: pd.DataFrame) -> dict:
    """Distribution, for inspecting before enforcement is switched on."""
    if df is None or df.empty or "is_accumulation" not in df.columns:
        return {}
    direction = (df["direction"].astype(str).str.lower()
                 if "direction" in df.columns
                 else pd.Series("long", index=df.index))
    longs = df[direction == "long"]
    if longs.empty:
        return {"longs": 0}

    acc = longs[longs["is_accumulation"]]
    return {
        "longs":               len(longs),
        "accumulation":        len(acc),
        "accumulation_pct":    round(len(acc) / len(longs) * 100, 1),
        "undone_disqualifiers": df.attrs.get("accumulation_undone", 0),
        "mean_score":          round(float(longs["accumulation_score"].mean()), 1)
                               if longs["accumulation_score"].notna().any() else 0,
        "top_score":           round(float(longs["accumulation_score"].max()), 1)
                               if longs["accumulation_score"].notna().any() else 0,
    }
