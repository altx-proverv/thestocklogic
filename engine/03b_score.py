"""
THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine (Vectorized)
=================================================================
Fully vectorized — no iterrows. Runs on 200K+ rows in <60 seconds.
Memory efficient — processes in chunks if needed.

Run: python3 engine/03b_score.py
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
# VECTORIZED SCORING
# ══════════════════════════════════════════════════════════════════

def load_sector_bias() -> dict:
    """Load sector bias from sector momentum parquet."""
    sector_file = Path("data/processed/sector_momentum.parquet")
    if not sector_file.exists():
        return {}
    try:
        sec_df = pd.read_parquet(sector_file)
        return dict(zip(sec_df["sector"], sec_df["trade_bias"]))
    except:
        return {}


def load_symbol_sector() -> dict:
    """Load symbol->sector mapping."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from universe import SYMBOL_SECTOR_MAP
        return SYMBOL_SECTOR_MAP
    except:
        return {}


def score_vectorized(df: pd.DataFrame, sector_bias: dict, symbol_sector: dict) -> pd.DataFrame:
    """
    Scores all rows using vectorized pandas operations.
    No loops. No iterrows. Pure numpy/pandas.
    """
    n = len(df)

    # ── Add sector context ────────────────────────────────────────
    df["sector"]      = df["symbol"].map(symbol_sector).fillna("OTHER")
    df["sector_bias"] = df["sector"].map(sector_bias).fillna("avoid")

    # ── DISQUALIFIERS (vectorized) ────────────────────────────────
    df["disqualified"]       = False
    df["disqualify_reason"]  = ""

    # Warmup
    mask = df["is_warmup"] == True
    df.loc[mask, "disqualified"]      = True
    df.loc[mask, "disqualify_reason"] = "warmup"

    # No trade zone
    mask = (~df["disqualified"]) & (df.get("no_trade_zone", pd.Series(0, index=df.index)) == 1)
    df.loc[mask, "disqualified"]      = True
    df.loc[mask, "disqualify_reason"] = "no_trade_zone"

    # Very low volume
    rvol = df.get("rvol", pd.Series(1.0, index=df.index)).fillna(1.0)
    mask = (~df["disqualified"]) & (rvol < 0.5)
    df.loc[mask, "disqualified"]      = True
    df.loc[mask, "disqualify_reason"] = "very_low_volume"

    # ATR too high
    atr_pct = df.get("atr_pct", pd.Series(2.0, index=df.index)).fillna(2.0)
    mask = (~df["disqualified"]) & (atr_pct > 8.0)
    df.loc[mask, "disqualified"]      = True
    df.loc[mask, "disqualify_reason"] = "atr_too_high"

    # Bear regime no reversal (long only)
    regime   = df.get("market_regime", pd.Series("unknown", index=df.index)).fillna("unknown")
    bull_liq = df.get("bull_liq_sweep", pd.Series(0, index=df.index)).fillna(0)
    choch_b  = df.get("choch_bull", pd.Series(0, index=df.index)).fillna(0)
    direction_col = df.get("direction", pd.Series("long", index=df.index)) if "direction" in df.columns else pd.Series("long", index=df.index)
    mask = (~df["disqualified"]) & (direction_col == "long") & \
           (regime == "bear") & (bull_liq == 0) & (choch_b == 0)
    df.loc[mask, "disqualified"]      = True
    df.loc[mask, "disqualify_reason"] = "bear_regime_no_reversal"

    # No SMC signal
    near_ob  = df.get("near_demand_ob",    pd.Series(0, index=df.index)).fillna(0)
    bull_fvg = df.get("price_in_bull_fvg", pd.Series(0, index=df.index)).fillna(0)
    bos_bull = df.get("bos_bull",          pd.Series(0, index=df.index)).fillna(0)
    choch_b2 = df.get("choch_bull",        pd.Series(0, index=df.index)).fillna(0)
    sup_ob   = df.get("near_supply_ob",    pd.Series(0, index=df.index)).fillna(0)
    bear_fvg = df.get("price_in_bear_fvg", pd.Series(0, index=df.index)).fillna(0)
    bos_bear = df.get("bos_bear",          pd.Series(0, index=df.index)).fillna(0)
    liq_bull = df.get("bull_liq_sweep",    pd.Series(0, index=df.index)).fillna(0)
    liq_bear = df.get("bear_liq_sweep",    pd.Series(0, index=df.index)).fillna(0)

    long_smc  = near_ob + bull_fvg + bos_bull + choch_b2 + liq_bull
    short_smc = sup_ob  + bear_fvg + bos_bear + liq_bear

    mask_long  = (~df["disqualified"]) & (direction_col == "long")  & (long_smc  == 0)
    mask_short = (~df["disqualified"]) & (direction_col == "short") & (short_smc == 0)
    df.loc[mask_long | mask_short, "disqualified"]      = True
    df.loc[mask_long | mask_short, "disqualify_reason"] = "no_smc_signal"

    # ── SCORE DIMENSIONS (vectorized) ────────────────────────────

    # 1. Market regime score (max 15)
    regime_base = regime.map({"bull":10.0,"sideways":6.0,"bear":3.0,"unknown":5.0}).fillna(5.0)

    # Sector tailwind boost
    is_long_strong  = (direction_col == "long")  & (df["sector_bias"] == "long")
    is_short_strong = (direction_col == "short") & (df["sector_bias"] == "short")
    regime_base = np.where(is_long_strong | is_short_strong,
                           np.minimum(regime_base + 3.0, 12.0), regime_base)

    vix = df.get("vix_close", pd.Series(15.0, index=df.index)).fillna(15.0)
    vix_pts = np.where(vix < 14, 5.0, np.where(vix < 18, 3.0, np.where(vix < 22, 1.0, 0.0)))

    ad = df.get("ad_ratio", pd.Series(1.0, index=df.index)).fillna(1.0)
    ad_pts = np.where(ad >= 2.0, 2.0, np.where(ad >= 1.2, 1.0, 0.0))

    regime_score = np.minimum(regime_base + vix_pts + ad_pts, 15.0)
    regime_score = np.where(df["disqualified"], 0.0, regime_score)

    # 2. SMC structure score (max 30)
    structure = df.get("structure_trend", pd.Series("ranging", index=df.index)).fillna("ranging")
    struct_pts_long  = structure.map({"uptrend":8.0,"ranging":4.0,"downtrend":0.0}).fillna(0.0)
    struct_pts_short = structure.map({"downtrend":8.0,"ranging":4.0,"uptrend":0.0}).fillna(0.0)

    smc_long  = struct_pts_long  + near_ob*8 + bull_fvg*7 + liq_bull*7 + bos_bull*5 + choch_b2*8
    smc_short = struct_pts_short + sup_ob*8  + bear_fvg*7 + liq_bear*7 + bos_bear*5 + \
                df.get("choch_bear", pd.Series(0,index=df.index)).fillna(0)*8

    smc_score = np.where(direction_col == "long",
                         np.minimum(smc_long, 30.0),
                         np.minimum(smc_short, 30.0))
    smc_score = np.where(df["disqualified"], 0.0, smc_score)

    # 3. Technical score (max 25)
    rsi = df.get("rsi", pd.Series(50.0, index=df.index)).fillna(50.0)
    p_ema20  = df.get("price_above_ema20",  pd.Series(0,index=df.index)).fillna(0)
    ema20_50 = df.get("ema20_above_ema50",  pd.Series(0,index=df.index)).fillna(0)
    ema50_200= df.get("ema50_above_ema200", pd.Series(0,index=df.index)).fillna(0)
    macd_r   = df.get("macd_hist_rising",   pd.Series(0,index=df.index)).fillna(0)
    macd_p   = df.get("macd_hist_positive", pd.Series(0,index=df.index)).fillna(0)

    ema_long  = p_ema20*4 + ema20_50*3 + ema50_200*3
    ema_short = (1-p_ema20)*4 + (1-ema20_50)*3 + (1-ema50_200)*3

    rsi_long  = np.where((rsi>=45)&(rsi<=65), 10.0,
                np.where((rsi>=35)&(rsi<45),   6.0,
                np.where((rsi>65)&(rsi<=70),   4.0,
                np.where(rsi<35,               3.0, 0.0))))
    rsi_short = np.where((rsi>=35)&(rsi<=55), 10.0,
                np.where((rsi>55)&(rsi<=65),   6.0,
                np.where(rsi>70,               3.0, 0.0)))

    macd_long  = macd_r*3 + macd_p*2
    macd_short = (1-macd_r)*3 + (1-macd_p)*2

    tech_long  = np.minimum(ema_long  + rsi_long  + macd_long,  25.0)
    tech_short = np.minimum(ema_short + rsi_short + macd_short, 25.0)
    tech_score = np.where(direction_col=="long", tech_long, tech_short)
    tech_score = np.where(df["disqualified"], 0.0, tech_score)

    # 4. Volume / institutional score (max 20)
    rvol2 = rvol.values
    rvol_pts = np.where(rvol2>=2.0, 10.0,
               np.where(rvol2>=1.5,  7.0,
               np.where(rvol2>=1.2,  4.0,
               np.where(rvol2>=1.0,  2.0, 0.0))))

    inst_buy = df.get("institutional_buying", pd.Series(0,index=df.index)).fillna(0)
    hi_del   = df.get("high_delivery",        pd.Series(0,index=df.index)).fillna(0)
    rs_pos   = df.get("rs_positive",          pd.Series(0,index=df.index)).fillna(0)

    vol_score = np.minimum(rvol_pts + inst_buy*7 + hi_del*4 + rs_pos*3, 20.0)
    vol_score = np.where(df["disqualified"], 0.0, vol_score)

    # 5. Risk/reward score (max 10)
    rr_pts  = np.where((atr_pct>=1.0)&(atr_pct<=3.0), 7.0,
               np.where((atr_pct>=0.5)&(atr_pct<1.0),  4.0,
               np.where((atr_pct>3.0)&(atr_pct<=5.0),  4.0, 0.0)))

    pct52 = df.get("pct_from_52w_high", pd.Series(-5.0,index=df.index)).fillna(-5.0)
    rr_ext = np.where(direction_col=="long",
                      np.where(pct52<-5, 3.0, np.where(pct52<=-0.0, 2.0, 1.0)),
                      np.where((pct52>=-5)&(pct52<=5), 3.0, 1.0))

    rr_score = np.minimum(rr_pts + rr_ext, 10.0)
    rr_score = np.where(df["disqualified"], 0.0, rr_score)

    # ── TOTAL SCORE ───────────────────────────────────────────────
    total = np.round(regime_score + smc_score + tech_score + vol_score + rr_score, 1)
    total = np.where(df["disqualified"], 0.0, total)

    df["regime_score"]    = np.round(regime_score, 1)
    df["smc_score"]       = np.round(smc_score, 1)
    df["technical_score"] = np.round(tech_score, 1)
    df["volume_score"]    = np.round(vol_score, 1)
    df["rr_score"]        = np.round(rr_score, 1)
    df["total_score"]     = total
    df["qualifies"]       = (~df["disqualified"]) & (total >= MIN_SCORE)

    # Assign grade based on score
    df["grade"] = "skip"
    df.loc[df["qualifies"] & (total >= 80), "grade"] = "A+"
    df.loc[df["qualifies"] & (total >= 75) & (total < 80), "grade"] = "A"
    df.loc[df["qualifies"] & (total >= 65) & (total < 75), "grade"] = "B"

    return df


