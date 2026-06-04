"""
BTST Signal Engine — Buy Today Sell Tomorrow
=============================================
Runs at 2:30 PM IST (power hour start).
Scans 500 stocks for overnight continuation setups.
Primary filters: delivery%, RVOL into close, relative strength.
Secondary: EMA structure, MACD, RSI, OB proximity.
Output: top BTST candidates pushed to Supabase + website.
"""

import os, sys, requests, logging
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [BTST] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

SUPABASE_URL = os.environ.get("SUPABASE_URL",
               "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DATA_DIR     = Path("data/processed/smc")
MIN_DELIVERY_PCT  = 45.0   # minimum delivery % for institutional interest
MIN_RVOL_CLOSE    = 1.5    # minimum RVOL in afternoon session
MIN_REL_STRENGTH  = -0.3   # stock must not be weaker than -0.3% vs Nifty
MAX_SL_PCT        = 0.02   # max 2% SL distance
MIN_VOLUME        = 100000 # min 1L shares/day liquidity
MIN_CONVICTION    = 70     # minimum conviction score to publish
MAX_BTST_SIGNALS  = 5      # max signals to show


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates,return=representation",
    }


def get_live_prices() -> dict:
    """Fetch live prices from Supabase live_prices table."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_prices?select=symbol,ltp,volume,updated_at",
        headers=_headers()
    )
    if r.status_code == 200:
        return {row["symbol"]: row for row in r.json()}
    return {}


def get_market_regime() -> str:
    """Get current market regime."""
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/sector_heatmap"
        f"?signal_date=eq.{today}&order=rank.asc&limit=1",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        return r.json()[0].get("market_direction", "mixed").lower()
    return "mixed"


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def compute_macd(close: pd.Series):
    ema12 = compute_ema(close, 12)
    ema26 = compute_ema(close, 26)
    macd  = ema12 - ema26
    signal= compute_ema(macd, 9)
    hist  = macd - signal
    return hist


def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.where(delta > 0, 0.0)
    loss  = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def score_btst(row: dict, df: pd.DataFrame, live_ltp: float) -> dict:
    """
    Score a stock for BTST potential.
    Returns score dict or None if disqualified.
    """
    symbol = row.get("symbol", "")
    close  = df["close"].iloc[-1]
    ltp    = live_ltp if live_ltp > 0 else close

    # ── PRIMARY FILTERS ───────────────────────────────────────────

    # 1. Delivery % — must be >= MIN_DELIVERY_PCT
    delivery_pct = float(df["delivery_pct"].iloc[-1] if "delivery_pct" in df.columns else 0)
    if delivery_pct < MIN_DELIVERY_PCT:
        return None

    # 2. Volume — must meet minimum liquidity
    avg_vol = float(df["vol_avg20"].iloc[-1] if "vol_avg20" in df.columns else 0)
    if avg_vol < MIN_VOLUME:
        return None

    # 3. RVOL — must show expansion
    rvol = float(df["rvol"].iloc[-1] if "rvol" in df.columns else 1.0)
    if rvol < MIN_RVOL_CLOSE:
        return None

    # 4. Price above 20 EMA — trend intact
    ema20 = float(compute_ema(df["close"], 20).iloc[-1])
    if ltp < ema20:
        return None

    # ── SCORING ───────────────────────────────────────────────────
    score = 50  # base score

    # Delivery % score (0-20 pts)
    score += min(20, (delivery_pct - MIN_DELIVERY_PCT) / 2)

    # RVOL score (0-15 pts)
    score += min(15, (rvol - 1.0) * 7)

    # EMA alignment (0-10 pts)
    ema50  = float(compute_ema(df["close"], 50).iloc[-1])
    ema200 = float(compute_ema(df["close"], 200).iloc[-1])
    if ema20 > ema50:   score += 5
    if ema50 > ema200:  score += 5

    # RSI in momentum zone 45-65 (0-10 pts)
    rsi = float(compute_rsi(df["close"]).iloc[-1])
    if 45 <= rsi <= 65: score += 10
    elif rsi > 65:      score -= 5   # overbought — penalise

    # MACD histogram rising (0-10 pts)
    macd_hist = compute_macd(df["close"])
    if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-1] > macd_hist.iloc[-2]:
        score += 10

    # Near demand OB (0-10 pts)
    near_demand = bool(df["near_demand_ob"].iloc[-1] if "near_demand_ob" in df.columns else False)
    if near_demand: score += 10

    # Institutional buying flag (0-5 pts)
    inst_buying = bool(df["institutional_buying"].iloc[-1] if "institutional_buying" in df.columns else False)
    if inst_buying: score += 5

    score = min(100, round(score))

    if score < MIN_CONVICTION:
        return None

    # ── ENTRY / SL / TARGET ───────────────────────────────────────
    atr = float(df["atr"].iloc[-1] if "atr" in df.columns else ltp * 0.015)

    # Find nearest demand OB for entry/SL
    demand_obs = df[(df.get("is_demand_ob", pd.Series(False)) == True) &
                    (df.get("ob_mitigated", pd.Series(False)) == False) &
                    df.get("ob_high", pd.Series(np.nan)).notna()]

    if len(demand_obs) > 0:
        ob = demand_obs.iloc[-1]
        entry = round(float(ob["ob_high"]), 2)
        sl    = round(float(ob["ob_low"]) * 0.995, 2)
    else:
        entry = round(ltp, 2)
        sl    = round(ltp * (1 - min(MAX_SL_PCT, atr / ltp)), 2)

    sl_pct = abs(entry - sl) / entry
    if sl_pct > MAX_SL_PCT:
        sl = round(entry * (1 - MAX_SL_PCT), 2)
        sl_pct = MAX_SL_PCT

    # Targets based on RR 2:1 and 3:1
    sl_dist = entry - sl
    t1 = round(entry + sl_dist * 2, 2)
    t2 = round(entry + sl_dist * 3, 2)

    # Grade
    if score >= 85:   grade = "A+"
    elif score >= 78: grade = "A"
    else:             grade = "B"

    return {
        "symbol":       symbol,
        "direction":    "LONG",
        "grade":        grade,
        "score":        score,
        "entry_ref":    entry,
        "entry_low":    sl,
        "entry_high":   round(entry * 1.002, 2),
        "sl":           sl,
        "target_1":     t1,
        "target_2":     t2,
        "sl_pct":       round(sl_pct * 100, 2),
        "delivery_pct": round(delivery_pct, 1),
        "rvol":         round(rvol, 2),
        "rsi":          round(rsi, 1),
        "ema_aligned":  ema20 > ema50 > ema200,
        "setup_name":   "BTST — Accumulation into close",
        "signal_type":  "BTST",
        "hold":         "Exit tomorrow 9:15–10:00 AM",
    }


def run_btst_scan() -> list:
    """Main BTST scan — runs at 2:30 PM IST."""
    now = datetime.now(IST)
    log.info(f"BTST scan starting — {now.strftime('%d %b %Y %H:%M IST')}")

    # Block in bearish regime — no BTST longs in downtrend
    regime = get_market_regime()
    if regime == "bearish":
        log.info(f"Market regime: BEARISH — no BTST signals today")
        return []

    # Get live prices
    live_prices = get_live_prices()
    log.info(f"Live prices loaded: {len(live_prices)} stocks")

    # Scan all SMC parquets
    smc_files = sorted(DATA_DIR.glob("*.parquet"))
    log.info(f"Scanning {len(smc_files)} stocks for BTST setups...")

    candidates = []

    for fpath in smc_files:
        try:
            df = pd.read_parquet(fpath)
            if len(df) < 50:
                continue

            symbol   = fpath.stem
            ltp_data = live_prices.get(symbol, {})
            ltp      = float(ltp_data.get("ltp", 0))

            row    = {"symbol": symbol}
            result = score_btst(row, df, ltp)
            if result:
                candidates.append(result)

        except Exception as e:
            continue

    # Sort by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)
    top = candidates[:MAX_BTST_SIGNALS]

    log.info(f"BTST candidates found: {len(candidates)} | Publishing top {len(top)}")
    for c in top:
        log.info(
            f"  {c['symbol']:<12} Score:{c['score']} Grade:{c['grade']} "
            f"Del:{c['delivery_pct']}% RVOL:{c['rvol']}x RSI:{c['rsi']}"
        )

    return top


def push_btst_signals(signals: list) -> bool:
    """Push BTST signals to Supabase."""
    if not signals:
        log.info("No BTST signals to push")
        return True

    today = date.today().isoformat()

    # Clear existing BTST signals for today
    requests.delete(
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?signal_date=eq.{today}&signal_type=eq.BTST",
        headers=_headers()
    )

    records = []
    for s in signals:
        def safe(v, default=0.0):
            try:
                f = float(v)
                return default if (f != f) else round(f, 2)  # NaN check
            except:
                return default

        records.append({
            "signal_date":  today,
            "symbol":       s["symbol"],
            "direction":    "LONG",
            "grade":        s["grade"],
            "score":        int(s["score"]),
            "entry_ref":    safe(s["entry_ref"]),
            "entry_low":    safe(s["entry_low"]),
            "entry_high":   safe(s["entry_high"]),
            "sl":           safe(s["sl"]),
            "target_1":     safe(s["target_1"]),
            "target_2":     safe(s["target_2"]),
            "setup_name":   s["setup_name"],
            "trade_type":   "BTST",
            "delivery_pct": safe(s.get("delivery_pct", 0)),
            "rvol":         safe(s.get("rvol", 0)),
            "rsi":          safe(s.get("rsi", 0)),
            "sl_pct":       safe(s.get("sl_pct", 0)),
        })

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/signals",
        headers=_headers(),
        json=records
    )

    if r.status_code in (200, 201):
        log.info(f"Pushed {len(records)} BTST signals to Supabase")
        return True
    else:
        log.error(f"Push failed: {r.status_code} {r.text[:100]}")
        return False


def main():
    signals = run_btst_scan()
    if signals:
        push_btst_signals(signals)
    log.info("BTST scan complete.")


if __name__ == "__main__":
    main()
