"""
THE STOCK LOGIC — Automated Trade Review Engine
================================================
Pulls all signals from Supabase, validates entries against real open prices,
checks outcomes against T1/SL over next 5 trading days using Bhavcopy data.

Output: complete P&L summary with win rates by grade, sector, setup.
"""

import os, sys, json, requests
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date, timedelta

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
DATA_DIR     = Path("data/processed/stocks")
PARQUET_DIR  = Path("data/processed/stocks")

# NSE holidays 2025-2026
from trading_calendar import is_trading_day, next_n_trading_days, friday_gap_risk_flag

def next_trading_days(from_date, n=5):
    return next_n_trading_days(from_date, n)
def fetch_signals():
    """Fetch all signals from Supabase."""
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals?order=signal_date.asc&select=*&limit=2000",
        headers=headers
    )
    return r.json()

def load_stock_data(symbol):
    """Load Bhavcopy parquet for a symbol."""
    path = PARQUET_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.set_index("date")
    return df

def evaluate_signal(sig, stock_df):
    """
    Evaluate a single signal against real price data.
    Returns dict with entry_status, outcome, days_to_exit, pnl.
    """
    signal_date = date.fromisoformat(sig["signal_date"])
    direction   = sig["direction"]
    entry_ref   = float(sig.get("entry_ref") or 0)
    entry_low   = float(sig.get("entry_low") or entry_ref * 0.998)
    entry_high  = float(sig.get("entry_high") or entry_ref * 1.002)
    sl          = float(sig.get("sl") or 0)
    t1          = float(sig.get("target_1") or 0)
    t2          = float(sig.get("target_2") or 0)
    qty         = int(sig.get("qty") or 1)
    risk_inr    = float(sig.get("risk_inr") or 0)

    if entry_ref <= 0 or sl <= 0 or t1 <= 0:
        return {"entry_status": "NO_LEVELS", "outcome": "SKIP"}

    # Get next trading day prices
    next_days = next_trading_days(signal_date, 6)
    if not next_days:
        return {"entry_status": "NO_DATA", "outcome": "SKIP"}

    entry_day = next_days[0]

    # Check entry feasibility using next day open
    if entry_day not in stock_df.index:
        return {"entry_status": "NO_DATA", "outcome": "SKIP"}

    next_open = float(stock_df.loc[entry_day, "open"])
    next_low  = float(stock_df.loc[entry_day, "low"])
    next_high = float(stock_df.loc[entry_day, "high"])

    # Entry validation
    if direction == "LONG":
        # Gap up past entry zone = missed
        if next_open > entry_high * 1.005:
            return {"entry_status": "MISSED_GAP_UP", "outcome": "MISSED",
                    "next_open": next_open, "entry_high": entry_high}
        # Gap down through SL = invalidated
        if next_open < sl:
            return {"entry_status": "GAPPED_BELOW_SL", "outcome": "INVALIDATED",
                    "next_open": next_open, "sl": sl}
        entry_feasible = True
        actual_entry = min(next_open, entry_high)  # conservative entry

    else:  # SHORT
        # Gap down past entry zone = missed
        if next_open < entry_low * 0.995:
            return {"entry_status": "MISSED_GAP_DOWN", "outcome": "MISSED",
                    "next_open": next_open, "entry_low": entry_low}
        # Gap up through SL = invalidated
        if next_open > sl:
            return {"entry_status": "GAPPED_ABOVE_SL", "outcome": "INVALIDATED",
                    "next_open": next_open, "sl": sl}
        entry_feasible = True
        actual_entry = max(next_open, entry_low)

    # Now check outcome over next 5 trading days
    check_days = next_days[0:5]
    outcome = "OPEN"
    exit_day = None
    exit_price = None
    days_held = 0

    for i, d in enumerate(check_days):
        if d not in stock_df.index:
            continue
        day_high = float(stock_df.loc[d, "high"])
        day_low  = float(stock_df.loc[d, "low"])

        if direction == "LONG":
            # Check SL first (intraday SL before T1)
            if day_low <= sl:
                outcome   = "LOSS"
                exit_price = sl
                exit_day   = d
                days_held  = i + 1
                break
            # Check T1
            if day_high >= t1:
                outcome   = "WIN_T1"
                exit_price = t1
                exit_day   = d
                days_held  = i + 1
                break
        else:  # SHORT
            if day_high >= sl:
                outcome   = "LOSS"
                exit_price = sl
                exit_day   = d
                days_held  = i + 1
                break
            if day_low <= t1:
                outcome   = "WIN_T1"
                exit_price = t1
                exit_day   = d
                days_held  = i + 1
                break

    # Calculate P&L
    if outcome == "WIN_T1":
        pnl = abs(exit_price - actual_entry) * qty
    elif outcome == "LOSS":
        pnl = -risk_inr if risk_inr > 0 else -abs(actual_entry - sl) * qty
    else:
        pnl = 0

    return {
        "entry_status":  "FEASIBLE",
        "outcome":       outcome,
        "next_open":     round(next_open, 2),
        "actual_entry":  round(actual_entry, 2),
        "exit_price":    round(exit_price, 2) if exit_price else None,
        "exit_day":      exit_day.isoformat() if exit_day else None,
        "days_held":     days_held,
        "pnl":           round(pnl, 2),
        "t1":            round(t1, 2),
        "sl":            round(sl, 2),
    }

