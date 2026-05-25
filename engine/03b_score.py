"""
THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine
=====================================================
Scores every stock-day using the institutional SMC framework.
Generates LONG (BTST/swing) and SHORT (intraday preferred) signals.

Scoring philosophy: ANTICIPATION not confirmation.
  - Find stocks approaching demand/supply zones
  - Detect smart money footprints BEFORE price moves
  - Volume confirmation at key levels
  - Market structure must support direction

Score dimensions (100 pts total):
  Market regime    15 pts  : top-down filter — trade with the trend
  SMC structure    30 pts  : OB, FVG, sweep, BOS, CHOCH
  Technical conf   25 pts  : EMA, RSI, MACD alignment
  Volume / inst    20 pts  : RVOL, delivery, institutional buying
  Risk/reward       10 pts : ATR viability, not overextended

Trade score grades:
  90+ = A+ → Full size, high conviction
  80+ = A  → Standard size
  70+ = B  → Half size, careful
  <70 = Skip → No trade, stay cash

Max loss per trade: ₹5,000 absolute
SL: 1.5–2% or previous swing low (whichever is structure-based)
RR minimum: 1:2 (prefer 1:3)
Long: BTST or swing 2–30 days
Short: intraday preferred, overnight only in strong bear regime

Reads : data/processed/smc/*.parquet
Writes: data/processed/signals_v2/daily_signals.parquet
Run   : python3 engine/03b_score.py
"""
import os, sys, logging, warnings
from pathlib import Path
import numpy as np
import pandas as pd
from tqdm import tqdm

