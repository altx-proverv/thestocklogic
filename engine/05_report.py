"""
THE STOCK LOGIC — Daily Signal Report Engine
=============================================
Generates a clean, actionable daily signal report for manual trading.
Output: one report per day showing A+ and A grade setups only.

YOU decide whether to take the trade.
YOU place orders manually on your broker.
YOU set the stop loss manually.
YOU exit when target is hit or SL triggers.

The engine's job: find the setups, do the math, present clearly.

Grades:
  A+  : score 80+  — highest conviction, take seriously
  A   : score 75+  — good setup, take if regime supports
  Skip: score <75  — not enough confluence, stay cash

Risk rule:
  Max ₹5,000 loss per trade (5% of ₹1L capital)
  Position size = ₹5,000 ÷ SL distance per share
  Total deployed across all open trades ≤ ₹1,00,000

Run: python3 engine/05_report.py [YYYY-MM-DD]
     (defaults to latest available date if no date given)
"""

import os, sys, logging, warnings
from pathlib import Path
from datetime import datetime, date
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

SIGNALS_FILE = Path("data/processed/signals_v2/all_scores_v2.parquet")
STOCKS_DIR   = Path("data/processed/stocks")
MARKET_FILE  = Path("data/processed/market.parquet")
REPORTS_DIR  = Path("reports/daily")

CAPITAL      = 100_000.0
MAX_RISK_INR = 5_000.0
MIN_GRADE_A  = 75.0    # A grade threshold
MIN_GRADE_AP = 80.0    # A+ grade threshold


# ══════════════════════════════════════════════════════════════════
# REPORT BUILDER
# ══════════════════════════════════════════════════════════════════

def get_market_context(target_date: pd.Timestamp) -> dict:
    """Load market context for the report date."""
    if not MARKET_FILE.exists():
        return {}
    mkt = pd.read_parquet(MARKET_FILE)
    mkt["date"] = pd.to_datetime(mkt["date"])
    row = mkt[mkt["date"] == target_date]
    if row.empty:
        # Get closest available date
        past = mkt[mkt["date"] <= target_date]
        if past.empty: return {}
        row = past.iloc[[-1]]
    r = row.iloc[0]
    return {
        "nifty_close":    r.get("nifty_close", np.nan),
        "vix_close":      r.get("vix_close", np.nan),
        "market_regime":  r.get("market_regime", "unknown"),
        "ad_ratio":       r.get("ad_ratio", np.nan),
        "advance_count":  r.get("advance_count", np.nan),
        "decline_count":  r.get("decline_count", np.nan),
    }


def load_signals_for_date(target_date: pd.Timestamp) -> pd.DataFrame:
    """Load all signals scored for the target date."""
    if not SIGNALS_FILE.exists():
        log.error(f"Signals file not found: {SIGNALS_FILE}")
        return pd.DataFrame()

    df = pd.read_parquet(SIGNALS_FILE)
    df["date"] = pd.to_datetime(df["date"])

    day = df[df["date"] == target_date]
    if day.empty:
        # Find closest available date
        available = sorted(df["date"].unique())
        past = [d for d in available if d <= target_date]
        if not past:
            log.warning(f"No data available for {target_date.date()}")
            return pd.DataFrame()
        target_date = past[-1]
        day = df[df["date"] == target_date]
        log.info(f"Using closest available date: {target_date.date()}")

    return day


def compute_position_size(entry: float, sl: float, direction: str) -> dict:
    """
    Compute position size based on ₹5,000 max risk.
    qty = ₹5,000 ÷ (entry - sl) per share
    """
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return {"qty": 0, "deployed": 0, "actual_risk": 0, "sl_pct": 0}

    qty          = max(1, int(MAX_RISK_INR / sl_distance))
    deployed     = round(entry * qty, 2)
    actual_risk  = round(sl_distance * qty, 2)
    sl_pct       = round(sl_distance / entry * 100, 2)

    # Cap: don't deploy more than full capital
    if deployed > CAPITAL:
        qty      = max(1, int(CAPITAL / entry))
        deployed = round(entry * qty, 2)
        actual_risk = round(sl_distance * qty, 2)

    return {
        "qty":         qty,
        "deployed":    deployed,
        "actual_risk": actual_risk,
        "sl_pct":      sl_pct,
    }


