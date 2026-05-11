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
    handlers=[logging.FileHandler("reports/02_indicators.log"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

STOCKS_DIR     = Path("data/processed/stocks")
INDICATORS_DIR = Path("data/processed/indicators")
MARKET_FILE    = Path("data/processed/market.parquet")
WARMUP_DAYS    = 200

def compute_all_indicators(df, market_df):
    df = df.sort_values("date").reset_index(drop=True)
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"].astype(float)
    df["ema9"]   = ta.trend.EMAIndicator(c, window=9,   fillna=False).ema_indicator()
    df["ema21"]  = ta.trend.EMAIndicator(c, window=21,  fillna=False).ema_indicator()
    df["ema50"]  = ta.trend.EMAIndicator(c, window=50,  fillna=False).ema_indicator()
    df["ema200"] = ta.trend.EMAIndicator(c, window=200, fillna=False).ema_indicator()
    df["vwap_proxy"]       = (h + l + c) / 3
    df["price_above_vwap"] = (c > df["vwap_proxy"]).astype(int)
    df["ema_align_score"]  = (
        (df["ema9"] > df["ema21"]).astype(int) +
        (df["ema21"] > df["ema50"]).astype(int) +
        (df["ema50"] > df["ema200"]).astype(int) +
        (c > df["ema9"]).astype(int)
    )
    def align_label(row):
        if pd.isna(row["ema200"]): return "warmup"
        e9,e21,e50,e200 = row["ema9"],row["ema21"],row["ema50"],row["ema200"]
        if e9>e21>e50>e200: return "full_bull"
        if e9>e21>e50:      return "bull"
        if e9>e21:          return "weak_bull"
        if e9<e21<e50<e200: return "full_bear"
        if e9<e21<e50:      return "bear"
        if e9<e21:          return "weak_bear"
        return "neutral"
    df["ema_alignment"] = df.apply(align_label, axis=1)
    prev = df["ema9"].shift(1)-df["ema21"].shift(1)
    curr = df["ema9"]-df["ema21"]
    df["ema_cross"] = 0
    df.loc[(prev<0)&(curr>0),"ema_cross"] =  1
    df.loc[(prev>0)&(curr<0),"ema_cross"] = -1
    df["rsi"] = ta.momentum.RSIIndicator(c, window=14, fillna=False).rsi()
    df["rsi_zone"] = "neutral"
    df.loc[df["rsi"]>=70,"rsi_zone"]                        = "overbought"
    df.loc[(df["rsi"]>=55)&(df["rsi"]<70),"rsi_zone"]      = "bull_momentum"
    df.loc[(df["rsi"]>=30)&(df["rsi"]<45),"rsi_zone"]      = "weak"
    df.loc[df["rsi"]<30,"rsi_zone"]                         = "oversold"
    df["rsi_score"] = 0.0
    df.loc[df["rsi_zone"]=="bull_momentum","rsi_score"] = 10.0
    df.loc[df["rsi_zone"]=="overbought","rsi_score"]    =  5.0
    df.loc[df["rsi_zone"]=="oversold","rsi_score"]      =  3.0
    macd = ta.trend.MACD(c, window_fast=12, window_slow=26, window_sign=9, fillna=False)
    df["macd_line"]        = macd.macd()
    df["macd_signal_line"] = macd.macd_signal()
    df["macd_hist"]        = macd.macd_diff()
    df["macd_hist_rising"]   = (df["macd_hist"]>df["macd_hist"].shift(1)).astype(int)
    df["macd_hist_positive"] = (df["macd_hist"]>0).astype(int)
    df["macd_score"] = df["macd_hist_rising"]*5.0 + df["macd_hist_positive"]*5.0
    p2 = df["macd_line"].shift(1)-df["macd_signal_line"].shift(1)
    c2 = df["macd_line"]-df["macd_signal_line"]
    df["macd_cross"] = 0
    df.loc[(p2<0)&(c2>0),"macd_cross"] =  1
    df.loc[(p2>0)&(c2<0),"macd_cross"] = -1
    df["vol_avg20"]       = v.rolling(20, min_periods=5).mean()
    df["rvol"]            = (v/df["vol_avg20"]).round(2)
    df["rvol_score"]      = 0.0
    df.loc[df["rvol"]>=1.5,"rvol_score"] = 10.0
    df.loc[(df["rvol"]>=1.0)&(df["rvol"]<1.5),"rvol_score"] = 5.0
    df["rvol_disqualify"] = (df["rvol"]<0.7).astype(int)
    df["obv"]             = ta.volume.OnBalanceVolumeIndicator(c, v, fillna=False).on_balance_volume()
    df["obv_slope"]       = df["obv"].diff(5)
    df["obv_rising"]      = (df["obv_slope"]>0).astype(int)
    df["obv_score"]       = df["obv_rising"]*10.0
    df["vol_spike"]       = (df["rvol"]>=2.0).astype(int)
    if "delivery_pct" in df.columns:
        df["delivery_avg20"]     = df["delivery_pct"].rolling(20, min_periods=5).mean()
        df["delivery_above_avg"] = (df["delivery_pct"]>df["delivery_avg20"]).astype(int)
        if "prev_close" in df.columns:
            df["delivery_up_day"] = ((c>df["prev_close"])&(df["delivery_pct"]>50)).astype(int)
        else:
            df["delivery_up_day"] = 0
    else:
        df["delivery_avg20"]=df["delivery_above_avg"]=df["delivery_up_day"]=0
    df["atr"]     = ta.volatility.AverageTrueRange(h,l,c,window=14,fillna=False).average_true_range()
    df["atr_pct"] = (df["atr"]/c*100).round(2)
    bb = ta.volatility.BollingerBands(c, window=20, window_dev=2.0, fillna=False)
    df["bb_upper"]     = bb.bollinger_hband()
    df["bb_lower"]     = bb.bollinger_lband()
    df["bb_mid"]       = bb.bollinger_mavg()
    df["bb_width"]     = bb.bollinger_wband()
    df["bb_width_avg"] = df["bb_width"].rolling(20, min_periods=5).mean()
    df["bb_squeeze"]   = (df["bb_width"]<df["bb_width_avg"]).astype(int)
    bb_range = df["bb_upper"]-df["bb_lower"]
    df["bb_position"]  = ((c-df["bb_lower"])/bb_range.replace(0,np.nan)).round(2)
    df["trade_viable"] = ((df["atr_pct"]>=1.0)&(df["atr_pct"]<=8.0)).astype(int)
    df["viability_score"] = 0.0
    df.loc[df["trade_viable"]==1,"viability_score"] = 15.0
    df.loc[(df["atr_pct"]>=0.5)&(df["atr_pct"]<1.0),"viability_score"] = 7.0
    if "prev_close" in df.columns:
        df["gap_pct"]       = ((df["open"]-df["prev_close"])/df["prev_close"]*100).round(2)
        df["up_today_pct"]  = ((c-df["prev_close"])/df["prev_close"]*100).round(2)
        df["fomo_disqualify"] = (df["up_today_pct"]>4.0).astype(int)
        df["gap_type"] = "flat"
        df.loc[df["gap_pct"]>1.5,"gap_type"]                          = "large_gap_up"
        df.loc[(df["gap_pct"]>0.5)&(df["gap_pct"]<=1.5),"gap_type"]  = "gap_up"
        df.loc[df["gap_pct"]<-1.5,"gap_type"]                         = "large_gap_down"
        df.loc[(df["gap_pct"]<-0.5)&(df["gap_pct"]>=-1.5),"gap_type"]= "gap_down"
    else:
        df["gap_pct"]=df["up_today_pct"]=0.0
        df["fomo_disqualify"]=0
        df["gap_type"]="flat"
    df["prev_high"]    = h.shift(1)
    df["prev_low"]     = l.shift(1)
    df["pdh_breakout"] = (c>df["prev_high"]).astype(int)
    df["mom5"]         = ((c-c.shift(5))/c.shift(5)*100).round(2)
    df["mom10"]        = ((c-c.shift(10))/c.shift(10)*100).round(2)
    df["high_52w"]     = h.rolling(252,min_periods=50).max()
    df["low_52w"]      = l.rolling(252,min_periods=50).min()
    df["pct_from_52w_high"] = ((c-df["high_52w"])/df["high_52w"]*100).round(2)
    df["near_52w_high"] = (df["pct_from_52w_high"]>=-2.0).astype(int)
    if not market_df.empty:
        cols = [x for x in ["date","vix_close","market_regime","ad_ratio","nifty_close"]
                if x in market_df.columns]
        df = df.merge(market_df[cols], on="date", how="left")
    df["vix_disqualify"] = (df["vix_close"]>25.0).astype(int) if "vix_close" in df.columns else 0
    df["ad_score"] = 0.0
    if "ad_ratio" in df.columns:
        df.loc[df["ad_ratio"]>=1.5,"ad_score"] = 5.0
        df.loc[(df["ad_ratio"]>=1.0)&(df["ad_ratio"]<1.5),"ad_score"] = 2.5
    df["is_warmup"] = False
    if len(df)>WARMUP_DAYS:
        df.iloc[:WARMUP_DAYS, df.columns.get_loc("is_warmup")] = True
    return df

def main():
    log.info("THE STOCK LOGIC — Stage 2: Indicator Engine")
    INDICATORS_DIR.mkdir(parents=True, exist_ok=True)
    market_df = pd.DataFrame()
    if MARKET_FILE.exists():
        market_df = pd.read_parquet(MARKET_FILE)
        market_df["date"] = pd.to_datetime(market_df["date"])
        log.info(f"Market data: {len(market_df)} rows")
    files = sorted(STOCKS_DIR.glob("*.parquet"))
    log.info(f"Processing {len(files)} stocks...")
    ok, failed, skipped = [], [], []
    for f in tqdm(files, desc="Computing indicators"):
        sym = f.stem
        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            if len(df)<50:
                skipped.append(sym); continue
            df = compute_all_indicators(df, market_df)
            df.to_parquet(INDICATORS_DIR/f"{sym}.parquet", index=False)
            ok.append(sym)
        except Exception as e:
            log.error(f"{sym}: {e}"); failed.append(sym)
    log.info(f"\nCOMPLETE  OK:{len(ok)}  Failed:{len(failed)}  Skipped:{len(skipped)}")
    if failed: log.warning(f"Failed: {failed}")
    rel = INDICATORS_DIR/"RELIANCE.parquet"
    if rel.exists():
        df = pd.read_parquet(rel)
        live = df[~df["is_warmup"]]
        log.info(f"\nRELIANCE ({len(live)} tradeable rows)")
        log.info(f"  EMA alignment:\n{live['ema_alignment'].value_counts().to_string()}")
        log.info(f"  RSI: {live['rsi'].min():.1f}–{live['rsi'].max():.1f}")
        log.info(f"  RVOL: {live['rvol'].min():.2f}–{live['rvol'].max():.2f}")
        log.info(f"  ATR%: {live['atr_pct'].min():.2f}–{live['atr_pct'].max():.2f}")
        log.info(f"  Viable: {live['trade_viable'].sum()}/{len(live)}")
    log.info(f"\nSTATUS: {'PASS' if len(ok)>=80 else 'PARTIAL'}")
    log.info("Next: python3 engine/03_signals.py")

if __name__=="__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