def determine_setup_names(df: pd.DataFrame) -> pd.Series:
    """Vectorized setup name assignment."""
    direction = df.get("direction", pd.Series("long", index=df.index))
    choch_b = df.get("choch_bull",        pd.Series(0,index=df.index)).fillna(0)
    choch_s = df.get("choch_bear",        pd.Series(0,index=df.index)).fillna(0)
    liq_b   = df.get("bull_liq_sweep",    pd.Series(0,index=df.index)).fillna(0)
    liq_s   = df.get("bear_liq_sweep",    pd.Series(0,index=df.index)).fillna(0)
    fvg_ob  = df.get("price_in_bull_fvg", pd.Series(0,index=df.index)).fillna(0) & \
               df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0)
    ob_bos  = df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0) & \
               df.get("bos_bull",          pd.Series(0,index=df.index)).fillna(0)
    fvg_b   = df.get("price_in_bull_fvg", pd.Series(0,index=df.index)).fillna(0)
    ob_only = df.get("near_demand_ob",    pd.Series(0,index=df.index)).fillna(0)
    bos_b   = df.get("bos_bull",          pd.Series(0,index=df.index)).fillna(0)
    sup_ob  = df.get("near_supply_ob",    pd.Series(0,index=df.index)).fillna(0)
    bear_fvg= df.get("price_in_bear_fvg", pd.Series(0,index=df.index)).fillna(0)
    bos_s   = df.get("bos_bear",          pd.Series(0,index=df.index)).fillna(0)
    inst_b  = df.get("institutional_buying", pd.Series(0,index=df.index)).fillna(0)

    name = pd.Series("Bullish Momentum Continuation", index=df.index)

    # Long setups (priority order)
    is_long = direction == "long"
    name = np.where(is_long & (bos_b>0),   "Break of Structure — Bullish",      name)
    name = np.where(is_long & (ob_only>0),  "Demand Order Block Retest",         name)
    name = np.where(is_long & (fvg_b>0),    "FVG Fill — Bullish Imbalance",      name)
    name = np.where(is_long & (ob_bos>0),   "Demand OB + BOS Continuation",      name)
    name = np.where(is_long & (fvg_ob>0),   "FVG + Demand OB Confluence",        name)
    name = np.where(is_long & (liq_b>0),    "Liquidity Sweep Reversal",          name)
    name = np.where(is_long & (choch_b>0),  "CHOCH Reversal — Trend Change",     name)
    name = np.where(is_long & (inst_b>0),   "Institutional Accumulation",        name)

    # Short setups
    is_short = direction == "short"
    name = np.where(is_short,                "Bearish Distribution",              name)
    name = np.where(is_short & (bos_s>0),    "Break of Structure — Bearish",      name)
    name = np.where(is_short & (bear_fvg>0), "FVG Fill — Bearish Imbalance",      name)
    name = np.where(is_short & (sup_ob>0),   "Supply Order Block Rejection",      name)
    name = np.where(is_short & (sup_ob>0) & (bos_s>0), "Supply OB + BOS Breakdown", name)
    name = np.where(is_short & (liq_s>0),    "Liquidity Sweep — Short",           name)
    name = np.where(is_short & (choch_s>0),  "CHOCH — Bearish Reversal",          name)

    return pd.Series(name, index=df.index)


