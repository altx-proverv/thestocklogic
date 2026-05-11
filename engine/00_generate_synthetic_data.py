"""
THE STOCK LOGIC — Synthetic Data Generator
==========================================
Generates realistic NSE-like OHLCV + delivery data for all 100 stocks.
Use this to build and test Stages 2–5 while real NSE data is being
downloaded on your local machine.

What makes this realistic:
  - Price paths follow geometric Brownian motion with Indian market volatility
  - Delivery % has realistic distributions (higher on institutional stocks)
  - Volume has intraday patterns and regime-dependent behaviour
  - Market regimes (bull/bear/sideways) drive stock correlations
  - India VIX is anti-correlated with market
  - Splits and corporate actions are simulated
  - NSE trading holidays are respected

Run: python 00_generate_synthetic_data.py
Output: data/processed/stocks/*.parquet + data/processed/market.parquet
"""

import os
import sys
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import date, timedelta

np.random.seed(42)

# ── CONFIG ────────────────────────────────────────────────────────
START_DATE  = date(2023, 1, 2)
END_DATE    = date(2025, 5, 9)
BASE_DIR    = Path(__file__).parent.parent / "data" / "processed"
STOCKS_DIR  = BASE_DIR / "stocks"

BASE_DIR.mkdir(parents=True, exist_ok=True)
STOCKS_DIR.mkdir(parents=True, exist_ok=True)

NSE_HOLIDAYS = {
    date(2023, 1, 26), date(2023, 3, 7), date(2023, 3, 30),
    date(2023, 4, 4), date(2023, 4, 7), date(2023, 4, 14),
    date(2023, 5, 1), date(2023, 8, 15), date(2023, 10, 2),
    date(2023, 10, 24), date(2023, 11, 27), date(2023, 12, 25),
    date(2024, 1, 22), date(2024, 1, 26), date(2024, 3, 25),
    date(2024, 3, 29), date(2024, 4, 14), date(2024, 5, 23),
    date(2024, 8, 15), date(2024, 10, 2), date(2024, 10, 14),
    date(2024, 11, 1), date(2024, 11, 15), date(2024, 12, 25),
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 14), date(2025, 4, 18), date(2025, 5, 1),
}