def main():
    print("="*60)
    print("THE STOCK LOGIC — AUTOMATED TRADE REVIEW")
    print("="*60)

    if not SUPABASE_KEY:
        print("ERROR: SUPABASE_SERVICE_KEY not set")
        sys.exit(1)

    # Fetch all signals
    print("\nFetching signals from Supabase...")
    signals = fetch_signals()
    if not signals or isinstance(signals, dict):
        print(f"Error: {signals}")
        sys.exit(1)
    print(f"Total signals: {len(signals)}")

    # Today — skip signals from today (no outcome yet)
    today = date.today()
    signals = [s for s in signals if date.fromisoformat(s["signal_date"]) < today]
    print(f"Evaluable signals (before today): {len(signals)}")

    # Process each signal
    results = []
    missing_stocks = set()
    processed = 0

    for sig in signals:
        symbol = sig["symbol"]
        stock_df = load_stock_data(symbol)
        if stock_df is None:
            missing_stocks.add(symbol)
            continue

        result = evaluate_signal(sig, stock_df)
        result.update({
            "signal_date": sig["signal_date"],
            "symbol":      symbol,
            "direction":   sig["direction"],
            "grade":       sig.get("grade", "B"),
            "score":       sig.get("score", 0),
            "sector":      sig.get("sector", "OTHER"),
            "setup_name":  sig.get("setup_name", ""),
            "entry_ref":   sig.get("entry_ref", 0),
            "risk_inr":    sig.get("risk_inr", 0),
        })
        results.append(result)
        processed += 1

    print(f"Processed: {processed} | Missing stock data: {len(missing_stocks)}")
    if missing_stocks:
        print(f"Missing: {list(missing_stocks)[:10]}")

    df = pd.DataFrame(results)

    # ── SUMMARY ────────────────────────────────────────────────
    feasible = df[df["entry_status"] == "FEASIBLE"]
    missed   = df[df["outcome"] == "MISSED"]
    invalid  = df[df["outcome"] == "INVALIDATED"]
    wins     = feasible[feasible["outcome"] == "WIN_T1"]
    losses   = feasible[feasible["outcome"] == "LOSS"]
    open_tr  = feasible[feasible["outcome"] == "OPEN"]

    print(f"\n{'='*60}")
    print("OVERALL SUMMARY")
    print(f"{'='*60}")
    print(f"  Total signals evaluated : {len(df)}")
    print(f"  Entry feasible          : {len(feasible)} ({len(feasible)/max(len(df),1)*100:.1f}%)")
    print(f"  Missed (gap past zone)  : {len(missed)} ({len(missed)/max(len(df),1)*100:.1f}%)")
    print(f"  Invalidated (gap to SL) : {len(invalid)} ({len(invalid)/max(len(df),1)*100:.1f}%)")
    print(f"\n  Of feasible entries:")
    print(f"    WIN  (hit T1)         : {len(wins)} ({len(wins)/max(len(feasible),1)*100:.1f}%)")
    print(f"    LOSS (hit SL)         : {len(losses)} ({len(losses)/max(len(feasible),1)*100:.1f}%)")
    print(f"    OPEN (5d expired)     : {len(open_tr)} ({len(open_tr)/max(len(feasible),1)*100:.1f}%)")

    if len(wins) + len(losses) > 0:
        win_rate = len(wins) / (len(wins) + len(losses)) * 100
        total_pnl = feasible["pnl"].sum()
        print(f"\n  Win rate (W+L only)     : {win_rate:.1f}%")
        print(f"  Total P&L               : ₹{total_pnl:,.0f}")
        print(f"  Avg win                 : ₹{wins['pnl'].mean():,.0f}" if len(wins) else "")
        print(f"  Avg loss                : ₹{losses['pnl'].mean():,.0f}" if len(losses) else "")
        if len(wins) and len(losses):
            print(f"  Profit factor           : {abs(wins['pnl'].sum()/losses['pnl'].sum()):.2f}")

    # ── BY GRADE ───────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("WIN RATE BY GRADE")
    print(f"{'='*60}")
    for grade in ["A+", "A", "B"]:
        g = feasible[feasible["grade"] == grade]
        gw = g[g["outcome"] == "WIN_T1"]
        gl = g[g["outcome"] == "LOSS"]
        if len(g):
            wr = len(gw)/max(len(gw)+len(gl),1)*100
            print(f"  {grade:<4} : {len(g):>3} signals | {len(gw):>2} W {len(gl):>2} L | Win rate: {wr:.1f}% | P&L: ₹{g['pnl'].sum():,.0f}")

    # ── BY SECTOR ──────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("WIN RATE BY SECTOR")
    print(f"{'='*60}")
    sector_stats = []
    for sector in feasible["sector"].unique():
        g  = feasible[feasible["sector"] == sector]
        gw = g[g["outcome"] == "WIN_T1"]
        gl = g[g["outcome"] == "LOSS"]
        if len(gw) + len(gl) > 0:
            wr = len(gw)/max(len(gw)+len(gl),1)*100
            sector_stats.append((sector, len(g), len(gw), len(gl), wr, g["pnl"].sum()))
    sector_stats.sort(key=lambda x: -x[4])
    for s in sector_stats:
        print(f"  {s[0]:<12} : {s[1]:>3} signals | {s[2]:>2} W {s[3]:>2} L | Win rate: {s[4]:.1f}% | P&L: ₹{s[5]:,.0f}")

    # ── BY DIRECTION ───────────────────────────────────────────
    print(f"\n{'='*60}")
    print("WIN RATE BY DIRECTION")
    print(f"{'='*60}")
    for direction in ["LONG", "SHORT"]:
        g  = feasible[feasible["direction"] == direction]
        gw = g[g["outcome"] == "WIN_T1"]
        gl = g[g["outcome"] == "LOSS"]
        if len(g):
            wr = len(gw)/max(len(gw)+len(gl),1)*100
            print(f"  {direction:<6} : {len(g):>3} signals | {len(gw):>2} W {len(gl):>2} L | Win rate: {wr:.1f}% | P&L: ₹{g['pnl'].sum():,.0f}")

    # ── RECENT SIGNALS DETAIL ──────────────────────────────────
    print(f"\n{'='*60}")
    print("RECENT SIGNALS DETAIL (last 30)")
    print(f"{'='*60}")
    recent = df.tail(30).sort_values("signal_date", ascending=False)
    print(f"  {'DATE':<12} {'SYM':<12} {'DIR':<6} {'GR':<4} {'SCORE':<6} {'ENTRY_REF':<10} {'OPEN_NXT':<10} {'STATUS':<12} {'OUTCOME':<12} {'P&L':>8}")
    print(f"  {'-'*100}")
    for _, r in recent.iterrows():
        pnl_str = f"₹{r['pnl']:,.0f}" if r.get('pnl') else "—"
        open_str = f"₹{r['next_open']:,.1f}" if r.get('next_open') else "—"
        outcome_icon = "✅" if r["outcome"] == "WIN_T1" else "❌" if r["outcome"] == "LOSS" else "⏳" if r["outcome"] == "OPEN" else "⊘"
        print(f"  {r['signal_date']:<12} {r['symbol']:<12} {r['direction']:<6} {r['grade']:<4} {str(r['score']):<6} ₹{float(r['entry_ref']):>7.1f}  {open_str:<10} {r['entry_status']:<12} {outcome_icon} {r['outcome']:<10} {pnl_str:>8}")

    # Save results
    df.to_csv("reports/trade_review.csv", index=False)
    print(f"\nFull results saved to: reports/trade_review.csv")
    print(f"\nSTATUS: COMPLETE")

if __name__ == "__main__":
    os.chdir(Path(__file__).parent if Path(__file__).parent.name != "engine" else Path(__file__).parent.parent)
    main()
