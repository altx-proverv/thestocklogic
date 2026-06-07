"""
ATLAS Execution — Trade Outcome Checker
=========================================
Runs after market close (3:30 PM) and morning GTT check.
Checks all OPEN atlas_trades against Zerodha positions/orders.
Marks closed trades as CLOSED with exit_price, pnl, exit_reason.
Releases capital back to available pool.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY
from atlas.execution.broker import get_kite, get_ltp
from atlas.risk.capital_manager import release_capital
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-OUTCOME] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def get_open_trades() -> list:
    """Fetch all OPEN atlas_trades."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?status=eq.OPEN&order=created_at.asc",
        headers=_headers()
    )
    if r.status_code == 200:
        return r.json()
    log.error(f"Failed to fetch open trades: {r.status_code}")
    return []


def get_zerodha_positions() -> dict:
    """Get current Zerodha positions as {symbol: qty}."""
    kite = get_kite()
    if not kite:
        return {}
    try:
        positions = kite.positions().get("net", [])
        holdings  = kite.holdings()
        pos_map   = {}
        for p in positions:
            sym = p.get("tradingsymbol", "")
            qty = int(p.get("quantity", 0))
            if qty != 0:
                pos_map[sym] = qty
        for h in holdings:
            sym = h.get("tradingsymbol", "")
            qty = int(h.get("quantity", 0))
            if qty > 0:
                pos_map[sym] = pos_map.get(sym, 0) + qty
        return pos_map
    except Exception as e:
        log.error(f"Failed to fetch Zerodha positions: {e}")
        return {}


def close_trade(trade: dict, exit_price: float, exit_reason: str) -> bool:
    """Mark a trade as CLOSED in Supabase and release capital."""
    trade_id  = trade["id"]
    entry     = float(trade.get("entry_price", 0))
    qty       = int(trade.get("qty", 0))
    direction = trade.get("direction", "LONG")
    capital   = float(trade.get("capital_deployed", 0))
    symbol    = trade.get("symbol", "")
    now       = datetime.now(IST)

    # Calculate P&L
    if direction == "LONG":
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty

    pnl = round(pnl, 2)

    # Update atlas_trades
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}",
        headers=_headers(),
        json={
            "status":      "CLOSED",
            "exit_price":  exit_price,
            "exit_date":   now.date().isoformat(),
            "pnl":         pnl,
            "exit_reason": exit_reason,
            "updated_at":  now.isoformat(),
        }
    )

    if r.status_code not in (200, 204):
        log.error(f"Failed to close trade {trade_id}: {r.status_code}")
        return False

    # Release capital
    release_capital(trade_id, capital, pnl, symbol)

    log.info(f"Trade closed: {symbol} {direction} | Exit: ₹{exit_price:,.1f} | P&L: ₹{pnl:+,.0f} | Reason: {exit_reason}")
    return True


def update_atlas_state_pnl():
    """Recalculate and update daily/weekly P&L in atlas_state."""
    from datetime import date, timedelta
    today      = date.today().isoformat()
    week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()

    # Daily P&L
    r1 = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?exit_date=eq.{today}&status=eq.CLOSED&select=pnl",
        headers=_headers()
    )
    daily_pnl = sum(float(t.get("pnl", 0)) for t in r1.json()) if r1.status_code == 200 else 0

    # Weekly P&L
    r2 = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?exit_date=gte.{week_start}&status=eq.CLOSED&select=pnl",
        headers=_headers()
    )
    weekly_pnl = sum(float(t.get("pnl", 0)) for t in r2.json()) if r2.status_code == 200 else 0

    # Update atlas_state
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/atlas_state?id=eq.1",
        headers=_headers(),
        json={
            "daily_pnl":  round(daily_pnl, 2),
            "weekly_pnl": round(weekly_pnl, 2),
            "updated_at": datetime.now(IST).isoformat(),
        }
    )
    log.info(f"Atlas state updated — Daily P&L: ₹{daily_pnl:+,.0f} | Weekly P&L: ₹{weekly_pnl:+,.0f}")
    return daily_pnl, weekly_pnl


def run():
    """Check all open trades against Zerodha and close resolved ones."""
    now = datetime.now(IST)
    log.info(f"Trade outcome check — {now.strftime('%d %b %Y %H:%M IST')}")

    open_trades = get_open_trades()
    if not open_trades:
        log.info("No open trades to check")
        return

    log.info(f"Checking {len(open_trades)} open trades...")

    # Get current Zerodha positions
    zerodha_positions = get_zerodha_positions()
    log.info(f"Zerodha positions: {zerodha_positions}")

    closed_count = 0
    for trade in open_trades:
        symbol    = trade.get("symbol", "")
        direction = trade.get("direction", "LONG")
        entry     = float(trade.get("entry_price", 0))
        sl        = float(trade.get("sl", 0))
        t1        = float(trade.get("target_1", 0))
        t2        = float(trade.get("target_2", 0))

        # Check if position still exists in Zerodha
        zerodha_qty = zerodha_positions.get(symbol, 0)

        if zerodha_qty == 0:
            # Position closed in Zerodha — determine exit reason using LTP
            ltp = get_ltp(symbol)
            if ltp <= 0:
                log.warning(f"Could not get LTP for {symbol} — skipping")
                continue

            # Determine exit reason
            if direction == "LONG":
                if t2 and ltp >= t2 * 0.995:
                    exit_reason = "T2_HIT"
                elif t1 and ltp >= t1 * 0.995:
                    exit_reason = "T1_HIT"
                elif sl and ltp <= sl * 1.005:
                    exit_reason = "SL_HIT"
                else:
                    exit_reason = "CLOSED_UNKNOWN"
            else:  # SHORT
                if t2 and ltp <= t2 * 1.005:
                    exit_reason = "T2_HIT"
                elif t1 and ltp <= t1 * 1.005:
                    exit_reason = "T1_HIT"
                elif sl and ltp >= sl * 0.995:
                    exit_reason = "SL_HIT"
                else:
                    exit_reason = "CLOSED_UNKNOWN"

            if close_trade(trade, ltp, exit_reason):
                closed_count += 1

    # Update P&L in atlas_state
    if closed_count > 0:
        daily_pnl, weekly_pnl = update_atlas_state_pnl()
        pnl_sign = "+" if daily_pnl >= 0 else ""
        send(
            f"📊 <b>ATLAS TRADE OUTCOME UPDATE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Closed trades: {closed_count}\n"
            f"Today P&L:  ₹{pnl_sign}{daily_pnl:,.0f}\n"
            f"Weekly P&L: ₹{'+' if weekly_pnl >= 0 else ''}{weekly_pnl:,.0f}\n"
            f"Time: {now.strftime('%H:%M IST')}"
        )
    else:
        log.info("No trades to close — all positions still open")
        update_atlas_state_pnl()

    log.info(f"Outcome check complete — {closed_count} trades closed")


if __name__ == "__main__":
    run()
