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
    INITIAL_CAPITAL
)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

HTTP_TIMEOUT = 10

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


def get_ltp(symbol: str, exchange: str = "NSE") -> float:
    """Get live price for a symbol."""
    kite = get_kite()
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
    product: str = "MIS"
) -> dict:
    """
    Place an order via Zerodha.
    direction: LONG or SHORT
    order_type: MARKET or LIMIT
    product: CNC (delivery -- longs held overnight) or MIS (intraday).
             Defaults to MIS so existing callers are unaffected.
    Returns order result dict.
    """
    from atlas.risk.kill_switch import check as kill_switch_check

    # Kill switch check — non-bypassable
    ks = kill_switch_check()
    if not ks:
        # kill_switch_check may return a plain bool; do not assume .reason
        _reason = getattr(ks, "reason", "kill switch active")
        log.warning(f"Order BLOCKED by kill switch: {_reason}")
        return {"success": False, "reason": _reason, "blocked_by": "kill_switch"}

    kite = get_kite()
    if not kite:
        return {"success": False, "reason": "Kite not initialized"}

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
        return {"success": False, "reason": str(e)}


def place_sl_order(
    symbol: str,
    direction: str,
    qty: int,
    sl_price: float,
    tag: str = "ATLAS_SL"
) -> dict:
    """Place a stop-loss order after entry."""
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
        # SL trigger price with small buffer
        trigger_price = round(sl_price * 1.001 if direction == "SHORT"
                             else sl_price * 0.999, 1)

        order_id = kite.place_order(
            variety=KiteConnect.VARIETY_REGULAR,
            tradingsymbol=symbol,
            exchange=KiteConnect.EXCHANGE_NSE,
            transaction_type=transaction,
            quantity=qty,
            order_type=KiteConnect.ORDER_TYPE_SL,
            product=KiteConnect.PRODUCT_MIS,
            validity=KiteConnect.VALIDITY_DAY,
            price=sl_price,
            trigger_price=trigger_price,
            tag=tag,
        )

        log.info(f"SL order placed: {symbol} SL @ ₹{sl_price} | Order ID: {order_id}")
        return {"success": True, "order_id": order_id, "sl_price": sl_price}

    except Exception as e:
        log.error(f"SL order failed for {symbol}: {e}")
        _note_broker_error(e)
        return {"success": False, "reason": str(e)}


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
