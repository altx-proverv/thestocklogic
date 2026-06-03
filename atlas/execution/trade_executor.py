"""
ATLAS Execution — Semi-Auto Trade Executor
===========================================
Flow:
1. Signal qualifies (conviction >= threshold)
2. Kill switch check
3. Capital fence check
4. Position sizing calculated
5. Telegram alert sent to Hemal
6. Wait for /trade SYMBOL approval (10 min window)
7. On approval — place entry order via Zerodha
8. Immediately place SL order
9. Place GTT for T1 + overnight management
10. Log trade to atlas_trades table
11. Update capital deployed
"""

import sys, requests, logging, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    ZERODHA_API_KEY, DEFAULT_AGENT_MODE
)
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.risk.capital_manager import can_deploy, deploy_capital, get_state
from atlas.risk.position_sizing import calculate, validate
from atlas.execution.broker import get_kite, place_order, place_sl_order
from atlas.reporting.telegram import send, send_trade_entry

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-EXEC] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Pending trades waiting for approval
# { symbol: { signal, sizing, expires_at } }
PENDING_TRADES = {}


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def get_agent_mode() -> str:
    state = get_state()
    return state.get("mode", DEFAULT_AGENT_MODE)


def format_signal_alert(signal: dict, sizing: dict) -> str:
    """Format Telegram alert for a new signal."""
    symbol    = signal.get("symbol", "")
    direction = signal.get("direction", "LONG")
    conviction= signal.get("conviction", 0)
    entry     = sizing.get("entry_price", 0)
    sl        = sizing.get("sl_price", 0)
    t1        = sizing.get("target_1", 0)
    t2        = sizing.get("target_2", 0)
    qty       = sizing.get("qty", 0)
    risk      = sizing.get("risk_inr", 0)
    reward    = sizing.get("reward_inr", 0)
    rr        = sizing.get("rr_ratio", 0)
    product   = sizing.get("product", "CNC")
    capital   = sizing.get("capital_deployed", 0)
    setup     = signal.get("setup_name", "")
    now       = datetime.now(IST).strftime("%H:%M IST")
    arrow     = "🟢" if direction == "LONG" else "🔴"

    msg = (
        f"{arrow} <b>ATLAS SIGNAL — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Direction:</b>  {direction} ({product})\n"
        f"<b>Setup:</b>      {setup}\n"
        f"<b>Conviction:</b> {conviction}/100\n"
        f"<b>Time:</b>       {now}\n\n"
        f"<b>Entry:</b>      ₹{entry:,.1f}\n"
        f"<b>Target 1:</b>   ₹{t1:,.1f}\n"
        f"<b>Target 2:</b>   ₹{t2:,.1f}\n"
        f"<b>Stop Loss:</b>  ₹{sl:,.1f}\n\n"
        f"<b>Qty:</b>        {qty} shares\n"
        f"<b>Capital:</b>    ₹{capital:,.0f}\n"
        f"<b>Risk:</b>       ₹{risk:,.0f}\n"
        f"<b>Reward:</b>     ₹{reward:,.0f}\n"
        f"<b>RR:</b>         {rr:.1f}:1\n\n"
        f"Reply within 10 min:\n"
        f"✅ /trade {symbol} — APPROVE\n"
        f"❌ /skip {symbol} — REJECT"
    )
    return msg


