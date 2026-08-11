"""
ATLAS Reporting — Telegram Bot
================================
Sends messages, alerts, and daily reports to Hemal via Telegram.
All ATLAS events flow through here.
"""

import os, sys, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send(message: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
    """Send a message to the ATLAS Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping message")
        return False
    try:
        payload = {
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       message,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json=payload,
            timeout=10
        )
        if r.status_code == 200:
            return True
        log.warning(f"Telegram send failed: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def send_with_buttons(message: str, symbol: str) -> bool:
    """Send signal alert with inline approve/skip/watch buttons."""
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ APPROVE", "callback_data": f"trade_{symbol}"},
            {"text": "❌ SKIP",    "callback_data": f"skip_{symbol}"},
            {"text": "👁 WATCH",   "callback_data": f"watch_{symbol}"},
        ]]
    }
    return send(message, reply_markup=reply_markup)


def send_signal_alert(signal: dict) -> bool:
    """Send a new high-conviction signal alert."""
    direction = signal.get("direction", "")
    symbol    = signal.get("symbol", "")
    conviction= signal.get("conviction", 0)
    entry     = signal.get("entry", 0)
    sl        = signal.get("sl", 0)
    t1        = signal.get("target_1", 0)
    t2        = signal.get("target_2", 0)
    setup     = signal.get("setup_name", "")
    session   = signal.get("session", "")
    rvol      = signal.get("rvol", 0)

    arrow = "🟢" if direction == "LONG" else "🔴"
    now   = datetime.now(IST).strftime("%H:%M IST")

    msg = f"""
{arrow} <b>ATLAS SIGNAL — {symbol}</b>
━━━━━━━━━━━━━━━━━━━━
<b>Direction:</b>  {direction}
<b>Setup:</b>     {setup}
<b>Session:</b>   {session.upper()}
<b>Time:</b>      {now}

<b>Entry:</b>     ₹{entry:,.1f}
<b>Target 1:</b>  ₹{t1:,.1f}
<b>Target 2:</b>  ₹{t2:,.1f}
<b>Stop Loss:</b> ₹{sl:,.1f}

<b>Conviction:</b> {conviction}/100
<b>RVOL:</b>       {rvol:.1f}x

⚠️ Educational only · Not SEBI advice
""".strip()
    return send(msg)


def send_trade_entry(trade: dict) -> bool:
    """Confirm trade entry execution."""
    symbol    = trade.get("symbol", "")
    direction = trade.get("direction", "")
    entry     = trade.get("entry_price", 0)
    sl        = trade.get("sl", 0)
    t1        = trade.get("target_1", 0)
    qty       = trade.get("qty", 0)
    risk      = abs(entry - sl) * qty if entry and sl and qty else 0
    now       = datetime.now(IST).strftime("%H:%M IST")

    arrow = "🟢" if direction == "LONG" else "🔴"

    msg = f"""
{arrow} <b>TRADE ENTERED — {symbol}</b>
━━━━━━━━━━━━━━━━━━━━
<b>Direction:</b> {direction}
<b>Entry:</b>     ₹{entry:,.1f}
<b>Qty:</b>       {qty} shares
<b>SL:</b>        ₹{sl:,.1f}
<b>T1:</b>        ₹{t1:,.1f}
<b>Max Risk:</b>  ₹{risk:,.0f}
<b>Time:</b>      {now}
""".strip()
    return send(msg)


def send_trade_exit(trade: dict) -> bool:
    """Confirm trade exit with P&L."""
    symbol     = trade.get("symbol", "")
    direction  = trade.get("direction", "")
    entry      = trade.get("entry_price", 0)
    exit_price = trade.get("exit_price", 0)
    pnl        = trade.get("pnl", 0)
    exit_reason= trade.get("exit_reason", "")
    now        = datetime.now(IST).strftime("%H:%M IST")

    icon = "✅" if pnl > 0 else "❌"

    msg = f"""
{icon} <b>TRADE CLOSED — {symbol}</b>
━━━━━━━━━━━━━━━━━━━━
<b>Direction:</b>  {direction}
<b>Entry:</b>      ₹{entry:,.1f}
<b>Exit:</b>       ₹{exit_price:,.1f}
<b>P&L:</b>        ₹{pnl:+,.0f}
<b>Reason:</b>     {exit_reason}
<b>Time:</b>       {now}
""".strip()
    return send(msg)


def send_kill_switch_alert(reason: str, daily_pnl: float) -> bool:
    """Send kill switch triggered alert."""
    msg = f"""
🚨 <b>KILL SWITCH TRIGGERED</b>
━━━━━━━━━━━━━━━━━━━━
<b>Reason:</b>    {reason}
<b>Daily P&L:</b> ₹{daily_pnl:+,.0f}
<b>Time:</b>      {datetime.now(IST).strftime("%H:%M IST")}

No further trades today.
Review and set tomorrow's directive.
""".strip()
    return send(msg)

# REMOVED: send_daily_report() and send_startup().
#
# Both were unreferenced, and both hardcoded "Kill-switch: capital * 0.02" --
# a 2%-of-capital daily cap that no longer exists in any form. ATLAS keeps no
# capital figure and no automated loss cap; drawdown and exits are the
# operator's. daily_report.generate_and_send() is the live report path and
# reads funds from the broker.
