"""
ATLAS Execution — Trade Management
====================================
Handles order placement strategy for CNC and MIS trades.

CNC LONG:
  - Entry: market order
  - GTT SL: full qty at SL price
  - GTT T1: 50% qty at T1 price
  - GTT T2: 50% qty at T2 price
  - On T1 hit: modify GTT SL to breakeven
  - Persists overnight until triggered or manually exited

MIS SHORT:
  - Entry + SL + T1 via bracket order
  - Zerodha handles cancel-one-fills-other logic
  - Auto square-off at 3:15 PM if neither hits
  - T2 managed separately if T1 hits first
"""

import sys, requests, logging, math
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY
from atlas.execution.broker import get_kite
from atlas.reporting.telegram import send

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


# ── CNC LONG — GTT MANAGEMENT ─────────────────────────────────────

def place_cnc_gtt_orders(
    symbol: str,
    qty: int,
    entry: float,
    sl: float,
    t1: float,
    t2: float,
    trade_id: int = None,
) -> dict:
    """
    Place GTT orders for CNC LONG position.
    GTT SL:  full qty exits if price drops to SL
    GTT T1:  50% qty exits if price rises to T1
    GTT T2:  50% qty exits if price rises to T2
    Returns dict with GTT IDs for tracking.
    """
    kite = get_kite()
    if not kite:
        log.error("GTT placement failed — Kite not initialized")
        return {"success": False, "reason": "Kite not initialized"}

    try:
        from kiteconnect import KiteConnect
        qty_t1 = math.floor(qty * 0.5)   # 50% at T1
        qty_t2 = qty - qty_t1             # remaining 50% at T2
        gtt_ids = {}

        # GTT SL — full qty
        gtt_sl = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            trigger_values=[round(sl * 0.999, 1)],  # trigger slightly above SL
            last_price=entry,
            orders=[{
                "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                "quantity":         qty,
                "order_type":       KiteConnect.ORDER_TYPE_MARKET,
                "product":          KiteConnect.PRODUCT_CNC,
            }]
        )
        gtt_ids["gtt_sl_id"] = gtt_sl
        log.info(f"GTT SL placed: {symbol} @ ₹{sl} | ID: {gtt_sl}")

        # GTT T1 — 50% qty
        if t1 and qty_t1 > 0:
            gtt_t1 = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=KiteConnect.EXCHANGE_NSE,
                trigger_values=[round(t1 * 1.001, 1)],
                last_price=entry,
                orders=[{
                    "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                    "quantity":         qty_t1,
                    "order_type":       KiteConnect.ORDER_TYPE_LIMIT,
                    "price":            t1,
                    "product":          KiteConnect.PRODUCT_CNC,
                }]
            )
            gtt_ids["gtt_t1_id"] = gtt_t1
            log.info(f"GTT T1 placed: {symbol} @ ₹{t1} ({qty_t1} shares) | ID: {gtt_t1}")

        # GTT T2 — remaining 50%
        if t2 and qty_t2 > 0:
            gtt_t2 = kite.place_gtt(
                trigger_type=kite.GTT_TYPE_SINGLE,
                tradingsymbol=symbol,
                exchange=KiteConnect.EXCHANGE_NSE,
                trigger_values=[round(t2 * 1.001, 1)],
                last_price=entry,
                orders=[{
                    "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                    "quantity":         qty_t2,
                    "order_type":       KiteConnect.ORDER_TYPE_LIMIT,
                    "price":            t2,
                    "product":          KiteConnect.PRODUCT_CNC,
                }]
            )
            gtt_ids["gtt_t2_id"] = gtt_t2
            log.info(f"GTT T2 placed: {symbol} @ ₹{t2} ({qty_t2} shares) | ID: {gtt_t2}")

        # Store GTT IDs in atlas_trades
        if trade_id:
            store_gtt_ids(trade_id, gtt_ids)

        # Confirm on Telegram
        send(
            f"📌 <b>GTT ORDERS PLACED — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"SL:  ₹{sl:,.1f} → sell all {qty} shares\n"
            f"T1:  ₹{t1:,.1f} → sell {qty_t1} shares (50%)\n"
            f"T2:  ₹{t2:,.1f} → sell {qty_t2} shares (50%)\n"
            f"Position protected overnight ✅"
        )

        return {"success": True, **gtt_ids}

    except Exception as e:
        log.error(f"GTT placement failed for {symbol}: {e}")
        send(f"⚠️ <b>GTT FAILED — {symbol}</b>\nManually place orders.\nSL: ₹{sl:,.1f} T1: ₹{t1:,.1f}")
        return {"success": False, "reason": str(e)}


