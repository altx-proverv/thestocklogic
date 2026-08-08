"""
TSL — Market Context Builder
=============================
Writes data/processed/market.parquet, which 02b_smc_signals.py reads and which
HAS NEVER EXISTED. Only 02b references it; nothing ever created it.

WHAT THIS SILENTLY BROKE
------------------------
merge_market_context() degrades gracefully when the file is absent, so for the
life of the system every signal carried:
    market_regime = "unknown"
    vix_close     = NaN
    ad_ratio      = NaN
    no_trade_zone = 0
and, because rs_5d/rs_20d are computed from nifty_close (which arrives via this
same merge), relative strength collapsed to the degenerate value 1.0 for every
stock on every day.

Four market-context features were dead: regime, VIX, breadth, relative strength.

WHAT THIS WRITES
----------------
    date, nifty_open, nifty_high, nifty_low, nifty_close,
    vix_close, advance_count, decline_count, ad_ratio,
    nifty_50dma, nifty_200dma, market_regime, extreme_bearish

REGIME (matching 02b's expected vocabulary -- bull/sideways/bear, NOT the
bullish/bearish/mixed vocabulary ATLAS uses for sector_heatmap):
    bull      nifty > 200DMA AND 50DMA > 200DMA
    bear      nifty < 200DMA
    sideways  everything else

extreme_bearish -- gate for hedge shorts. True when regime is bear AND nifty
closes below its 200 DMA. Rare by design: shorts are defensive, not a profit
engine.

SOURCES
-------
    Nifty 50 / India VIX  -- Upstox historical candles (same endpoint
                             index_state.py already uses)
    advance / decline     -- computed from data/processed/stocks/*.parquet.
                             Free: it is your own universe.

RUN ORDER: after 01b_download_bhavcopy, BEFORE 02b_smc_signals.
    python3 engine/build_market.py
"""

import sys, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

log = logging.getLogger("MARKET")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [MARKET] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

IST = timezone(timedelta(hours=5, minutes=30))

STOCKS_DIR  = Path("data/processed/stocks")
MARKET_FILE = Path("data/processed/market.parquet")

NIFTY_KEY = "NSE_INDEX|Nifty 50"
VIX_KEY   = "NSE_INDEX|India VIX"

# 200 DMA needs 200 trading days ~= 290 calendar days. Pull extra for safety.
BACKFILL_DAYS = 500


def _fetch_index_daily(index_key: str, days: int = BACKFILL_DAYS) -> pd.DataFrame:
    """Daily candles for one index from Upstox."""
    try:
        from engine.upstox_ws import BASE_URL, get_headers
    except Exception as e:
        log.error(f"cannot import upstox helpers: {e}")
        return pd.DataFrame()

    to_d = datetime.now(IST).date()
    fr_d = to_d - timedelta(days=days)
    url = f"{BASE_URL}/historical-candle/{index_key}/day/{to_d.isoformat()}/{fr_d.isoformat()}"

    try:
        r = requests.get(url, headers=get_headers(), timeout=30)
        if r.status_code != 200:
            log.error(f"{index_key}: HTTP {r.status_code} {r.text[:180]}")
            return pd.DataFrame()
        candles = r.json().get("data", {}).get("candles", []) or []
        if not candles:
            log.error(f"{index_key}: no candles returned")
            return pd.DataFrame()
        df = pd.DataFrame(candles).iloc[:, :6]
        df.columns = ["ts", "open", "high", "low", "close", "volume"]
        df["date"] = pd.to_datetime(df["ts"], format="mixed", utc=True).dt.tz_convert(IST).dt.normalize().dt.tz_localize(None)
        df = df[["date", "open", "high", "low", "close"]].sort_values("date").reset_index(drop=True)
        log.info(f"{index_key}: {len(df)} candles, {df['date'].min().date()} to {df['date'].max().date()}")
        return df
    except Exception as e:
        log.error(f"{index_key} fetch failed: {e}")
        return pd.DataFrame()