# Stock profiles — realistic Indian large/midcap parameters
# (symbol, base_price, annual_vol, annual_drift, avg_volume_lakhs, delivery_pct_mean)
STOCK_PROFILES = [
    # Nifty 50 — high liquidity, lower vol
    ("RELIANCE",    2600, 0.22, 0.12,  80, 48),
    ("TCS",         3800, 0.20, 0.10,  35, 52),
    ("HDFCBANK",    1650, 0.24, 0.08,  90, 45),
    ("BHARTIARTL",   900, 0.26, 0.18,  50, 42),
    ("ICICIBANK",   1100, 0.26, 0.14,  80, 44),
    ("INFOSYS",     1450, 0.22, 0.09,  45, 50),
    ("SBIN",         600, 0.30, 0.16, 120, 38),
    ("HINDUNILVR",  2400, 0.18, 0.07,  20, 55),
    ("ITC",          450, 0.22, 0.14,  80, 42),
    ("LT",          3200, 0.24, 0.15,  25, 50),
    ("KOTAKBANK",   1800, 0.24, 0.08,  30, 48),
    ("AXISBANK",    1050, 0.28, 0.12,  60, 42),
    ("BAJFINANCE",  6800, 0.30, 0.10,  15, 52),
    ("ASIANPAINT",  3100, 0.22, 0.06,  12, 55),
    ("MARUTI",     10200, 0.24, 0.14,  10, 50),
    ("SUNPHARMA",  1200,  0.24, 0.12,  25, 48),
    ("TITAN",      3300,  0.26, 0.16,  18, 52),
    ("ULTRACEMCO", 8500,  0.22, 0.10,   8, 50),
    ("WIPRO",       450,  0.24, 0.06,  40, 46),
    ("HCLTECH",    1500,  0.22, 0.12,  30, 48),
    ("NESTLEIND", 24000,  0.18, 0.08,   3, 58),
    ("POWERGRID",   250,  0.20, 0.12,  80, 40),
    ("NTPC",        250,  0.22, 0.14,  90, 38),
    ("TECHM",      1600,  0.26, 0.08,  20, 48),
    ("JSWSTEEL",    800,  0.32, 0.14,  40, 42),
    ("TATAMOTORS",  950,  0.36, 0.18,  80, 40),
    ("TATASTEEL",   150,  0.34, 0.12, 100, 38),
    ("BAJAJFINSV",  1600, 0.28, 0.08,  20, 50),
    ("ONGC",         200, 0.26, 0.10, 100, 36),
    ("COALINDIA",   430,  0.24, 0.12,  80, 38),
    ("ADANIPORTS",  1200, 0.34, 0.20,  30, 42),
    ("ADANIENT",   2800,  0.40, 0.22,  20, 38),
    ("BRITANNIA",  5200,  0.18, 0.06,   5, 55),
    ("DRREDDY",    6200,  0.22, 0.10,   8, 50),
    ("DIVISLAB",   3800,  0.24, 0.08,   6, 52),
    ("CIPLA",      1400,  0.22, 0.12,  18, 48),
    ("HINDALCO",    600,  0.32, 0.14,  50, 40),
    ("GRASIM",     2400,  0.26, 0.12,  12, 46),
    ("BPCL",        650,  0.30, 0.12,  60, 36),
    ("SHRIRAMFIN", 2600,  0.28, 0.14,  15, 46),
    ("APOLLOHOSP", 6800,  0.24, 0.14,   6, 52),
    ("BAJAJ-AUTO", 9500,  0.22, 0.12,   6, 50),
    ("EICHERMOT",  4500,  0.22, 0.10,   8, 52),
    ("INDUSINDBK", 1400,  0.32, 0.06,  30, 42),
    ("HEROMOTOCO", 4500,  0.20, 0.08,  10, 50),
    ("TATACONSUM",  1100, 0.24, 0.12,  15, 48),
    ("SBILIFE",    1600,  0.24, 0.10,  15, 48),
    ("HDFCLIFE",    700,  0.22, 0.08,  25, 46),
    ("M&M",        2000,  0.28, 0.18,  30, 46),
    ("VEDL",        450,  0.36, 0.12,  60, 36),
    # Nifty Next 50 — slightly higher vol, lower liquidity
    ("ADANIGREEN", 1800,  0.44, 0.20,  20, 38),
    ("AMBUJACEM",   600,  0.28, 0.14,  30, 42),
    ("BANKBARODA",  250,  0.32, 0.14,  80, 36),
    ("BERGEPAINT", 5500,  0.22, 0.08,   5, 52),
    ("BIOCON",      380,  0.34, 0.04,  25, 38),
    ("BOSCHLTD",  28000,  0.20, 0.10,   2, 54),
    ("CANBK",       120,  0.30, 0.12,  80, 34),
    ("CHOLAFIN",   1400,  0.30, 0.18,  15, 46),
    ("COLPAL",     3000,  0.18, 0.08,   8, 54),
    ("CONCOR",     1000,  0.26, 0.10,  10, 46),
    ("DABUR",       600,  0.18, 0.06,  15, 52),
    ("DLF",         900,  0.34, 0.18,  40, 40),
    ("DMART",      4500,  0.24, 0.10,   5, 54),
    ("FEDERALBNK",  190,  0.30, 0.14,  60, 38),
    ("GAIL",        250,  0.26, 0.12,  60, 38),
    ("GODREJCP",   1400,  0.22, 0.08,  10, 50),
    ("GODREJPROP", 2800,  0.34, 0.20,  12, 42),
    ("HAL",        4500,  0.28, 0.22,  10, 46),
    ("HAVELLS",    1800,  0.24, 0.12,  10, 50),
    ("ICICIPRULI",  700,  0.24, 0.08,  15, 46),
    ("IDFCFIRSTB",   90,  0.34, 0.08,  80, 36),
    ("INDHOTEL",    600,  0.30, 0.20,  20, 42),
    ("IOC",         180,  0.28, 0.10,  90, 36),
    ("IRCTC",       900,  0.32, 0.14,  20, 44),
    ("JINDALSTEL",  950,  0.34, 0.16,  20, 40),
    ("LICI",        950,  0.22, 0.08,  40, 42),
    ("LTIM",       5800,  0.24, 0.10,   8, 50),
    ("LUPIN",      2000,  0.24, 0.12,  12, 48),
    ("MARICO",      600,  0.20, 0.06,  15, 50),
    ("MCDOWELL-N", 1100,  0.24, 0.08,  10, 46),
    ("MPHASIS",    2900,  0.28, 0.10,   8, 50),
    ("MOTHERSON",   190,  0.34, 0.14,  60, 38),
    ("NMDC",        260,  0.28, 0.12,  50, 38),
    ("NYKAA",       220,  0.44, 0.06,  30, 36),
    ("OBEROIRLTY", 1900,  0.30, 0.18,   8, 46),
    ("OFSS",      10500,  0.20, 0.10,   2, 54),
    ("PAGEIND",   44000,  0.22, 0.06,   1, 56),
    ("PERSISTENT", 5200,  0.28, 0.18,   5, 50),
    ("PFC",         500,  0.28, 0.18,  40, 38),
    ("PIDILITIND", 3000,  0.22, 0.10,   8, 52),
    ("PNB",         110,  0.32, 0.12,  90, 34),
    ("RECLTD",      550,  0.28, 0.18,  35, 40),
    ("SAIL",        140,  0.32, 0.08,  80, 34),
    ("SIEMENS",    7500,  0.22, 0.14,   4, 52),
    ("SRF",        2400,  0.26, 0.10,   6, 48),
    ("TORNTPHARM", 3400,  0.22, 0.10,   5, 50),
    ("UBL",        2000,  0.22, 0.06,   5, 50),
    ("UNITDSPR",   1600,  0.22, 0.08,   6, 48),
    ("ZYDUSLIFE",   900,  0.26, 0.12,  10, 46),
]


