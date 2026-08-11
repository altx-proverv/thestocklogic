"""
ATLAS Risk — Live Broker Funds
==============================
The only capital question ATLAS asks: does the broker have enough cash for
this trade right now?

There is no allocated capital, no deployed-capital ledger, no capital fence and
no stored balance. Those were a second, drifting copy of a number the broker
already knows -- atlas_state.capital said Rs1,50,000 while config said Rs3,00,000
while atlas.html said Rs3,00,000, and the kill switch derived its loss caps from
whichever it happened to read. The operator manages the money pool; ATLAS reads
it live and sizes each trade against it.

FAIL CLOSED
-----------
Every function here raises FundsUnavailable rather than returning a permissive
default. A margin call that errors, times out, or comes back malformed means we
do not know what is available, and "do not know" must block the trade. This is
the same defect that made the kill switch return ALLOWED when Supabase was
down; it is not reintroduced here.

Note the asymmetry: a *readable* balance of Rs0 is not an error. It returns 0.0
and the affordability check fails on the number. Only an unreadable balance
raises.

RESTING GTTs
------------
A GTT trigger is not a position and does not appear in kite.margins(), but it
will consume cash the moment it fires. Counting only positions would let ATLAS
commit the same rupee twice -- once to a resting trigger and again to a new
entry. pending_gtt_commitment() sums the notional of every active ATLAS BUY
trigger and available_funds() subtracts it.

Zerodha does not block margin for a resting GTT, so this is deliberately more
conservative than the broker. That is the correct direction to err.
"""

import logging

from atlas.config import FUNDS_SAFETY_BUFFER_PCT

log = logging.getLogger("ATLAS-FUNDS")


class FundsUnavailable(Exception):
    """Broker funds could not be determined. Never swallowed into a default."""


def _kite():
    from atlas.execution.broker import get_kite
    kite = get_kite()
    if kite is None:
        raise FundsUnavailable("no authenticated Kite session (login required)")
    return kite


def live_margin() -> float:
    """Available equity cash from the broker, right now.

    Raises FundsUnavailable if the call fails or the response is not shaped the
    way we expect. Returns 0.0 only when the broker genuinely reports zero.
    """
    kite = _kite()
    try:
        margins = kite.margins()
    except Exception as e:
        raise FundsUnavailable(f"kite.margins() failed: {type(e).__name__}: {e}") from e

    if not isinstance(margins, dict):
        raise FundsUnavailable(f"kite.margins() returned {type(margins).__name__}, expected dict")

    equity = margins.get("equity")
    if not isinstance(equity, dict):
        raise FundsUnavailable("kite.margins() has no 'equity' block")

    available = equity.get("available")
    if not isinstance(available, dict):
        raise FundsUnavailable("kite.margins() equity has no 'available' block")

    if "live_balance" not in available:
        raise FundsUnavailable("kite.margins() available has no 'live_balance'")

    try:
        return float(available["live_balance"])
    except (TypeError, ValueError) as e:
        raise FundsUnavailable(
            f"live_balance is not numeric: {available['live_balance']!r}") from e


def pending_gtt_commitment() -> float:
    """Notional of every active ATLAS BUY trigger resting at the broker.

    Cash that is spoken for but invisible to kite.margins(). Raises rather than
    returning 0.0 on failure -- an unreadable GTT book would silently free up
    money that is already committed.
    """
    from atlas.execution.gtt import GTT_TAG

    kite = _kite()
    try:
        gtts = kite.get_gtts()
    except Exception as e:
        raise FundsUnavailable(f"kite.get_gtts() failed: {type(e).__name__}: {e}") from e

    if gtts is None:
        raise FundsUnavailable("kite.get_gtts() returned None")

    total = 0.0
    for g in gtts:
        if (g or {}).get("status") != "active":
            continue
        for o in (g.get("orders") or []):
            if (o.get("tag") or "") != GTT_TAG:
                continue
            if str(o.get("transaction_type", "")).upper() != "BUY":
                continue          # a resting SELL frees cash, it does not commit it
            try:
                qty   = float(o.get("quantity") or 0)
                price = float(o.get("price") or 0)
            except (TypeError, ValueError):
                raise FundsUnavailable(
                    f"unparseable GTT order on {(g.get('condition') or {}).get('tradingsymbol', '?')}")
            if price <= 0:
                # A market-order GTT has no price to size against. Refuse rather
                # than treat it as free.
                raise FundsUnavailable(
                    f"active ATLAS GTT with no limit price on "
                    f"{(g.get('condition') or {}).get('tradingsymbol', '?')}")
            total += qty * price
    return round(total, 2)


def available_funds() -> dict:
    """Cash ATLAS may actually commit: broker balance minus resting GTTs."""
    margin = live_margin()
    gtt    = pending_gtt_commitment()
    return {
        "margin":        round(margin, 2),
        "gtt_committed": gtt,
        "available":     round(margin - gtt, 2),
    }


def can_afford(capital_required: float) -> tuple:
    """(ok, reason, detail).

    ok is False on insufficient funds AND on any failure to read them. The
    caller never has to distinguish -- both mean do not trade.
    """
    try:
        need = float(capital_required)
    except (TypeError, ValueError):
        return False, f"invalid capital_required: {capital_required!r}", {}
    if need <= 0:
        return False, f"invalid capital_required: Rs{need:,.0f}", {}

    try:
        f = available_funds()
    except FundsUnavailable as e:
        log.error(f"FUNDS UNREADABLE — blocking. {e}")
        return False, f"broker funds unreadable: {e}", {"data_available": False}

    # Buffer covers brokerage, taxes and slippage on the way in.
    need_buffered = need * (1.0 + FUNDS_SAFETY_BUFFER_PCT)
    detail = {**f, "data_available": True,
              "required": round(need, 2),
              "required_buffered": round(need_buffered, 2)}

    if need_buffered > f["available"]:
        return False, (
            f"insufficient funds: need Rs{need_buffered:,.0f} "
            f"(Rs{need:,.0f} + {FUNDS_SAFETY_BUFFER_PCT*100:.0f}% buffer), "
            f"available Rs{f['available']:,.0f} "
            f"(broker Rs{f['margin']:,.0f} less Rs{f['gtt_committed']:,.0f} resting GTTs)"
        ), detail

    return True, (
        f"funds available: Rs{f['available']:,.0f} "
        f"(broker Rs{f['margin']:,.0f} less Rs{f['gtt_committed']:,.0f} resting GTTs)"
    ), detail


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-FUNDS] %(message)s")
    print("=== ATLAS LIVE FUNDS ===")
    try:
        f = available_funds()
        for k, v in f.items():
            print(f"  {k:<15} Rs{v:>14,.2f}")
    except FundsUnavailable as e:
        print(f"  UNAVAILABLE: {e}")
        print("  -> every trade would be BLOCKED (fail closed)")