warnings.filterwarnings("ignore")
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("reports/03b_score.log"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

SMC_DIR     = Path("data/processed/smc")
SIGNALS_DIR = Path("data/processed/signals_v2")
PLAYBOOKS   = Path("data/processed/signals_v2/playbooks")

MAX_LOSS_INR  = 5000.0
MIN_SL_PCT    = 0.015
MAX_SL_PCT    = 0.02
MIN_RR        = 2.0
TOP_N_LONG    = 5
TOP_N_SHORT   = 2
MIN_SCORE     = 65


# ══════════════════════════════════════════════════════════════════
# SCORING FUNCTIONS
# ══════════════════════════════════════════════════════════════════

def score_market_regime(row) -> float:
    """
    Max 15 pts.
    Top-down filter — must trade WITH the regime.
    No trade zone = 0 pts and will be disqualified.
    """
    if row.get("no_trade_zone", 0) == 1:
        return 0.0

    regime = row.get("market_regime", "unknown")
    vix    = row.get("vix_close", 15.0)
    ad     = row.get("ad_ratio", 1.0)
    direction = row.get("direction", "long")

    # Regime base score
    regime_pts = {"bull": 10.0, "sideways": 6.0,
                  "bear": 3.0, "unknown": 5.0}.get(regime, 5.0)

    # VIX adjustment
    vix_pts = 0.0
    if pd.notna(vix):
        if vix < 14:   vix_pts = 5.0
        elif vix < 18: vix_pts = 3.0
        elif vix < 22: vix_pts = 1.0

    # A/D breadth
    ad_pts = 0.0
    if pd.notna(ad):
        if ad >= 2.0:   ad_pts = 2.0
        elif ad >= 1.2: ad_pts = 1.0

    # Sector momentum bonus — if stock in strong sector, boost regime score
    sector_bias = row.get("sector_bias", "avoid")
    if direction == "long" and sector_bias == "long":
        regime_pts = min(regime_pts + 3.0, 12.0)  # sector tailwind
    elif direction == "short" and sector_bias == "short":
        regime_pts = min(regime_pts + 3.0, 12.0)  # sector tailwind for shorts

    return min(regime_pts + vix_pts + ad_pts, 15.0)


def score_smc_structure(row, direction: str) -> float:
    """
    Max 30 pts.
    The core SMC signal — smart money footprints.
    Direction: "long" or "short"
    """
    score = 0.0
    structure = row.get("structure_trend", "ranging")

    if direction == "long":
        # Structure alignment
        if structure == "uptrend":   score += 8.0
        elif structure == "ranging": score += 4.0

        # Demand order block proximity (anticipation entry)
        if row.get("near_demand_ob", 0) == 1:
            score += 8.0

        # Bullish FVG fill (price returning to imbalance)
        if row.get("price_in_bull_fvg", 0) == 1:
            score += 7.0

        # Liquidity sweep (stop hunt + reversal)
        if row.get("bull_liq_sweep", 0) == 1:
            score += 7.0

        # Break of structure bullish (trend confirmation)
        if row.get("bos_bull", 0) == 1:
            score += 5.0

        # CHOCH bullish (early reversal signal — premium)
        if row.get("choch_bull", 0) == 1:
            score += 8.0

    else:  # short
        if structure == "downtrend": score += 8.0
        elif structure == "ranging": score += 4.0

        if row.get("near_supply_ob", 0) == 1:  score += 8.0
        if row.get("price_in_bear_fvg", 0) == 1: score += 7.0
        if row.get("bear_liq_sweep", 0) == 1:  score += 7.0
        if row.get("bos_bear", 0) == 1:        score += 5.0
        if row.get("choch_bear", 0) == 1:      score += 8.0

    return min(score, 30.0)


def score_technical(row, direction: str) -> float:
    """
    Max 25 pts.
    Technical indicators must CONFIRM the SMC signal.
    RSI in anticipation zone (not peaked), EMA stack, MACD turning.
    """
    score = 0.0
    rsi = row.get("rsi", 50.0)

    if direction == "long":
        # EMA stack (10 pts)
        ema_pts = 0.0
        ema_pts += row.get("price_above_ema20", 0)  * 4.0
        ema_pts += row.get("ema20_above_ema50", 0)  * 3.0
        ema_pts += row.get("ema50_above_ema200", 0) * 3.0
        score += min(ema_pts, 10.0)

        # RSI — anticipation zone 45-65 (momentum building, NOT peaked)
        if pd.notna(rsi):
            if 45 <= rsi <= 65:   score += 10.0   # sweet spot
            elif 35 <= rsi < 45:  score += 6.0    # recovering from oversold
            elif 65 < rsi <= 70:  score += 4.0    # slightly overbought but ok
            elif rsi < 35:        score += 3.0    # oversold reversal potential
            # >70: don't score — overextended, SMC approach should catch this

        # MACD turning (5 pts)
        if row.get("macd_hist_rising", 0) == 1: score += 3.0
        if row.get("macd_positive", 0) == 1:    score += 2.0

    else:  # short
        # EMA stack bearish
        ema_pts = 0.0
        ema_pts += (1 - row.get("price_above_ema20", 1))  * 4.0
        ema_pts += (1 - row.get("ema20_above_ema50", 1))  * 3.0
        ema_pts += (1 - row.get("ema50_above_ema200", 1)) * 3.0
        score += min(ema_pts, 10.0)

        # RSI short zone 35-55
        if pd.notna(rsi):
            if 35 <= rsi <= 55:   score += 10.0
            elif 55 < rsi <= 65:  score += 6.0
            elif rsi > 70:        score += 3.0   # overbought short

        # MACD declining
        if row.get("macd_hist_rising", 0) == 0: score += 3.0
        if row.get("macd_positive", 0) == 0:    score += 2.0

    return min(score, 25.0)


def score_volume_institutional(row, direction: str) -> float:
    """
    Max 20 pts.
    Volume must confirm at the key level.
    Institutional buying/selling = highest conviction signal.
    """
    score = 0.0

    rvol = row.get("rvol", 1.0)
    if pd.isna(rvol): rvol = 1.0

    # RVOL (10 pts)
    if rvol >= 2.0:                   score += 10.0
    elif rvol >= 1.5:                 score += 7.0
    elif rvol >= 1.2:                 score += 4.0
    elif rvol >= 1.0:                 score += 2.0
    # < 1.0: no points — low participation

    # Delivery (institutional participation)
    if direction == "long":
        if row.get("institutional_buying", 0) == 1:
            score += 7.0   # high delivery + up close + RVOL — strongest signal
        elif row.get("high_delivery", 0) == 1:
            score += 4.0

    # RS positive (stock outperforming Nifty)
    if row.get("rs_positive", 0) == 1:
        score += 3.0

    return min(score, 20.0)


def score_risk_reward(row, direction: str) -> float:
    """
    Max 10 pts.
    Is the trade structurally viable?
    ATR must support SL without being too wide.
    Stock must not be overextended.
    """
    score  = 0.0
    atr_pct = row.get("atr_pct", 2.0)
    pct_52w = row.get("pct_from_52w_high", -5.0)

    # ATR viability
    if pd.notna(atr_pct):
        if 1.0 <= atr_pct <= 3.0:   score += 7.0   # ideal range
        elif 0.5 <= atr_pct < 1.0:  score += 4.0   # tight but ok
        elif 3.0 < atr_pct <= 5.0:  score += 4.0   # wide but manageable
        # >5%: too volatile for reliable SL

    if direction == "long":
        # Not at extreme 52W high (overextended)
        if pd.notna(pct_52w):
            if pct_52w < -20:       score += 3.0   # lots of room to recover
            elif pct_52w < -5:      score += 3.0   # moderate room
            elif -5 <= pct_52w <= 0: score += 2.0  # near high but can break out
    else:
        # Short: ideal near 52W high area
        if pd.notna(pct_52w):
            if -5 <= pct_52w <= 5:  score += 3.0
            elif pct_52w < -10:     score += 1.0

    return min(score, 10.0)


# ══════════════════════════════════════════════════════════════════
# DISQUALIFIERS
# ══════════════════════════════════════════════════════════════════

def check_disqualifiers(row, direction: str) -> tuple:
    """Returns (disqualified, reason). Hard rules — no exceptions."""

    # Warmup
    if row.get("is_warmup", True):
        return True, "warmup"

    # No trade zone
    if row.get("no_trade_zone", 0) == 1:
        return True, "no_trade_zone"

    # RVOL too low — no institutional participation
    rvol = row.get("rvol", 1.0)
    if pd.notna(rvol) and rvol < 0.5:
        return True, "very_low_volume"

    # ATR too high — can't set reliable SL within ₹5K limit
    atr_pct = row.get("atr_pct", 2.0)
    if pd.notna(atr_pct) and atr_pct > 8.0:
        return True, "atr_too_high"

    # Direction vs regime check
    regime = row.get("market_regime", "unknown")
    if direction == "long" and regime == "bear":
        # Only allow longs in bear if there's a liquidity sweep reversal
        if row.get("bull_liq_sweep", 0) == 0 and row.get("choch_bull", 0) == 0:
            return True, "bear_regime_no_reversal"

    # No SMC signal at all
    long_smc = (row.get("near_demand_ob", 0) + row.get("price_in_bull_fvg", 0) +
                row.get("bull_liq_sweep", 0) + row.get("bos_bull", 0) +
                row.get("choch_bull", 0))
    short_smc = (row.get("near_supply_ob", 0) + row.get("price_in_bear_fvg", 0) +
                 row.get("bear_liq_sweep", 0) + row.get("bos_bear", 0) +
                 row.get("choch_bear", 0))

    if direction == "long" and long_smc == 0:
        return True, "no_smc_signal"
    if direction == "short" and short_smc == 0:
        return True, "no_smc_signal"

    return False, ""


# ══════════════════════════════════════════════════════════════════
# POSITION SIZING & LEVELS
# ══════════════════════════════════════════════════════════════════

def compute_trade_levels(row, direction: str, capital: float = 100_000.0) -> dict:
    """
    Computes entry, SL, T1, T2, position size.
    SL: structure-based (prev swing low/high) or 1.5–2%
    Position size: MAX_LOSS_INR / SL_distance per share
    RR: minimum 1:2 (target at 2x SL distance)
    """
    close   = row.get("close", 0.0)
    atr     = row.get("atr",   close * 0.02)

    if close <= 0:
        return {}

    # Entry zone (next day open ± 0.3%)
    entry_ref = close
    entry_low  = round(entry_ref * 0.998, 2)
    entry_high = round(entry_ref * 1.002, 2)

    if direction == "long":
        # SL: max of (1.5% below close) or (1x ATR below close)
        sl_pct_raw  = max(MIN_SL_PCT, min(MAX_SL_PCT, atr / close))
        sl          = round(close * (1 - sl_pct_raw), 2)
        sl_distance = close - sl
        sl_pct_actual = sl_distance / close * 100

        # RR filter
        t1_rr2 = round(close + sl_distance * 2.0, 2)  # 1:2 RR
        t2_rr3 = round(close + sl_distance * 3.0, 2)  # 1:3 RR

    else:  # short
        sl_pct_raw  = max(MIN_SL_PCT, min(MAX_SL_PCT, atr / close))
        sl          = round(close * (1 + sl_pct_raw), 2)
        sl_distance = sl - close
        sl_pct_actual = sl_distance / close * 100

        t1_rr2 = round(close - sl_distance * 2.0, 2)
        t2_rr3 = round(close - sl_distance * 3.0, 2)

    # Position size: never lose more than ₹5,000
    if sl_distance <= 0:
        return {}

    qty = max(1, int(MAX_LOSS_INR / sl_distance))
    trade_value = round(entry_ref * qty, 2)

    # Cap: don't use more than 20% of capital
    max_trade = capital * 0.20
    if trade_value > max_trade:
        qty = max(1, int(max_trade / entry_ref))
        trade_value = round(entry_ref * qty, 2)

    actual_risk = round(sl_distance * qty, 2)
    rr_achievable = round((t1_rr2 - entry_ref) / sl_distance, 2) \
                    if direction == "long" else \
                    round((entry_ref - t1_rr2) / sl_distance, 2)

    # Reject if RR < minimum
    if rr_achievable < MIN_RR * 0.9:  # 10% tolerance
        return {}

    return {
        "entry_ref":       round(entry_ref, 2),
        "entry_low":       entry_low,
        "entry_high":      entry_high,
        "sl":              sl,
        "target_1":        t1_rr2,
        "target_2":        t2_rr3,
        "qty":             qty,
        "trade_value":     trade_value,
        "sl_pct":          round(sl_pct_actual, 2),
        "risk_inr":        actual_risk,
        "rr_1":            2.0,
        "rr_2":            3.0,
        "rr_achievable":   rr_achievable,
    }


def determine_setup_name(row, direction: str) -> str:
    """Descriptive setup name for the trade."""
    if direction == "long":
        if row.get("choch_bull", 0):      return "CHOCH Reversal — Trend Change"
        if row.get("bull_liq_sweep", 0):  return "Liquidity Sweep Reversal"
        if row.get("price_in_bull_fvg", 0) and row.get("near_demand_ob", 0):
            return "FVG + Demand OB Confluence"
        if row.get("near_demand_ob", 0) and row.get("bos_bull", 0):
            return "Demand OB + BOS Continuation"
        if row.get("price_in_bull_fvg", 0): return "FVG Fill — Bullish Imbalance"
        if row.get("near_demand_ob", 0):    return "Demand Order Block Retest"
        if row.get("bos_bull", 0):          return "Break of Structure — Bullish"
        if row.get("institutional_buying", 0): return "Institutional Accumulation"
        return "Bullish Momentum Continuation"
    else:
        if row.get("choch_bear", 0):       return "CHOCH — Bearish Reversal"
        if row.get("bear_liq_sweep", 0):   return "Liquidity Sweep — Short"
        if row.get("near_supply_ob", 0) and row.get("bos_bear", 0):
            return "Supply OB + BOS Breakdown"
        if row.get("price_in_bear_fvg", 0): return "FVG Fill — Bearish Imbalance"
        if row.get("near_supply_ob", 0):    return "Supply Order Block Rejection"
        if row.get("bos_bear", 0):          return "Break of Structure — Bearish"
        return "Bearish Distribution"


def score_trade(row, direction: str) -> dict:
    """Full scoring for one stock-day in given direction."""
    disq, reason = check_disqualifiers(row, direction)
    if disq:
        return {"direction": direction, "total_score": 0, "qualifies": False,
                "disqualified": True, "disqualify_reason": reason}

    r = score_market_regime(row)
    s = score_smc_structure(row, direction)
    t = score_technical(row, direction)
    v = score_volume_institutional(row, direction)
    rr = score_risk_reward(row, direction)
    total = round(r + s + t + v + rr, 1)

    grade = "A+" if total >= 90 else "A" if total >= 80 else "B" if total >= 70 else "skip"

    levels = compute_trade_levels(row, direction)
    setup  = determine_setup_name(row, direction) if total >= MIN_SCORE else ""

    # Trade type
    regime = row.get("market_regime", "unknown")
    if direction == "long":
        trade_type = "BTST" if total >= 80 else "Swing 2-7d"
    else:
        trade_type = "Intraday" if regime != "bear" else "BTST Short"

    return {
        "direction":          direction,
        "total_score":        total,
        "grade":              grade,
        "regime_score":       round(r, 1),
        "smc_score":          round(s, 1),
        "technical_score":    round(t, 1),
        "volume_score":       round(v, 1),
        "rr_score":           round(rr, 1),
        "qualifies":          total >= MIN_SCORE and bool(levels),
        "disqualified":       False,
        "disqualify_reason":  "",
        "setup_name":         setup,
        "trade_type":         trade_type,
        **levels
    }


# ══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════

def process_all():
    log.info("Loading SMC data...")
    files = sorted(SMC_DIR.glob("*.parquet"))
    dfs = []
    for f in tqdm(files, desc="Loading"):
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"])
        dfs.append(df)

    combined = pd.concat(dfs, ignore_index=True)
    log.info(f"Loaded: {len(combined):,} rows, {combined['symbol'].nunique()} symbols")
    return combined


def score_all(combined: pd.DataFrame) -> pd.DataFrame:
    log.info("Scoring all stock-days (long + short)...")

    # Load sector momentum data if available
    sector_bias_map = {}
    sector_file = Path("data/processed/sector_momentum.parquet")
    if sector_file.exists():
        try:
            sec_df = pd.read_parquet(sector_file)
            # Build date -> sector -> bias map
            for _, sr in sec_df.iterrows():
                sector_bias_map[sr["sector"]] = sr["trade_bias"]
            log.info(f"Sector bias loaded: {sector_bias_map}")
        except Exception as e:
            log.warning(f"Could not load sector momentum: {e}")

    # Load symbol->sector map
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from universe import SYMBOL_SECTOR_MAP as _SECTOR_MAP
    except:
        _SECTOR_MAP = {}

    records = []

    for _, row in tqdm(combined.iterrows(), total=len(combined), desc="Scoring"):
        # Add sector bias to row
        sym = row.get("symbol", "")
        sector = _SECTOR_MAP.get(sym, "OTHER")
        row = row.copy()
        row["sector"] = sector
        row["sector_bias"] = sector_bias_map.get(sector, "avoid")
        base = {
            "date":           row["date"],
            "symbol":         row["symbol"],
            "close":          row.get("close", 0),
            "open":           row.get("open", 0),
            "volume":         row.get("volume", 0),
            "rvol":           row.get("rvol", np.nan),
            "rsi":            row.get("rsi", np.nan),
            "atr_pct":        row.get("atr_pct", np.nan),
            "delivery_pct":   row.get("delivery_pct", np.nan),
            "market_regime":  row.get("market_regime", "unknown"),
            "vix_close":      row.get("vix_close", np.nan),
            "structure_trend":row.get("structure_trend", "ranging"),
            "no_trade_zone":  row.get("no_trade_zone", 0),
            "is_warmup":      row.get("is_warmup", True),
            "rs_positive":    row.get("rs_positive", 0),
        }

        for direction in ["long", "short"]:
            scored = score_trade(row, direction)
            record = {**base, **scored}
            records.append(record)

    return pd.DataFrame(records)


def build_playbooks(scored: pd.DataFrame) -> pd.DataFrame:
    log.info("Building daily playbooks...")

    qualifying = scored[scored["qualifies"] == True].copy()
    log.info(f"Qualifying signals: {len(qualifying):,}")

    all_plays = []
    dates = sorted(qualifying["date"].unique())

    for d in dates:
        day = qualifying[qualifying["date"] == d]

        longs  = day[day["direction"] == "long"].sort_values("total_score", ascending=False).head(TOP_N_LONG)
        shorts = day[day["direction"] == "short"].sort_values("total_score", ascending=False).head(TOP_N_SHORT)

        plays = pd.concat([longs, shorts])
        plays["playbook_date"] = d
        plays["rank"] = range(1, len(plays) + 1)
        all_plays.append(plays)

    if not all_plays:
        return pd.DataFrame()

    return pd.concat(all_plays, ignore_index=True)


def main():
    log.info("THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOKS.mkdir(parents=True, exist_ok=True)

    log.info("\n── Step 1: Loading SMC data ──")
    combined = process_all()

    log.info("\n── Step 2: Scoring ──")
    scored = score_all(combined)
    scored.to_parquet(SIGNALS_DIR / "all_scores_v2.parquet", index=False)

    log.info("\n── Step 3: Building playbooks ──")
    playbooks = build_playbooks(scored)

    if not playbooks.empty:
        playbooks.to_parquet(SIGNALS_DIR / "daily_signals.parquet", index=False)
        log.info(f"Playbooks: {playbooks['playbook_date'].nunique()} days, "
                 f"{len(playbooks)} total signals")

    # ── Report ────────────────────────────────────────────────────
    live = scored[~scored["is_warmup"]]
    q    = live[live["qualifies"] == True]
    dq   = live[live["disqualified"] == True]

    log.info(f"\n{'='*60}")
    log.info("SIGNAL ENGINE REPORT")
    log.info(f"{'='*60}")
    log.info(f"  Total stock-days  : {len(live):,}")
    log.info(f"  Qualifying signals: {len(q):,} ({len(q)/len(live)*100:.1f}%)")
    log.info(f"  Disqualified      : {len(dq):,} ({len(dq)/len(live)*100:.1f}%)")

    if len(q) > 0:
        log.info(f"\n  Score distribution (qualifying):")
        log.info(f"    Mean   : {q['total_score'].mean():.1f}")
        log.info(f"    Median : {q['total_score'].median():.1f}")
        log.info(f"    A+/A   : {(q['total_score']>=80).sum()}")
        log.info(f"    B      : {((q['total_score']>=70)&(q['total_score']<80)).sum()}")

    log.info(f"\n  By direction:")
    for d in ["long","short"]:
        sub = q[q["direction"]==d]
        log.info(f"    {d}: {len(sub)} signals, avg score {sub['total_score'].mean():.1f}")

    log.info(f"\n  Disqualification breakdown:")
    for reason, cnt in dq["disqualify_reason"].value_counts().items():
        log.info(f"    {reason:<30}: {cnt:,}")

    log.info(f"\n  Top setups:")
    for setup, cnt in q["setup_name"].value_counts().head(8).items():
        log.info(f"    {setup:<40}: {cnt}")

    # Sample playbook
    if not playbooks.empty:
        last_date = playbooks["playbook_date"].max()
        last_day  = playbooks[playbooks["playbook_date"] == last_date]
        log.info(f"\n  Sample playbook — {pd.Timestamp(last_date).strftime('%d %b %Y')}:")
        log.info(f"  {'#':>2} {'SYM':<12} {'DIR':<6} {'SCORE':>6} {'GRADE':>5} "
                 f"{'ENTRY':>8} {'T1':>8} {'T2':>8} {'SL':>8} {'RISK₹':>7} {'SETUP'}")
        log.info(f"  {'-'*105}")
        for _, r in last_day.iterrows():
            log.info(
                f"  {int(r.get('rank',0)):>2} {r['symbol']:<12} "
                f"{'↑LONG' if r['direction']=='long' else '↓SHORT':<6} "
                f"{r['total_score']:>6.1f} {r.get('grade',''):>5} "
                f"₹{r.get('entry_ref',0):>7.1f} "
                f"₹{r.get('target_1',0):>7.1f} "
                f"₹{r.get('target_2',0):>7.1f} "
                f"₹{r.get('sl',0):>7.1f} "
                f"₹{r.get('risk_inr',0):>6.0f} "
                f"{r.get('setup_name','')}"
            )

    log.info(f"\nSTATUS: {'PASS' if len(q)>0 else 'FAIL'} — Ready for Stage 4b (BTST backtest)")
    log.info("Next: python3 engine/04b_backtest.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
