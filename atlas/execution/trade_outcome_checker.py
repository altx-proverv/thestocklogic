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
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-OUTCOME] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _num(v, default: float = 0.0) -> float:
    """Coerce a PostgREST value to float. NULL columns arrive as None, and
    dict.get(key, 0) does NOT protect against that -- the default only applies
    when the key is absent, and these keys are present-but-null. float(None)
    raises, which is how a null stop took down the whole run."""
    try:
        return default if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return default


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
    """
    Everything currently held, as {symbol: qty}, read from BOTH broker books.

    They are different books and a position is only in one of them at a time:
    positions() is the intraday book, where MIS lives and where a CNC buy also
    appears on the day it fills; holdings() is the delivery book, which a CNC
    buy moves into the following day.

    holdings() must sum quantity AND t1_quantity. Under T+1 settlement shares
    bought the previous session have not reached the demat account yet, so the
    holding reports quantity=0 with the entire position in t1_quantity:

        BPCL  quantity=0  t1_quantity=215  opening_quantity=215  product=CNC

    Counting quantity alone read that as flat and closed a live 215-share
    position as CLOSED_UNKNOWN -- BPCL on 28 Aug, COROMANDEL on 24 Aug, each
    exactly one session after entry. The bug only surfaced when ATLAS's lot was
    the whole holding; a pre-existing settled lot in the same symbol kept
    quantity above zero and masked it, which is why three other open positions
    were never touched.

    Summing both fields is correct independent of settlement timing -- together
    they are simply what is held. Quantities accumulate across books rather than
    overwrite, so a symbol appearing in both is the net of the two. Gate 3b
    permits only one position per symbol, so the pathological case of equal and
    opposite legs netting to a false zero cannot arise from ATLAS itself.
    """
    kite = get_kite()
    if not kite:
        return {}
    try:
        pos_map = {}

        for p in kite.positions().get("net", []):
            sym = p.get("tradingsymbol", "")
            qty = int(p.get("quantity", 0) or 0)
            if sym and qty != 0:
                pos_map[sym] = pos_map.get(sym, 0) + qty

        for h in kite.holdings():
            sym = h.get("tradingsymbol", "")
            # Both fields, always. t1_quantity is the unsettled leg.
            qty = int(h.get("quantity", 0) or 0) + int(h.get("t1_quantity", 0) or 0)
            if sym and qty > 0:
                pos_map[sym] = pos_map.get(sym, 0) + qty

        return pos_map
    except Exception as e:
        log.error(f"Failed to fetch Zerodha positions: {e}")
        return {}


def close_trade(trade: dict, exit_price: float, exit_reason: str) -> bool:
    """Mark a trade as CLOSED in Supabase and release capital."""
    trade_id  = trade["id"]
    entry     = _num(trade.get("entry_price"))
    qty       = int(_num(trade.get("qty")))
    direction = trade.get("direction", "LONG")
    symbol    = trade.get("symbol", "")
    now       = datetime.now(IST)
    # capital_deployed was read here into an unused local. It is not a column
    # on atlas_trades at all, so it was always the 0 default -- see the note
    # below on why no capital is released.

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

    # No capital to release. ATLAS keeps no capital ledger -- available funds
    # are read live from the broker at decision time, so a closed trade frees
    # its cash at the broker without anything here needing to record it.

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

        # One malformed row must not end the run. Everything below this point
        # is per-trade, and the loop previously had no guard at all -- the
        # first bad row killed the process before any other position was even
        # looked at.
        try:
            entry = _num(trade.get("entry_price"))

            # THE STOP LIVES IN stop_price, NOT sl.
            #
            # atlas_entry._log_intent writes stop_price and has never written
            # sl. The sl column exists, so PostgREST returns "sl": null, and
            # .get("sl", 0) yields None rather than the default -- the default
            # only applies when the KEY is absent. float(None) then raised
            # TypeError on the first open trade, before the zerodha_qty check,
            # killing run() outright. It did not degrade to CLOSED_UNKNOWN; it
            # meant this module could not close anything at all while any
            # position was open, which is why atlas_trades holds zero CLOSED
            # rows. Read stop_price first and fall back to sl for hand-inserted
            # rows; _num() absorbs null either way.
            stop = _num(trade.get("stop_price")) or _num(trade.get("sl"))
            t1   = _num(trade.get("target_1"))
            t2   = _num(trade.get("target_2"))

            # Check if position still exists in Zerodha
            zerodha_qty = zerodha_positions.get(symbol, 0)
            if zerodha_qty != 0:
                continue

            # Position closed in Zerodha — determine exit reason using LTP
            ltp = get_ltp(symbol)
            if ltp <= 0:
                log.warning(f"Could not get LTP for {symbol} — skipping")
                continue

            # Determine exit reason. NOTE: ATLAS enters without targets by
            # design -- see atlas_entry's docstring, "No SL, no target, no exit
            # orders. Exits manual." So t1/t2 are absent on agent-created rows
            # and the T1_HIT/T2_HIT branches only fire for rows that carry
            # targets from somewhere else. The stop branch is the one that
            # matters here, and it is the one that was unreachable.
            if direction == "LONG":
                if t2 and ltp >= t2 * 0.995:
                    exit_reason = "T2_HIT"
                elif t1 and ltp >= t1 * 0.995:
                    exit_reason = "T1_HIT"
                elif stop and ltp <= stop * 1.005:
                    exit_reason = "SL_HIT"
                else:
                    exit_reason = "CLOSED_UNKNOWN"
            else:  # SHORT
                if t2 and ltp <= t2 * 1.005:
                    exit_reason = "T2_HIT"
                elif t1 and ltp <= t1 * 1.005:
                    exit_reason = "T1_HIT"
                elif stop and ltp >= stop * 0.995:
                    exit_reason = "SL_HIT"
                else:
                    exit_reason = "CLOSED_UNKNOWN"

            # CLOSED_UNKNOWN is a real answer when no stop is on the row, but
            # it is a different statement from "exited away from a known stop".
            # Say which, so the reason can be judged later.
            if exit_reason == "CLOSED_UNKNOWN":
                log.info(
                    f"{symbol}: exit reason undetermined — "
                    + (f"LTP Rs{ltp:.2f} vs stop Rs{stop:.2f}, no targets on row"
                       if stop else "no stop or targets recorded on this row")
                )

            if close_trade(trade, ltp, exit_reason):
                closed_count += 1

        except Exception as e:
            log.error(f"Outcome check failed for {symbol}: {type(e).__name__}: {e}")
            continue

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