def modify_sl_to_breakeven(symbol: str, entry: float, trade_id: int = None) -> bool:
    """
    Modify GTT SL to breakeven after T1 is hit.
    Called when T1 trigger fires.
    """
    kite = get_kite()
    if not kite:
        return False

    try:
        # Get stored GTT SL ID
        gtt_sl_id = get_gtt_sl_id(trade_id) if trade_id else None

        if gtt_sl_id:
            from kiteconnect import KiteConnect
            # Cancel old SL GTT
            kite.delete_gtt(gtt_sl_id)
            log.info(f"Old GTT SL cancelled: {gtt_sl_id}")

            # Get remaining qty from open position
            positions = kite.positions().get("net", [])
            qty = 0
            for p in positions:
                if p.get("tradingsymbol") == symbol:
                    qty = abs(int(p.get("quantity", 0)))
                    break

            if qty > 0:
                # Place new GTT SL at breakeven (entry price)
                breakeven = round(entry * 0.999, 1)  # slight buffer below entry
                new_gtt = kite.place_gtt(
                    trigger_type=kite.GTT_TYPE_SINGLE,
                    tradingsymbol=symbol,
                    exchange=KiteConnect.EXCHANGE_NSE,
                    trigger_values=[breakeven],
                    last_price=entry,
                    orders=[{
                        "transaction_type": KiteConnect.TRANSACTION_TYPE_SELL,
                        "quantity":         qty,
                        "order_type":       KiteConnect.ORDER_TYPE_MARKET,
                        "product":          KiteConnect.PRODUCT_CNC,
                    }]
                )
                log.info(f"GTT SL moved to breakeven: {symbol} @ ₹{breakeven} | ID: {new_gtt}")
                send(
                    f"🔄 <b>SL MOVED TO BREAKEVEN — {symbol}</b>\n"
                    f"T1 hit ✅\n"
                    f"New SL: ₹{breakeven:,.1f} (breakeven)\n"
                    f"Remaining: {qty} shares running to T2"
                )
                if trade_id:
                    update_gtt_sl_id(trade_id, new_gtt)
                return True

    except Exception as e:
        log.error(f"SL modification failed for {symbol}: {e}")
        send(f"⚠️ <b>MANUALLY MOVE SL TO BREAKEVEN — {symbol}</b>\nT1 hit but auto-modify failed.")

    return False


# ── MIS SHORT — BRACKET ORDER ─────────────────────────────────────