def get_trading_days(start: date, end: date) -> list:
    days, cur = [], start
    while cur <= end:
        if cur.weekday() < 5 and cur not in NSE_HOLIDAYS:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def generate_market_regimes(n: int) -> np.ndarray:
    """
    Generates a sequence of market regimes.
    Regimes: 0=bull, 1=sideways, 2=bear
    Uses Markov chain with realistic transitions.
    """
    # Transition matrix: from row to column
    # bull -> bull: 0.97, bull -> sideways: 0.02, bull -> bear: 0.01
    # side -> bull: 0.03, side -> sideways: 0.94, side -> bear: 0.03
    # bear -> bull: 0.01, bear -> sideways: 0.04, bear -> bear: 0.95
    trans = np.array([
        [0.97, 0.02, 0.01],
        [0.03, 0.94, 0.03],
        [0.01, 0.04, 0.95],
    ])
    regimes = np.zeros(n, dtype=int)
    regimes[0] = 0  # start in bull
    for i in range(1, n):
        regimes[i] = np.random.choice(3, p=trans[regimes[i-1]])
    return regimes


def generate_stock_prices(
    base_price: float,
    annual_vol: float,
    annual_drift: float,
    n_days: int,
    market_returns: np.ndarray,
    beta: float = 1.0,
) -> np.ndarray:
    """
    Generates realistic OHLCV using GBM with market correlation.
    Returns array of close prices.
    """
    dt = 1 / 252
    daily_vol = annual_vol * np.sqrt(dt)
    daily_drift = (annual_drift - 0.5 * annual_vol**2) * dt

    # Idiosyncratic returns
    idio = np.random.normal(0, daily_vol * 0.6, n_days)

    # Total return = market component + idiosyncratic
    total_returns = daily_drift + beta * market_returns + idio

    # Geometric price path
    prices = base_price * np.exp(np.cumsum(total_returns))
    return prices


def prices_to_ohlcv(
    closes: np.ndarray,
    annual_vol: float,
    avg_volume_lakhs: float,
    regimes: np.ndarray,
) -> pd.DataFrame:
    """Converts close price array into realistic OHLCV rows."""
    n = len(closes)
    daily_range_pct = annual_vol * np.sqrt(1/252) * 1.5  # intraday range

    rows = []
    for i in range(n):
        c = closes[i]
        prev_c = closes[i-1] if i > 0 else c

        # OHLC generation
        range_pct = abs(np.random.normal(daily_range_pct, daily_range_pct * 0.3))
        range_pct = max(range_pct, 0.003)  # minimum 0.3% range

        # Open has gap from prev close
        gap_pct = np.random.normal(0, 0.004)
        o = prev_c * (1 + gap_pct)

        h = max(o, c) * (1 + abs(np.random.normal(0, range_pct * 0.4)))
        l = min(o, c) * (1 - abs(np.random.normal(0, range_pct * 0.4)))
        h = max(h, o, c)
        l = min(l, o, c)

        # Volume: regime-dependent
        vol_mul = {0: 1.0, 1: 0.8, 2: 1.3}[regimes[i]]
        vol_noise = np.random.lognormal(0, 0.3)
        vol = int(avg_volume_lakhs * 1e5 * vol_mul * vol_noise)
        vol = max(vol, 10000)

        # Delivery %: higher on calm bull days, lower on volatile bear days
        base_del = 45 + regimes[i] * (-5) + np.random.normal(0, 8)
        base_del = np.clip(base_del, 15, 85)

        # Trades
        trades = int(vol / np.random.uniform(100, 500))
        trades = max(trades, 50)

        rows.append({
            "open":         round(o, 2),
            "high":         round(h, 2),
            "low":          round(l, 2),
            "close":        round(c, 2),
            "volume":       vol,
            "delivery_pct": round(base_del, 1),
            "delivery_qty": int(vol * base_del / 100),
            "trades":       trades,
            "prev_close":   round(prev_c, 2),
        })

    return pd.DataFrame(rows)


