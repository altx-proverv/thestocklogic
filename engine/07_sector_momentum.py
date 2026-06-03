"""
THE STOCK LOGIC — Phase 2: Sector Momentum Engine
==================================================
Computes daily sector rankings based on price momentum.
Identifies top 3 (long bias) and bottom 3 (short bias) sectors.

Logic:
  - For each sector, compute median 5d and 20d return of all stocks
  - Rank sectors by combined momentum score
  - Top 3 = long candidates, Bottom 3 = short candidates
  - Mixed market = show both

Reads : data/processed/stocks/*.parquet
Writes: data/processed/sector_momentum.parquet
        Supabase sector_heatmap table

Run: python3 engine/07_sector_momentum.py
"""

import os, sys, logging, warnings
from pathlib import Path
from datetime import date
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

STOCKS_DIR   = Path("data/processed/stocks")
OUTPUT_FILE  = Path("data/processed/sector_momentum.parquet")

try:
    from engine.universe import SYMBOL_SECTOR_MAP, SECTORS, get_sector_stocks
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from universe import SYMBOL_SECTOR_MAP, SECTORS, get_sector_stocks


def compute_sector_momentum(target_date: pd.Timestamp = None) -> pd.DataFrame:
    """
    Computes sector momentum for a given date.
    Returns DataFrame with one row per sector, ranked by momentum.
    """
    all_rows = []

    for sym, sector in SYMBOL_SECTOR_MAP.items():
        f = STOCKS_DIR / f"{sym}.parquet"
        if not f.exists():
            continue

        try:
            df = pd.read_parquet(f)
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date")

            if target_date:
                df = df[df["date"] <= target_date]

            if len(df) < 25:
                continue

            close = df["close"].iloc[-1]
            c5    = df["close"].iloc[-6] if len(df) >= 6 else df["close"].iloc[0]
            c20   = df["close"].iloc[-21] if len(df) >= 21 else df["close"].iloc[0]
            c1    = df["close"].iloc[-2] if len(df) >= 2 else close

            ret_1d  = (close - c1)  / c1  * 100
            ret_5d  = (close - c5)  / c5  * 100
            ret_20d = (close - c20) / c20 * 100
            latest_date = df["date"].iloc[-1]

            all_rows.append({
                "symbol":     sym,
                "sector":     sector,
                "date":       latest_date,
                "close":      close,
                "ret_1d":     round(ret_1d, 2),
                "ret_5d":     round(ret_5d, 2),
                "ret_20d":    round(ret_20d, 2),
                "volume":     float(df["volume"].iloc[-1]),
                "vol_avg20":  float(df["volume"].iloc[-20:].mean()),
            })

        except Exception as e:
            continue

    if not all_rows:
        return pd.DataFrame()

    stocks_df = pd.DataFrame(all_rows)

    # Sector aggregation — use median to avoid outlier distortion
    sector_df = stocks_df.groupby("sector").agg(
        stock_count   = ("symbol", "count"),
        ret_1d_median = ("ret_1d",  "median"),
        ret_5d_median = ("ret_5d",  "median"),
        ret_20d_median= ("ret_20d", "median"),
        ret_1d_mean   = ("ret_1d",  "mean"),
        ret_5d_mean   = ("ret_5d",  "mean"),
        advancing     = ("ret_1d",  lambda x: (x > 0).sum()),
        declining     = ("ret_1d",  lambda x: (x < 0).sum()),
        date          = ("date",    "max"),
    ).reset_index()

    # Momentum score: weighted combination
    # 5d carries more weight than 20d for short-term trading
    sector_df["momentum_score"] = (
        sector_df["ret_5d_median"]  * 0.6 +
        sector_df["ret_20d_median"] * 0.3 +
        sector_df["ret_1d_median"]  * 0.1
    ).round(2)

    # Rank sectors
    sector_df = sector_df.sort_values("momentum_score", ascending=False).reset_index(drop=True)
    sector_df["rank"] = range(1, len(sector_df) + 1)

    # Classify
    n = len(sector_df)
    top3    = sector_df.head(3).index.tolist()
    bot3    = sector_df.tail(3).index.tolist()

    sector_df["classification"] = "neutral"
    sector_df.loc[top3, "classification"] = "strong"
    sector_df.loc[bot3, "classification"] = "weak"

    # Market direction — uses today's actual returns + signal mix
    # NOT just historical momentum (which lags by 5-20 days)
    
    all_pos = (sector_df["ret_1d_median"] > 0).all()
    all_neg = (sector_df["ret_1d_median"] < 0).all()
    top3_1d = sector_df.head(3)["ret_1d_median"].mean()
    bot3_1d = sector_df.tail(3)["ret_1d_median"].mean()
    adv_total = sector_df["advancing"].sum()
    dec_total = sector_df["declining"].sum()
    adv_ratio = adv_total / max(adv_total + dec_total, 1)

    # Hard override: if majority stocks declining today = bearish
    if adv_ratio < 0.35 or (all_neg and bot3_1d < -0.5):
        market_direction = "bearish"
    elif adv_ratio > 0.65 and top3_1d > 0.3:
        market_direction = "bullish"
    else:
        market_direction = "mixed"

    sector_df["market_direction"] = market_direction

    # Advancing/declining ratio per sector
    sector_df["adv_dec_ratio"] = (
        sector_df["advancing"] / sector_df["declining"].replace(0, 1)
    ).round(2)

    # Trade bias per sector
    sector_df["trade_bias"] = "avoid"
    sector_df.loc[sector_df["classification"] == "strong", "trade_bias"] = "long"
    sector_df.loc[sector_df["classification"] == "weak",   "trade_bias"] = "short"

    # REGIME OVERRIDE — trade_bias must agree with market_direction
    if market_direction == "bearish":
        sector_df.loc[sector_df["trade_bias"] == "long", "trade_bias"] = "avoid"
    elif market_direction == "bullish":
        sector_df.loc[sector_df["trade_bias"] == "short", "trade_bias"] = "avoid"

    return sector_df, stocks_df


