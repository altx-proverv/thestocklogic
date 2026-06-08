"""
THE STOCK LOGIC — Stage 1b: Bhavcopy Download via jugaad-data
=============================================================
Uses jugaad-data which handles NSE session/cookie management.
Downloads real Bhavcopy CSVs, parses them, builds per-stock parquets.

Run from thestocklogic/ folder:
    python3 engine/01b_download_bhavcopy.py

Takes ~30-60 mins for 2 years of data.
Safe to stop and restart — skips already downloaded days.
"""

import os
import sys
import time
import logging
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

try:
    from jugaad_data.nse import bhavcopy_save
except ImportError:
    print("Run: pip install jugaad-data")
    sys.exit(1)

# ── LOGGING ───────────────────────────────────────────────────────
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("reports/01b_download.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────
START_DATE    = date(2023, 1, 1)
END_DATE      = date.today()
RAW_DIR       = Path("data/raw/bhavcopy")
PROCESSED_DIR = Path("data/processed")
STOCKS_DIR    = Path("data/processed/stocks")
DELAY         = 0.3   # seconds between requests

NSE_HOLIDAYS = {
    date(2023, 1, 26), date(2023, 3, 7),  date(2023, 3, 30),
    date(2023, 4, 4),  date(2023, 4, 7),  date(2023, 4, 14),
    date(2023, 5, 1),  date(2023, 8, 15), date(2023, 10, 2),
    date(2023, 10, 24),date(2023, 11, 27),date(2023, 12, 25),
    date(2024, 1, 22), date(2024, 1, 26), date(2024, 3, 25),
    date(2024, 3, 29), date(2024, 4, 14), date(2024, 5, 23),
    date(2024, 8, 15), date(2024, 10, 2), date(2024, 10, 14),
    date(2024, 11, 1), date(2024, 11, 15),date(2024, 12, 25),
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 14), date(2025, 4, 18), date(2025, 5, 1),
}

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from universe import ALL_SYMBOLS as _ALL_SYMS, SYMBOL_SECTOR_MAP
NIFTY100_SYMBOLS = set(_ALL_SYMS)


# ── HELPERS ───────────────────────────────────────────────────────

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


def get_trading_days(start: date, end: date) -> list:
    days, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def csv_filename(d: date) -> str:
    """jugaad-data saves files as cmDDMMMYYYYbhav.csv"""
    return f"cm{d.strftime('%d%b%Y')}bhav.csv"


