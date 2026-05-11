"""
THE STOCK LOGIC — Stage 3: Signal Scoring Engine
=================================================
Scores every Nifty 100 stock every trading day from 0-100.
Applies hard disqualifiers. Selects top 7 for daily playbook.

Scoring dimensions (total 100 pts):
  Trend     25pts : EMA alignment + price above VWAP
  Momentum  20pts : RSI zone + MACD histogram
  Volume    20pts : RVOL + OBV direction
  Viability 20pts : ATR trade viability + VIX environment
  Sentiment 15pts : A/D ratio + market regime + delivery

Hard disqualifiers (auto-reject regardless of score):
  - RVOL < 0.7x average volume
  - VIX > 25 (all longs rejected)
  - Stock already up > 4% today
  - Trade not viable (ATR out of range)
  - Warmup period (EMA 200 not yet valid)

Reads : data/processed/indicators/*.parquet
Writes: data/processed/signals/daily_signals.parquet
        data/processed/signals/playbooks/YYYY-MM-DD.parquet

Run: python3 engine/03_signals.py
"""

import os, sys, logging, warnings
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
from tqdm import tqdm

warnings.filterwarnings("ignore")

Path("reports").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("reports/03_signals.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

INDICATORS_DIR = Path("data/processed/indicators")
SIGNALS_DIR    = Path("data/processed/signals")
PLAYBOOKS_DIR  = Path("data/processed/signals/playbooks")
MARKET_FILE    = Path("data/processed/market.parquet")

# How many stocks in the daily playbook
TOP_N          = 7
MIN_SCORE      = 65       # minimum score to qualify
SL_PCT         = 0.03     # 3% stop loss
RR_RATIO       = 3.0      # reward:risk ratio
TARGET_PCT     = SL_PCT * RR_RATIO   # 9% target


# ══════════════════════════════════════════════════════════════════
# SCORING ENGINE
# ══════════════════════════════════════════════════════════════════

def score_trend(row) -> float:
    """
    Max 25 points.
    EMA alignment (0-15) + price above VWAP (5) + EMA cross bonus (5)
    """
    score = 0.0

    alignment = row.get("ema_alignment", "warmup")
    align_scores = {
        "full_bull":  15.0,
        "bull":       12.0,
        "weak_bull":   7.0,
        "neutral":     3.0,
        "weak_bear":   0.0,
        "bear":        0.0,
        "full_bear":   0.0,
        "warmup":      0.0,
    }
    score += align_scores.get(alignment, 0.0)

    # Price above VWAP proxy
    if row.get("price_above_vwap", 0) == 1:
        score += 5.0

    # EMA 9/21 bullish cross today (bonus — fresh signal)
    if row.get("ema_cross", 0) == 1:
        score += 5.0

    return min(score, 25.0)


def score_momentum(row) -> float:
    """
    Max 20 points.
    RSI zone (0-10) + MACD histogram (0-10)
    """
    score = 0.0
    score += min(row.get("rsi_score", 0.0), 10.0)
    score += min(row.get("macd_score", 0.0), 10.0)
    return min(score, 20.0)


def score_volume(row) -> float:
    """
    Max 20 points.
    RVOL (0-10) + OBV direction (0-10)
    """
    score = 0.0
    score += min(row.get("rvol_score", 0.0), 10.0)
    score += min(row.get("obv_score", 0.0), 10.0)

    # Delivery bonus: high delivery on up day = institutional buying
    if row.get("delivery_up_day", 0) == 1:
        score += 3.0  # bonus — can push past 20 before cap
    if row.get("delivery_above_avg", 0) == 1:
        score += 2.0

    return min(score, 20.0)


def score_viability(row) -> float:
    """
    Max 20 points.
    ATR trade viability (0-15) + VIX environment (0-5)
    """
    score = 0.0
    score += min(row.get("viability_score", 0.0), 15.0)

    # VIX environment
    vix = row.get("vix_close", np.nan)
    if pd.isna(vix):
        score += 3.0   # unknown — partial credit
    elif vix < 13:
        score += 5.0   # very low fear — best environment
    elif vix < 17:
        score += 5.0   # normal
    elif vix < 20:
        score += 3.0   # slightly elevated
    elif vix < 25:
        score += 1.0   # elevated — caution
    else:
        score += 0.0   # > 25 caught by disqualifier

    return min(score, 20.0)


def score_sentiment(row) -> float:
    """
    Max 15 points.
    Market regime (0-5) + A/D ratio (0-5) + momentum (0-5)
    """
    score = 0.0

    # Market regime
    regime = row.get("market_regime", "unknown")
    regime_scores = {
        "bull":     5.0,
        "sideways": 2.0,
        "bear":     0.0,
        "unknown":  2.0,
        "warmup":   0.0,
    }
    score += regime_scores.get(regime, 2.0)

    # A/D ratio
    score += min(row.get("ad_score", 0.0), 5.0)

    # Price momentum (5-day)
    mom5 = row.get("mom5", 0.0)
    if pd.isna(mom5):
        mom5 = 0.0
    if 1.0 <= mom5 <= 5.0:
        score += 5.0   # healthy momentum
    elif 0.0 <= mom5 < 1.0:
        score += 2.0   # slight positive
    elif mom5 > 5.0:
        score += 1.0   # too hot — overextended
    else:
        score += 0.0   # negative momentum

    return min(score, 15.0)


def apply_disqualifiers(row) -> tuple:
    """
    Returns (is_disqualified: bool, reason: str)
    Hard rules — if any fires, stock is excluded regardless of score.
    """
    # Warmup period
    if row.get("is_warmup", True):
        return True, "warmup"

    # RVOL too low — no participation
    if row.get("rvol_disqualify", 0) == 1:
        return True, "low_rvol"

    # VIX panic — no longs
    if row.get("vix_disqualify", 0) == 1:
        return True, "vix_panic"

    # FOMO — already up too much
    if row.get("fomo_disqualify", 0) == 1:
        return True, "fomo"

    # Trade not viable — ATR doesn't support 3:1 RR
    if row.get("trade_viable", 0) == 0:
        return True, "not_viable"

    # EMA alignment in warmup
    if row.get("ema_alignment", "warmup") == "warmup":
        return True, "ema_warmup"

    return False, ""


def compute_entry_target_sl(row) -> dict:
    """
    Computes entry zone, target, and stop loss levels.

    Entry  : close price (signal generated at EOD, enter next day open)
    SL     : entry × (1 - 3%)
    Target : entry × (1 + 9%)  — 3:1 RR

    For bear setups (short signals), we flip direction.
    Currently only long setups for Phase 1.
    """
    close = row.get("close", 0.0)
    atr   = row.get("atr", close * 0.02)

    # Entry zone: close ± 0.3% (allows for next-day open variation)
    entry_low  = round(close * 0.997, 2)
    entry_high = round(close * 1.003, 2)

    # SL: 3% below entry (using close as proxy)
    sl = round(close * (1 - SL_PCT), 2)

    # Target: 9% above entry (3:1 RR)
    target = round(close * (1 + TARGET_PCT), 2)

    # ATR-based SL as sanity check (use whichever is tighter)
    atr_sl = round(close - (1.5 * atr), 2)
    sl = max(sl, atr_sl)  # use higher of the two (tighter stop)

    return {
        "entry_low":   entry_low,
        "entry_high":  entry_high,
        "target":      target,
        "sl":          sl,
        "rr_ratio":    RR_RATIO,
        "sl_pct":      round((close - sl) / close * 100, 2),
        "target_pct":  round((target - close) / close * 100, 2),
    }


def determine_setup_name(row) -> str:
    """Human-readable setup name based on indicator combination."""
    alignment = row.get("ema_alignment", "")
    rsi_zone  = row.get("rsi_zone", "")
    ema_cross = row.get("ema_cross", 0)
    macd_hist_rising = row.get("macd_hist_rising", 0)
    vol_spike = row.get("vol_spike", 0)
    bb_squeeze = row.get("bb_squeeze", 0)
    near_52w   = row.get("near_52w_high", 0)
    pdh        = row.get("pdh_breakout", 0)
    delivery   = row.get("delivery_up_day", 0)

    if ema_cross == 1 and rsi_zone == "bull_momentum":
        return "EMA Cross + RSI Momentum"
    if alignment == "full_bull" and vol_spike == 1:
        return "Full Bull + Volume Surge"
    if bb_squeeze == 1 and macd_hist_rising == 1:
        return "BB Squeeze Breakout"
    if near_52w == 1 and alignment in ("full_bull", "bull"):
        return "52-Week High Breakout"
    if pdh == 1 and rsi_zone == "bull_momentum":
        return "PDH Breakout + Momentum"
    if delivery == 1 and alignment in ("full_bull", "bull"):
        return "Institutional Buying Signal"
    if alignment == "full_bull" and rsi_zone == "bull_momentum":
        return "EMA Aligned + RSI Momentum"
    if macd_hist_rising == 1 and rsi_zone == "bull_momentum":
        return "MACD + RSI Momentum"
    if alignment in ("bull", "weak_bull") and row.get("obv_rising", 0) == 1:
        return "EMA Trend + OBV Rising"
    if rsi_zone == "bull_momentum" and row.get("price_above_vwap", 0) == 1:
        return "VWAP Momentum Play"
    return "Technical Momentum Setup"


# ══════════════════════════════════════════════════════════════════
# SCORE ONE ROW
# ══════════════════════════════════════════════════════════════════

def score_row(row) -> dict:
    """Scores a single stock-day row. Returns full score breakdown."""
    disqualified, reason = apply_disqualifiers(row)

    if disqualified:
        return {
            "total_score":      0.0,
            "trend_score":      0.0,
            "momentum_score":   0.0,
            "volume_score":     0.0,
            "viability_score_s":0.0,
            "sentiment_score":  0.0,
            "disqualified":     True,
            "disqualify_reason":reason,
            "qualifies":        False,
        }

    t  = score_trend(row)
    m  = score_momentum(row)
    v  = score_volume(row)
    vi = score_viability(row)
    s  = score_sentiment(row)

    total = round(t + m + v + vi + s, 1)

    return {
        "total_score":       total,
        "trend_score":       round(t, 1),
        "momentum_score":    round(m, 1),
        "volume_score":      round(v, 1),
        "viability_score_s": round(vi, 1),
        "sentiment_score":   round(s, 1),
        "disqualified":      False,
        "disqualify_reason": "",
        "qualifies":         total >= MIN_SCORE,
    }


# ══════════════════════════════════════════════════════════════════
# PROCESS ALL STOCKS
# ══════════════════════════════════════════════════════════════════

def load_all_indicators() -> pd.DataFrame:
    """Loads all indicator parquets into one combined DataFrame."""
    files = sorted(INDICATORS_DIR.glob("*.parquet"))
    dfs = []
    for f in tqdm(files, desc="Loading indicators"):
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df)
        except Exception as e:
            log.warning(f"Could not load {f.stem}: {e}")

    if not dfs:
        log.error("No indicator files found.")
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    log.info(f"Loaded: {len(combined):,} rows, {combined['symbol'].nunique()} symbols")
    return combined