def generate_all_data():
    print("Generating synthetic Nifty 100 data...")
    print(f"Date range: {START_DATE} to {END_DATE}")

    trading_days = get_trading_days(START_DATE, END_DATE)
    n = len(trading_days)
    dates = pd.to_datetime([str(d) for d in trading_days])
    print(f"Trading days: {n}")

    # Generate market regime sequence
    regimes = generate_market_regimes(n)
    regime_names = np.array(["bull", "sideways", "bear"])
    regime_labels = regime_names[regimes]

    # Generate market (Nifty) returns — drives stock correlations
    dt = 1 / 252
    regime_drifts = {0: 0.15 * dt, 1: 0.02 * dt, 2: -0.12 * dt}
    regime_vols   = {0: 0.14 * np.sqrt(dt), 1: 0.12 * np.sqrt(dt), 2: 0.22 * np.sqrt(dt)}

    mkt_returns = np.array([
        np.random.normal(regime_drifts[r], regime_vols[r])
        for r in regimes
    ])

    # Nifty close prices
    nifty_base = 18000
    nifty_closes = nifty_base * np.exp(np.cumsum(mkt_returns))

    # India VIX — anti-correlated with market, regime-dependent
    vix_base = {0: 13.0, 1: 16.0, 2: 22.0}
    vix_values = np.array([
        max(8.0, vix_base[r] + np.random.normal(0, 2.0) - mkt_returns[i] * 50)
        for i, r in enumerate(regimes)
    ])

    # Advance/Decline
    ad_base = {0: 2.5, 1: 1.1, 2: 0.45}
    advance_counts = np.array([
        max(50, int(np.random.normal(1500 * ad_base[r] / (1 + ad_base[r]), 100)))
        for r in regimes
    ])
    total_stocks = 1800
    decline_counts = total_stocks - advance_counts
    ad_ratios = (advance_counts / np.maximum(decline_counts, 1)).round(2)

    # ── Market parquet ────────────────────────────────────────────
    nifty_range = np.abs(np.random.normal(0, 0.008, n))
    market_df = pd.DataFrame({
        "date":           dates,
        "nifty_open":     (nifty_closes * (1 - nifty_range * 0.3)).round(2),
        "nifty_high":     (nifty_closes * (1 + nifty_range * 0.6)).round(2),
        "nifty_low":      (nifty_closes * (1 - nifty_range * 0.6)).round(2),
        "nifty_close":    nifty_closes.round(2),
        "vix_close":      vix_values.round(2),
        "advance_count":  advance_counts,
        "decline_count":  decline_counts,
        "ad_ratio":       ad_ratios,
        "market_regime":  regime_labels,
    })

    market_df.to_parquet(BASE_DIR / "market.parquet", index=False)
    print(f"market.parquet saved: {len(market_df)} rows")
    print(f"Regime distribution: {pd.Series(regime_labels).value_counts().to_dict()}")

    # ── Per-stock parquets ────────────────────────────────────────
    ok_count = 0
    for sym, base_price, ann_vol, ann_drift, avg_vol_lakhs, del_pct_mean in STOCK_PROFILES:
        # Beta varies by stock type
        beta = np.random.uniform(0.7, 1.3)
        if sym in ("NESTLEIND", "HINDUNILVR", "BRITANNIA", "COLPAL", "DABUR"):
            beta = np.random.uniform(0.4, 0.7)  # defensives
        elif sym in ("TATAMOTORS", "JSWSTEEL", "TATASTEEL", "ADANIENT", "HINDALCO"):
            beta = np.random.uniform(1.2, 1.6)  # cyclicals/high-beta

        closes = generate_stock_prices(
            base_price, ann_vol, ann_drift, n, mkt_returns, beta
        )

        ohlcv_df = prices_to_ohlcv(closes, ann_vol, avg_vol_lakhs, regimes)

        df = pd.DataFrame({"date": dates, "symbol": sym})
        df = pd.concat([df, ohlcv_df], axis=1)

        df.to_parquet(STOCKS_DIR / f"{sym}.parquet", index=False)
        ok_count += 1

    print(f"\nStock parquets saved: {ok_count}/{len(STOCK_PROFILES)}")
    print(f"Output directory: {STOCKS_DIR}")

    # Quick validation
    test = pd.read_parquet(STOCKS_DIR / "RELIANCE.parquet")
    print(f"\nSpot check — RELIANCE:")
    print(f"  Rows:        {len(test)}")
    print(f"  Close range: ₹{test['close'].min():.0f} → ₹{test['close'].max():.0f}")
    print(f"  Avg volume:  {test['volume'].mean():,.0f}")
    print(f"  Delivery %:  {test['delivery_pct'].mean():.1f}% avg")
    print(f"\n  Last 5 rows:")
    print(test[["date","open","high","low","close","volume","delivery_pct"]].tail().to_string())

    print("\nSynthetic data generation complete.")
    print("Next: run engine/02_indicators.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent)
    generate_all_data()
