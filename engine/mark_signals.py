"""
THE STOCK LOGIC — Daily Directional Marking
===========================================
Marks every LIVE signal previous close -> current close, once per trading day.

A signal published in the last WINDOW_DAYS trading days is live. Each evening it
is marked again: LONG up = correct today, SHORT down = correct today. The unit is
TODAY'S DIRECTION, not the outcome of a trade. A signal down 15% since its call
still counts as correct on any day it rises; cumulative move is tracked
separately.

WHY DAILY MARKS AND NOT MARK-TO-TODAY
-------------------------------------
Measured from the signal date to now, accuracy is dominated by how old the
window happens to be -- 42.6% at one day, 71.6% past sixty on this data, because
given enough days a LONG drifts up with the market. A blended figure mostly
reports the age mix. Every daily mark is a one-day move, so all marks are
comparable whatever the signal's age.

Older signals do contribute MORE marks than recent ones (a 20-day-old signal has
been marked 19 times, yesterday's once). That is inherent to a live book and the
page says so.

DELIBERATELY SEPARATE from signal_outcomes and the trade record. Those measure a
trade with an entry, a stop and a target. This measures a directional call. The
two are not comparable and are never mixed.

Run: python3 engine/mark_signals.py [--backfill N] [--date YYYY-MM-DD]
Cron: after 06_push_supabase in the EOD chain.
"""

import os
import sys
import argparse
import logging
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
try:
    from engine.trading_calendar import is_trading_day
except ModuleNotFoundError:
    from trading_calendar import is_trading_day

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
STOCKS_DIR   = Path("data/processed/stocks")
MARKET_FILE  = Path("data/processed/market.parquet")

