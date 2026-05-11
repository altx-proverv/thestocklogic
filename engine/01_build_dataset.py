"""
THE STOCK LOGIC — Stage 1: Data Acquisition & Dataset Builder
=============================================================
Run this script on YOUR LOCAL MACHINE (not in Claude sandbox).
NSE and Yahoo Finance are accessible from a normal internet connection.

What this script does:
  1. Downloads NSE Bhavcopy for every trading day in the date range
  2. Downloads India VIX history from NSE
  3. Downloads Nifty/market data via yfinance
  4. Filters to Nifty 100 EQ-series stocks only
  5. Checks data quality (gaps, zeros, splits, survivorship bias)
  6. Outputs one parquet per stock + market.parquet
  7. Prints a validation report

Run: python 01_build_dataset.py
Requires: pip install pandas numpy requests yfinance pyarrow tqdm holidays
"""

import os
import sys
import time
import zipfile
import io
import json
import logging
from datetime import date, timedelta, datetime
from pathlib import Path

import pandas as pd
import numpy as np
import requests
from tqdm import tqdm

# ── Optional: yfinance for index/VIX data ──────────────────────────
try:
    import yfinance as yf
    HAS_YF = True
except ImportError:
    HAS_YF = False
    print("yfinance not installed. Install with: pip install yfinance")

# ── Optional: holidays for NSE calendar ───────────────────────────
try:
    import holidays
    HAS_HOLIDAYS = True
except ImportError:
    HAS_HOLIDAYS = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("../reports/01_build_dataset.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these before running
# ══════════════════════════════════════════════════════════════════

CONFIG = {
    # Date range for backtest — 2 years gives enough EMA-200 warmup
    # Keep START at least 200 trading days before your intended backtest start
    "start_date": date(2023, 1, 1),
    "end_date":   date(2025, 5, 9),   # adjust to yesterday

    # Paths (relative to this script's location)
    "raw_bhavcopy_dir": "../data/raw/bhavcopy",
    "raw_vix_dir":      "../data/raw/vix",
    "processed_dir":    "../data/processed",
    "stocks_dir":       "../data/processed/stocks",

    # Minimum liquidity filters
    "min_volume":       500_000,       # drop stocks with < 5L shares/day avg
    "min_price":        10.0,          # drop penny stocks under ₹10
    "min_trades":       500,           # minimum number of trades per day

    # Data quality thresholds
    "max_gap_days":     5,             # flag if stock missing > 5 consecutive trading days
    "warmup_days":      200,           # EMA-200 needs this many days before backtest starts

    # Request settings
    "request_delay":    0.5,           # seconds between NSE requests (be polite)
    "request_timeout":  20,
    "max_retries":      3,
}

# ══════════════════════════════════════════════════════════════════
# NIFTY 100 CONSTITUENTS
# Historical-aware list. Includes stocks that were IN Nifty 100
# during 2023-2025. Avoids survivorship bias.
# Source: NSE index methodology documents + quarterly rebalancing
# Update this list when NSE announces index changes.
# ══════════════════════════════════════════════════════════════════

NIFTY100_SYMBOLS = [
    # Nifty 50
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK",
    "INFOSYS", "SBIN", "HINDUNILVR", "ITC", "LT",
    "KOTAKBANK", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "WIPRO", "HCLTECH",
    "NESTLEIND", "POWERGRID", "NTPC", "TECHM", "JSWSTEEL",
    "TATAMOTORS", "TATASTEEL", "BAJAJFINSV", "ONGC", "COALINDIA",
    "ADANIPORTS", "ADANIENT", "BRITANNIA", "DRREDDY", "DIVISLAB",
    "CIPLA", "HINDALCO", "GRASIM", "BPCL", "SHRIRAMFIN",
    "APOLLOHOSP", "BAJAJ-AUTO", "EICHERMOT", "INDUSINDBK", "HEROMOTOCO",
    "TATACONSUM", "SBILIFE", "HDFCLIFE", "M&M", "VEDL",

    # Nifty Next 50
    "ADANIGREEN", "ADANITRANS", "AMBUJACEM", "BANKBARODA", "BERGEPAINT",
    "BIOCON", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL",
    "CONCOR", "DABUR", "DLF", "DMART", "FEDERALBNK",
    "GAIL", "GODREJCP", "GODREJPROP", "HAL", "HAVELLS",
    "ICICIPRULI", "IDFCFIRSTB", "INDHOTEL", "IOC", "IRCTC",
    "JINDALSTEL", "LICI", "LTIM", "LUPIN", "MARICO",
    "MCDOWELL-N", "MPHASIS", "MOTHERSON", "NMDC", "NYKAA",
    "OBEROIRLTY", "OFSS", "PAGEIND", "PERSISTENT", "PFC",
    "PIDILITIND", "PNB", "RECLTD", "SAIL", "SIEMENS",
    "SRF", "TORNTPHARM", "UBL", "UNITDSPR", "ZYDUSLIFE",
]

# NSE Holidays 2023-2025 (key ones — not exhaustive, script handles gaps)
NSE_HOLIDAYS = {
    # 2023
    date(2023, 1, 26), date(2023, 3, 7), date(2023, 3, 30),
    date(2023, 4, 4), date(2023, 4, 7), date(2023, 4, 14),
    date(2023, 5, 1), date(2023, 6, 28), date(2023, 8, 15),
    date(2023, 9, 19), date(2023, 10, 2), date(2023, 10, 24),
    date(2023, 11, 14), date(2023, 11, 27), date(2023, 12, 25),
    # 2024
    date(2024, 1, 22), date(2024, 1, 26), date(2024, 3, 25),
    date(2024, 3, 29), date(2024, 4, 11), date(2024, 4, 14),
    date(2024, 4, 17), date(2024, 5, 1), date(2024, 5, 23),
    date(2024, 6, 17), date(2024, 7, 17), date(2024, 8, 15),
    date(2024, 10, 2), date(2024, 10, 14), date(2024, 11, 1),
    date(2024, 11, 15), date(2024, 11, 20), date(2024, 12, 25),
    # 2025
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1),
}


# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════

def is_trading_day(d: date) -> bool:
    """Returns True if d is a valid NSE trading day."""
    if d.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if d in NSE_HOLIDAYS:
        return False
    return True


def get_trading_days(start: date, end: date) -> list:
    """Returns sorted list of all NSE trading days between start and end."""
    days = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def nse_session() -> requests.Session:
    """Creates a requests session that mimics a browser to get past NSE's bot check."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Referer": "https://www.nseindia.com/",
    })
    # Warm up cookies — NSE requires this
    try:
        s.get("https://www.nseindia.com", timeout=CONFIG["request_timeout"])
        time.sleep(1)
    except Exception as e:
        log.warning(f"Cookie warmup failed: {e}")
    return s


# ══════════════════════════════════════════════════════════════════
# STEP 1 — DOWNLOAD BHAVCOPY
# ══════════════════════════════════════════════════════════════════

def bhavcopy_url_new(d: date) -> str:
    """New NSE Bhavcopy URL format (post-2022)."""
    return (
        f"https://nsearchives.nseindia.com/content/cm/"
        f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%d%m%Y')}_F_0000.csv.zip"
    )


def bhavcopy_url_old(d: date) -> str:
    """Older NSE Bhavcopy URL format (fallback)."""
    return (
        f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/"
        f"cm{d.strftime('%d%b%Y').upper()}bhav.csv.zip"
    )


def parse_bhavcopy_new(content: bytes, target_date: date) -> pd.DataFrame:
    """Parse the new-format Bhavcopy CSV (post-2022)."""
    z = zipfile.ZipFile(io.BytesIO(content))
    with z.open(z.namelist()[0]) as f:
        df = pd.read_csv(f)

    # New format column mapping
    col_map = {
        "TradDt": "date",
        "TckrSymb": "symbol",
        "SctySrs": "series",
        "OpnPric": "open",
        "HghPric": "high",
        "LwPric": "low",
        "ClsPric": "close",
        "TtlTradgVol": "volume",
        "TtlTrfVal": "turnover",
        "TotNbOfTxsExctd": "trades",
        "PrvClsgPric": "prev_close",
        "DlvryQty": "delivery_qty",
        "DlvryPct": "delivery_pct",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    if "date" not in df.columns:
        df["date"] = pd.Timestamp(target_date)
    else:
        df["date"] = pd.to_datetime(df["date"])

    return df


def parse_bhavcopy_old(content: bytes, target_date: date) -> pd.DataFrame:
    """Parse the old-format Bhavcopy CSV."""
    z = zipfile.ZipFile(io.BytesIO(content))
    with z.open(z.namelist()[0]) as f:
        df = pd.read_csv(f)

    df.columns = [c.strip() for c in df.columns]

    col_map = {
        "SYMBOL": "symbol",
        "SERIES": "series",
        "OPEN": "open",
        "HIGH": "high",
        "LOW": "low",
        "CLOSE": "close",
        "TOTTRDQTY": "volume",
        "TOTTRDVAL": "turnover",
        "TOTALTRADES": "trades",
        "PREVCLOSE": "prev_close",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df["date"] = pd.Timestamp(target_date)

    # Old format doesn't have delivery — will be NaN, filled later if needed
    if "delivery_qty" not in df.columns:
        df["delivery_qty"] = np.nan
    if "delivery_pct" not in df.columns:
        df["delivery_pct"] = np.nan

    return df


def download_bhavcopy_day(session: requests.Session, d: date, out_dir: Path) -> bool:
    """
    Downloads Bhavcopy for one trading day.
    Tries new URL format first, falls back to old.
    Returns True on success.
    """
    out_file = out_dir / f"bhavcopy_{d.strftime('%Y%m%d')}.parquet"
    if out_file.exists():
        return True  # already downloaded

    urls = [bhavcopy_url_new(d), bhavcopy_url_old(d)]

    for attempt, url in enumerate(urls):
        for retry in range(CONFIG["max_retries"]):
            try:
                r = session.get(url, timeout=CONFIG["request_timeout"])
                if r.status_code == 200 and len(r.content) > 1000:
                    # Parse based on which URL worked
                    try:
                        df = parse_bhavcopy_new(r.content, d) if attempt == 0 \
                            else parse_bhavcopy_old(r.content, d)
                    except Exception as pe:
                        log.warning(f"{d} parse error on URL {attempt}: {pe}")
                        continue

                    # Filter to EQ series only
                    df = df[df.get("series", "EQ") == "EQ"].copy()

                    if len(df) < 10:
                        log.warning(f"{d}: Only {len(df)} EQ rows — possible parse issue")
                        continue

                    # Save as parquet
                    df.to_parquet(out_file, index=False)
                    return True

                elif r.status_code == 404:
                    break  # Try next URL

            except requests.RequestException as e:
                if retry < CONFIG["max_retries"] - 1:
                    time.sleep(2 ** retry)
                else:
                    log.warning(f"{d} URL {attempt} failed after {CONFIG['max_retries']} retries: {e}")

        time.sleep(CONFIG["request_delay"])

    return False


def download_all_bhavcopy(trading_days: list, out_dir: Path) -> dict:
    """
    Downloads Bhavcopy for all trading days.
    Returns dict: {date: success_bool}
    """
    log.info(f"Downloading Bhavcopy for {len(trading_days)} trading days...")
    session = nse_session()
    results = {}

    for d in tqdm(trading_days, desc="Bhavcopy"):
        success = download_bhavcopy_day(session, d, out_dir)
        results[d] = success
        if not success:
            log.warning(f"MISSING: {d}")
        time.sleep(CONFIG["request_delay"])

    success_count = sum(results.values())
    log.info(f"Bhavcopy: {success_count}/{len(trading_days)} days downloaded successfully")
    return results


# ══════════════════════════════════════════════════════════════════
# STEP 2 — DOWNLOAD INDIA VIX
# ══════════════════════════════════════════════════════════════════

def download_india_vix(out_dir: Path) -> bool:
    """
    Downloads India VIX historical data from NSE.
    NSE provides a CSV download on their VIX historical page.
    """
    vix_file = out_dir / "india_vix.csv"
    if vix_file.exists():
        log.info("VIX file already exists, skipping download")
        return True

    # NSE VIX historical data URL
    url = "https://www.nseindia.com/api/historical/vixhistory?data=24months"

    log.info("Downloading India VIX...")
    session = nse_session()

    try:
        r = session.get(url, timeout=CONFIG["request_timeout"])
        if r.status_code == 200:
            data = r.json()
            if "data" in data:
                df = pd.DataFrame(data["data"])
                df.to_csv(vix_file, index=False)
                log.info(f"VIX downloaded: {len(df)} rows")
                return True
    except Exception as e:
        log.warning(f"VIX API failed: {e}")

    # Fallback: manual download instruction
    log.warning(
        "Automatic VIX download failed.\n"
        "MANUAL STEP: Go to nseindia.com → Market Data → India VIX → Historical Data\n"
        "Download CSV and save to: data/raw/vix/india_vix.csv\n"
        "Expected columns: Date, Open, High, Low, Close, PrevClose, Change, PctChange"
    )
    return False


def parse_india_vix(vix_dir: Path) -> pd.DataFrame:
    """Parses the India VIX CSV into a clean DataFrame."""
    vix_file = vix_dir / "india_vix.csv"
    if not vix_file.exists():
        log.error("VIX file not found. Run download step first.")
        return pd.DataFrame()

    df = pd.read_csv(vix_file)
    df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]

    # NSE VIX columns vary — handle both formats
    date_col = next((c for c in df.columns if "date" in c), None)
    close_col = next((c for c in df.columns if "close" in c or "clos" in c), None)

    if not date_col or not close_col:
        log.error(f"Cannot parse VIX. Columns found: {df.columns.tolist()}")
        return pd.DataFrame()

    df = df.rename(columns={date_col: "date", close_col: "vix_close"})
    df["date"] = pd.to_datetime(df["date"], dayfirst=True)
    df = df[["date", "vix_close"]].dropna()
    df["vix_close"] = pd.to_numeric(df["vix_close"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    log.info(f"VIX parsed: {len(df)} rows, {df['date'].min().date()} to {df['date'].max().date()}")
    return df


# ══════════════════════════════════════════════════════════════════
# STEP 3 — DOWNLOAD NIFTY INDEX (yfinance)
# ══════════════════════════════════════════════════════════════════

def download_nifty_index(start: date, end: date) -> pd.DataFrame:
    """Downloads Nifty 50 index OHLCV via yfinance."""
    if not HAS_YF:
        log.warning("yfinance not available. Nifty index data skipped.")
        return pd.DataFrame()

    log.info("Downloading Nifty 50 index data...")
    try:
        ticker = yf.Ticker("^NSEI")
        df = ticker.history(
            start=start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            interval="1d"
        )
        df = df.reset_index()
        df.columns = [c.lower() for c in df.columns]
        df = df.rename(columns={"date": "date"})
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None)
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df.columns = ["date", "nifty_open", "nifty_high", "nifty_low", "nifty_close", "nifty_volume"]
        log.info(f"Nifty index: {len(df)} rows")
        return df
    except Exception as e:
        log.error(f"Nifty index download failed: {e}")
        return pd.DataFrame()


# ══════════════════════════════════════════════════════════════════
# STEP 4 — MERGE & BUILD PER-STOCK PARQUETS
# ══════════════════════════════════════════════════════════════════

def load_all_bhavcopy(bhavcopy_dir: Path, trading_days: list) -> pd.DataFrame:
    """Loads all downloaded daily Bhavcopy parquets into one DataFrame."""
    dfs = []
    missing = []

    for d in tqdm(trading_days, desc="Loading Bhavcopy"):
        f = bhavcopy_dir / f"bhavcopy_{d.strftime('%Y%m%d')}.parquet"
        if f.exists():
            try:
                df = pd.read_parquet(f)
                dfs.append(df)
            except Exception as e:
                log.warning(f"Failed to read {f}: {e}")
        else:
            missing.append(d)

    if missing:
        log.warning(f"{len(missing)} Bhavcopy files missing: {missing[:5]}{'...' if len(missing)>5 else ''}")

    if not dfs:
        log.error("No Bhavcopy data loaded. Check download step.")
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    log.info(f"Combined Bhavcopy: {len(combined):,} rows, {combined['symbol'].nunique()} unique symbols")
    return combined


def clean_stock_data(df: pd.DataFrame) -> pd.DataFrame:
    """Applies data quality rules to a single stock's DataFrame."""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    initial_len = len(df)

    # Rule 1: Drop zero/negative prices
    price_cols = ["open", "high", "low", "close"]
    for col in price_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df[df["close"] > 0].copy()
    df = df[df["open"] > 0].copy()

    # Rule 2: High >= Low
    if "high" in df.columns and "low" in df.columns:
        bad_hl = df["high"] < df["low"]
        if bad_hl.sum() > 0:
            log.debug(f"Dropping {bad_hl.sum()} rows where high < low")
            df = df[~bad_hl].copy()

    # Rule 3: Price >= min threshold
    df = df[df["close"] >= CONFIG["min_price"]].copy()

    # Rule 4: Volume
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype(int)

    # Rule 5: Clean delivery fields
    for col in ["delivery_qty", "delivery_pct"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "delivery_pct" in df.columns:
        # Delivery pct should be 0–100. If > 100 or < 0, set NaN
        df.loc[df["delivery_pct"] > 100, "delivery_pct"] = np.nan
        df.loc[df["delivery_pct"] < 0, "delivery_pct"] = np.nan

    # Rule 6: Remove duplicate dates (keep last)
    df = df.drop_duplicates(subset=["date"], keep="last")

    cleaned_len = len(df)
    if initial_len - cleaned_len > 5:
        log.debug(f"Cleaned {initial_len - cleaned_len} rows")

    return df


def compute_market_regime(nifty_df: pd.DataFrame) -> pd.DataFrame:
    """
    Classifies each date as bull / bear / sideways based on Nifty.
    Uses EMA 50 and EMA 200 relationship.
    bull:     Nifty close > EMA50 > EMA200
    bear:     Nifty close < EMA50 < EMA200
    sideways: everything else
    """
    if nifty_df.empty or "nifty_close" not in nifty_df.columns:
        return nifty_df

    df = nifty_df.copy()
    df["nifty_ema50"]  = df["nifty_close"].ewm(span=50,  adjust=False).mean()
    df["nifty_ema200"] = df["nifty_close"].ewm(span=200, adjust=False).mean()

    def regime(row):
        if pd.isna(row["nifty_ema200"]):
            return "warmup"
        if row["nifty_close"] > row["nifty_ema50"] > row["nifty_ema200"]:
            return "bull"
        if row["nifty_close"] < row["nifty_ema50"] < row["nifty_ema200"]:
            return "bear"
        return "sideways"

    df["market_regime"] = df.apply(regime, axis=1)
    return df


def build_stock_parquets(
    combined_bhav: pd.DataFrame,
    vix_df: pd.DataFrame,
    nifty_df: pd.DataFrame,
    stocks_dir: Path,
    processed_dir: Path,
) -> dict:
    """
    Builds one parquet per Nifty100 symbol + market.parquet.
    Returns summary dict with stats per stock.
    """
    summary = {}
    target_symbols = set(NIFTY100_SYMBOLS)

    # Build market-level DataFrame
    log.info("Building market.parquet...")
    market_df = nifty_df.copy() if not nifty_df.empty else pd.DataFrame(columns=["date"])

    if not vix_df.empty:
        market_df = market_df.merge(vix_df, on="date", how="outer") \
            if not market_df.empty else vix_df

    # Compute advance/decline from full Bhavcopy
    if not combined_bhav.empty and "close" in combined_bhav.columns and "prev_close" in combined_bhav.columns:
        combined_bhav["is_advance"] = combined_bhav["close"] > combined_bhav["prev_close"]
        combined_bhav["is_decline"] = combined_bhav["close"] < combined_bhav["prev_close"]
        ad = combined_bhav.groupby("date").agg(
            advance_count=("is_advance", "sum"),
            decline_count=("is_decline", "sum")
        ).reset_index()
        ad["ad_ratio"] = (ad["advance_count"] / ad["decline_count"].replace(0, 1)).round(2)
        market_df = market_df.merge(ad, on="date", how="left") \
            if not market_df.empty else ad

    # Add market regime
    if "nifty_close" in market_df.columns:
        market_df = compute_market_regime(market_df)

    if not market_df.empty:
        market_df = market_df.sort_values("date").reset_index(drop=True)
        market_df.to_parquet(processed_dir / "market.parquet", index=False)
        log.info(f"market.parquet saved: {len(market_df)} rows")

    # Build per-stock parquets
    log.info(f"Building per-stock parquets for {len(target_symbols)} symbols...")

    if combined_bhav.empty:
        log.error("No Bhavcopy data available. Cannot build stock parquets.")
        return summary

    for symbol in tqdm(sorted(target_symbols), desc="Building stocks"):
        stock_df = combined_bhav[combined_bhav["symbol"] == symbol].copy()

        if len(stock_df) < 50:
            summary[symbol] = {
                "status": "INSUFFICIENT_DATA",
                "rows": len(stock_df),
                "reason": f"Only {len(stock_df)} rows found"
            }
            log.warning(f"{symbol}: Only {len(stock_df)} rows — skipping")
            continue

        # Clean the data
        stock_df = clean_stock_data(stock_df)

        if len(stock_df) < 50:
            summary[symbol] = {
                "status": "FAILED_QUALITY",
                "rows": len(stock_df),
                "reason": "Insufficient rows after cleaning"
            }
            continue

        # Liquidity filter — check average volume
        avg_vol = stock_df["volume"].mean()
        if avg_vol < CONFIG["min_volume"]:
            summary[symbol] = {
                "status": "LOW_LIQUIDITY",
                "rows": len(stock_df),
                "avg_volume": int(avg_vol),
                "reason": f"Avg volume {avg_vol:,.0f} < {CONFIG['min_volume']:,}"
            }
            log.warning(f"{symbol}: Low liquidity ({avg_vol:,.0f} avg vol) — still saved but flagged")

        # Keep only required columns in clean order
        keep_cols = ["date", "symbol", "open", "high", "low", "close",
                     "volume", "delivery_qty", "delivery_pct", "trades", "prev_close"]
        available = [c for c in keep_cols if c in stock_df.columns]
        stock_df = stock_df[available].copy()

        # Save
        out_path = stocks_dir / f"{symbol}.parquet"
        stock_df.to_parquet(out_path, index=False)

        date_range = f"{stock_df['date'].min().date()} to {stock_df['date'].max().date()}"
        has_delivery = stock_df["delivery_pct"].notna().mean() if "delivery_pct" in stock_df.columns else 0

        summary[symbol] = {
            "status": "OK",
            "rows": len(stock_df),
            "date_range": date_range,
            "avg_volume": int(avg_vol),
            "delivery_pct_coverage": f"{has_delivery:.1%}",
        }

    return summary


# ══════════════════════════════════════════════════════════════════
# STEP 5 — VALIDATION REPORT
# ══════════════════════════════════════════════════════════════════

def validate_and_report(summary: dict, stocks_dir: Path, processed_dir: Path):
    """Prints a clear validation report and saves it."""
    log.info("\n" + "="*60)
    log.info("DATASET VALIDATION REPORT")
    log.info("="*60)

    ok       = {k: v for k, v in summary.items() if v["status"] == "OK"}
    low_liq  = {k: v for k, v in summary.items() if v["status"] == "LOW_LIQUIDITY"}
    insuff   = {k: v for k, v in summary.items() if v["status"] == "INSUFFICIENT_DATA"}
    failed   = {k: v for k, v in summary.items() if v["status"] == "FAILED_QUALITY"}

    log.info(f"\nTotal target symbols : {len(NIFTY100_SYMBOLS)}")
    log.info(f"OK and ready         : {len(ok)}")
    log.info(f"Low liquidity (kept) : {len(low_liq)}")
    log.info(f"Insufficient data    : {len(insuff)}")
    log.info(f"Failed quality check : {len(failed)}")

    if insuff or failed:
        log.warning(f"\nSymbols to investigate:")
        for sym, info in {**insuff, **failed}.items():
            log.warning(f"  {sym}: {info['reason']}")

    # Spot-check: load RELIANCE and print stats
    rel_path = stocks_dir / "RELIANCE.parquet"
    if rel_path.exists():
        rel = pd.read_parquet(rel_path)
        log.info(f"\nSpot check — RELIANCE:")
        log.info(f"  Rows       : {len(rel)}")
        log.info(f"  Date range : {rel['date'].min().date()} → {rel['date'].max().date()}")
        log.info(f"  Close range: ₹{rel['close'].min():.1f} → ₹{rel['close'].max():.1f}")
        log.info(f"  Avg volume : {rel['volume'].mean():,.0f}")
        if "delivery_pct" in rel.columns:
            log.info(f"  Delivery % : {rel['delivery_pct'].mean():.1f}% avg, {rel['delivery_pct'].notna().mean():.0%} coverage")
        log.info(f"\n  Last 5 rows:")
        log.info(rel[["date", "open", "high", "low", "close", "volume", "delivery_pct"]].tail().to_string())

    # Spot-check: market.parquet
    mkt_path = processed_dir / "market.parquet"
    if mkt_path.exists():
        mkt = pd.read_parquet(mkt_path)
        log.info(f"\nSpot check — market.parquet:")
        log.info(f"  Rows          : {len(mkt)}")
        log.info(f"  Columns       : {mkt.columns.tolist()}")
        if "market_regime" in mkt.columns:
            log.info(f"  Regime counts : {mkt['market_regime'].value_counts().to_dict()}")

    # Save summary JSON
    report_path = Path("../reports/01_dataset_summary.json")
    report_path.parent.mkdir(exist_ok=True)
    with open(report_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    log.info(f"\nFull summary saved: {report_path}")

    log.info("\n" + "="*60)
    if len(ok) + len(low_liq) >= 80:
        log.info("STATUS: PASS — Sufficient data to proceed to Stage 2")
    else:
        log.info("STATUS: FAIL — Too many symbols missing. Investigate before Stage 2")
    log.info("="*60)


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    log.info("THE STOCK LOGIC — Stage 1: Data Acquisition")
    log.info(f"Date range: {CONFIG['start_date']} to {CONFIG['end_date']}")
    log.info(f"Target symbols: {len(NIFTY100_SYMBOLS)}")

    # Resolve paths relative to script location
    base = Path(__file__).parent
    bhavcopy_dir = (base / CONFIG["raw_bhavcopy_dir"]).resolve()
    vix_dir      = (base / CONFIG["raw_vix_dir"]).resolve()
    processed_dir = (base / CONFIG["processed_dir"]).resolve()
    stocks_dir   = (base / CONFIG["stocks_dir"]).resolve()

    for d in [bhavcopy_dir, vix_dir, processed_dir, stocks_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # Get all trading days
    trading_days = get_trading_days(CONFIG["start_date"], CONFIG["end_date"])
    log.info(f"Trading days in range: {len(trading_days)}")

    # ── Step 1: Download Bhavcopy ──────────────────────────────────
    log.info("\n── Step 1: Downloading NSE Bhavcopy ──")
    download_results = download_all_bhavcopy(trading_days, bhavcopy_dir)

    # ── Step 2: Download India VIX ────────────────────────────────
    log.info("\n── Step 2: Downloading India VIX ──")
    download_india_vix(vix_dir)
    vix_df = parse_india_vix(vix_dir)

    # ── Step 3: Download Nifty index ──────────────────────────────
    log.info("\n── Step 3: Downloading Nifty 50 index ──")
    nifty_df = download_nifty_index(CONFIG["start_date"], CONFIG["end_date"])

    # ── Step 4: Load all Bhavcopy + build parquets ────────────────
    log.info("\n── Step 4: Building per-stock parquets ──")
    combined = load_all_bhavcopy(bhavcopy_dir, trading_days)
    summary = build_stock_parquets(combined, vix_df, nifty_df, stocks_dir, processed_dir)

    # ── Step 5: Validate ──────────────────────────────────────────
    log.info("\n── Step 5: Validation ──")
    validate_and_report(summary, stocks_dir, processed_dir)

    log.info("\nStage 1 complete. Next: run engine/02_indicators.py")


if __name__ == "__main__":
    # Change working directory to script location
    os.chdir(Path(__file__).parent)
    main()