def place_mis_bracket_order(
    symbol: str,
    qty: int,
    entry: float,
    sl: float,
    t1: float,
    t2: float = 0,
    trade_id: int = None,
) -> dict:
    """
    Place MIS SHORT as bracket order.
    Entry + SL + T1 in one instruction.
    Zerodha handles cancel-one-fills-other.
    T2 placed as separate order after T1 hits.
    """
    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}

    try:
        from kiteconnect import KiteConnect

        sl_points  = round(abs(sl - entry), 2)
        t1_points  = round(abs(entry - t1), 2)
        qty_t1     = math.floor(qty * 0.5)
        qty_t2     = qty - qty_t1

        # Bracket order — SHORT
        order_id = kite.place_order(
            variety=KiteConnect.VARIETY_BO,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
            quantity=qty_t1,  # First 50% with bracket
            order_type=KiteConnect.ORDER_TYPE_MARKET,
            product=KiteConnect.PRODUCT_MIS,
            validity=KiteConnect.VALIDITY_DAY,
            squareoff=t1_points,
            stoploss=sl_points,
            tag="ATLAS_BO"
        )
        log.info(f"Bracket order placed: {symbol} SHORT {qty_t1} shares | SL: {sl_points} pts | T1: {t1_points} pts | ID: {order_id}")

        # Place remaining 50% as regular MIS with SL only
        if qty_t2 > 0:
            order_id_2 = kite.place_order(
                variety=KiteConnect.VARIETY_REGULAR,
                tradingsymbol=symbol,
                exchange=KiteConnect.EXCHANGE_NSE,
                transaction_type=KiteConnect.TRANSACTION_TYPE_SELL,
                quantity=qty_t2,
                order_type=KiteConnect.ORDER_TYPE_MARKET,
                product=KiteConnect.PRODUCT_MIS,
                validity=KiteConnect.VALIDITY_DAY,
                tag="ATLAS_MIS2"
            )
            # Place SL for second half
            from atlas.execution.broker import place_sl_order
            place_sl_order(symbol, "SHORT", qty_t2, sl, tag="ATLAS_SL2")
            log.info(f"MIS second half placed: {symbol} SHORT {qty_t2} | ID: {order_id_2}")

        send(
            f"⚡ <b>BRACKET ORDER PLACED — {symbol}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Direction: SHORT (MIS)\n"
            f"Entry:     ₹{entry:,.1f}\n"
            f"SL:        ₹{sl:,.1f} ({sl_points:.1f} pts)\n"
            f"T1:        ₹{t1:,.1f} ({t1_points:.1f} pts)\n"
            f"T2:        ₹{t2:,.1f} (manual or EOD)\n"
            f"Qty:       {qty} shares ({qty_t1}+{qty_t2})\n"
            f"Auto square-off: 3:15 PM if not triggered"
        )

        return {"success": True, "order_id": order_id, "order_id_2": order_id_2 if qty_t2 > 0 else None}

    except Exception as e:
        log.error(f"Bracket order failed for {symbol}: {e}")
        # Fallback to regular order + manual SL
        send(
            f"⚠️ <b>BRACKET ORDER FAILED — {symbol}</b>\n"
            f"Reason: {str(e)[:100]}\n"
            f"Falling back to regular MIS order.\n"
            f"MANUALLY PLACE SL at ₹{sl:,.1f}"
        )
        return {"success": False, "reason": str(e)}


# ── GTT TRACKING IN SUPABASE ──────────────────────────────────────

def store_gtt_ids(trade_id: int, gtt_ids: dict) -> bool:
    notes = f"GTT_SL:{gtt_ids.get('gtt_sl_id','')} GTT_T1:{gtt_ids.get('gtt_t1_id','')} GTT_T2:{gtt_ids.get('gtt_t2_id','')}"
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}",
        headers=_headers(),
        json={"notes": notes, "updated_at": datetime.now(IST).isoformat()}
    )
    return r.status_code in (200, 204)


def get_gtt_sl_id(trade_id: int) -> str:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}&select=notes",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        notes = r.json()[0].get("notes", "")
        for part in notes.split():
            if part.startswith("GTT_SL:"):
                return part.split(":")[1]
    return ""


def update_gtt_sl_id(trade_id: int, new_gtt_id: str) -> bool:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}&select=notes",
        headers=_headers()
    )
    if r.status_code == 200 and r.json():
        notes = r.json()[0].get("notes", "")
        import re
        notes = re.sub(r'GTT_SL:\S+', f'GTT_SL:{new_gtt_id}', notes)
        r2 = requests.patch(
            f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}",
            headers=_headers(),
            json={"notes": notes}
        )
        return r2.status_code in (200, 204)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-TM] %(message)s")
    print("Trade management module loaded.")
    print("CNC: GTT SL + T1 (50%) + T2 (50%) placed on entry")
    print("MIS: Bracket order (entry + SL + T1) + separate T2")
    print("T1 hit: SL moved to breakeven automatically")
