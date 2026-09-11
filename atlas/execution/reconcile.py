"""
ATLAS — Reconcile
=================
Settles PENDING trade rows against the broker, and reports where the ledger and
the book disagree.

WHAT PENDING MEANS
------------------
atlas_entry writes a PENDING row before it places an order, so that no death
can leave a position the next evaluation cannot see. PENDING therefore means
"an order for this symbol may exist at the broker right now", and Gate 3b
blocks on it.

That is the safe direction, but it is not free: a PENDING row that is never
settled blocks its symbol for the rest of the session. A legitimate PENDING
lives for as long as one order placement -- a second or two -- so anything
older than STALE_AFTER_SECONDS is by definition unresolved and is this module's
work.

HOW A ROW IS SETTLED
--------------------
The order carries tag ATLAS:<row id>, and atlas_trades.id is a bigint, so the
tag fits Kite's 20-character limit and the join is exact rather than a guess
from symbol and timestamp.

    a COMPLETE order with our tag     -> OPEN, at the real fill price
    a REJECTED/CANCELLED order        -> CANCELLED
    no order with our tag             -> CANCELLED; we died before placing
    the broker cannot be read         -> LEAVE IT PENDING

The last line is the one that matters. An unreadable broker is not a flat
broker. This codebase has already closed six positions from a single exception
that was read as "no positions", and the same mistake here would cancel a row
whose order is live and free the symbol to be entered a second time. When in
doubt the row stays PENDING and the symbol stays blocked, because over-blocking
costs a missed trade and under-blocking costs a double position.

BOUNDED
-------
If rows cannot be settled because the broker is unreadable, that is not a
transient to be retried 360 times. Past MAX_UNSETTLED the breaker halts: ATLAS
cannot see its own book, so it has no business adding to it.
"""

import logging
from datetime import datetime, timezone, timedelta

import requests

from atlas.config import SUPABASE_URL, SUPABASE_KEY
from atlas.risk import breaker

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# A real PENDING lives for one order placement. Two minutes is far past that
# and well short of anything that would matter if we are wrong.
STALE_AFTER_SECONDS = 120

# Unsettleable rows tolerated before halting.
MAX_UNSETTLED = 3

TAG_PREFIX = "ATLAS:"

_TERMINAL_REJECTED = ("REJECTED", "CANCELLED")


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def stale_pending(now=None) -> list:
    """PENDING rows old enough that they cannot still be in flight."""
    now = now or datetime.now(IST)
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?status=eq.PENDING&select=id,symbol,direction,qty,entry_price,"
            f"stop_price,created_at,order_id&order=created_at.asc",
            headers=_headers(), timeout=15)
    except Exception as e:
        log.error(f"could not list PENDING rows: {type(e).__name__}: {e}")
        breaker.record_read(False, "atlas_trades", str(e))
        return []
    if r.status_code != 200:
        log.error(f"could not list PENDING rows: HTTP {r.status_code} {r.text[:200]}")
        breaker.record_read(False, "atlas_trades", f"HTTP {r.status_code}")
        return []
    breaker.record_read(True, "atlas_trades")

    out = []
    for row in r.json():
        ts = row.get("created_at")
        if not ts:
            out.append(row)                      # no timestamp: treat as stale
            continue
        try:
            created = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if (now - created).total_seconds() >= STALE_AFTER_SECONDS:
                out.append(row)
        except Exception:
            out.append(row)
    return out


def _orders_by_tag() -> dict:
    """{row_id: order} for today's ATLAS orders. None if the broker is unreadable.

    None and {} mean different things and must not be conflated: the first is
    "we do not know", the second is "there are none".
    """
    try:
        from atlas.execution.broker import get_kite
        kite = get_kite()
        if not kite:
            log.error("no Kite client — cannot read orders")
            return None
        orders = kite.orders()
    except Exception as e:
        log.error(f"could not read broker orders: {type(e).__name__}: {e}")
        return None

    if orders is None:
        return None

    out = {}
    for o in orders:
        tag = str(o.get("tag") or "")
        if not tag.startswith(TAG_PREFIX):
            continue
        try:
            rid = int(tag[len(TAG_PREFIX):])
        except ValueError:
            continue
        # Keep the most recently updated order for a row id.
        prev = out.get(rid)
        if prev is None or str(o.get("order_timestamp", "")) >= str(
                prev.get("order_timestamp", "")):
            out[rid] = o
    return out


