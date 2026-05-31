"""
THE STOCK LOGIC — ORB Detection Engine
=======================================
Opening Range Breakout detection using Upstox 1-minute candles.

Opening Range = High/Low of 9:15–9:30 AM (first 15 minutes)

Logic:
  1. At 9:30 AM — fetch 1-min candles, compute ORB high/low
  2. At 9:45 AM — check if price has broken above/below ORB
  3. Valid breakout = price + volume confirmation (RVOL > 1.3x)
  4. Push ORB signals to Supabase live_signals table

Run: python3 engine/orb_engine.py
"""

import os, sys, json, logging, requests
from pathlib import Path
from datetime import date, datetime, timedelta
import pandas as pd
import numpy as np

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL     = "https://api.upstox.com/v2"
SUPABASE_URL = os.environ.get("SUPABASE_URL",
               "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DATA_DIR     = Path("data")


def get_headers() -> dict:
    token_file = DATA_DIR / "upstox_token.json"
    data = json.loads(token_file.read_text())
    token = data.get("access_token")
    return {
        "Authorization": f"Bearer {token}",
        "Accept":        "application/json"
    }


def fetch_intraday_candles(instrument_key: str, interval: str = "1minute") -> pd.DataFrame:
    """Fetch today's intraday candles for one instrument."""
    headers = get_headers()
    today = date.today().isoformat()

    r = requests.get(
        f"{BASE_URL}/historical-candle/intraday/{instrument_key}/{interval}",
        headers=headers, timeout=10
    )

    if r.status_code != 200:
        return pd.DataFrame()

    data = r.json().get("data", {}).get("candles", [])
    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data, columns=["timestamp","open","high","low","close","volume","oi"])
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def compute_orb(candles: pd.DataFrame) -> dict:
    """
    Compute Opening Range from 9:15–9:30 AM candles.
    Returns dict with orb_high, orb_low, orb_range, orb_midpoint.
    """
    if candles.empty:
        return {}

    # Filter 9:15 to 9:30 AM
    orb_start = candles["timestamp"].dt.hour*60 + candles["timestamp"].dt.minute >= 9*60+15
    orb_end   = candles["timestamp"].dt.hour*60 + candles["timestamp"].dt.minute <= 9*60+29
    orb_candles = candles[orb_start & orb_end]

    if orb_candles.empty:
        return {}

    orb_high  = orb_candles["high"].max()
    orb_low   = orb_candles["low"].min()
    orb_range = orb_high - orb_low
    orb_open  = orb_candles.iloc[0]["open"]

    # Volume context
    orb_vol   = orb_candles["volume"].sum()

    return {
        "orb_high":    round(orb_high, 2),
        "orb_low":     round(orb_low, 2),
        "orb_range":   round(orb_range, 2),
        "orb_range_pct": round(orb_range / orb_open * 100, 2) if orb_open > 0 else 0,
        "orb_open":    round(orb_open, 2),
        "orb_volume":  int(orb_vol),
        "candle_count": len(orb_candles),
    }


def detect_breakout(candles: pd.DataFrame, orb: dict, symbol: str) -> dict:
    """
    Detect if price has broken out of the ORB range.
    Checks candles after 9:30 AM.
    """
    if not orb or candles.empty:
        return {}

    orb_high  = orb["orb_high"]
    orb_low   = orb["orb_low"]
    orb_range = orb["orb_range"]

    # Post-ORB candles (after 9:30 AM)
    post_orb = candles[
        candles["timestamp"].dt.hour*60 + candles["timestamp"].dt.minute > 9*60+29
    ].copy()

    if post_orb.empty:
        return {}

    # Latest price
    latest = post_orb.iloc[-1]
    ltp    = latest["close"]
    ltp_time = latest["timestamp"].strftime("%H:%M")

    # Volume confirmation — RVOL vs ORB volume
    post_vol = post_orb["volume"].mean() if len(post_orb) > 0 else 0
    orb_vol_per_candle = orb["orb_volume"] / max(orb["candle_count"], 1)
    rvol = post_vol / orb_vol_per_candle if orb_vol_per_candle > 0 else 0

    # Breakout conditions
    long_breakout  = ltp > orb_high * 1.001  # 0.1% buffer above ORB high
    short_breakout = ltp < orb_low  * 0.999  # 0.1% buffer below ORB low

    # Volume must confirm (RVOL > 1.2x)
    vol_confirmed = rvol >= 1.2

    # ORB range filter — skip if range too wide (>4%) or too narrow (<0.3%)
    range_pct = orb.get("orb_range_pct", 0)
    if range_pct > 4.0 or range_pct < 0.3:
        return {}

    if long_breakout and vol_confirmed:
        return {
            "symbol":      symbol,
            "direction":   "LONG",
            "setup_name":  "ORB Breakout — Long",
            "entry":       round(orb_high * 1.001, 2),
            "sl":          round(orb_low, 2),
            "target_1":    round(orb_high + orb_range * 1.5, 2),
            "target_2":    round(orb_high + orb_range * 2.5, 2),
            "ltp":         round(ltp, 2),
            "orb_high":    orb_high,
            "orb_low":     orb_low,
            "orb_range_pct": range_pct,
            "rvol":        round(rvol, 2),
            "signal_time": ltp_time,
            "session":     "morning",
        }
    elif short_breakout and vol_confirmed:
        return {
            "symbol":      symbol,
            "direction":   "SHORT",
            "setup_name":  "ORB Breakdown — Short",
            "entry":       round(orb_low * 0.999, 2),
            "sl":          round(orb_high, 2),
            "target_1":    round(orb_low - orb_range * 1.5, 2),
            "target_2":    round(orb_low - orb_range * 2.5, 2),
            "ltp":         round(ltp, 2),
            "orb_high":    orb_high,
            "orb_low":     orb_low,
            "orb_range_pct": range_pct,
            "rvol":        round(rvol, 2),
            "signal_time": ltp_time,
            "session":     "morning",
        }

    return {}