def compute_trade_levels_vectorized(df: pd.DataFrame) -> pd.DataFrame:
    """Vectorized entry/SL/target computation."""
    close   = df["close"].fillna(0)
    atr     = df.get("atr", close * 0.02).fillna(close * 0.02)
    direction = df.get("direction", pd.Series("long", index=df.index))

    entry_ref  = close
    entry_low  = (close * 0.998).round(2)
    entry_high = (close * 1.002).round(2)

    atr_pct = (atr / close.replace(0, np.nan)).fillna(0.02)
    sl_pct  = np.clip(atr_pct, MIN_SL_PCT, MAX_SL_PCT)

    sl_long  = (close * (1 - sl_pct)).round(2)
    sl_short = (close * (1 + sl_pct)).round(2)
    sl = np.where(direction == "long", sl_long, sl_short)

    sl_dist = np.abs(close - sl)
    t1_long  = (close + sl_dist * 2.0).round(2)
    t1_short = (close - sl_dist * 2.0).round(2)
    t2_long  = (close + sl_dist * 3.0).round(2)
    t2_short = (close - sl_dist * 3.0).round(2)
    t1 = np.where(direction == "long", t1_long, t1_short)
    t2 = np.where(direction == "long", t2_long, t2_short)

    # Position sizing: ₹5,000 / SL distance
    qty_raw = np.floor(MAX_LOSS_INR / sl_dist.replace(0, np.nan)).fillna(1).astype(int)
    qty     = np.maximum(qty_raw, 1)
    # Cap at 20% of ₹1L = ₹20,000
    qty     = np.minimum(qty, np.floor(20000 / close.replace(0, np.nan)).fillna(1).astype(int))
    qty     = np.maximum(qty, 1)

    risk_inr = (sl_dist * qty).round(2)

    df["entry_ref"]   = entry_ref.round(2)
    df["entry_low"]   = entry_low
    df["entry_high"]  = entry_high
    df["sl"]          = pd.Series(sl, index=df.index).round(2)
    df["target_1"]    = pd.Series(t1, index=df.index).round(2)
    df["target_2"]    = pd.Series(t2, index=df.index).round(2)
    df["sl_pct"]      = (sl_pct * 100).round(2)
    df["qty"]         = pd.Series(qty, index=df.index)
    df["risk_inr"]    = pd.Series(risk_inr, index=df.index)
    df["rr_1"]        = 2.0
    df["rr_2"]        = 3.0

    return df