def build_trade_card(row: pd.Series) -> dict:
    """Build a complete trade card from a signal row."""
    entry  = row.get("entry_ref", row.get("close", 0.0))
    sl     = row.get("sl", 0.0)
    t1     = row.get("target_1", 0.0)
    t2     = row.get("target_2", 0.0)
    direction = row.get("direction", "long")

    if entry <= 0 or sl <= 0:
        return None

    pos = compute_position_size(entry, sl, direction)
    if pos["qty"] == 0:
        return None

    # Validate RR
    if direction == "long":
        rr1 = round((t1 - entry) / abs(entry - sl), 2) if abs(entry-sl) > 0 else 0
        rr2 = round((t2 - entry) / abs(entry - sl), 2) if abs(entry-sl) > 0 else 0
    else:
        rr1 = round((entry - t1) / abs(sl - entry), 2) if abs(sl-entry) > 0 else 0
        rr2 = round((entry - t2) / abs(sl - entry), 2) if abs(sl-entry) > 0 else 0

    if rr1 < 1.5:   # minimum 1.5:1 RR
        return None

    score   = row.get("total_score", 0)
    grade   = "A+" if score >= MIN_GRADE_AP else "A"

    # Trade type based on direction and regime
    regime = row.get("market_regime", "unknown")
    if direction == "short":
        trade_type = "INTRADAY SHORT" if regime != "bear" else "BTST SHORT"
        hold_note  = "Exit same day (before 3:25 PM)" if trade_type == "INTRADAY SHORT" \
                     else "Can hold overnight"
    else:
        if score >= 85:
            trade_type = "SWING LONG"
            hold_note  = "Hold 3-15 trading days or until target/SL"
        else:
            trade_type = "BTST LONG"
            hold_note  = "Hold 1-3 trading days or until target/SL"

    # Invalidation conditions
    invalidation = []
    if direction == "long":
        invalidation.append(f"Close below ₹{sl:.1f} — exit immediately, no second chances")
        invalidation.append(f"Volume dries up (RVOL < 0.7) without price progress by Day 2")
        if row.get("structure_trend", "") == "uptrend":
            invalidation.append("Nifty breaks below its own daily support")
    else:
        invalidation.append(f"Close above ₹{sl:.1f} — exit same day, no overnight")
        invalidation.append("Broad market gaps up strongly next morning")

    return {
        "symbol":       row.get("symbol", ""),
        "date":         row.get("date"),
        "direction":    direction.upper(),
        "trade_type":   trade_type,
        "grade":        grade,
        "score":        score,
        "setup_name":   row.get("setup_name", ""),
        "market_regime":regime,
        "structure":    row.get("structure_trend", ""),

        # Levels
        "entry_low":    round(row.get("entry_low", entry * 0.998), 2),
        "entry_high":   round(row.get("entry_high", entry * 1.002), 2),
        "entry_ref":    round(entry, 2),
        "sl":           round(sl, 2),
        "target_1":     round(t1, 2),
        "target_2":     round(t2, 2),
        "rr_1":         rr1,
        "rr_2":         rr2,

        # Position
        "qty":          pos["qty"],
        "deployed":     pos["deployed"],
        "actual_risk":  pos["actual_risk"],
        "sl_pct":       pos["sl_pct"],

        # Context
        "rsi":          round(row.get("rsi", 0), 1),
        "rvol":         round(row.get("rvol", 0), 2),
        "vix_close":    round(row.get("vix_close", 0), 1),
        "delivery_pct": round(row.get("delivery_pct", 0), 1),
        "hold_note":    hold_note,
        "invalidation": invalidation,

        # Score breakdown
        "score_regime":    round(row.get("regime_score", 0), 1),
        "score_smc":       round(row.get("smc_score", 0), 1),
        "score_technical": round(row.get("technical_score", 0), 1),
        "score_volume":    round(row.get("volume_score", 0), 1),
        "score_rr":        round(row.get("rr_score", 0), 1),
    }


# ══════════════════════════════════════════════════════════════════
# REPORT FORMATTER
# ══════════════════════════════════════════════════════════════════

