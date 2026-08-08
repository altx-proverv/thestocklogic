"""
ATLAS GTT Entry -- Good Till Triggered zone orders
===================================================
When a LONG signal's zone sits below current price, ATLAS does not skip it and
does not poll. It places a GTT buy trigger AT the zone edge and lets Zerodha
watch the price.

BROKER CONSTRAINTS (verified against Zerodha docs, Aug 2026)
-----------------------------------------------------------
  - GTT is CNC (delivery) ONLY. Not available for MIS/intraday. So GTT covers
    LONGS only. A SHORT out of range at 09:37 is skipped -- there is no
    equivalent mechanism for intraday shorts.
  - A GTT stays live for ONE YEAR or until triggered. It does NOT expire daily.
    Operator manages cancellation manually (explicit decision).
  - Cash must be maintained while GTTs are pending, or Zerodha RMS may cancel
    them at its discretion.
  - A triggered GTT places a LIMIT order. If price gaps hard through the zone,
    the limit does not fill -- which is protective, not a fault.
  - Once triggered the GTT leaves the queue. If the order does not execute it
    must be placed again.
  - Max 500 active GTTs per account. Not a constraint at 3/day.

KNOWN AND ACCEPTED
------------------
A GTT fires whenever price reaches the zone, with no awareness of market
direction at that moment. A long trigger can fill into a market that has since
turned bearish. Operator monitors active trades manually (explicit decision).

LIMIT PRICE
-----------
Trigger sits at the zone edge; the limit is placed GTT_LIMIT_BUFFER above it so
a normal touch fills. A hard gap below still will not fill.
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ATLAS-GTT")
IST = timezone(timedelta(hours=5, minutes=30))

GTT_LIMIT_BUFFER = 0.003   # limit 0.3% above trigger, so a touch fills
GTT_TAG = "ATLAS"


def place_zone_gtt(symbol: str, qty: int, trigger_price: float,
                   last_price: float, tag: str = GTT_TAG) -> dict:
    """Single-trigger BUY GTT at the zone edge, CNC product.

    trigger_price -- the zone edge price must fall to (entry_ref for a long)
    last_price    -- current LTP, required by the Kite GTT API
    """
    from atlas.execution.broker import get_kite

    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}

    if trigger_price <= 0 or qty <= 0:
        return {"success": False, "reason": "invalid trigger price or qty"}

    if trigger_price >= last_price:
        # Buy-the-dip only. A trigger at or above LTP would fire instantly.
        return {"success": False,
                "reason": f"trigger Rs{trigger_price:.1f} not below LTP Rs{last_price:.1f}"}

    limit_price = round(trigger_price * (1 + GTT_LIMIT_BUFFER), 1)

    try:
        from kiteconnect import KiteConnect
        trigger_id = kite.place_gtt(
            trigger_type=kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            trigger_values=[round(trigger_price, 1)],
            last_price=round(last_price, 1),
            orders=[{
                "transaction_type": KiteConnect.TRANSACTION_TYPE_BUY,
                "quantity":         int(qty),
                "order_type":       KiteConnect.ORDER_TYPE_LIMIT,
                "product":          KiteConnect.PRODUCT_CNC,
                "price":            limit_price,
                "tag":              tag,
            }],
        )
        log.info(f"GTT placed: BUY {qty} {symbol} trigger Rs{trigger_price:.1f} "
                 f"limit Rs{limit_price:.1f} | id {trigger_id}")
        return {
            "success":       True,
            "trigger_id":    trigger_id,
            "symbol":        symbol,
            "qty":           qty,
            "trigger_price": round(trigger_price, 1),
            "limit_price":   limit_price,
            "product":       "CNC",
        }
    except Exception as e:
        log.error(f"GTT placement failed for {symbol}: {e}")
        return {"success": False, "reason": str(e)}


def list_atlas_gtts() -> list:
    """Active GTTs placed by ATLAS, identified by order tag."""
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if not kite:
        return []
    try:
        out = []
        for g in (kite.get_gtts() or []):
            if g.get("status") != "active":
                continue
            orders = g.get("orders", []) or []
            if any((o.get("tag") or "") == GTT_TAG for o in orders):
                out.append(g)
        return out
    except Exception as e:
        log.warning(f"GTT list failed: {e}")
        return []


def cancel_gtt(trigger_id: int) -> dict:
    """Cancel one GTT. Used for manual cleanup, not on a schedule --
    daily expiry is handled by the operator."""
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}
    try:
        kite.delete_gtt(trigger_id=trigger_id)
        log.info(f"GTT cancelled: {trigger_id}")
        return {"success": True, "trigger_id": trigger_id}
    except Exception as e:
        log.error(f"GTT cancel failed for {trigger_id}: {e}")
        return {"success": False, "reason": str(e)}


def gtt_summary() -> str:
    """Telegram-friendly summary of pending ATLAS GTTs."""
    gtts = list_atlas_gtts()
    if not gtts:
        return "No pending ATLAS GTT triggers."
    lines = [f"<b>Pending GTT triggers ({len(gtts)})</b>"]
    for g in gtts:
        cond = g.get("condition", {}) or {}
        sym = cond.get("tradingsymbol", "?")
        trig = (cond.get("trigger_values") or [0])[0]
        qty = sum(int(o.get("quantity", 0)) for o in (g.get("orders") or []))
        lines.append(f"{sym}: {qty} @ trigger Rs{trig}")
    return "\n".join(lines)
