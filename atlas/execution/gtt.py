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
        resp = kite.place_gtt(
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
        # kite.place_gtt() returns {"trigger_id": N}, not a bare id. Passing the
        # dict straight through stored '{"trigger_id": 331456858}' in
        # atlas_trades.gtt_trigger_id, which never matches str(gtt["id"]) from
        # get_gtts() -- so the trigger-id lookup that replaced tag matching was
        # itself broken for the three GTTs placed on 2026-08-12.
        trigger_id = resp.get("trigger_id") if isinstance(resp, dict) else resp
        if trigger_id is None:
            return {"success": False,
                    "reason": f"place_gtt returned no trigger_id: {resp!r}"}
        trigger_id = str(trigger_id)
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


def list_active_gtts() -> list:
    """EVERY active GTT on the account, unfiltered.

    This is what the funds check must use. A resting BUY commits cash whoever
    placed it -- ATLAS, or the operator by hand in Kite -- so "is it ours?" is
    the wrong question when asking what money is spoken for.
    """
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if not kite:
        return []
    try:
        return [g for g in (kite.get_gtts() or []) if g.get("status") == "active"]
    except Exception as e:
        log.warning(f"GTT list failed: {e}")
        return []


def list_atlas_gtts() -> list:
    """Active GTTs ATLAS placed, matched by trigger id recorded in atlas_trades.

    It used to match on the order `tag`. That can never work: Kite's get_gtts()
    response omits the tag field entirely -- not null, absent -- so
    (o.get("tag") or "") was always "" and this returned []. GTT 331263278
    (GRASIM, 2026-08-11) was invisible to ATLAS for exactly this reason while
    resting live at the broker.

    place_zone_gtt still SENDS a tag; Kite accepts and discards it. The
    authoritative link is atlas_trades.gtt_trigger_id.
    """
    import requests
    from atlas.config import SUPABASE_URL, SUPABASE_KEY

    active = list_active_gtts()
    if not active:
        return []
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?gtt_trigger_id=not.is.null&select=gtt_trigger_id",
            headers={"apikey": SUPABASE_KEY,
                     "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=10)
        if r.status_code != 200:
            log.warning(f"could not read recorded trigger ids: HTTP {r.status_code}")
            return []
        known = {str(x["gtt_trigger_id"]) for x in r.json() if x.get("gtt_trigger_id")}
    except Exception as e:
        log.warning(f"could not read recorded trigger ids: {e}")
        return []

    return [g for g in active if str(g.get("id")) in known]


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
