"""
ATLAS Execution — Broker Abstraction Layer
==========================================
Clean interface for order placement.
Currently supports Zerodha Kite Connect.
Designed to support multiple brokers via abstraction.

All order placement goes through this layer.
Kill switch is checked before every order.
"""

import os, sys, logging, requests, threading, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    ZERODHA_API_KEY, ZERODHA_API_SECRET, ZERODHA_USER_ID,
)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

HTTP_TIMEOUT = 10

# Slippage cap on MARKET orders, as a percentage. Kite requires this on every
# MARKET order placed through the API and rejects the order outright without it.
#
# 3% is Kite's own web default and is DELIBERATELY not tied to
# MAX_ENTRY_DIST_PCT (0.30%). Those measure different things: the entry gate is
# a publication distance off the previous close, while this is tolerance for how
# far the fill may slip from LTP at the moment of execution. A 0.30% cap here
# would reject the fill on any gap open -- exactly the mornings worth trading.
MARKET_PROTECTION_PCT = 3

# Token + client cache.
#
# get_kite() used to do a fresh Supabase round-trip AND build a new KiteConnect
# on EVERY call, and it is called by get_ltp, get_positions, get_holdings,
# place_order, place_sl_order, cancel_order, get_order_status,
# get_account_balance, all three gtt.py helpers, trade_management and
# trade_outcome_checker. market_open evaluates up to ~150 candidates at 09:37,
# each costing a get_ltp -> get_kite -> Supabase fetch, so a signal batch that
# grew from 4 to 154 multiplied that traffic by ~38x on the latency-sensitive
# path.
#
# A Kite access token is valid for the whole trading day, so this is pure waste.
# TTL is short enough that a mid-day re-login is picked up quickly. Empty tokens
# are never cached, so a login completing after a failed lookup takes effect on
# the next call rather than after the TTL.
KITE_CACHE_TTL = 300.0   # seconds

_cache_lock = threading.Lock()
_cache = {"token": "", "kite": None, "at": 0.0}


_AUTH_ERROR_MARKERS = (
    "TokenException", "PermissionException",
    "Incorrect `api_key` or `access_token`",
    "Invalid `api_key` or `access_token`",
)


def _note_broker_error(e: Exception):
    """If the broker rejected our credentials, drop the cache so the next call
    re-reads the token rather than reusing a dead one for the rest of the TTL.
    Matched on text so kiteconnect.exceptions stays an optional import."""
    blob = f"{type(e).__name__}: {e}"
    if any(m in blob for m in _AUTH_ERROR_MARKERS):
        log.warning("Broker rejected the access token — invalidating cache")
        invalidate_kite_cache()


def invalidate_kite_cache():
    """Drop the cached token and client. Call after a re-login, or when the
    broker rejects the token mid-session."""
    with _cache_lock:
        _cache.update({"token": "", "kite": None, "at": 0.0})
    log.info("Kite cache invalidated")


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }


def _fetch_access_token() -> str:
    """Uncached read of the stored Zerodha access token."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/broker_tokens"
            f"?broker=eq.zerodha&order=created_at.desc&limit=1",
            headers=_headers(), timeout=HTTP_TIMEOUT
        )
    except requests.RequestException as e:
        log.error(f"broker_tokens fetch failed: {type(e).__name__}: {e}")
        return ""
    if r.status_code == 200 and r.json():
        return r.json()[0].get("access_token") or ""
    if r.status_code != 200:
        log.error(f"broker_tokens fetch failed: HTTP {r.status_code}")
    return ""


def get_access_token(force_refresh: bool = False) -> str:
    """Stored Zerodha access token, cached for KITE_CACHE_TTL seconds."""
    with _cache_lock:
        fresh = (not force_refresh
                 and _cache["token"]
                 and (time.monotonic() - _cache["at"]) < KITE_CACHE_TTL)
        if fresh:
            return _cache["token"]

    token = _fetch_access_token()          # network call outside the lock
    if token:
        with _cache_lock:
            _cache["token"] = token
            _cache["at"] = time.monotonic()
    return token


def get_kite(force_refresh: bool = False):
    """Authenticated KiteConnect instance, cached alongside the token."""
    with _cache_lock:
        fresh = (not force_refresh
                 and _cache["kite"] is not None
                 and (time.monotonic() - _cache["at"]) < KITE_CACHE_TTL)
        if fresh:
            return _cache["kite"]

    token = get_access_token(force_refresh=force_refresh)
    if not token:
        log.error("No Zerodha access token found — login required")
        return None
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=ZERODHA_API_KEY)
        kite.set_access_token(token)
    except Exception as e:
        log.error(f"KiteConnect init failed: {e}")
        return None

    with _cache_lock:
        _cache["kite"] = kite
    return kite


def get_ltp(symbol: str, exchange: str = "NSE", kite=None) -> float:
    """Get live price for a symbol.

    kite IS ACCEPTED SO A CALLER THAT ALREADY HAS A SESSION CAN USE IT. The
    emergency exit prices its limit off this: it was given a kite for placing the
    order but this read went through get_kite() regardless, so the order and the
    price it depends on came from two different sessions. In production both
    resolve to the same cached client, which is why it never showed -- but it
    means a caller cannot test or operate the exit with one session, and the
    exit's price read could fail for a reason its order placement would not.
    """
    kite = kite or get_kite()
    if not kite:
        return 0.0
    try:
        instrument = f"{exchange}:{symbol}"
        data = kite.ltp([instrument])
        return float(data[instrument]["last_price"])
    except Exception as e:
        log.error(f"LTP fetch failed for {symbol}: {e}")
        _note_broker_error(e)
        return 0.0


def get_positions() -> list:
    """Get all open positions."""
    kite = get_kite()
    if not kite:
        return []
    try:
        positions = kite.positions()
        return positions.get("net", [])
    except Exception as e:
        log.error(f"Positions fetch failed: {e}")
        _note_broker_error(e)
        return []


def get_holdings() -> list:
    """Get holdings (overnight positions)."""
    kite = get_kite()
    if not kite:
        return []
    try:
        return kite.holdings()
    except Exception as e:
        log.error(f"Holdings fetch failed: {e}")
        _note_broker_error(e)
        return []


def place_order(
    symbol: str,
    direction: str,
    qty: int,
    order_type: str = "MARKET",
    price: float = 0,
    sl: float = 0,
    tag: str = "ATLAS",
    product: str = "MIS",
    intent: str = "ENTRY"
) -> dict:
    """
    Place an order via Zerodha.
    direction: LONG or SHORT
    order_type: MARKET or LIMIT
    product: CNC (delivery -- longs held overnight) or MIS (intraday).
             Defaults to MIS so existing callers are unaffected.
    intent: ENTRY (blocked while halted) or EXIT (always permitted).
    Returns order result dict.

    THE HALT IS ENFORCED HERE, AT THE BOTTOM, not only by the caller.
    -----------------------------------------------------------------
    ATLAS ran 360 cycles on 2026-10-08 and placed nothing, which was correct --
    but it was correct because cycle() returned early, and an early return is one
    guard in one place. This function is the only route from ATLAS to the broker
    for an entry, so the second guard belongs in it: a caller that forgets to
    check cannot reach the exchange.

    ENTRY IS BLOCKED, EXIT IS NOT, and the asymmetry is the whole point. A halted
    ATLAS must still be able to protect or close a position it already holds --
    blocking exits would turn a halt into the unprotected-position incident it
    exists to prevent. So the default is ENTRY: an unlabelled order is treated as
    an entry and refused while halted, which fails in the safe direction.
    """
    from atlas.risk.kill_switch import check as kill_switch_check

    if str(intent).upper() != "EXIT":
        from atlas.risk import breaker
        _halted, _why = breaker.is_halted()
        if _halted:
            log.warning(f"ENTRY BLOCKED at the broker layer — ATLAS is halted "
                        f"({_why}). {direction} {qty} {symbol} was not sent.")
            return {"success": False, "reason": f"ATLAS halted: {_why}",
                    "blocked_by": "halt", "error_type": "Halted",
                    "determinate": True}
        try:
            from atlas.signal.market_open import paused as _paused_check
            _p, _pw = _paused_check()
        except Exception as e:
            # UNREADABLE STATE IS NOT PERMISSION. Same rule as the cycle check.
            _p, _pw = True, f"pause state unreadable ({e})"
        if _p:
            log.warning(f"ENTRY BLOCKED at the broker layer — {_pw}. "
                        f"{direction} {qty} {symbol} was not sent.")
            return {"success": False, "reason": f"ATLAS paused: {_pw}",
                    "blocked_by": "paused", "error_type": "Paused",
                    "determinate": True}

    # Kill switch check — non-bypassable
    ks = kill_switch_check()
    if not ks:
        # kill_switch_check may return a plain bool; do not assume .reason
        _reason = getattr(ks, "reason", "kill switch active")
        log.warning(f"Order BLOCKED by kill switch: {_reason}")
        # Blocked before anything was sent. Determinate.
        return {"success": False, "reason": _reason, "blocked_by": "kill_switch",
                "error_type": "KillSwitch", "determinate": True}

    kite = get_kite()
    if not kite:
        # No client, so nothing was sent. Determinate.
        return {"success": False, "reason": "Kite not initialized",
                "error_type": "NoClient", "determinate": True}

    try:
        from kiteconnect import KiteConnect
        transaction = (
            KiteConnect.TRANSACTION_TYPE_BUY
            if direction == "LONG"
            else KiteConnect.TRANSACTION_TYPE_SELL
        )
        order_params = {
            "tradingsymbol":   symbol,
            "exchange":        KiteConnect.EXCHANGE_NSE,
            "transaction_type": transaction,
            "quantity":        qty,
            "order_type":      KiteConnect.ORDER_TYPE_MARKET if order_type == "MARKET"
                               else KiteConnect.ORDER_TYPE_LIMIT,
            "product":         (KiteConnect.PRODUCT_CNC
                                if str(product).upper() == "CNC"
                                else KiteConnect.PRODUCT_MIS),
            "validity":        KiteConnect.VALIDITY_DAY,
            "tag":             tag,
        }
        if order_type == "LIMIT" and price:
            order_params["price"] = price
        elif order_type == "MARKET":
            # Kite REJECTS every API-placed MARKET order that omits this:
            #   "Market orders without market protection are not allowed via
            #    API. Please set market protection or use a Limit order."
            # NMDC and RAMCOCEM both failed this way at 09:37 on 2026-08-13 --
            # the first orders to reach the broker after the GTT path was
            # removed. Not symbol-specific and not a token problem: the session
            # authenticated, and Kite refused the order on content.
            order_params["market_protection"] = MARKET_PROTECTION_PCT

        order_id = kite.place_order(
            variety=KiteConnect.VARIETY_REGULAR,
            **order_params
        )

        log.info(f"Order placed: {direction} {qty} {symbol} | Order ID: {order_id}")
        return {
            "success":  True,
            "order_id": order_id,
            "symbol":   symbol,
            "direction": direction,
            "qty":      qty,
            "type":     order_type,
            "product":  str(product).upper(),
        }

    except Exception as e:
        log.error(f"Order placement failed for {symbol}: {e}")
        _note_broker_error(e)
        # WHETHER THE ORDER EXISTS IS A DIFFERENT QUESTION FROM WHETHER THE CALL
        # SUCCEEDED, and the caller cannot tell them apart from `reason` alone.
        #
        # A margin rejection and a socket timeout both arrive here as
        # success=False. The first means Kite processed the request and refused
        # it, so no order exists. The second means we never heard back, and the
        # order may be live. A caller that rolls back its own record on
        # success=False would, on a timeout, erase the only trace of a real
        # position -- which is the duplicate-entry bug the two-phase write in
        # atlas_entry exists to prevent.
        return {"success": False, "reason": str(e),
                "error_type": type(e).__name__,
                "determinate": _is_determinate(e)}


# An exception type here means Kite ANSWERED and the answer was a refusal, so
# no order was created. Anything else -- a network error, a timeout, something
# unrecognised -- means the request's fate is unknown.
#
# Matched on class NAME so kiteconnect.exceptions stays an optional import, the
# same approach _note_broker_error already uses.
_DETERMINATE_ERRORS = (
    "InputException",       # malformed request; rejected on content
    "TokenException",       # auth refused; never reached the OMS
    "PermissionException",  # not allowed to trade this
    "MarginException",      # insufficient funds; refused
    "OrderException",       # OMS refused the order
    "GeneralException",     # unclassified, but the API did respond
)


def _is_determinate(e: Exception) -> bool:
    """True only when we KNOW no order was created. Unknown defaults to False."""
    return type(e).__name__ in _DETERMINATE_ERRORS


def place_sl_order(
    symbol: str,
    direction: str,
    qty: int,
    sl_price: float,
    tag: str = "ATLAS_SL"
) -> dict:
    """Place a stop-loss order after entry.

    SECOND IMPLEMENTATION OF THE STOP, and it had the Task-1 defect that
    exits.py was just rebuilt to remove: it sent price=sl_price, i.e. a limit
    sitting AT the trigger, which can rest unfilled through the exact move it
    exists to escape. It also rounded the trigger to one decimal rather than the
    0.05 tick, and an off-tick price is rejected outright.

    THE ARITHMETIC NOW COMES FROM exits.py so there is one definition of
    "trigger, and a limit far enough past it to fill". Its only caller is
    trade_management.place_mis_bracket_order, which nothing imports -- so this
    was a landmine rather than a live bug, which is the only reason it survived
    2026-10-07 unnoticed.
    """
    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}

    try:
        from kiteconnect import KiteConnect
        # For LONG position, SL is a SELL order
        # For SHORT position, SL is a BUY order
        transaction = (
            KiteConnect.TRANSACTION_TYPE_SELL
            if direction == "LONG"
            else KiteConnect.TRANSACTION_TYPE_BUY
        )
        from atlas.execution.exits import stop_trigger, stop_limit_price
        trigger_price = stop_trigger(sl_price, direction)
        limit_price = stop_limit_price(trigger_price, direction)

        order_id = kite.place_order(
            variety=KiteConnect.VARIETY_REGULAR,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            transaction_type=transaction,
            quantity=qty,
            order_type=KiteConnect.ORDER_TYPE_SL,
            product=KiteConnect.PRODUCT_MIS,
            validity=KiteConnect.VALIDITY_DAY,
            price=limit_price,
            trigger_price=trigger_price,
            tag=tag,
        )

        log.info(f"SL order placed: {symbol} trigger ₹{trigger_price} "
                 f"limit ₹{limit_price} | Order ID: {order_id}")
        return {"success": True, "order_id": order_id, "sl_price": sl_price,
                "trigger_price": trigger_price, "limit_price": limit_price}

    except Exception as e:
        log.error(f"SL order failed for {symbol}: {e}")
        _note_broker_error(e)
        return {"success": False, "reason": str(e)}


def order_margin(symbol: str, direction: str, qty: int, product: str,
                 price: float = 0, order_type: str = "MARKET") -> dict:
    """
    What the BROKER says this order requires. -> {"ok", "total", "reason"}

    WHY THIS EXISTS. capital_required was computed from
    SHORT_MARGIN_PCT_ESTIMATE = 0.20 and Gate 6 gated on that number. With no cap
    on trades, an underestimate does not merely mis-report: a short consumes a
    fifth of a long's capital at 20%, so five shorts fit where one long did, and if
    the real requirement is 30% the fifth is unfunded. Asking the broker removes
    the guess from the only check that stands between ATLAS and an over-committed
    account.

    ok=False means "could not ask", never "no margin needed". The caller falls back
    to the estimate and logs that it did -- refusing every trade because the margin
    endpoint is unreachable would be a different failure, not a safer one.
    """
    kite = get_kite()
    if not kite:
        return {"ok": False, "total": None, "reason": "kite unavailable"}
    d = str(direction).upper()
    req = [{
        "exchange": "NSE", "tradingsymbol": symbol,
        "transaction_type": "BUY" if d == "LONG" else "SELL",
        "variety": "regular", "product": str(product).upper(),
        "order_type": str(order_type).upper(), "quantity": int(qty),
        "price": float(price or 0), "trigger_price": 0.0,
    }]
    try:
        res = kite.order_margins(req)
    except Exception as e:
        log.warning(f"order_margins failed for {symbol}: {e}")
        _note_broker_error(e)
        return {"ok": False, "total": None, "reason": f"order_margins failed: {e}"}
    try:
        row = (res or [])[0]
        total = float(row.get("total"))
    except Exception as e:
        return {"ok": False, "total": None,
                "reason": f"unreadable order_margins response ({e}): {str(res)[:120]}"}
    if total <= 0:
        # Zero required margin is not a real answer for an equity order.
        return {"ok": False, "total": None,
                "reason": f"order_margins returned total={total}"}
    log.info(f"{symbol} {d} {qty} {product}: broker margin Rs{total:,.0f}")
    return {"ok": True, "total": total, "reason": "broker order_margins"}


def cancel_order(order_id: str) -> bool:
    """Cancel an open order."""
    kite = get_kite()
    if not kite:
        return False
    try:
        from kiteconnect import KiteConnect
        kite.cancel_order(variety=KiteConnect.VARIETY_REGULAR, order_id=order_id)
        log.info(f"Order cancelled: {order_id}")
        return True
    except Exception as e:
        log.error(f"Cancel order failed {order_id}: {e}")
        _note_broker_error(e)
        return False


def get_order_status(order_id: str) -> dict:
    """Get status of a placed order."""
    kite = get_kite()
    if not kite:
        return {}
    try:
        orders = kite.orders()
        for o in orders:
            if str(o.get("order_id")) == str(order_id):
                return o
        return {}
    except Exception as e:
        log.error(f"Order status failed {order_id}: {e}")
        _note_broker_error(e)
        return {}


def get_account_balance() -> dict:
    """Get available margin/balance."""
    kite = get_kite()
    if not kite:
        return {}
    try:
        margins = kite.margins()
        equity = margins.get("equity", {})
        return {
            "available": float(equity.get("available", {}).get("live_balance", 0)),
            "used":      float(equity.get("utilised", {}).get("debits", 0)),
            "total":     float(equity.get("net", 0)),
        }
    except Exception as e:
        log.error(f"Balance fetch failed: {e}")
        _note_broker_error(e)
        return {}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                       format="%(asctime)s [ATLAS-BROKER] %(message)s")
    print("=== ATLAS BROKER STATUS ===")
    kite = get_kite()
    if kite:
        print("Kite Connect: initialized")
        bal = get_account_balance()
        if bal:
            print(f"Available balance: INR {bal.get('available', 0):,.0f}")
        else:
            print("Balance: requires valid access token")
    else:
        print("Kite Connect: not initialized (needs login)")
    print("\nBroker abstraction layer ready.")