def push_orb_signals(signals: list):
    """Push ORB signals to Supabase live_signals table."""
    if not signals or not SUPABASE_KEY:
        return

    today = date.today().isoformat()
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }

    # Clear existing ORB signals for today
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/live_signals?signal_date=eq.{today}&session=eq.morning",
        headers=headers
    )

    records = [{
        "signal_date": today,
        "session":     s["session"],
        "symbol":      s["symbol"],
        "direction":   s["direction"],
        "setup_name":  s["setup_name"],
        "entry":       s["entry"],
        "sl":          s["sl"],
        "target_1":    s["target_1"],
        "target_2":    s["target_2"],
        "ltp":         s["ltp"],
        "signal_time": s["signal_time"],
    } for s in signals]

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/live_signals",
        headers=headers, json=records
    )
    if r.status_code in (200, 201):
        log.info(f"Pushed {len(records)} ORB signals to Supabase")
    else:
        log.error(f"Push failed: {r.status_code} — {r.text[:100]}")


def run_orb_scan(max_stocks: int = 500):
    """
    Full ORB scan across Nifty 500.
    Fetches candles for each stock and detects breakouts.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from universe import INSTRUMENT_KEYS, SYMBOL_SECTOR_MAP

    log.info("="*50)
    log.info(f"ORB SCAN — {datetime.now().strftime('%d %b %Y %H:%M IST')}")
    log.info("="*50)

    symbols = list(INSTRUMENT_KEYS.keys())[:max_stocks]
    log.info(f"Scanning {len(symbols)} stocks...")

    orb_data   = {}
    signals    = []
    processed  = 0
    errors     = 0

    for sym in symbols:
        inst_key = INSTRUMENT_KEYS.get(sym)
        if not inst_key:
            continue

        try:
            candles = fetch_intraday_candles(inst_key)
            if candles.empty:
                continue

            orb = compute_orb(candles)
            if not orb:
                continue

            orb_data[sym] = orb

            signal = detect_breakout(candles, orb, sym)
            if signal:
                signal["sector"] = SYMBOL_SECTOR_MAP.get(sym, "OTHER")
                signals.append(signal)
                log.info(
                    f"  {'↑' if signal['direction']=='LONG' else '↓'} {sym:<12} "
                    f"{signal['direction']:<6} "
                    f"Entry:{signal['entry']:>8.1f} "
                    f"SL:{signal['sl']:>8.1f} "
                    f"T1:{signal['target_1']:>8.1f} "
                    f"RVOL:{signal['rvol']:>4.1f}x "
                    f"ORB:{orb['orb_range_pct']:.1f}%"
                )

            processed += 1

        except Exception as e:
            errors += 1
            if errors <= 3:
                log.warning(f"  {sym}: {e}")

    log.info(f"\nProcessed: {processed} | Errors: {errors}")
    log.info(f"ORB signals found: {len(signals)}")

    if signals:
        log.info(f"\nSignal summary:")
        longs  = [s for s in signals if s["direction"]=="LONG"]
        shorts = [s for s in signals if s["direction"]=="SHORT"]
        log.info(f"  Long breakouts : {len(longs)}")
        log.info(f"  Short breakdowns: {len(shorts)}")

        # Save locally
        DATA_DIR.mkdir(exist_ok=True)
        with open(DATA_DIR / "orb_signals_today.json", "w") as f:
            json.dump(signals, f, indent=2)

        # Push to Supabase
        push_orb_signals(signals)
    else:
        log.info("No ORB breakouts detected yet.")

    # Save ORB levels for all stocks
    with open(DATA_DIR / "orb_levels_today.json", "w") as f:
        json.dump(orb_data, f, indent=2)

    log.info(f"ORB levels saved: {len(orb_data)} stocks")
    return signals


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)

    now = datetime.now()
    hour_min = now.hour * 60 + now.minute

    # Market hours check
    if hour_min < 9*60+30:
        log.info("Before 9:30 AM — ORB window not complete yet")
        log.info("Run after 9:30 AM IST")
    elif hour_min > 15*60+30:
        log.info("Market closed. Running in analysis mode...")
        run_orb_scan()
    else:
        run_orb_scan()