def _compute_breadth() -> pd.DataFrame:
    """Advance/decline counts from the stock universe already on disk."""
    files = sorted(STOCKS_DIR.glob("*.parquet"))
    if not files:
        log.warning("no stock parquets -- breadth unavailable")
        return pd.DataFrame()

    frames = []
    for f in files:
        try:
            d = pd.read_parquet(f, columns=["date", "close"])
            if len(d) < 2:
                continue
            d = d.sort_values("date")
            d["chg"] = d["close"].diff()
            frames.append(d[["date", "chg"]])
        except Exception:
            continue

    if not frames:
        return pd.DataFrame()

    allc = pd.concat(frames, ignore_index=True)
    allc["date"] = pd.to_datetime(allc["date"])
    g = allc.groupby("date")["chg"]
    out = pd.DataFrame({
        "advance_count": g.apply(lambda s: int((s > 0).sum())),
        "decline_count": g.apply(lambda s: int((s < 0).sum())),
    }).reset_index()
    out["ad_ratio"] = (out["advance_count"] /
                       out["decline_count"].replace(0, np.nan)).round(3)
    log.info(f"breadth: {len(out)} days from {len(files)} symbols")
    return out


def _classify(df: pd.DataFrame) -> pd.DataFrame:
    """200/50 DMA regime + the extreme-bearish hedge gate."""
    df = df.sort_values("date").reset_index(drop=True)
    df["nifty_50dma"]  = df["nifty_close"].rolling(50,  min_periods=50).mean().round(2)
    df["nifty_200dma"] = df["nifty_close"].rolling(200, min_periods=200).mean().round(2)

    above200 = df["nifty_close"] > df["nifty_200dma"]
    below200 = df["nifty_close"] < df["nifty_200dma"]
    stacked  = df["nifty_50dma"] > df["nifty_200dma"]

    df["market_regime"] = "sideways"
    df.loc[above200 & stacked, "market_regime"] = "bull"
    df.loc[below200,           "market_regime"] = "bear"
    df.loc[df["nifty_200dma"].isna(), "market_regime"] = "unknown"

    # Hedge-short gate. Deliberately strict.
    df["extreme_bearish"] = (df["market_regime"] == "bear") & below200
    return df


def build() -> pd.DataFrame:
    log.info("Building market context...")

    nifty = _fetch_index_daily(NIFTY_KEY)
    if nifty.empty:
        log.error("ABORT: no Nifty data. market.parquet not written.")
        return pd.DataFrame()
    nifty = nifty.rename(columns={"open": "nifty_open", "high": "nifty_high",
                                  "low": "nifty_low", "close": "nifty_close"})

    vix = _fetch_index_daily(VIX_KEY)
    if not vix.empty:
        vix = vix[["date", "close"]].rename(columns={"close": "vix_close"})
        nifty = nifty.merge(vix, on="date", how="left")
    else:
        log.warning("VIX unavailable -- vix_close will be NaN, no_trade_zone degrades")
        nifty["vix_close"] = np.nan

    breadth = _compute_breadth()
    if not breadth.empty:
        nifty = nifty.merge(breadth, on="date", how="left")
    else:
        nifty["advance_count"] = np.nan
        nifty["decline_count"] = np.nan
        nifty["ad_ratio"] = np.nan

    out = _classify(nifty)

    MARKET_FILE.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(MARKET_FILE, index=False)

    last = out.iloc[-1]
    log.info(f"Wrote {MARKET_FILE} -- {len(out)} rows")
    log.info(f"Latest {last['date'].date()}: Nifty {last['nifty_close']:.0f} | "
             f"200DMA {last['nifty_200dma'] if pd.notna(last['nifty_200dma']) else 'n/a'} | "
             f"regime {last['market_regime']} | extreme_bearish {bool(last['extreme_bearish'])} | "
             f"VIX {last['vix_close'] if pd.notna(last['vix_close']) else 'n/a'} | "
             f"A/D {last['ad_ratio'] if pd.notna(last['ad_ratio']) else 'n/a'}")

    counts = out["market_regime"].value_counts().to_dict()
    log.info(f"Regime distribution: {counts}")
    return out


if __name__ == "__main__":
    df = build()
    sys.exit(0 if not df.empty else 1)
