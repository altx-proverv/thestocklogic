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


def send(message: str, parse_mode: str = "HTML") -> bool:
    """Send a message to the ATLAS Telegram channel."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — skipping message")
        return False
    try:
        r = requests.post(
            f"{BASE_URL}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": parse_mode,
            },
            timeout=10
        )
        if r.status_code == 200:
            return True
        log.warning(f"Telegram send failed: {r.status_code} {r.text[:100]}")
        return False
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


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


def send_daily_report(report: dict) -> bool:
    """Send end-of-day performance report."""
    date_str     = report.get("date", datetime.now(IST).strftime("%d %b %Y"))
    capital      = report.get("capital", 100000)
    daily_pnl    = report.get("daily_pnl", 0)
    daily_pnl_pct= daily_pnl / capital * 100
    weekly_pnl   = report.get("weekly_pnl", 0)
    trades       = report.get("trades", [])
    wins         = [t for t in trades if t.get("pnl", 0) > 0]
    losses       = [t for t in trades if t.get("pnl", 0) < 0]
    open_trades  = [t for t in trades if t.get("status") == "OPEN"]
    regime       = report.get("regime", "MIXED")
    mode         = report.get("agent_mode", "NORMAL")
    signals_gen  = report.get("signals_generated", 0)
    signals_filtered = report.get("signals_filtered", 0)
    signals_traded   = report.get("signals_traded", 0)

    pnl_icon = "🟢" if daily_pnl >= 0 else "🔴"

    trade_lines = ""
    for t in trades:
        if t.get("status") == "OPEN":
            trade_lines += f"\n⏳ {t['symbol']} {t['direction']} ₹{t.get('entry_price',0):,.1f} → OPEN"
        elif t.get("pnl", 0) > 0:
            trade_lines += f"\n✅ {t['symbol']} {t['direction']} +₹{t['pnl']:,.0f}"
        else:
            trade_lines += f"\n❌ {t['symbol']} {t['direction']} ₹{t['pnl']:,.0f}"

    win_rate = len(wins) / max(len(wins)+len(losses), 1) * 100

    msg = f"""
📊 <b>ATLAS DAILY REPORT — {date_str}</b>
━━━━━━━━━━━━━━━━━━━━━━━━

{pnl_icon} <b>CAPITAL STATUS</b>
Capital:      ₹{capital:,.0f}
Today P&L:    ₹{daily_pnl:+,.0f} ({daily_pnl_pct:+.2f}%)
Weekly P&L:   ₹{weekly_pnl:+,.0f}

📈 <b>TRADES TODAY</b>{trade_lines if trade_lines else chr(10)+"  No trades today"}

📡 <b>SIGNAL QUALITY</b>
Generated:  {signals_gen}
Filtered:   {signals_filtered} (below threshold)
Traded:     {signals_traded}
Win rate:   {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)

🌐 <b>TOMORROW'S CONTEXT</b>
Regime:     {regime}
Agent mode: {mode}
Kill-switch: ₹{capital * 0.02:,.0f} daily cap

Reply with your directive:
/approve — proceed as planned
/pause — no trading tomorrow
/cautious — reduce aggression
/aggressive — increase aggression
""".strip()
    return send(msg)


def send_startup(mode: str, capital: float) -> bool:
    """Send ATLAS startup notification."""
    msg = f"""
🤖 <b>ATLAS ONLINE</b>
━━━━━━━━━━━━━━━━━━━━
Mode:    {mode}
Capital: ₹{capital:,.0f}
Time:    {datetime.now(IST).strftime("%d %b %Y %H:%M IST")}

System is active. Kill-switch armed.
Daily loss cap: ₹{capital * 0.02:,.0f}
""".strip()
    return send(msg)


if __name__ == "__main__":
    print("Testing Telegram connection...")
    ok = send("🤖 <b>ATLAS Telegram connected successfully.</b>\nSystem is online.")
    print("✅ Message sent" if ok else "❌ Failed — check token and chat_id")