def push_to_supabase(sector_df: pd.DataFrame):
    """Push sector heatmap to Supabase."""
    import requests

    url = os.environ.get("SUPABASE_URL",
          "https://eibdlcanpudjgmkjxrga.supabase.co")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    if not key:
        log.warning("SUPABASE_SERVICE_KEY not set — skipping push")
        return

    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates"
    }

    signal_date = sector_df["date"].max().strftime("%Y-%m-%d")

    # Delete existing for this date
    requests.delete(
        f"{url}/rest/v1/sector_heatmap?signal_date=eq.{signal_date}",
        headers=headers
    )

    records = []
    for _, row in sector_df.iterrows():
        records.append({
            "signal_date":      signal_date,
            "sector":           row["sector"],
            "rank":             int(row["rank"]),
            "momentum_score":   float(row["momentum_score"]),
            "ret_1d":           float(row["ret_1d_median"]),
            "ret_5d":           float(row["ret_5d_median"]),
            "ret_20d":          float(row["ret_20d_median"]),
            "stock_count":      int(row["stock_count"]),
            "advancing":        int(row["advancing"]),
            "declining":        int(row["declining"]),
            "classification":   row["classification"],
            "trade_bias":       row["trade_bias"],
            "market_direction": row["market_direction"],
        })

    r = requests.post(
        f"{url}/rest/v1/sector_heatmap",
        headers=headers,
        json=records
    )

    if r.status_code in (200, 201):
        log.info(f"✓ Pushed {len(records)} sector records to Supabase")
    else:
        log.error(f"Push failed: {r.status_code} — {r.text}")


def print_heatmap(sector_df: pd.DataFrame):
    """Print sector heatmap to terminal."""
    date_str = pd.Timestamp(sector_df["date"].max()).strftime("%d %b %Y")
    direction = sector_df["market_direction"].iloc[0].upper()

    log.info(f"\n{'='*60}")
    log.info(f"SECTOR HEATMAP — {date_str} — Market: {direction}")
    log.info(f"{'='*60}")
    log.info(f"{'Rank':<5} {'Sector':<12} {'Score':>7} {'1D%':>7} {'5D%':>7} {'20D%':>7} {'Bias':<8} {'A/D'}")
    log.info(f"{'-'*60}")

    for _, row in sector_df.iterrows():
        icon = "🟢" if row["classification"] == "strong" else \
               "🔴" if row["classification"] == "weak" else "⚪"
        log.info(
            f"{icon} {int(row['rank']):<4} "
            f"{row['sector']:<12} "
            f"{row['momentum_score']:>+7.2f} "
            f"{row['ret_1d_median']:>+7.2f} "
            f"{row['ret_5d_median']:>+7.2f} "
            f"{row['ret_20d_median']:>+7.2f} "
            f"{row['trade_bias']:<8} "
            f"{int(row['advancing'])}/{int(row['declining'])}"
        )

    log.info(f"\n  🟢 LONG sectors : "
             f"{', '.join(sector_df[sector_df['trade_bias']=='long']['sector'].tolist())}")
    log.info(f"  🔴 SHORT sectors: "
             f"{', '.join(sector_df[sector_df['trade_bias']=='short']['sector'].tolist())}")
    log.info(f"{'='*60}")


def main():
    log.info("THE STOCK LOGIC — Phase 2: Sector Momentum Engine")

    result = compute_sector_momentum()

    if isinstance(result, tuple):
        sector_df, stocks_df = result
    else:
        log.error("No data computed")
        return

    if sector_df.empty:
        log.error("No sector data. Run 01b_download_bhavcopy.py first.")
        return

    # Save locally
    sector_df.to_parquet(OUTPUT_FILE, index=False)
    log.info(f"Saved: {OUTPUT_FILE}")

    # Print heatmap
    print_heatmap(sector_df)

    # Push to Supabase
    push_to_supabase(sector_df)

    log.info("\nNext: update 03b_score.py to use sector context")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
