"""
THE STOCK LOGIC — Stage 2b: SMC Institutional Signal Engine
============================================================
Replaces the confirmation-based indicator engine with an
anticipation-based Smart Money Concepts engine.

Signal philosophy: find stocks BEFORE the move, not after.

Detects per stock per day:
  SMC:        Order blocks, FVGs, liquidity sweeps, BOS, CHOCH
  Technical:  EMA stack, RSI 45-65 zone, MACD momentum, RVOL
  Market:     Sector RS, VIX regime, breadth
  Structure:  Swing highs/lows, market structure trend

Trade types:
  Long  → BTST or swing 2-30 days (overnight OK)
  Short → Intraday preferred (exit same day unless strong bear)

Reads : data/processed/stocks/*.parquet + market.parquet
Writes: data/processed/smc/*.parquet

Run: python3 engine/02b_smc_signals.py
"""
import os, sys, logging, warnings
from pathlib import Path
import numpy as np
import pandas as pd
import ta
from tqdm import tqdm

warnings.filterwarnings("ignore")
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("reports/02b_smc.log"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

STOCKS_DIR  = Path("data/processed/stocks")
SMC_DIR     = Path("data/processed/smc")
MARKET_FILE = Path("data/processed/market.parquet")

SWING_LOOKBACK  = 5    # candles each side for swing detection
OB_LOOKFORWARD  = 3    # candles to confirm OB validity
FVG_MIN_SIZE    = 0.15 # minimum FVG gap size % to qualify
MIN_OB_MOVE     = 0.015 # minimum forward move after OB candle (1.5%)
RVOL_THRESHOLD  = 1.5  # volume confirmation threshold


# ══════════════════════════════════════════════════════════════════
# SWING STRUCTURE
# ══════════════════════════════════════════════════════════════════

def detect_swing_points(df: pd.DataFrame, lookback: int = SWING_LOOKBACK) -> pd.DataFrame:
    """
    Detects swing highs and lows. A swing high is the highest point
    within lookback candles on each side. Same for swing lows.
    These define market structure — the skeleton of price action.
    """
    n = len(df)
    df["swing_high"] = False
    df["swing_low"]  = False

    for i in range(lookback, n - lookback):
        window_h = df["high"].iloc[i - lookback: i + lookback + 1]
        window_l = df["low"].iloc[i - lookback: i + lookback + 1]

        if df["high"].iloc[i] == window_h.max():
            df.loc[df.index[i], "swing_high"] = True
        if df["low"].iloc[i] == window_l.min():
            df.loc[df.index[i], "swing_low"] = True

    return df


def detect_market_structure(df: pd.DataFrame) -> pd.DataFrame:
    """
    Identifies Break of Structure (BOS) and Change of Character (CHOCH).

    BOS  : price closes above previous swing high (bullish) or
           below previous swing low (bearish)
    CHOCH: first opposing BOS after a series of same-direction BOS
           signals a potential trend reversal

    Also tracks the overall structure trend:
      structure_trend: "uptrend" | "downtrend" | "ranging"
    """
    df["bos_bull"]   = False
    df["bos_bear"]   = False
    df["choch_bull"] = False
    df["choch_bear"] = False
    df["structure_trend"] = "ranging"

    sh_prices = []  # recent swing high prices
    sl_prices = []  # recent swing low prices
    last_bos  = None

    for i in range(len(df)):
        if df["swing_high"].iloc[i]:
            sh_prices.append(df["high"].iloc[i])
            if len(sh_prices) > 5:
                sh_prices.pop(0)

        if df["swing_low"].iloc[i]:
            sl_prices.append(df["low"].iloc[i])
            if len(sl_prices) > 5:
                sl_prices.pop(0)

        if sh_prices:
            prev_sh = sh_prices[-1] if len(sh_prices) < 2 else max(sh_prices[-3:])
            if df["close"].iloc[i] > prev_sh:
                df.loc[df.index[i], "bos_bull"] = True
                if last_bos == "bear":
                    df.loc[df.index[i], "choch_bull"] = True
                last_bos = "bull"

        if sl_prices:
            prev_sl = sl_prices[-1] if len(sl_prices) < 2 else min(sl_prices[-3:])
            if df["close"].iloc[i] < prev_sl:
                df.loc[df.index[i], "bos_bear"] = True
                if last_bos == "bull":
                    df.loc[df.index[i], "choch_bear"] = True
                last_bos = "bear"

    # Rolling structure trend: last 20 days direction of BOS signals
    bull_bos = df["bos_bull"].rolling(20, min_periods=3).sum()
    bear_bos = df["bos_bear"].rolling(20, min_periods=3).sum()
    df.loc[bull_bos > bear_bos * 1.5, "structure_trend"] = "uptrend"
    df.loc[bear_bos > bull_bos * 1.5, "structure_trend"] = "downtrend"

    return df


# ══════════════════════════════════════════════════════════════════
# ORDER BLOCKS
# ══════════════════════════════════════════════════════════════════

def detect_order_blocks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Demand OB: last bearish candle before a significant bullish move.
    Supply OB: last bullish candle before a significant bearish move.

    The logic: institutions place large orders leaving a footprint —
    a candle that "caused" the subsequent move. When price returns
    to that candle's range, institutions defend it again.

    Also tracks whether each OB has been:
      - mitigated (price returned to it) — may weaken it
      - unmitigated — still valid, acts as strong S/R
    """
    df["is_demand_ob"]   = False
    df["is_supply_ob"]   = False
    df["ob_high"]        = np.nan
    df["ob_low"]         = np.nan
    df["ob_mitigated"]   = False
    df["near_demand_ob"] = False   # price within 1% of a demand OB
    df["near_supply_ob"] = False

    demand_obs = []  # list of {ob_high, ob_low, date_idx}
    supply_obs = []

    for i in range(1, len(df) - OB_LOOKFORWARD):
        c  = df["close"].iloc[i]
        o  = df["open"].iloc[i]
        ph = df["high"].iloc[i]
        pl = df["low"].iloc[i]

        # Max forward move in next OB_LOOKFORWARD candles
        fwd_highs = df["high"].iloc[i+1 : i+1+OB_LOOKFORWARD]
        fwd_lows  = df["low"].iloc[i+1 : i+1+OB_LOOKFORWARD]
        fwd_high  = fwd_highs.max() if len(fwd_highs) > 0 else c
        fwd_low   = fwd_lows.min()  if len(fwd_lows) > 0  else c

        # Demand OB: bearish candle + subsequent strong bullish move
        if c < o:  # bearish candle
            fwd_up_move = (fwd_high - c) / c
            if fwd_up_move >= MIN_OB_MOVE:
                df.loc[df.index[i], "is_demand_ob"] = True
                df.loc[df.index[i], "ob_high"]      = o    # bearish open
                df.loc[df.index[i], "ob_low"]        = c    # bearish close
                demand_obs.append({"high": o, "low": c, "idx": i})

        # Supply OB: bullish candle + subsequent strong bearish move
        if c > o:  # bullish candle
            fwd_down_move = (c - fwd_low) / c
            if fwd_down_move >= MIN_OB_MOVE:
                df.loc[df.index[i], "is_supply_ob"] = True
                df.loc[df.index[i], "ob_high"]      = c    # bullish close
                df.loc[df.index[i], "ob_low"]        = o    # bullish open
                supply_obs.append({"high": c, "low": o, "idx": i})

    # For each row, check if price is near an unmitigated OB
    for i in range(len(df)):
        curr_close = df["close"].iloc[i]
        curr_low   = df["low"].iloc[i]
        curr_high  = df["high"].iloc[i]

        # Check demand OBs
        for ob in demand_obs:
            if ob["idx"] >= i:
                continue  # future OB
            # Price entering demand OB zone (within 1%)
            if curr_low <= ob["high"] * 1.01 and curr_close >= ob["low"] * 0.99:
                df.loc[df.index[i], "near_demand_ob"] = True
                break

        # Check supply OBs
        for ob in supply_obs:
            if ob["idx"] >= i:
                continue
            if curr_high >= ob["low"] * 0.99 and curr_close <= ob["high"] * 1.01:
                df.loc[df.index[i], "near_supply_ob"] = True
                break

    return df


# ══════════════════════════════════════════════════════════════════
# FAIR VALUE GAPS
# ══════════════════════════════════════════════════════════════════

def detect_fvgs(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fair Value Gap (FVG / Imbalance):
    3-candle pattern where candle[i-1] and candle[i+1] don't overlap.

    Bullish FVG: candle[i+1].low > candle[i-1].high
    Bearish FVG: candle[i+1].high < candle[i-1].low

    These are "imbalances" in price — areas price skipped over.
    Price tends to return to fill these gaps before continuing.
    Entry when price returns to fill the FVG from the direction of the trend.
    """
    n = len(df)
    df["bullish_fvg"]      = False
    df["bearish_fvg"]      = False
    df["fvg_high"]         = np.nan
    df["fvg_low"]          = np.nan
    df["fvg_size_pct"]     = np.nan
    df["price_in_bull_fvg"] = False   # price currently filling a bullish FVG
    df["price_in_bear_fvg"] = False

    bull_fvgs = []  # {high, low, created_at_idx}
    bear_fvgs = []

    for i in range(1, n - 1):
        prev_high = df["high"].iloc[i - 1]
        prev_low  = df["low"].iloc[i - 1]
        next_high = df["high"].iloc[i + 1]
        next_low  = df["low"].iloc[i + 1]
        mid_close = df["close"].iloc[i]

        # Bullish FVG
        if next_low > prev_high:
            gap_size = (next_low - prev_high) / mid_close * 100
            if gap_size >= FVG_MIN_SIZE:
                df.loc[df.index[i], "bullish_fvg"]  = True
                df.loc[df.index[i], "fvg_high"]     = next_low
                df.loc[df.index[i], "fvg_low"]      = prev_high
                df.loc[df.index[i], "fvg_size_pct"] = round(gap_size, 2)
                bull_fvgs.append({"high": next_low, "low": prev_high, "idx": i})

        # Bearish FVG
        elif next_high < prev_low:
            gap_size = (prev_low - next_high) / mid_close * 100
            if gap_size >= FVG_MIN_SIZE:
                df.loc[df.index[i], "bearish_fvg"]  = True
                df.loc[df.index[i], "fvg_high"]     = prev_low
                df.loc[df.index[i], "fvg_low"]      = next_high
                df.loc[df.index[i], "fvg_size_pct"] = round(gap_size, 2)
                bear_fvgs.append({"high": prev_low, "low": next_high, "idx": i})

    # Check if current price is filling a previous FVG
    for i in range(2, n):
        cl = df["close"].iloc[i]
        lo = df["low"].iloc[i]
        hi = df["high"].iloc[i]

        for fvg in bull_fvgs:
            if fvg["idx"] >= i:
                continue
            # Price returning into bullish FVG from above
            if lo <= fvg["high"] and cl >= fvg["low"]:
                df.loc[df.index[i], "price_in_bull_fvg"] = True
                break

        for fvg in bear_fvgs:
            if fvg["idx"] >= i:
                continue
            # Price returning into bearish FVG from below
            if hi >= fvg["low"] and cl <= fvg["high"]:
                df.loc[df.index[i], "price_in_bear_fvg"] = True
                break

    return df


# ══════════════════════════════════════════════════════════════════
# LIQUIDITY SWEEPS
# ══════════════════════════════════════════════════════════════════

def detect_liquidity_sweeps(df: pd.DataFrame) -> pd.DataFrame:
    """
    Liquidity sweep: price briefly breaks below a swing low (hunting retail stops)
    then reverses strongly — smart money accumulated at the sweep.

    Bullish sweep: low goes below previous swing low, then closes above it.
    Bearish sweep: high goes above previous swing high, then closes below it.

    Entry: on the close of the reversal candle or next open.
    """
    df["bull_liq_sweep"]  = False
    df["bear_liq_sweep"]  = False

    # Get swing lows/highs indices
    sh_idx = df.index[df["swing_high"]].tolist()
    sl_idx = df.index[df["swing_low"]].tolist()

    # Convert to positional for lookup
    sh_pos = [df.index.get_loc(idx) for idx in sh_idx]
    sl_pos = [df.index.get_loc(idx) for idx in sl_idx]

    for i in range(2, len(df)):
        lo_today = df["low"].iloc[i]
        hi_today = df["high"].iloc[i]
        cl_today = df["close"].iloc[i]

        # Bullish sweep: today's low went below recent swing low, but close recovered above
        prev_sl_pos = [p for p in sl_pos if p < i]
        if prev_sl_pos:
            recent_sl_price = df["low"].iloc[prev_sl_pos[-1]]
            if lo_today < recent_sl_price and cl_today > recent_sl_price:
                df.loc[df.index[i], "bull_liq_sweep"] = True

        # Bearish sweep: today's high went above recent swing high, close below
        prev_sh_pos = [p for p in sh_pos if p < i]
        if prev_sh_pos:
            recent_sh_price = df["high"].iloc[prev_sh_pos[-1]]
            if hi_today > recent_sh_price and cl_today < recent_sh_price:
                df.loc[df.index[i], "bear_liq_sweep"] = True

    return df


# ══════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS (supporting layer)
# ══════════════════════════════════════════════════════════════════

def compute_technicals(df: pd.DataFrame) -> pd.DataFrame:
    """EMA stack, RSI, MACD, ATR, RVOL — supporting confirmation."""
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"].astype(float)

    # EMA stack
    df["ema20"]  = ta.trend.EMAIndicator(c, window=20,  fillna=False).ema_indicator()
    df["ema50"]  = ta.trend.EMAIndicator(c, window=50,  fillna=False).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(c, window=200, fillna=False).ema_indicator()

    # EMA posture
    df["price_above_ema20"]  = (c > df["ema20"]).astype(int)
    df["ema20_above_ema50"]  = (df["ema20"] > df["ema50"]).astype(int)
    df["ema50_above_ema200"] = (df["ema50"] > df["ema200"]).astype(int)

    # RSI — anticipation zone: 45-65 for longs (momentum building, not peaked)
    df["rsi"] = ta.momentum.RSIIndicator(c, window=14, fillna=False).rsi()
    df["rsi_long_zone"]  = ((df["rsi"] >= 45) & (df["rsi"] <= 65)).astype(int)
    df["rsi_short_zone"] = ((df["rsi"] >= 35) & (df["rsi"] <= 55)).astype(int)
    df["rsi_oversold"]   = (df["rsi"] < 35).astype(int)
    df["rsi_overbought"] = (df["rsi"] > 70).astype(int)

    # MACD
    macd = ta.trend.MACD(c, window_fast=12, window_slow=26, window_sign=9, fillna=False)
    df["macd_hist"]        = macd.macd_diff()
    df["macd_hist_rising"] = (df["macd_hist"] > df["macd_hist"].shift(1)).astype(int)
    df["macd_positive"]    = (df["macd_hist"] > 0).astype(int)

    # ATR
    df["atr"]     = ta.volatility.AverageTrueRange(h, l, c, window=14, fillna=False).average_true_range()
    df["atr_pct"] = (df["atr"] / c * 100).round(2)

    # RVOL
    df["vol_avg20"]      = v.rolling(20, min_periods=5).mean()
    df["rvol"]           = (v / df["vol_avg20"]).round(2)
    df["vol_spike"]      = (df["rvol"] >= 2.0).astype(int)
    df["vol_confirming"] = (df["rvol"] >= RVOL_THRESHOLD).astype(int)

    # Delivery % (institutional participation indicator)
    if "delivery_pct" in df.columns:
        df["delivery_avg"] = df["delivery_pct"].rolling(20, min_periods=5).mean()
        df["high_delivery"] = (df["delivery_pct"] > 55).astype(int)
        df["institutional_buying"] = (
            (df["close"] > df["prev_close"]) &
            (df["delivery_pct"] > 55) &
            (df["rvol"] >= 1.5)
        ).astype(int) if "prev_close" in df.columns else 0
    else:
        df["high_delivery"] = 0
        df["institutional_buying"] = 0

    # Gap analysis
    if "prev_close" in df.columns:
        df["gap_pct"] = ((df["open"] - df["prev_close"]) / df["prev_close"] * 100).round(2)
    else:
        df["gap_pct"] = 0.0

    # 52-week proximity
    df["high_52w"]         = h.rolling(252, min_periods=50).max()
    df["pct_from_52w_high"] = ((c - df["high_52w"]) / df["high_52w"] * 100).round(2)

    return df


# ══════════════════════════════════════════════════════════════════
# RELATIVE STRENGTH
# ══════════════════════════════════════════════════════════════════

def compute_relative_strength(df: pd.DataFrame, nifty_df: pd.DataFrame) -> pd.DataFrame:
    """
    RS = stock return / Nifty return over 5 and 20 days.
    RS > 1.0 = stock outperforming Nifty = institutional interest.
    """
    if nifty_df.empty or "nifty_close" not in nifty_df.columns:
        df["rs_5d"]  = 1.0
        df["rs_20d"] = 1.0
        df["rs_positive"] = 0
        return df

    merged = df.merge(nifty_df[["date","nifty_close"]], on="date", how="left")

    # 5-day RS
    stock_ret5  = merged["close"] / merged["close"].shift(5) - 1
    nifty_ret5  = merged["nifty_close"] / merged["nifty_close"].shift(5) - 1
    merged["rs_5d"]  = (stock_ret5 / nifty_ret5.replace(0, np.nan)).round(2)

    # 20-day RS
    stock_ret20 = merged["close"] / merged["close"].shift(20) - 1
    nifty_ret20 = merged["nifty_close"] / merged["nifty_close"].shift(20) - 1
    merged["rs_20d"] = (stock_ret20 / nifty_ret20.replace(0, np.nan)).round(2)

    # RS positive: stock outperforming on both timeframes
    merged["rs_positive"] = (
        (merged["rs_5d"] > 1.0) & (merged["rs_20d"] > 1.0)
    ).astype(int)

    df = merged.drop(columns=["nifty_close"])
    return df


# ══════════════════════════════════════════════════════════════════
# MARKET CONTEXT
# ══════════════════════════════════════════════════════════════════

def merge_market_context(df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    """Merges VIX, regime, breadth into stock data."""
    if market_df.empty:
        df["vix_close"]     = np.nan
        df["market_regime"] = "unknown"
        df["ad_ratio"]      = np.nan
        df["no_trade_zone"] = 0
        return df

    cols = [c for c in ["date","vix_close","market_regime","ad_ratio",
                         "nifty_close","advance_count","decline_count",
                         "nifty_open","nifty_high","nifty_low"]
            if c in market_df.columns]
    df = df.merge(market_df[cols], on="date", how="left")

    # No trade zone: VIX > 25 OR A/D ratio extreme
    df["no_trade_zone"] = 0
    if "vix_close" in df.columns:
        df.loc[df["vix_close"] > 25, "no_trade_zone"] = 1
    if "ad_ratio" in df.columns:
        # Both sides: extreme fear (A/D < 0.3) or both selling
        df.loc[df["ad_ratio"] < 0.3, "no_trade_zone"] = 1

    # Market regime score for longs
    df["regime_long_score"] = 0.0
    if "market_regime" in df.columns:
        df.loc[df["market_regime"] == "bull",     "regime_long_score"] = 1.0
        df.loc[df["market_regime"] == "sideways", "regime_long_score"] = 0.5
        df.loc[df["market_regime"] == "bear",     "regime_long_score"] = 0.0

    return df


# ══════════════════════════════════════════════════════════════════
# MASTER SMC PROCESSING
# ══════════════════════════════════════════════════════════════════

def compute_smc_signals(df: pd.DataFrame, market_df: pd.DataFrame) -> pd.DataFrame:
    """Runs the full SMC + technical pipeline on one stock."""
    df = df.sort_values("date").reset_index(drop=True)

    if len(df) < 50:
        return df

    # SMC layers
    df = detect_swing_points(df)
    df = detect_market_structure(df)
    df = detect_order_blocks(df)
    df = detect_fvgs(df)
    df = detect_liquidity_sweeps(df)

    # Technical confirmation
    df = compute_technicals(df)

    # Market context — merges nifty_close, vix, regime, breadth in one pass
    df = merge_market_context(df, market_df)

    # Relative strength — computed in-place using nifty_close already merged above
    if "nifty_close" in df.columns:
        stock_ret5  = df["close"] / df["close"].shift(5) - 1
        nifty_ret5  = df["nifty_close"] / df["nifty_close"].shift(5) - 1
        df["rs_5d"]  = (stock_ret5 / nifty_ret5.replace(0, np.nan)).round(2)

        stock_ret20 = df["close"] / df["close"].shift(20) - 1
        nifty_ret20 = df["nifty_close"] / df["nifty_close"].shift(20) - 1
        df["rs_20d"] = (stock_ret20 / nifty_ret20.replace(0, np.nan)).round(2)

        df["rs_positive"] = (
            (df["rs_5d"] > 1.0) & (df["rs_20d"] > 1.0)
        ).astype(int)
    else:
        df["rs_5d"] = 1.0
        df["rs_20d"] = 1.0
        df["rs_positive"] = 0

    # Warmup flag
    df["is_warmup"] = range(len(df))
    df["is_warmup"] = df["is_warmup"] < 50

    return df


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    log.info("THE STOCK LOGIC — Stage 2b: SMC Signal Engine")
    SMC_DIR.mkdir(parents=True, exist_ok=True)

    # Load market data
    market_df = pd.DataFrame()
    if MARKET_FILE.exists():
        market_df = pd.read_parquet(MARKET_FILE)
        market_df["date"] = pd.to_datetime(market_df["date"])
        log.info(f"Market data: {len(market_df)} rows")

    files = sorted(STOCKS_DIR.glob("*.parquet"))
    log.info(f"Processing {len(files)} stocks...")

    ok, failed, skipped = [], [], []

    for f in tqdm(files, desc="SMC signals"):
        sym = f.stem
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            if len(df) < 50:
                skipped.append(sym)
                continue
            df = compute_smc_signals(df, market_df)
            df.to_parquet(SMC_DIR / f"{sym}.parquet", index=False)
            ok.append(sym)
        except Exception as e:
            log.error(f"{sym}: {e}")
            failed.append(sym)

    log.info(f"\nCOMPLETE  OK:{len(ok)}  Failed:{len(failed)}  Skipped:{len(skipped)}")

    # Spot check
    rel = SMC_DIR / "RELIANCE.parquet"
    if rel.exists():
        df = pd.read_parquet(rel)
        live = df[~df["is_warmup"]]
        log.info(f"\nSpot check — RELIANCE ({len(live)} rows)")
        log.info(f"  Swing highs     : {live['swing_high'].sum()}")
        log.info(f"  Swing lows      : {live['swing_low'].sum()}")
        log.info(f"  BOS bullish     : {live['bos_bull'].sum()}")
        log.info(f"  BOS bearish     : {live['bos_bear'].sum()}")
        log.info(f"  CHOCH bull      : {live['choch_bull'].sum()}")
        log.info(f"  Demand OBs      : {live['is_demand_ob'].sum()}")
        log.info(f"  Supply OBs      : {live['is_supply_ob'].sum()}")
        log.info(f"  Near demand OB  : {live['near_demand_ob'].sum()}")
        log.info(f"  Bullish FVGs    : {live['bullish_fvg'].sum()}")
        log.info(f"  Price in bull FVG: {live['price_in_bull_fvg'].sum()}")
        log.info(f"  Liq sweeps bull : {live['bull_liq_sweep'].sum()}")
        log.info(f"  Structure trend :\n{live['structure_trend'].value_counts().to_string()}")
        log.info(f"  RS positive days: {live['rs_positive'].sum()}")
        log.info(f"  Institutional buy: {live['institutional_buying'].sum()}")
        log.info(f"  No trade zone   : {live['no_trade_zone'].sum()} days")

    log.info(f"\nSTATUS: {'PASS' if len(ok)>=80 else 'PARTIAL'}")
    log.info("Next: python3 engine/03b_score.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