def print_report(target_date: pd.Timestamp, trades: list, mkt: dict):
    """Prints the full daily report to terminal."""
    d_str = target_date.strftime("%A, %d %B %Y")

    print()
    print("═" * 72)
    print(f"  THE STOCK LOGIC  —  DAILY SIGNAL REPORT")
    print(f"  {d_str}")
    print("═" * 72)

    # Market context
    print()
    print("  MARKET CONTEXT")
    print("  " + "─" * 50)
    regime    = mkt.get("market_regime", "unknown").upper()
    vix       = mkt.get("vix_close", 0)
    nifty     = mkt.get("nifty_close", 0)
    ad        = mkt.get("ad_ratio", 0)
    adv       = mkt.get("advance_count", 0)
    dec       = mkt.get("decline_count", 0)

    regime_emoji = {"BULL":"🟢","BEAR":"🔴","SIDEWAYS":"🟡"}.get(regime,"⚪")
    vix_flag = "✓ LOW FEAR" if vix < 15 else "⚠ ELEVATED" if vix < 22 else "🚫 HIGH FEAR"

    print(f"  Nifty      : ₹{nifty:,.0f}")
    print(f"  Regime     : {regime_emoji} {regime}")
    print(f"  India VIX  : {vix:.1f}  {vix_flag}")
    print(f"  A/D Ratio  : {ad:.2f}  ({int(adv)} up / {int(dec)} down)")

    # No trade zone warning
    if vix >= 25 or (ad < 0.3 and ad > 0):
        print()
        print("  🚫 NO TRADE ZONE — VIX/breadth conditions unfavorable")
        print("     Recommendation: STAY CASH today")
        print("═" * 72)
        return

    if not trades:
        print()
        print("  📭 NO A+ OR A GRADE SETUPS TODAY")
        print("     Recommendation: STAY CASH — wait for quality setups")
        print("     Next scan: tomorrow pre-market")
        print("═" * 72)
        return

    # Signals
    ap_trades = [t for t in trades if t["grade"] == "A+"]
    a_trades  = [t for t in trades if t["grade"] == "A"]

    print()
    print(f"  SIGNALS TODAY:  {len(ap_trades)} A+  |  {len(a_trades)} A  |  "
          f"{len(trades)} total")
    print(f"  Max risk if ALL taken: ₹{sum(t['actual_risk'] for t in trades):,.0f}")

    for i, t in enumerate(trades, 1):
        grade_str = "★ A+" if t["grade"] == "A+" else "  A "
        dir_arrow = "↑" if t["direction"] == "LONG" else "↓"
        dir_color_open  = ""
        dir_color_close = ""

        print()
        print(f"  {'─'*70}")
        print(f"  {grade_str}  {i}. {t['symbol']:<12}  "
              f"{dir_arrow} {t['direction']:<6}  "
              f"Score: {t['score']:.0f}/100  "
              f"│  {t['trade_type']}")
        print(f"  {'─'*70}")

        # Setup thesis
        print(f"  📐 Setup    : {t['setup_name']}")
        print(f"  📊 Structure: {t['structure'].replace('_',' ').title()}")
        print(f"  🗓  Hold     : {t['hold_note']}")
        print()

        # Levels — the most important section
        print(f"  LEVELS")
        print(f"  Entry zone  : ₹{t['entry_low']}  –  ₹{t['entry_high']}")
        print(f"  Reference   : ₹{t['entry_ref']}  (yesterday's close)")
        print(f"  Stop Loss   : ₹{t['sl']}  ({t['sl_pct']}% from entry)")
        print(f"  Target 1    : ₹{t['target_1']}  (RR {t['rr_1']:.1f}:1)")
        print(f"  Target 2    : ₹{t['target_2']}  (RR {t['rr_2']:.1f}:1)")
        print()

        # Position sizing
        print(f"  POSITION SIZING  (max risk ₹5,000)")
        print(f"  Quantity    : {t['qty']} shares")
        print(f"  Capital     : ₹{t['deployed']:,.0f}  deployed")
        print(f"  Risk        : ₹{t['actual_risk']:,.0f}  if SL hit")
        print()

        # Technical context
        print(f"  CONTEXT")
        print(f"  RSI         : {t['rsi']:.1f}  │  "
              f"RVOL: {t['rvol']:.2f}x  │  "
              f"VIX: {t['vix_close']:.1f}  │  "
              f"Delivery: {t['delivery_pct']:.1f}%")
        print()

        # Score breakdown
        print(f"  SCORE BREAKDOWN  ({t['score']:.0f}/100)")
        print(f"  Market regime : {t['score_regime']:>4.0f}/15  │  "
              f"SMC structure : {t['score_smc']:>4.0f}/30")
        print(f"  Technical     : {t['score_technical']:>4.0f}/25  │  "
              f"Volume/Inst   : {t['score_volume']:>4.0f}/20  │  "
              f"Risk/Reward   : {t['score_rr']:>4.0f}/10")
        print()

        # Invalidation
        print(f"  ⚠  TRADE INVALID IF:")
        for inv in t["invalidation"]:
            print(f"     • {inv}")

    # Summary table
    print()
    print("  " + "─" * 70)
    print(f"  QUICK REFERENCE")
    print(f"  {'#':<3} {'SYMBOL':<12} {'DIR':<6} {'GRADE':<5} "
          f"{'ENTRY':>8} {'SL':>8} {'T1':>8} {'T2':>8} {'QTY':>5} {'RISK':>7}")
    print(f"  {'─'*70}")
    for i, t in enumerate(trades, 1):
        print(f"  {i:<3} {t['symbol']:<12} "
              f"{'↑' if t['direction']=='LONG' else '↓'}{t['direction']:<5} "
              f"{t['grade']:<5} "
              f"₹{t['entry_ref']:>7.1f} "
              f"₹{t['sl']:>7.1f} "
              f"₹{t['target_1']:>7.1f} "
              f"₹{t['target_2']:>7.1f} "
              f"{t['qty']:>5} "
              f"₹{t['actual_risk']:>6.0f}")

    print()
    print("  " + "─" * 70)
    print(f"  ⚠  DISCLAIMER: Educational algo output only. Not SEBI-registered")
    print(f"     advice. All trading decisions are solely yours.")
    print("═" * 72)
    print()


