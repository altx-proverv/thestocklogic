"""
THE BOOKS -- which trades get stop orders at the broker (operator, 2026-11-10).

ONE QUESTION, ASKED BY EVERY STOP TOUCHPOINT: does this trade's book want a stop
at the broker? It is decided here from the trade's own direction and product,
never from a global flag, because the two books have opposite needs:

    LONG_DELIVERY    LONG + CNC, the investing mandate. No stop orders. An open
                     long without one is the intended state.
    SHORT_INTRADAY   SHORT + MIS. Mandatory broker-side stops. A short has no
                     floor -- non-negotiable.
    UNKNOWN          any other combination (a LONG explicitly MIS, a blank
                     direction). Treated as needing stops: the safe direction for
                     a book that does.

A MISSING PRODUCT IS READ FROM THE DIRECTION, because ATLAS fixes it: longs are
LONG_PRODUCT_TYPE (CNC) and broker.place_order refuses a short that is not MIS.
Several callers build trade dicts without the column (the unprotected path,
reconcile's fill-unreadable path), and reading those as UNKNOWN would raise an
UNPROTECTED alarm for every pilot long.

THE SHORT BOOK CANNOT BE TURNED OFF HERE. If SHORT_BOOK_BROKER_STOPS is anything
but True this module refuses to import, which stops every module that asks it --
the entry path included -- rather than letting one edited constant strip stops
from shorts.
"""
from __future__ import annotations

from atlas.config import (LONG_BOOK_BROKER_STOPS, SHORT_BOOK_BROKER_STOPS,
                          LONG_PRODUCT_TYPE, SHORT_PRODUCT_TYPE)

if SHORT_BOOK_BROKER_STOPS is not True:                     # pragma: no cover
    raise ImportError("SHORT_BOOK_BROKER_STOPS must be True: the short book's "
                      "broker-side stops are non-negotiable (config.py)")

LONG_DELIVERY = "LONG_DELIVERY"
SHORT_INTRADAY = "SHORT_INTRADAY"
UNKNOWN = "UNKNOWN"


def book_of(direction, product) -> str:
    d = str(direction or "").strip().upper()
    p = str(product or "").strip().upper()
    if not p:
        p = str(LONG_PRODUCT_TYPE if d == "LONG" else
                SHORT_PRODUCT_TYPE if d == "SHORT" else "").upper()
    if d == "LONG" and p == str(LONG_PRODUCT_TYPE).upper():
        return LONG_DELIVERY
    if d == "SHORT" and p == str(SHORT_PRODUCT_TYPE).upper():
        return SHORT_INTRADAY
    return UNKNOWN


def book_of_trade(t: dict) -> str:
    return book_of((t or {}).get("direction"), (t or {}).get("product"))


def broker_stops(direction, product) -> bool:
    """True when this book places stop orders at the broker."""
    b = book_of(direction, product)
    if b == LONG_DELIVERY:
        return bool(LONG_BOOK_BROKER_STOPS)
    return True                     # SHORT_INTRADAY and UNKNOWN: stops, always


def broker_stops_for_trade(t: dict) -> bool:
    return broker_stops((t or {}).get("direction"), (t or {}).get("product"))