def queue_signal(signal: dict) -> dict:
    """
    Process a signal through all checks and queue for approval.
    Returns result dict with status.
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

    # Step 1 — Position sizing
    sizing = calculate(
        direction=direction,
        entry_price=entry,
        sl_price=sl,
        target_1=t1,
        target_2=t2,
        conviction=conviction,
        agent_mode=mode,
    )

    is_valid, size_reason = validate(sizing)
    if not is_valid:
        log.warning(f"Signal rejected — sizing invalid: {size_reason}")
        return {"status": "REJECTED", "reason": size_reason}

    # Step 2 — Kill switch check
    signal["conviction"]        = conviction
    signal["capital_required"]  = sizing.get("capital_deployed", 0)
    ks = kill_switch_check(signal)
    if not ks:
        log.warning(f"Signal blocked by kill switch: {ks.reason}")
        return {"status": "BLOCKED", "reason": ks.reason}

    # Step 3 — Queue for approval
    expires_at = datetime.now(IST) + timedelta(minutes=10)
    PENDING_TRADES[symbol] = {
        "signal":     signal,
        "sizing":     sizing,
        "expires_at": expires_at,
    }

    # Step 4 — Send Telegram alert
    alert = format_signal_alert(signal, sizing)
    send(alert)

    log.info(f"Signal queued for approval: {symbol} — expires {expires_at.strftime('%H:%M IST')}")
    return {"status": "PENDING", "symbol": symbol, "expires_at": str(expires_at)}


def approve_trade(symbol: str) -> dict:
    """
    Execute an approved trade.
    Called when user sends /trade SYMBOL.
    """
    if symbol not in PENDING_TRADES:
        return {"status": "ERROR", "reason": f"No pending trade for {symbol}"}

    pending    = PENDING_TRADES[symbol]
    signal     = pending["signal"]
    sizing     = pending["sizing"]
    expires_at = pending["expires_at"]

    # Check if expired
    if datetime.now(IST) > expires_at:
        del PENDING_TRADES[symbol]
        return {"status": "EXPIRED", "reason": "Approval window expired (10 min)"}

    direction = signal.get("direction", "LONG")
    entry     = sizing.get("entry_price", 0)
    sl        = sizing.get("sl_price", 0)
    t1        = sizing.get("target_1", 0)
    qty       = sizing.get("qty", 0)
    product   = sizing.get("product", "CNC")
    capital   = sizing.get("capital_deployed", 0)

    log.info(f"Executing approved trade: {symbol} {direction} {qty} shares")

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
        del PENDING_TRADES[symbol]
        return {"status": "FAILED", "reason": reason}

    order_id = order_result.get("order_id")

    # Place SL order immediately
    sl_result = place_sl_order(
        symbol=symbol,
        direction=direction,
        qty=qty,
        sl_price=sl,
        tag="ATLAS_SL"
    )

    # Log trade to Supabase
    trade_record = {
        "symbol":           symbol,
        "direction":        direction,
        "entry_price":      entry,
        "sl":               sl,
        "target_1":         t1,
        "target_2":         sizing.get("target_2", 0),
        "qty":              qty,
        "conviction":       float(signal.get("conviction", 0)),
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
        trade_data = r.json()
        trade_id = trade_data[0].get("id") if trade_data else None

    # Deploy capital
    if trade_id:
        deploy_capital(trade_id, capital, symbol)

    # Place GTT for CNC longs
    if product == "CNC" and direction == "LONG":
        place_gtt_orders(symbol, direction, qty, sl, t1, sizing.get("target_2", 0))

    # Confirm on Telegram
    send_trade_entry({
        "symbol":       symbol,
        "direction":    direction,
        "entry_price":  entry,
        "sl":           sl,
        "target_1":     t1,
        "qty":          qty,
    })

    del PENDING_TRADES[symbol]
    log.info(f"Trade executed: {symbol} {direction} {qty} @ ₹{entry}")
    return {"status": "EXECUTED", "order_id": order_id, "trade_id": trade_id}


def skip_trade(symbol: str) -> dict:
    """Reject a pending trade signal."""
    if symbol in PENDING_TRADES:
        del PENDING_TRADES[symbol]
        send(f"⏭ <b>SKIPPED — {symbol}</b>\nSignal rejected.")
        log.info(f"Trade skipped: {symbol}")
        return {"status": "SKIPPED"}
    return {"status": "ERROR", "reason": f"No pending trade for {symbol}"}


def place_gtt_orders(symbol, direction, qty, sl, t1, t2=0):
    """Place GTT orders for overnight CNC position management."""
    kite = get_kite()
    if not kite:
        log.error("GTT placement failed — Kite not initialized")
        return

    try:
        from kiteconnect import KiteConnect

        # GTT for SL — triggers if price drops to SL
        gtt_sl = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            trigger_values=[sl],
            last_price=0,
            orders=[{
                "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                "quantity":         qty,
                "order_type":       KiteConnect.ORDER_TYPE_MARKET,
                "product":          KiteConnect.PRODUCT_CNC,
            }]
        )
        log.info(f"GTT SL placed: {symbol} @ ₹{sl} | GTT ID: {gtt_sl}")

        # GTT for T1 — triggers if price hits T1
        if t1:
            gtt_t1 = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=KiteConnect.EXCHANGE_NSE,
                trigger_values=[t1],
                last_price=0,
                orders=[{
                    "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                    "quantity":         qty // 2,  # Exit 50% at T1
                    "order_type":       KiteConnect.ORDER_TYPE_MARKET,
                    "product":          KiteConnect.PRODUCT_CNC,
                }]
            )
            log.info(f"GTT T1 placed: {symbol} @ ₹{t1} | GTT ID: {gtt_t1}")

        send(
            f"📌 <b>GTT ORDERS PLACED — {symbol}</b>\n"
            f"SL trigger:  ₹{sl:,.1f} → sell all {qty} shares\n"
            f"T1 trigger:  ₹{t1:,.1f} → sell {qty//2} shares (50%)\n"
            f"Position protected overnight."
        )

    except Exception as e:
        log.error(f"GTT placement failed for {symbol}: {e}")
        send(f"⚠️ <b>GTT FAILED — {symbol}</b>\nManually place SL order.\nSL: ₹{sl:,.1f}")


def cleanup_expired():
    """Remove expired pending trades."""
    now     = datetime.now(IST)
    expired = [s for s, p in PENDING_TRADES.items() if now > p["expires_at"]]
    for symbol in expired:
        log.info(f"Pending trade expired: {symbol}")
        send(f"⏰ <b>SIGNAL EXPIRED — {symbol}</b>\nApproval window closed.")
        del PENDING_TRADES[symbol]
    return expired


if __name__ == "__main__":
    # Test with a sample signal
    test_signal = {
        "symbol":     "TECHM",
        "direction":  "LONG",
        "conviction": 81,
        "score":      81,
        "entry_ref":  1543.0,
        "entry":      1543.0,
        "sl":         1512.0,
        "target_1":   1605.0,
        "target_2":   1635.0,
        "setup_name": "CHOCH Reversal",
    }
    print("=== SEMI-AUTO EXECUTION TEST ===")
    print("Queuing test signal — check Telegram...")
    result = queue_signal(test_signal)
    print(f"Result: {result}")
