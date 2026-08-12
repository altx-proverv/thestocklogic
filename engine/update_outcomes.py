"""
THE STOCK LOGIC — Signal Outcome Updater
=========================================
Runs daily after EOD pipeline.
Fetches all signals, evaluates outcomes against real price data,
pushes results to signal_outcomes table in Supabase.
"""

import os, sys, requests, logging
import pandas as pd
from pathlib import Path
from datetime import date, timedelta

sys.path.insert(0, str(Path(__file__).parent))
from trading_calendar import next_n_trading_days, is_trading_day
try:
    from engine.zone_entry import measurement_targets
except ModuleNotFoundError:
    from zone_entry import measurement_targets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DATA_DIR     = Path("data/processed/stocks")


def sb_headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }


def fetch_all_signals():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals?order=signal_date.asc&select=*&limit=5000",
        headers=sb_headers()
    )
    return r.json()


DECIDED = ("WIN_T1", "WIN_T2", "LOSS", "MISSED", "INVALIDATED")


def nat_key(row) -> tuple:
    """(signal_date, symbol, direction) -- what actually identifies a signal.

    signals.id does NOT: 06_push_supabase deletes and re-inserts a whole date on
    every run, so every id for that date changes. Outcomes used to be keyed on
    that id under a foreign key, so re-running a date destroyed its measured
    history via the cascade.
    """
    return (str(row.get("signal_date")), str(row.get("symbol")),
            str(row.get("direction") or "").upper())