def _patch(row_id, patch: dict) -> bool:
    try:
        r = requests.patch(f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{row_id}",
                           headers=_headers(), json=patch, timeout=10)
    except Exception as e:
        log.error(f"could not settle trade {row_id}: {type(e).__name__}: {e}")
        return False
    if r.status_code not in (200, 204):
        log.error(f"could not settle trade {row_id}: HTTP {r.status_code} "
                  f"{r.text[:200]}")
        return False
    return True


def settle(now=None) -> dict:
    """Resolve every stale PENDING row. Returns a summary."""
    rows = stale_pending(now)
    if not rows:
        return {"checked": 0, "opened": 0, "cancelled": 0, "unsettled": 0}

    log.info(f"reconcile: {len(rows)} stale PENDING row(s)")

    tagged = _orders_by_tag()
    if tagged is None:
        # Unreadable broker. Settle nothing; every row stays PENDING and keeps
        # blocking. This is the six-positions bug, refused.
        log.error(f"reconcile: broker unreadable — leaving all {len(rows)} "
                  f"PENDING row(s) untouched and blocking")
        if len(rows) >= MAX_UNSETTLED:
            breaker.halt("cannot settle PENDING trades",
                         f"{len(rows)} rows pending and the broker is unreadable")
        return {"checked": len(rows), "opened": 0, "cancelled": 0,
                "unsettled": len(rows), "broker_readable": False}

    opened = cancelled = unsettled = 0
    for row in rows:
        rid = row["id"]
        order = tagged.get(rid)

        if order is None:
            # No order carries our tag, and the broker WAS readable, so the
            # order was never placed. Safe to free the symbol.
            if _patch(rid, {"status": "CANCELLED",
                            "notes": "reconcile: no broker order for tag "
                                     f"{TAG_PREFIX}{rid} — never placed"}):
                cancelled += 1
                log.info(f"  trade {rid} {row.get('symbol')}: CANCELLED (no order)")
            else:
                unsettled += 1
            continue

        status = str(order.get("status") or "").upper()

        if status == "COMPLETE":
            fill = order.get("average_price") or row.get("entry_price")
            if _patch(rid, {"status": "OPEN",
                            "order_id": str(order.get("order_id") or ""),
                            "entry_price": fill,
                            "qty": order.get("filled_quantity") or row.get("qty"),
                            "notes": f"reconcile: order {order.get('order_id')} "
                                     f"filled at Rs{fill} — promoted to OPEN. "
                                     f"MANUAL RISK REQUIRED - place SL at Rs"
                                     f"{row.get('stop_price')}"}):
                opened += 1
                log.warning(f"  trade {rid} {row.get('symbol')}: OPEN at Rs{fill} "
                            f"— this position existed unrecorded; place its stop")
            else:
                unsettled += 1

        elif status in _TERMINAL_REJECTED:
            if _patch(rid, {"status": "CANCELLED",
                            "notes": f"reconcile: broker order {status} — "
                                     f"{str(order.get('status_message') or '')[:150]}"}):
                cancelled += 1
                log.info(f"  trade {rid} {row.get('symbol')}: CANCELLED ({status})")
            else:
                unsettled += 1
        else:
            # OPEN / TRIGGER PENDING / VALIDATION PENDING -- genuinely still in
            # flight. Leave it; the next pass settles it.
            unsettled += 1
            log.info(f"  trade {rid} {row.get('symbol')}: still {status} — leaving")

    if unsettled >= MAX_UNSETTLED:
        breaker.halt("PENDING trades will not settle",
                     f"{unsettled} row(s) unresolved after a reconcile pass")

    summary = {"checked": len(rows), "opened": opened, "cancelled": cancelled,
               "unsettled": unsettled, "broker_readable": True}
    log.info(f"reconcile: {summary}")

    if opened:
        try:
            from atlas.reporting.telegram import send
            send(f"⚠️ <b>ATLAS RECONCILE</b>\n"
                 f"{opened} position(s) were live at the broker with an "
                 f"incomplete ledger row and have been promoted to OPEN.\n"
                 f"Check /atlas and place their stops.")
        except Exception as e:
            log.error(f"could not send reconcile alert: {e}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    print(settle())
