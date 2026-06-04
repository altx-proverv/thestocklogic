"""
ATLAS Execution — Autonomous Trade Executor
=============================================
ATLAS decides and executes autonomously.
You receive real-time notifications of what was traded.
Max 3 trades per day. Kill switch non-bypassable.
You can pause ATLAS anytime via /pause on Telegram.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    DEFAULT_AGENT_MODE
)
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.risk.capital_manager import can_deploy, deploy_capital, get_state
from atlas.risk.position_sizing import calculate, validate
from atlas.execution.broker import get_kite, place_order, place_sl_order, get_ltp
from atlas.reporting.telegram import send, send_trade_entry

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-EXEC] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# Keep PENDING_TRADES for /watch command compatibility
PENDING_TRADES = {}


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def get_agent_mode() -> str:
    return get_state().get("mode", DEFAULT_AGENT_MODE)


def get_today_trade_count() -> int:
    """Count trades already taken today."""
    today = datetime.now(IST).date().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?entry_date=eq.{today}&select=id",
        headers=_headers()
    )
    if r.status_code == 200:
        return len(r.json())
    return 0


def queue_signal(signal: dict) -> dict:
    """
    Process a signal through all checks and execute autonomously.
    Sends real-time notification after execution.
    """
    symbol    = signal.get("symbol", "")
    direction = signal.get("direction", "LONG")
    conviction= float(signal.get("conviction", signal.get("score", 0)))
    entry     = float(signal.get("entry_ref", signal.get("entry", 0)))
    sl        = float(signal.get("sl", 0))
    t1        = float(signal.get("target_1", 0))
    t2        = float(signal.get("target_2", 0))
    mode      = get_agent_mode()

    log.info(f"Processing signal: {symbol} {direction} Conv:{conviction}")

    # Check daily trade limit
    today_count = get_today_trade_count()
    if today_count >= 3:
        log.info(f"Daily trade limit reached ({today_count}/3) — skipping {symbol}")
        return {"status": "SKIPPED", "reason": "Daily trade limit reached (3/3)"}

    # Step 1 — Position sizing
    sizing = calculate(
        entry_price=entry,
        sl_price=sl,
        target_price=t1,
        conviction=conviction,
        agent_mode=mode,
        direction=direction,
    )

    is_valid, size_reason = validate(sizing)
    if not is_valid:
        log.warning(f"Signal rejected — sizing invalid: {size_reason}")
        return {"status": "REJECTED", "reason": size_reason}

    # Step 2 — Kill switch check
    signal["conviction"]       = conviction
    signal["capital_required"] = sizing.get("capital_deployed", 0)
    ks = kill_switch_check(signal)
    if not ks:
        log.warning(f"Signal blocked by kill switch: {ks.reason}")
        return {"status": "BLOCKED", "reason": ks.reason}

    # Step 3 — Price validation
    current_ltp = get_ltp(symbol)
    if current_ltp > 0:
        drift_pct = abs(current_ltp - entry) / entry * 100
        if direction == "LONG" and current_ltp > entry * 1.005:
            log.warning(f"Entry missed — {symbol} LTP ₹{current_ltp:.1f} drifted {drift_pct:.1f}% above entry")
            return {"status": "MISSED", "reason": f"Price drifted {drift_pct:.1f}% above entry zone"}
        if direction == "SHORT" and current_ltp < entry * 0.995:
            log.warning(f"Entry missed — {symbol} LTP ₹{current_ltp:.1f} drifted {drift_pct:.1f}% below entry")
            return {"status": "MISSED", "reason": f"Price drifted {drift_pct:.1f}% below entry zone"}
        entry = current_ltp  # Use live price as entry

    # Step 4 — Execute
    return _execute(signal, sizing, entry)


def _execute(signal: dict, sizing: dict, live_entry: float) -> dict:
    """Place order and manage the trade."""
    symbol    = signal.get("symbol", "")
    direction = signal.get("direction", "LONG")
    sl        = sizing.get("sl_price", 0)
    t1        = sizing.get("target_1", 0)
    t2        = sizing.get("target_2", 0)
    qty       = sizing.get("qty", 0)
    product   = "MIS" if direction.upper() == "SHORT" else "CNC"
    capital   = sizing.get("capital_deployed", 0)
    conviction= float(signal.get("conviction", 0))

    log.info(f"Executing: {symbol} {direction} {qty} shares @ ₹{live_entry:.1f}")

    # Place entry order
    order_result = place_order(
        symbol=symbol,
        direction=direction,
        qty=qty,
        order_type="MARKET",
        tag="ATLAS"
    )

    if not order_result.get("success"):
        reason = order_result.get("reason", "Order failed")
        send(f"❌ <b>ORDER FAILED — {symbol}</b>\n{reason}")
        return {"status": "FAILED", "reason": reason}

    order_id = order_result.get("order_id")

    # Log trade to Supabase
    trade_record = {
        "symbol":           symbol,
        "direction":        direction,
        "entry_price":      live_entry,
        "sl":               sl,
        "target_1":         t1,
        "target_2":         t2,
        "qty":              qty,
        "conviction":       conviction,
        "setup_name":       signal.get("setup_name", ""),
        "status":           "OPEN",
        "entry_date":       datetime.now(IST).date().isoformat(),
        "agent_mode":       get_agent_mode(),
        "capital_deployed": capital,
        "notes":            f"Order ID: {order_id}",
    }

    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/atlas_trades",
        headers=_headers(),
        json=trade_record
    )

    trade_id = None
    if r.status_code in (200, 201):
        data = r.json()
        trade_id = data[0].get("id") if data else None

    # Deploy capital
    if trade_id:
        deploy_capital(trade_id, capital, symbol)

    # Place trade management orders
    from atlas.execution.trade_management import place_cnc_gtt_orders, place_mis_bracket_order
    if product == "CNC" and direction == "LONG":
        place_cnc_gtt_orders(symbol, qty, live_entry, sl, t1, t2, trade_id)
    elif product == "MIS" and direction == "SHORT":
        place_mis_bracket_order(symbol, qty, live_entry, sl, t1, t2, trade_id)

    # Real-time notification
    today_count = get_today_trade_count()
    arrow = "🟢" if direction == "LONG" else "🔴"
    send(
        f"{arrow} <b>ATLAS TRADE TAKEN — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Direction:</b> {direction} ({product})\n"
        f"<b>Entry:</b>     ₹{live_entry:,.1f}\n"
        f"<b>Stop Loss:</b> ₹{sl:,.1f}\n"
        f"<b>Target 1:</b>  ₹{t1:,.1f}\n"
        f"<b>Target 2:</b>  ₹{t2:,.1f}\n"
        f"<b>Qty:</b>       {qty} shares\n"
        f"<b>Risk:</b>      ₹{abs(live_entry-sl)*qty:,.0f}\n"
        f"<b>Setup:</b>     {signal.get('setup_name','')}\n"
        f"<b>Conv:</b>      {conviction:.0f}/100\n\n"
        f"Trade {today_count}/3 today · Send /pause to stop"
    )

    log.info(f"ATLAS executed: {symbol} {direction} {qty} @ ₹{live_entry:.1f} | Trade {today_count}/3")
    return {"status": "EXECUTED", "order_id": order_id, "trade_id": trade_id}


def skip_trade(symbol: str) -> dict:
    if symbol in PENDING_TRADES:
        del PENDING_TRADES[symbol]
    return {"status": "SKIPPED"}


def approve_trade(symbol: str) -> dict:
    """Kept for backward compatibility — not used in autonomous mode."""
    return {"status": "INFO", "reason": "ATLAS is in autonomous mode — trades execute automatically"}


def cleanup_expired():
    PENDING_TRADES.clear()
    return []