def parse_bhavcopy_csv(csv_path: Path, d: date) -> pd.DataFrame:
    """
    Parses Bhavcopy CSV — handles both old and new NSE formats.
    Old format (pre-2026): SYMBOL, SERIES, OPEN_PRICE, CLOSE_PRICE, DELIV_PER etc.
    New format (2026+):    TckrSymb, SctySrs, OpnPric, ClsPric (no delivery %)
    """
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]

    # Detect format
    is_new_format = "TckrSymb" in df.columns

    if is_new_format:
        # New 2026+ NSE format
        series_col = "SctySrs"
        if series_col in df.columns:
            df = df[df[series_col].str.strip() == "EQ"].copy()
        if len(df) == 0:
            return pd.DataFrame()
        col_map = {
            "TckrSymb":       "symbol",
            "OpnPric":        "open",
            "HghPric":        "high",
            "LwPric":         "low",
            "ClsPric":        "close",
            "PrvsClsgPric":   "prev_close",
            "TtlTradgVol":    "volume",
            "TtlNbOfTxsExctd":"trades",
        }
        df = df.rename(columns=col_map)
        # No delivery % in new format
        df["delivery_qty"] = np.nan
        df["delivery_pct"] = np.nan

    else:
        # Old format (pre-2026)
        if "SERIES" in df.columns:
            df = df[df["SERIES"].str.strip() == "EQ"].copy()
        if len(df) == 0:
            return pd.DataFrame()
        col_map = {
            "SYMBOL":       "symbol",
            "PREV_CLOSE":   "prev_close",
            "OPEN_PRICE":   "open",
            "HIGH_PRICE":   "high",
            "LOW_PRICE":    "low",
            "CLOSE_PRICE":  "close",
            "TTL_TRD_QNTY": "volume",
            "NO_OF_TRADES": "trades",
            "DELIV_QTY":    "delivery_qty",
            "DELIV_PER":    "delivery_pct",
        }
        df = df.rename(columns=col_map)

    # Add date
    df["date"] = pd.Timestamp(d)

    # Clean numeric columns
    num_cols = ["open","high","low","close","prev_close",
                "volume","trades","delivery_qty","delivery_pct"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "delivery_pct" in df.columns:
        df.loc[df["delivery_pct"] > 100, "delivery_pct"] = np.nan
        df.loc[df["delivery_pct"] < 0,   "delivery_pct"] = np.nan

    df = df[df["close"] > 0].copy()

    keep = ["date","symbol","open","high","low","close",
            "volume","delivery_qty","delivery_pct","trades","prev_close"]
    available = [c for c in keep if c in df.columns]
    return df[available].reset_index(drop=True)


# ── STEP 1: DOWNLOAD ──────────────────────────────────────────────

def download_all(trading_days: list) -> dict:
    """Downloads all Bhavcopy CSVs. Returns {date: True/False}."""
    results = {}
    missing = []

    for d in tqdm(trading_days, desc="Downloading Bhavcopy"):
        fname = csv_filename(d)
        fpath = RAW_DIR / fname

        if fpath.exists():
            results[d] = True
            continue

        try:
            bhavcopy_save(d, str(RAW_DIR))
            if fpath.exists():
                results[d] = True
            else:
                results[d] = False
                missing.append(d)
                log.warning(f"MISSING after download: {d}")
        except Exception as e:
            results[d] = False
            missing.append(d)
            log.warning(f"FAILED {d}: {e}")

        time.sleep(DELAY)

    success = sum(results.values())
    log.info(f"Downloaded: {success}/{len(trading_days)} days")
    if missing:
        log.warning(f"Missing {len(missing)} days: {missing[:5]}{'...' if len(missing)>5 else ''}")
    return results


# ── STEP 1b: DOWNLOAD DELIVERY DATA ─────────────────────────────

def download_delivery_data(d: date) -> dict:
    """
    Download sec_bhavdata_full file which contains DELIV_PER.
    Returns {symbol: delivery_pct} for the given date.
    URL: https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
    """
    import requests as _req
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    try:
        r = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.warning(f"Delivery data not available for {d}: {r.status_code}")
            return {}
        import io
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        # Filter EQ series only
        if "SERIES" in df.columns:
            df = df[df["SERIES"].str.strip() == "EQ"].copy()
        if "SYMBOL" not in df.columns or "DELIV_PER" not in df.columns:
            return {}
        df["SYMBOL"] = df["SYMBOL"].str.strip()
        delivery_map = dict(zip(df["SYMBOL"], pd.to_numeric(df["DELIV_PER"], errors="coerce")))
        log.info(f"Delivery data loaded for {d}: {len(delivery_map)} stocks")
        return delivery_map
    except Exception as e:
        log.warning(f"Delivery data download failed for {d}: {e}")
        return {}


# ── STEP 2: BUILD PARQUETS ────────────────────────────────────────

def build_parquets(trading_days: list):
    """Loads all CSVs, filters to Nifty 100, saves per-stock parquets."""
    log.info("Loading and parsing all Bhavcopy CSVs...")

    all_dfs = []
    for d in tqdm(trading_days, desc="Parsing CSVs"):
        fpath = RAW_DIR / csv_filename(d)
        if not fpath.exists():
            continue
        try:
            df = parse_bhavcopy_csv(fpath, d)
            if len(df) > 0:
                all_dfs.append(df)
        except Exception as e:
            log.warning(f"Parse error {d}: {e}")

    if not all_dfs:
        log.error("No data parsed. Check download step.")
        return

    combined = pd.concat(all_dfs, ignore_index=True)
    log.info(f"Total rows: {len(combined):,} across {combined['symbol'].nunique()} symbols")

    # Build per-stock parquets
    # Download delivery data for most recent trading day
    latest_day = max(trading_days) if trading_days else None
    delivery_map = {}
    if latest_day:
        delivery_map = download_delivery_data(latest_day)
        log.info(f"Delivery map loaded: {len(delivery_map)} stocks")

    log.info("Building per-stock parquets...")
    ok, skipped = 0, 0

    for symbol in tqdm(sorted(NIFTY100_SYMBOLS), desc="Building stocks"):
        df = combined[combined["symbol"] == symbol].copy()

        if len(df) < 50:
            log.warning(f"{symbol}: only {len(df)} rows — skipping")
            skipped += 1
            continue

        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        # Inject latest delivery_pct from sec_bhavdata_full
        if delivery_map and symbol in delivery_map:
            del_pct = delivery_map[symbol]
            if not pd.isna(del_pct):
                df.loc[df.index[-1], "delivery_pct"] = float(del_pct)
        out = STOCKS_DIR / f"{symbol}.parquet"
        df.to_parquet(out, index=False)
        ok += 1

    log.info(f"Parquets saved: {ok} OK, {skipped} skipped")


# ── STEP 3: VALIDATE ──────────────────────────────────────────────

def validate():
    """Quick spot check on the output."""
    files = list(STOCKS_DIR.glob("*.parquet"))
    log.info(f"\n{'='*50}")
    log.info(f"VALIDATION")
    log.info(f"{'='*50}")
    log.info(f"Stock parquets: {len(files)}")

    for sym in ["RELIANCE", "TCS", "SBIN"]:
        p = STOCKS_DIR / f"{sym}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            del_cov = df["delivery_pct"].notna().mean() if "delivery_pct" in df.columns else 0
            log.info(
                f"{sym}: {len(df)} rows | "
                f"₹{df['close'].min():.0f}–₹{df['close'].max():.0f} | "
                f"vol avg {df['volume'].mean():,.0f} | "
                f"delivery coverage {del_cov:.0%}"
            )
        else:
            log.warning(f"{sym}: parquet NOT found")

    if len(files) >= 80:
        log.info("\nSTATUS: PASS — Ready for Stage 2 (indicator engine)")
    else:
        log.info(f"\nSTATUS: PARTIAL — Only {len(files)} stocks. Check missing symbols.")
    log.info(f"{'='*50}")


# ── MAIN ──────────────────────────────────────────────────────────

def main():
    log.info("THE STOCK LOGIC — Stage 1b: Bhavcopy Download")
    log.info(f"Range: {START_DATE} to {END_DATE}")

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)

    trading_days = get_trading_days(START_DATE, END_DATE)
    log.info(f"Trading days: {len(trading_days)}")

    # Step 1: Download
    log.info("\n── Step 1: Downloading ──")
    download_all(trading_days)

    # Step 2: Build parquets
    log.info("\n── Step 2: Building parquets ──")
    build_parquets(trading_days)

    # Step 3: Validate
    log.info("\n── Step 3: Validating ──")
    validate()

    log.info("\nDone. Next: python3 engine/02_indicators.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