def save_report(target_date: pd.Timestamp, trades: list, mkt: dict):
    """Saves report as a text file."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    filename = REPORTS_DIR / f"{target_date.strftime('%Y-%m-%d')}_signals.txt"

    import io, contextlib
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        print_report(target_date, trades, mkt)

    with open(filename, "w") as f:
        f.write(buffer.getvalue())

    return filename


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    # Get target date from argument or use latest
    if len(sys.argv) > 1:
        try:
            target_date = pd.Timestamp(sys.argv[1])
        except:
            log.error(f"Invalid date: {sys.argv[1]}. Use YYYY-MM-DD format.")
            return
    else:
        # Find latest date in signals
        if SIGNALS_FILE.exists():
            df = pd.read_parquet(SIGNALS_FILE)
            df["date"] = pd.to_datetime(df["date"])
            target_date = df["date"].max()
        else:
            target_date = pd.Timestamp(date.today())

    log.info(f"Generating signal report for: {target_date.date()}")

    # Load signals for date
    day_signals = load_signals_for_date(target_date)

    if day_signals.empty:
        print(f"\nNo signals found for {target_date.date()}")
        return

    # Actual report date (may differ if weekend/holiday)
    target_date = day_signals["date"].iloc[0]

    # Filter to A+ and A only (score >= 75)
    qualified = day_signals[
        day_signals["qualifies"] == True
    ].copy()

    # Build trade cards
    trades = []
    for _, row in qualified.iterrows():
        card = build_trade_card(row)
        if card is not None:
            trades.append(card)

    # Sort: A+ first, then by score descending
    trades.sort(key=lambda x: (-int(x["grade"]=="A+"), -x["score"]))

    # Limit to top 5 (quality over quantity)
    trades = trades[:5]

    # Market context
    mkt = get_market_context(target_date)

    # Print to terminal
    print_report(target_date, trades, mkt)

    # Save to file
    saved = save_report(target_date, trades, mkt)
    log.info(f"Report saved: {saved}")

    # Also save as CSV for easy reference
    if trades:
        csv_path = REPORTS_DIR / f"{target_date.strftime('%Y-%m-%d')}_signals.csv"
        pd.DataFrame(trades).to_csv(csv_path, index=False)
        log.info(f"CSV saved: {csv_path}")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