def compute_all_scores(combined: pd.DataFrame) -> pd.DataFrame:
    """Applies scoring to every row in the combined DataFrame."""
    log.info("Computing scores for all stock-days...")

    score_records = []
    for _, row in tqdm(combined.iterrows(), total=len(combined), desc="Scoring"):
        scores = score_row(row)
        levels = compute_entry_target_sl(row) if not scores["disqualified"] else {
            "entry_low": np.nan, "entry_high": np.nan,
            "target": np.nan, "sl": np.nan,
            "rr_ratio": np.nan, "sl_pct": np.nan, "target_pct": np.nan
        }
        setup = determine_setup_name(row) if scores["qualifies"] else ""
        score_records.append({**scores, **levels, "setup_name": setup})

    scores_df = pd.DataFrame(score_records)

    # Combine with original data
    result = pd.concat([
        combined[["date","symbol","open","high","low","close","volume",
                  "delivery_pct","rvol","rsi","atr_pct","ema_alignment",
                  "rsi_zone","market_regime","vix_close","is_warmup"]].reset_index(drop=True),
        scores_df.reset_index(drop=True)
    ], axis=1)

    return result


def build_daily_playbooks(scored: pd.DataFrame) -> pd.DataFrame:
    """
    For each trading day, selects the top N qualifying stocks.
    Returns a DataFrame of all daily playbooks.
    """
    log.info("Building daily playbooks...")

    qualifying = scored[scored["qualifies"] == True].copy()
    log.info(f"Total qualifying signals: {len(qualifying):,} across all days")

    playbooks = []
    dates = sorted(qualifying["date"].unique())

    for d in dates:
        day_df = qualifying[qualifying["date"] == d].copy()
        day_df = day_df.sort_values("total_score", ascending=False)

        # Take top N
        top = day_df.head(TOP_N).copy()
        top["rank"] = range(1, len(top) + 1)
        top["playbook_date"] = d
        playbooks.append(top)

    if not playbooks:
        log.warning("No qualifying signals found in any day.")
        return pd.DataFrame()

    all_playbooks = pd.concat(playbooks, ignore_index=True)
    log.info(f"Playbooks built: {len(dates)} trading days, avg {len(all_playbooks)/len(dates):.1f} stocks/day")
    return all_playbooks


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    log.info("THE STOCK LOGIC — Stage 3: Signal Scoring Engine")

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOKS_DIR.mkdir(parents=True, exist_ok=True)

    # Load all indicator data
    log.info("\n── Step 1: Loading indicators ──")
    combined = load_all_indicators()
    if combined.empty:
        log.error("No data. Run Stage 2 first.")
        return

    # Score every stock-day
    log.info("\n── Step 2: Scoring ──")
    scored = compute_all_scores(combined)

    # Save full scored dataset
    scored.to_parquet(SIGNALS_DIR / "all_scores.parquet", index=False)
    log.info(f"All scores saved: {len(scored):,} rows")

    # Build playbooks
    log.info("\n── Step 3: Building playbooks ──")
    playbooks = build_daily_playbooks(scored)

    if not playbooks.empty:
        playbooks.to_parquet(SIGNALS_DIR / "daily_signals.parquet", index=False)

        # Save individual daily playbooks
        for d, group in playbooks.groupby("playbook_date"):
            fname = pd.Timestamp(d).strftime("%Y-%m-%d")
            group.to_parquet(PLAYBOOKS_DIR / f"{fname}.parquet", index=False)

        log.info(f"Daily signals saved: {len(playbooks)} total signals")

    # ── VALIDATION REPORT ─────────────────────────────────────────
    log.info(f"\n{'='*60}")
    log.info("SIGNAL SCORING VALIDATION REPORT")
    log.info(f"{'='*60}")

    non_warmup = scored[~scored["is_warmup"]]
    qualifying = non_warmup[non_warmup["qualifies"] == True]
    disqualified = non_warmup[non_warmup["disqualified"] == True]

    total_days   = non_warmup["date"].nunique()
    total_rows   = len(non_warmup)
    qual_rows    = len(qualifying)
    disq_rows    = len(disqualified)

    log.info(f"\nUniverse stats:")
    log.info(f"  Trading days analysed : {total_days}")
    log.info(f"  Total stock-days      : {total_rows:,}")
    log.info(f"  Qualifying signals    : {qual_rows:,} ({qual_rows/total_rows*100:.1f}%)")
    log.info(f"  Disqualified          : {disq_rows:,} ({disq_rows/total_rows*100:.1f}%)")

    log.info(f"\nDisqualification breakdown:")
    dq_counts = non_warmup[non_warmup["disqualified"]==True]["disqualify_reason"].value_counts()
    for reason, count in dq_counts.items():
        log.info(f"  {reason:<20}: {count:,}")

    if not qualifying.empty:
        log.info(f"\nScore distribution (qualifying signals):")
        log.info(f"  Mean score  : {qualifying['total_score'].mean():.1f}")
        log.info(f"  Median score: {qualifying['total_score'].median():.1f}")
        log.info(f"  Min score   : {qualifying['total_score'].min():.1f}")
        log.info(f"  Max score   : {qualifying['total_score'].max():.1f}")

        log.info(f"\nTop setups by frequency:")
        setup_counts = qualifying["setup_name"].value_counts().head(5)
        for setup, count in setup_counts.items():
            log.info(f"  {setup:<35}: {count}")

        log.info(f"\nTop stocks by signal frequency:")
        sym_counts = qualifying["symbol"].value_counts().head(10)
        for sym, count in sym_counts.items():
            avg_score = qualifying[qualifying["symbol"]==sym]["total_score"].mean()
            log.info(f"  {sym:<15}: {count} signals, avg score {avg_score:.1f}")

    # Sample playbook — show last available day
    if not playbooks.empty:
        last_date = playbooks["playbook_date"].max()
        last_day  = playbooks[playbooks["playbook_date"]==last_date]
        log.info(f"\nSample playbook — {pd.Timestamp(last_date).strftime('%d %b %Y')}:")
        log.info(f"  {'#':<3} {'SYMBOL':<15} {'SCORE':<8} {'ENTRY':<10} {'TARGET':<10} {'SL':<10} {'SETUP'}")
        log.info(f"  {'-'*80}")
        for _, row in last_day.iterrows():
            log.info(
                f"  {int(row['rank']):<3} "
                f"{row['symbol']:<15} "
                f"{row['total_score']:<8.1f} "
                f"₹{row['entry_low']:<9.1f} "
                f"₹{row['target']:<9.1f} "
                f"₹{row['sl']:<9.1f} "
                f"{row['setup_name']}"
            )

        # Days with enough signals
        signals_per_day = playbooks.groupby("playbook_date").size()
        full_days = (signals_per_day >= TOP_N).sum()
        log.info(f"\nDays with full {TOP_N}-stock playbook: {full_days}/{total_days} ({full_days/total_days*100:.0f}%)")

    log.info(f"\n{'='*60}")
    status = "PASS" if qual_rows > 0 and not playbooks.empty else "FAIL"
    log.info(f"STATUS: {status} — Ready for Stage 4 (backtest)")
    log.info(f"{'='*60}")
    log.info("Next: python3 engine/04_backtest.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
