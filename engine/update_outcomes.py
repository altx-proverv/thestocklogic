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


def fetch_existing_outcomes():
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signal_outcomes?select=signal_id,outcome",
        headers=sb_headers()
    )
    data = r.json()
    # Return set of signal_ids that are already decided (not OPEN)
    decided = set()
    open_ids = set()
    for row in data:
        if row["outcome"] in ("WIN_T1", "WIN_T2", "LOSS", "MISSED", "INVALIDATED"):
            decided.add(row["signal_id"])
        else:
            open_ids.add(row["signal_id"])
    return decided, open_ids


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
            if day_low <= sl:
                outcome = "LOSS"; exit_price = sl; exit_day = d; days_held = i+1; break
            if day_high >= t1:
                outcome = "WIN_T1"; exit_price = t1; exit_day = d; days_held = i+1; break
        else:
            if day_high >= sl:
                outcome = "LOSS"; exit_price = sl; exit_day = d; days_held = i+1; break
            if day_low <= t1:
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
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/signal_outcomes",
        headers=sb_headers(),
        json=records
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

    decided_ids, open_ids = fetch_existing_outcomes()
    log.info(f"Already decided: {len(decided_ids)} | Open (need recheck): {len(open_ids)}")

    today = date.today()
    to_process = [
        s for s in signals
        if date.fromisoformat(s["signal_date"]) < today
        and s["id"] not in decided_ids
    ]
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
            "signal_id":    sig["id"],
            "symbol":       sig["symbol"],
            "signal_date":  sig["signal_date"],
            "direction":    sig["direction"],
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