# ══════════════════════════════════════════════════════════════════
# MAIN PIPELINE
# ══════════════════════════════════════════════════════════════════

def load_all_smc() -> pd.DataFrame:
    """Load all SMC parquets into one DataFrame."""
    files = sorted(SMC_DIR.glob("*.parquet"))
    dfs = []
    for f in tqdm(files, desc="Loading"):
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            dfs.append(df)
        except Exception as e:
            log.warning(f"Skip {f.stem}: {e}")

    combined = pd.concat(dfs, ignore_index=True)
    log.info(f"Loaded: {len(combined):,} rows, {combined['symbol'].nunique()} symbols")
    return combined


def process_direction(combined: pd.DataFrame, direction: str,
                      sector_bias: dict, symbol_sector: dict) -> pd.DataFrame:
    """Process one direction (long or short) for all rows."""
    df = combined.copy()
    df["direction"] = direction
    df = score_vectorized(df, sector_bias, symbol_sector)
    df["setup_name"] = determine_setup_names(df)

    # Compute levels for qualifying signals
    qual_mask = df["qualifies"]
    if qual_mask.sum() > 0:
        levels = compute_trade_levels_vectorized(df[qual_mask].copy())
        for col in ["entry_ref","entry_low","entry_high","sl","target_1",
                    "target_2","sl_pct","qty","risk_inr","rr_1","rr_2"]:
            if col in levels.columns:
                df.loc[qual_mask, col] = levels[col].values

    return df