def fetch_existing_outcomes():
    """Natural keys already decided. Anything else is re-scored and upserted."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signal_outcomes"
        f"?select=signal_date,symbol,direction,outcome",
        headers=sb_headers(), timeout=30
    )
    if r.status_code != 200:
        log.error(f"could not read existing outcomes: HTTP {r.status_code} {r.text[:200]}")
        sys.exit(1)
    decided = {nat_key(row) for row in r.json() if (row.get("outcome") or "") in DECIDED}
    return decided


def load_stock(symbol):
    path = DATA_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.set_index("date")


def evaluate(sig, stock_df):
    signal_date = date.fromisoformat(sig["signal_date"])
    direction   = sig["direction"]
    entry_ref   = float(sig.get("entry_ref") or 0)
    entry_low   = float(sig.get("entry_low") or entry_ref * 0.998)
    entry_high  = float(sig.get("entry_high") or entry_ref * 1.002)
    sl          = float(sig.get("sl") or 0)
    t1          = float(sig.get("target_1") or 0)
    qty         = int(sig.get("qty") or 1)
    risk_inr    = float(sig.get("risk_inr") or 0)

    # Signals pushed before the measurement yardstick was restored carry a NULL
    # target_1 and used to fall straight through the guard below as SKIP --
    # which is why this job reported "Processed: 0" while looking healthy.
    # Derive the same 2R/3R yardstick so the historical record is scored too.
    if t1 <= 0 and entry_ref > 0 and sl > 0:
        _t1, _ = measurement_targets(entry_ref, sl, direction)
        t1 = float(_t1 or 0)

    if entry_ref <= 0 or sl <= 0 or t1 <= 0:
        return {"entry_status": "NO_LEVELS", "outcome": "SKIP"}

    next_days = next_n_trading_days(signal_date, 6)
    if not next_days:
        return {"entry_status": "NO_DATA", "outcome": "OPEN"}

    entry_day = next_days[0]
    if entry_day not in stock_df.index:
        return {"entry_status": "NO_DATA", "outcome": "OPEN"}

    next_open = float(stock_df.loc[entry_day, "open"])

    # Entry validation
    if direction == "LONG":
        if next_open > entry_high * 1.005:
            return {"entry_status": "MISSED_GAP_UP", "outcome": "MISSED", "actual_entry": next_open}
        if next_open < sl:
            return {"entry_status": "GAPPED_BELOW_SL", "outcome": "INVALIDATED", "actual_entry": next_open}
        actual_entry = min(next_open, entry_high)
    else:
        if next_open < entry_low * 0.995:
            return {"entry_status": "MISSED_GAP_DOWN", "outcome": "MISSED", "actual_entry": next_open}
        if next_open > sl:
            return {"entry_status": "GAPPED_ABOVE_SL", "outcome": "INVALIDATED", "actual_entry": next_open}
        actual_entry = max(next_open, entry_low)

    # Check outcome over next 5 days
    check_days = next_days[0:5]
    outcome    = "OPEN"
    exit_day   = None
    exit_price = None
    days_held  = 0

    for i, d in enumerate(check_days):
        if d not in stock_df.index:
            continue
        day_high = float(stock_df.loc[d, "high"])
        day_low  = float(stock_df.loc[d, "low"])

        if direction == "LONG":
            hit_sl = day_low  <= sl
            hit_t1 = day_high >= t1
            # Both touched same day: sequence unknowable from daily OHLC
            if hit_sl and hit_t1:
                outcome = "AMBIGUOUS"; exit_price = None; exit_day = d; days_held = i+1; break
            if hit_sl:
                outcome = "LOSS"; exit_price = sl; exit_day = d; days_held = i+1; break
            if hit_t1:
                outcome = "WIN_T1"; exit_price = t1; exit_day = d; days_held = i+1; break
        else:
            hit_sl = day_high >= sl
            hit_t1 = day_low  <= t1
            if hit_sl and hit_t1:
                outcome = "AMBIGUOUS"; exit_price = None; exit_day = d; days_held = i+1; break
            if hit_sl:
                outcome = "LOSS"; exit_price = sl; exit_day = d; days_held = i+1; break
            if hit_t1:
                outcome = "WIN_T1"; exit_price = t1; exit_day = d; days_held = i+1; break

    if outcome == "WIN_T1":
        pnl = abs(exit_price - actual_entry) * qty
    elif outcome == "LOSS":
        pnl = -risk_inr if risk_inr > 0 else -abs(actual_entry - sl) * qty
    else:
        pnl = 0

    return {
        "entry_status": "FEASIBLE",
        "outcome":      outcome,
        "actual_entry": round(actual_entry, 2),
        "exit_price":   round(exit_price, 2) if exit_price else None,
        "exit_day":     exit_day.isoformat() if exit_day else None,
        "days_held":    days_held,
        "pnl":          round(pnl, 2),
    }


def push_outcomes(records):
    if not records:
        return
    # UPSERT on the natural key. sb_headers already sends
    # Prefer: resolution=merge-duplicates, but with no unique constraint to
    # conflict on it silently inserted every time -- so every OPEN outcome was
    # re-inserted nightly and the table reached 10.3 copies per key, worst case
    # 29. on_conflict names the constraint's columns so the merge actually fires.
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/signal_outcomes"
        f"?on_conflict=signal_date,symbol,direction",
        headers=sb_headers(),
        json=records, timeout=60
    )
    if r.status_code not in (200, 201):
        log.error(f"Push failed: {r.status_code} {r.text[:200]}")
    else:
        log.info(f"Pushed {len(records)} outcomes to Supabase")


def main():
    log.info("="*50)
    log.info("SIGNAL OUTCOME UPDATER")
    log.info("="*50)

    if not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY not set")
        sys.exit(1)

    signals = fetch_all_signals()
    if not signals or isinstance(signals, dict):
        log.error(f"Failed to fetch signals: {signals}")
        sys.exit(1)
    log.info(f"Total signals: {len(signals)}")

    decided = fetch_existing_outcomes()
    log.info(f"Already decided: {len(decided)} natural key(s)")

    # Dedup the SIGNALS too. 06_push can leave more than one row per
    # (date, symbol, direction) -- and since outcomes are now uniquely keyed on
    # exactly that, two signal rows sharing a key would upsert over each other.
    today = date.today()
    seen, to_process = set(), []
    for sig in signals:
        try:
            if date.fromisoformat(sig["signal_date"]) >= today:
                continue
        except (TypeError, ValueError):
            continue
        k = nat_key(sig)
        if k in decided or k in seen:
            continue
        seen.add(k)
        to_process.append(sig)
    log.info(f"To process: {len(to_process)}")

    records  = []
    skipped  = 0
    no_data  = 0

    for sig in to_process:
        stock_df = load_stock(sig["symbol"])
        if stock_df is None:
            no_data += 1
            continue

        result = evaluate(sig, stock_df)

        if result["outcome"] == "SKIP":
            skipped += 1
            continue

        records.append({
            # Soft pointer only -- the natural key below is authoritative.
            "signal_id":    sig["id"],
            "symbol":       sig["symbol"],
            "signal_date":  sig["signal_date"],
            "direction":    sig["direction"],
            # Levels copied ONTO the outcome so the accuracy record is
            # self-contained and survives a re-push of the signal it came from.
            "entry_ref":    sig.get("entry_ref"),
            "sl":           sig.get("sl"),
            "target_1":     sig.get("target_1"),
            "grade":        sig.get("grade", "B"),
            "score":        sig.get("score", 0),
            "sector":       sig.get("sector", "OTHER"),
            "outcome":      result["outcome"],
            "entry_status": result["entry_status"],
            "actual_entry": result.get("actual_entry"),
            "exit_price":   result.get("exit_price"),
            "exit_day":     result.get("exit_day"),
            "days_held":    result.get("days_held", 0),
            "pnl":          result.get("pnl", 0),
        })

    log.info(f"Processed: {len(records)} | No data: {no_data} | Skipped: {skipped}")

    # Batch push in chunks of 100
    for i in range(0, len(records), 100):
        push_outcomes(records[i:i+100])

    # Summary
    if records:
        from collections import Counter
        outcomes = Counter(r["outcome"] for r in records)
        log.info(f"Outcomes: {dict(outcomes)}")
        feasible = [r for r in records if r["entry_status"] == "FEASIBLE"]
        wins     = [r for r in feasible if r["outcome"] == "WIN_T1"]
        losses   = [r for r in feasible if r["outcome"] == "LOSS"]
        if wins or losses:
            wr = len(wins) / max(len(wins)+len(losses), 1) * 100
            pnl = sum(r["pnl"] for r in feasible)
            log.info(f"Win rate: {wr:.1f}% ({len(wins)}W/{len(losses)}L) | P&L: ₹{pnl:,.0f}")

    log.info("DONE")


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()