WINDOW_DAYS = 20     # a signal is live for this many trading days
BATCH       = 500    # rows per upsert request


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def fetch_signals() -> pd.DataFrame:
    """Every published signal. Deduped on the natural key -- 06_push can leave
    more than one row per (date, symbol, direction) and marks are uniquely keyed
    on exactly that, so duplicates would upsert over each other."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?select=signal_date,symbol,direction,setup_name&limit=20000",
        headers=_headers(), timeout=60)
    if r.status_code != 200:
        log.error(f"signal fetch failed: HTTP {r.status_code} {r.text[:200]}")
        sys.exit(1)
    df = pd.DataFrame(r.json())
    if df.empty:
        return df
    df["signal_date"] = pd.to_datetime(df["signal_date"])
    df["direction"]   = df["direction"].astype(str).str.upper()
    return df.drop_duplicates(subset=["signal_date", "symbol", "direction"])


def load_closes() -> dict:
    """{symbol: DataFrame[date, close]} sorted ascending."""
    out = {}
    for f in STOCKS_DIR.glob("*.parquet"):
        try:
            d = pd.read_parquet(f, columns=["date", "close"])
        except Exception as e:
            log.warning(f"{f.stem}: unreadable ({e})")
            continue
        d["date"] = pd.to_datetime(d["date"])
        out[f.stem] = d.sort_values("date").reset_index(drop=True)
    return out


def nifty_series() -> pd.DataFrame:
    if not MARKET_FILE.exists():
        log.warning("market.parquet absent — no Nifty benchmark")
        return pd.DataFrame()
    d = pd.read_parquet(MARKET_FILE, columns=["date", "nifty_close"])
    d["date"] = pd.to_datetime(d["date"])
    return d.dropna().sort_values("date").reset_index(drop=True)


def trading_dates(closes: dict) -> list:
    """Every date on which we hold closes, ascending. This is the real trading
    calendar as observed in the data, which beats a hardcoded holiday list."""
    seen = set()
    for d in closes.values():
        seen.update(d["date"].tolist())
    return sorted(seen)


def mark_one_date(signals: pd.DataFrame, closes: dict, all_dates: list,
                  mark_date: pd.Timestamp) -> list:
    """Rows for every live signal on one mark date."""
    idx = all_dates.index(mark_date)
    if idx == 0:
        return []                                   # no previous close to mark from
    prev_date = all_dates[idx - 1]
    window    = all_dates[max(0, idx - WINDOW_DAYS + 1): idx + 1]
    window_start = window[0]

    live = signals[(signals["signal_date"] >= window_start) &
                   (signals["signal_date"] <= mark_date)]

    rows = []
    for s in live.itertuples():
        # A signal contributes from the day AFTER it publishes. On its own
        # signal date the reference close IS the mark close, so it can be
        # neither correct nor incorrect -- excluded, not counted as a miss.
        if s.signal_date >= mark_date:
            continue
        d = closes.get(s.symbol)
        if d is None:
            continue

        def close_on(dt):
            hit = d.loc[d["date"] == dt, "close"]
            return float(hit.iloc[0]) if len(hit) else None

        ref, prev, cur = close_on(s.signal_date), close_on(prev_date), close_on(mark_date)
        if ref is None or prev is None or cur is None or ref <= 0 or prev <= 0:
            continue

        sign  = 1.0 if s.direction == "LONG" else -1.0
        daily = (cur - prev) / prev * 100.0 * sign
        cum   = (cur - ref) / ref * 100.0 * sign
        # Exactly flat: neither correct nor incorrect. NULL, not False.
        correct = None if cur == prev else bool(daily > 0)

        age = sum(1 for x in all_dates
                  if s.signal_date < x <= mark_date)

        rows.append({
            "signal_date":    s.signal_date.date().isoformat(),
            "symbol":         s.symbol,
            "direction":      s.direction,
            "mark_date":      mark_date.date().isoformat(),
            "ref_close":      round(ref, 2),
            "prev_close":     round(prev, 2),
            "mark_close":     round(cur, 2),
            "daily_move_pct": round(daily, 4),
            "cum_move_pct":   round(cum, 4),
            "correct_today":  correct,
            "setup_name":     s.setup_name,
            "age_days":       age,
        })
    return rows


def upsert(table: str, rows: list, conflict: str) -> bool:
    ok = True
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
                          headers=_headers(), json=chunk, timeout=120)
        if r.status_code not in (200, 201, 204):
            log.error(f"{table} upsert failed: HTTP {r.status_code} {r.text[:250]}")
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=1,
                    help="number of most recent trading days to mark (default 1)")
    ap.add_argument("--date", help="mark this date only (YYYY-MM-DD)")
    args = ap.parse_args()

    if not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY not set")
        return 1

    signals = fetch_signals()
    if signals.empty:
        log.error("no signals to mark")
        return 1
    closes = load_closes()
    if not closes:
        log.error("no close data in data/processed/stocks")
        return 1
    all_dates = trading_dates(closes)
    log.info(f"{len(signals)} signals | {len(closes)} symbols | "
             f"closes through {all_dates[-1].date()}")

    if args.date:
        want = [pd.Timestamp(args.date)]
        if want[0] not in all_dates:
            log.error(f"{args.date} is not a date we hold closes for")
            return 1
    else:
        want = all_dates[-args.backfill:]

    total = 0
    for md in want:
        rows = mark_one_date(signals, closes, all_dates, md)
        if not rows:
            log.info(f"{md.date()}: nothing live")
            continue
        if upsert("signal_marks", rows,
                  "signal_date,symbol,direction,mark_date"):
            scored = [r for r in rows if r["correct_today"] is not None]
            hit = sum(1 for r in scored if r["correct_today"])
            acc = f"{hit / len(scored) * 100:.1f}%" if scored else "n/a"
            log.info(f"{md.date()}: {len(rows)} marks | correct {hit}/{len(scored)} = {acc}")
            total += len(rows)

    # Benchmark
    nf = nifty_series()
    if not nf.empty:
        mrows = []
        for md in want:
            i = nf.index[nf["date"] == md]
            if len(i) == 0 or i[0] == 0:
                continue
            i = i[0]
            cur  = float(nf.loc[i, "nifty_close"])
            prev = float(nf.loc[i - 1, "nifty_close"])
            if prev <= 0:
                continue
            mrows.append({"mark_date": md.date().isoformat(),
                          "nifty_close": round(cur, 2),
                          "nifty_move_pct": round((cur - prev) / prev * 100.0, 4)})
        if mrows:
            upsert("market_marks", mrows, "mark_date")
            log.info(f"benchmark: {len(mrows)} Nifty mark(s)")
        else:
            log.warning("no Nifty marks written — market.parquet may lag the stock closes")

    log.info(f"DONE — {total} mark row(s)")
    return 0


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    sys.exit(main())