def build_playbooks(scored: pd.DataFrame) -> pd.DataFrame:
    qualifying = scored[scored["qualifies"]].copy()
    log.info(f"Qualifying signals: {len(qualifying):,}")

    all_plays = []
    for d, group in qualifying.groupby("date"):
        longs  = group[group["direction"]=="long"].nlargest(TOP_N_LONG,  "total_score")
        shorts = group[group["direction"]=="short"].nlargest(TOP_N_SHORT, "total_score")
        plays  = pd.concat([longs, shorts])
        plays["playbook_date"] = d
        plays["rank"] = range(1, len(plays)+1)
        all_plays.append(plays)

    if not all_plays:
        return pd.DataFrame()
    return pd.concat(all_plays, ignore_index=True)


def main():
    log.info("THE STOCK LOGIC — Stage 3b: SMC Trade Scoring Engine (Vectorized)")
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOKS.mkdir(parents=True, exist_ok=True)

    # Load sector context
    sector_bias   = load_sector_bias()
    symbol_sector = load_symbol_sector()
    log.info(f"Sector bias: {sector_bias}")

    # Load all SMC data
    log.info("\n── Step 1: Loading SMC data ──")
    combined = load_all_smc()

    # Score in batches of 100 stocks to avoid OOM on t2.micro
    log.info("\n── Step 2: Scoring (vectorized — batched) ──")
    symbols = combined["symbol"].unique().tolist()
    batch_size = 100
    all_qualifying = []

    for i in range(0, len(symbols), batch_size):
        batch_syms = symbols[i:i+batch_size]
        batch = combined[combined["symbol"].isin(batch_syms)].copy()
        log.info(f"Batch {i//batch_size+1}/{(len(symbols)-1)//batch_size+1}: {len(batch_syms)} stocks, {len(batch):,} rows")

        longs = process_direction(batch, "long", sector_bias, symbol_sector)
        longs_q = longs[longs["qualifies"]].copy()
        del longs

        shorts = process_direction(batch, "short", sector_bias, symbol_sector)
        shorts_q = shorts[shorts["qualifies"]].copy()
        del shorts
        del batch

        batch_q = pd.concat([longs_q, shorts_q], ignore_index=True)
        del longs_q, shorts_q
        all_qualifying.append(batch_q)

    del combined
    scored = pd.concat(all_qualifying, ignore_index=True)
    del all_qualifying
    log.info(f"Total qualifying: {len(scored):,}")
    scored.to_parquet(SIGNALS_DIR / "all_scores_v2.parquet", index=False)

        # Build playbooks
    log.info("\n── Step 3: Building playbooks ──")
    playbooks = build_playbooks(scored)
    if not playbooks.empty:
        playbooks.to_parquet(SIGNALS_DIR / "daily_signals.parquet", index=False)
        log.info(f"Playbooks: {playbooks['playbook_date'].nunique()} days, "
                 f"{len(playbooks)} signals")

    # Report
    live = scored[~scored["is_warmup"]]
    q    = live[live["qualifies"]]
    dq   = live[live["disqualified"]]

    log.info(f"\n{'='*60}")
    log.info("SIGNAL ENGINE REPORT")
    log.info(f"{'='*60}")
    log.info(f"  Total stock-days  : {len(live):,}")
    log.info(f"  Qualifying signals: {len(q):,} ({len(q)/max(len(live),1)*100:.1f}%)")
    log.info(f"  Disqualified      : {len(dq):,} ({len(dq)/max(len(live),1)*100:.1f}%)")

    if len(q):
        log.info(f"\n  Score distribution:")
        log.info(f"    Mean   : {q['total_score'].mean():.1f}")
        log.info(f"    Median : {q['total_score'].median():.1f}")
        log.info(f"    A+/A   : {(q['total_score']>=75).sum()}")
        log.info(f"    B      : {((q['total_score']>=65)&(q['total_score']<75)).sum()}")

    log.info(f"\n  By direction:")
    for d in ["long","short"]:
        sub = q[q["direction"]==d]
        if len(sub):
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
        last_day  = playbooks[playbooks["playbook_date"]==last_date]
        log.info(f"\n  Sample playbook — {pd.Timestamp(last_date).strftime('%d %b %Y')}:")
        log.info(f"  {'#':>2} {'SYM':<12} {'DIR':<6} {'SCORE':>6} {'GRADE':>5} "
                 f"{'ENTRY':>8} {'T1':>8} {'T2':>8} {'SL':>8} {'RISK₹':>7} {'SETUP'}")
        log.info(f"  {'-'*100}")
        for _, r in last_day.iterrows():
            grade = "A+" if r["total_score"]>=80 else "A" if r["total_score"]>=75 else "B"
            log.info(
                f"  {int(r.get('rank',0)):>2} {r['symbol']:<12} "
                f"{'↑LONG' if r['direction']=='long' else '↓SHORT':<6} "
                f"{r['total_score']:>6.1f} {grade:>5} "
                f"₹{r.get('entry_ref',0):>7.1f} "
                f"₹{r.get('target_1',0):>7.1f} "
                f"₹{r.get('target_2',0):>7.1f} "
                f"₹{r.get('sl',0):>7.1f} "
                f"₹{r.get('risk_inr',0):>6.0f} "
                f"{r.get('setup_name','')}"
            )

    log.info(f"\nSTATUS: {'PASS' if len(q)>0 else 'FAIL'}")
    log.info("Next: python3 engine/06_push_supabase.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
