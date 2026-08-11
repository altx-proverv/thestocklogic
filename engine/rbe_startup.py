"""
THE STOCK LOGIC — RBE Startup: Range Map Builder
=================================================
Runs at 9:00 AM IST before market open.

For each of 500 stocks:
  1. Fetch 30-day daily OHLC via Zerodha historical API
  2. ATR-adaptive lookback:
       low ATR (<2%)  -> 15-20 day range
       high ATR (>4%) -> 5-8 day range
       else           -> 10-12 day range
  3. Range = swing high / swing low within lookback
  4. PDH/PDL from yesterday
  5. 20-day avg volume + time-of-day volume curve baseline

Output: data/processed/rbe/range_map.json
Run: python3 engine/rbe_startup.py
"""
import os, sys, json, time, logging
from pathlib import Path
from datetime import date, timedelta, datetime, timezone

# rbe_engine validates generated_at against the IST date. Stamping it with a
# naive date.today() (box-local = UTC) agrees only while both jobs run in the
# early UTC morning; past 18:30 UTC the dates diverge and the engine would
# reject a map that had just been built. Match the rest of the repo.
IST = timezone(timedelta(hours=5, minutes=30))


def today_ist() -> date:
    return datetime.now(IST).date()

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd

from zerodha_tokens import ZERODHA_TOKEN_MAP
from atlas.execution.broker import get_kite

Path("reports").mkdir(exist_ok=True)
# StreamHandler ONLY. The crontab already redirects stdout to reports/rbe.log,
# and a FileHandler on that same path wrote every line a second time -- which is
# how the "error parsing request" that explained a whole dead session ended up
# buried under ~370 duplicated heartbeat lines. Other engine scripts keep a
# FileHandler safely because cron sends their stdout to a DIFFERENT file
# (reports/cron.log); RBE was the only collision.
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

RBE_DIR = Path("data/processed/rbe")

# Per-symbol exceptions were swallowed entirely, so 500 auth rejections looked
# identical to 500 symbols with no data. Log the first few reasons.
MAX_LOGGED_ERRORS = 5
MAX_FAIL_PCT      = 20.0   # warn above this; a total failure aborts outright

# NSE intraday volume distribution (U-shape) — fraction of daily volume
# traded by the END of each 15-min bucket from 9:15 to 15:30
VOLUME_CURVE = {
    "09:30": 0.12, "09:45": 0.18, "10:00": 0.23, "10:15": 0.27,
    "10:30": 0.31, "10:45": 0.34, "11:00": 0.37, "11:15": 0.40,
    "11:30": 0.43, "11:45": 0.46, "12:00": 0.48, "12:15": 0.50,
    "12:30": 0.52, "12:45": 0.54, "13:00": 0.56, "13:15": 0.59,
    "13:30": 0.62, "13:45": 0.65, "14:00": 0.68, "14:15": 0.72,
    "14:30": 0.76, "14:45": 0.81, "15:00": 0.87, "15:15": 0.94,
    "15:30": 1.00,
}


def adaptive_lookback(atr_pct: float) -> int:
    """ATR-adaptive range lookback."""
    if atr_pct < 2.0:
        return 18
    elif atr_pct > 4.0:
        return 7
    else:
        return 11


def build_range(df: pd.DataFrame) -> dict:
    """Build range map entry from 30-day OHLC dataframe."""
    if len(df) < 15:
        return None

    c = df["close"].iloc[-1]
    # ATR(14)
    hl  = df["high"] - df["low"]
    hc  = (df["high"] - df["close"].shift(1)).abs()
    lc  = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    atr = tr.rolling(14, min_periods=7).mean().iloc[-1]
    atr_pct = round(atr / c * 100, 2)

    lb = adaptive_lookback(atr_pct)
    window = df.tail(lb)

    return {
        "range_high":  round(float(window["high"].max()), 2),
        "range_low":   round(float(window["low"].min()), 2),
        "lookback":    lb,
        "atr":         round(float(atr), 2),
        "atr_pct":     atr_pct,
        "pdh":         round(float(df["high"].iloc[-1]), 2),
        "pdl":         round(float(df["low"].iloc[-1]), 2),
        "prev_close":  round(float(df["close"].iloc[-1]), 2),
        "avg_vol_20d": int(df["volume"].tail(20).mean()),
    }


def main():
    log.info("=" * 50)
    log.info("RBE STARTUP — Range Map Builder")
    log.info("=" * 50)
    RBE_DIR.mkdir(parents=True, exist_ok=True)

    kite = get_kite()
    if kite is None:
        log.error("No Zerodha session — aborting. Run morning login first.")
        sys.exit(1)

    to_date   = today_ist() - timedelta(days=1)
    from_date = to_date - timedelta(days=45)  # 45 cal days ≈ 30 trading days

    range_map, failed = {}, []
    symbols = list(ZERODHA_TOKEN_MAP.items())
    log.info(f"Fetching 30-day OHLC for {len(symbols)} stocks...")

    logged_errors = 0
    for i, (sym, token) in enumerate(symbols):
        try:
            candles = kite.historical_data(token, from_date, to_date, "day")
            if not candles:
                failed.append(sym)
                continue
            df = pd.DataFrame(candles)
            entry = build_range(df)
            if entry:
                range_map[sym] = entry
            else:
                failed.append(sym)
        except Exception as e:
            failed.append(sym)
            # Log the REASON for the first few. Swallowing every exception is
            # how an expired access token turned into 500 silent failures, an
            # empty range map, and a clean exit 0.
            if logged_errors < MAX_LOGGED_ERRORS:
                log.error(f"  {sym}: {type(e).__name__}: {str(e)[:160]}")
                logged_errors += 1
                if logged_errors == MAX_LOGGED_ERRORS:
                    log.error("  (further per-symbol errors suppressed)")
            if "Too many requests" in str(e):
                time.sleep(1)
        # Zerodha rate limit: 3 req/sec
        time.sleep(0.34)
        if (i + 1) % 100 == 0:
            log.info(f"  {i+1}/{len(symbols)} done")

    fail_pct = len(failed) / max(len(symbols), 1) * 100

    # Do NOT write an empty map. rbe_engine reads this file and used to
    # subscribe to zero tokens on it, which Kite rejects outright and which
    # produced a full 6-hour session of empty heartbeats. Leaving the previous
    # file in place is strictly better -- rbe_engine rejects it as stale.
    if not range_map:
        log.error(f"ABORT: 0 of {len(symbols)} symbols returned data "
                  f"({len(failed)} failures). range_map.json NOT written; the "
                  f"existing file is left untouched.")
        log.error("Most likely cause: the Zerodha access token is missing or "
                  "expired. Complete the 08:30 IST Telegram login, then re-run.")
        sys.exit(1)

    out = {
        "generated_at": today_ist().isoformat(),
        "volume_curve": VOLUME_CURVE,
        "stocks": range_map,
    }
    with open(RBE_DIR / "range_map.json", "w") as f:
        json.dump(out, f)

    log.info(f"Range map: {len(range_map)} stocks | Failed: {len(failed)}")
    if fail_pct > MAX_FAIL_PCT:
        log.warning(f"{len(failed)}/{len(symbols)} symbols failed ({fail_pct:.0f}%) "
                    f"-- range map is incomplete, coverage will be partial.")
    if failed[:10]:
        log.info(f"Failed sample: {failed[:10]}")
    log.info(f"Written: {RBE_DIR / 'range_map.json'}")
    log.info("STATUS: " + ("PASS" if len(range_map) >= 450 else "PARTIAL"))


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
