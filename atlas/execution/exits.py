"""
ATLAS Execution — Automatic stops and targets
=============================================
Everything that protects a position after it fills. Before this, ENABLE_EXIT_MANAGEMENT
was False and place_zone_gtt() had been deleted, so there was no exit path at all:
ATLAS opened positions and left them to a human.

THREE DECISIONS THAT SHAPE THE WHOLE MODULE
-------------------------------------------
1. FILL DETECTION IS SAME-CYCLE AND BLOCKING. The obvious design detects the fill
   on the next 60s cycle, which leaves a minute of unprotected exposure on every
   entry in a system nobody is watching. So enter_trade blocks on await_fill()
   and places protection in the same call. A MARKET order fills in well under a
   second; FILL_POLL_SECONDS is the outer bound before we stop waiting and treat
   the order as indeterminate.

2. A STOP THAT CANNOT BE PLACED MEANS EXIT AT MARKET, IMMEDIATELY. Not retry,
   not alert-and-hold. An unprotected open position is worse than a bad exit:
   the bad exit costs a known amount once, the unprotected position is an
   unbounded loss that nobody is watching. This is a deliberate risk preference
   and it is the opposite of what the module would do if it optimised for exit
   price.

3. SHORTS GET AN SL-M AT THE EXCHANGE, NOT A GTT. GTT is CNC-only, and switching
   a short to CNC to make a GTT work would add overnight gap risk to a trade
   whose whole design is to be flat by 15:20. An SL-M does the same job without
   changing what the trade is.

WHY THE TWO SIDES USE DIFFERENT MECHANISMS
------------------------------------------
  LONG  CNC, held overnight  -> GTT. A regular order is DAY validity and
        disappears at 15:30, so a long protected by a regular stop is
        unprotected every night. GTT persists at the broker.
  SHORT MIS, flat by 15:20   -> SL-M stop + LIMIT target, plus a mandatory time
        exit. DAY validity is sufficient because the position cannot outlive the
        session, and MIS is not GTT-eligible anyway.

TICK SIZE. Every price sent to the exchange is rounded to TICK. An order at
1234.567 is rejected, and a rejected stop is an unprotected position -- which
under decision 2 becomes a market exit. Rounding is not cosmetic here.

STRINGS, NOT SDK CONSTANTS. kiteconnect is not installed in development, so the
documented REST values are used directly ("SL-M", "CNC", "SELL", ...). They are
what the SDK constants expand to, and using them keeps this module testable
against an injected fake kite rather than only on the box.
"""

from __future__ import annotations

import time
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

log = logging.getLogger("ATLAS-EXITS")

TICK = 0.05                 # NSE equity tick
FILL_POLL_SECONDS = 20      # outer bound on waiting for a MARKET fill
FILL_POLL_INTERVAL = 1.0
TARGET_R_MULTIPLE = 2.0     # the 2R target

# A stop trigger sits slightly inside the level so the trigger fires before the
# level is passed, rather than exactly on it.
STOP_TRIGGER_BUFFER = 0.001

# ══════════════════════════════════════════════════════════════════
# ORDER TYPES THE BROKER ACTUALLY ACCEPTS
# ══════════════════════════════════════════════════════════════════
#
# Verified against the Kite Connect v3 order documentation and Zerodha support on
# 2026-10-09, not from memory:
#
#   SL-M IS BLOCKED ON BSE for equity, equity derivatives, currency and commodity
#   -- the exchange discontinued it to stop erroneous orders filling far from the
#   market -- and it is also blocked for index options. Zerodha's own guidance is
#   to use a stoploss-LIMIT (SL) with the limit set significantly past the trigger,
#   which gives market-like execution while protecting against a freak fill.
#
#   market_protection applies to MARKET and SL-M ONLY. It is a decimal percent,
#   0 < p <= 100, and -1 selects the broker's own guideline. It is NOT a parameter
#   of SL or LIMIT, so the SL stop below must not send it.
#
# WHAT WENT WRONG ON 2026-10-07. BHARTIARTL SHORT 5 @ 1833 MIS at 14:12:09. The
# SL-M stop was rejected -- "Market orders without market protection are not
# allowed via API" -- and the emergency exit was rejected for the SAME reason,
# because it was a MARKET order and MARKET is the same family SL-M belongs to for
# this check. Two layers of protection were one layer twice. The position sat
# unprotected for 68 minutes until the broker's own cutoff closed it.

# How far past the trigger the stop's limit price sits. The limit must be far
# enough through the book to fill in the move the stop exists to escape, and close
# enough not to be rejected by the exchange's Limit Price Protection band. 0.5%
# past a trigger that is itself 0.1% inside the stop.
STOP_LIMIT_SLIP_PCT = 0.005

# How far through the book an emergency exit prices itself. Wider than the stop's,
# because this runs when the position is ALREADY unprotected and a missed fill is
# the failure being escaped.
EXIT_LIMIT_SLIP_PCT = 0.010

TERMINAL_OK = ("COMPLETE",)
TERMINAL_BAD = ("REJECTED", "CANCELLED")


# ══════════════════════════════════════════════════════════════════
# THE FALLBACK MUST NOT SHARE A FAILURE MODE
# ══════════════════════════════════════════════════════════════════
#
# This is the lesson from 2026-10-07, and it is not about order types. The stop
# failed, the fallback was asked to save the position, and the fallback failed for
# THE SAME REASON -- so there was never a second layer. Two mechanisms are only
# two if they can fail independently.
#
# Each mechanism declares what it sends. assert_independent() refuses a
# primary/fallback pair that shares an order_type or a trigger dependency, and it
# is called at module import, so a future change that quietly makes the fallback
# the same shape as the primary breaks the import rather than the position.
# `api` IS THE STRONGEST INDEPENDENCE AXIS AND IT IS NEW HERE. A GTT is not a
# regular order: it is a resting instruction at the broker, placed through
# POST /gtt/triggers, governed by its own rules (500 pending per client, its own
# rejection reasons, its own status vocabulary). A marketable LIMIT goes through
# POST /orders. Two mechanisms on two endpoints with two rule sets cannot share
# a rejection the way SL-M and MARKET did -- which is the failure this axis
# exists to make structurally impossible rather than merely unlikely.
ORDER_SPECS = {
    # ONE two-leg OCO instruction resting at the broker: stop leg and target
    # leg, either firing cancels the other. CNC only -- GTT does not exist for
    # MIS, which is the fact that forced SL-M on 7 October.
    "GTT_OCO":      {"order_type": "GTT",    "needs_trigger": True,
                     "market_protection": False, "immediate": False,
                     "api": "gtt", "product": "CNC"},
    # immediate, priced through the book, no trigger and no market protection
    "LIMIT_THROUGH": {"order_type": "LIMIT", "needs_trigger": False,
                      "market_protection": False, "immediate": True,
                      "api": "orders", "product": "any"},
    # RETIRED 2026-10-12, kept so the independence check can still name it and
    # so the record of what was tried survives. The SL stop built in 954b68e
    # existed because MIS shorts cannot take a GTT. There are no MIS shorts any
    # more, so there is nothing for it to protect.
    "SL":           {"order_type": "SL",     "needs_trigger": True,
                     "market_protection": False, "immediate": False,
                     "api": "orders", "product": "MIS"},
    # kept only so the independence check can name them as the thing NOT to pair
    "SLM":          {"order_type": "SL-M",   "needs_trigger": True,
                     "market_protection": True, "immediate": False,
                     "api": "orders", "product": "MIS"},
    "MARKET":       {"order_type": "MARKET", "needs_trigger": False,
                     "market_protection": True, "immediate": True,
                     "api": "orders", "product": "any"},
}

# WHAT PROTECTS A POSITION, IN TWO PHASES.
#
# A GTT checks the DEMAT account and shares bought today reach demat at T+1, so
# a GTT stop triggering on the purchase day can be rejected for want of
# holdings even though the GTT was accepted. A regular SL order checks the
# ORDER BOOK, which knows about today's buy. Different systems, different
# requirements -- and that difference is the seam:
#
#   DAY 0            SL order, DAY validity, dies at the 16:00 flush
#   PRE-CLOSE 15:20  the two GTTs are placed, and cannot fire today
#   DAY 1 ONWARDS    the GTTs carry the position
#
# No window between them: the GTT is resting before the SL order expires.
PRIMARY_STOP_DAY0 = "SL"
PRIMARY_STOP_OVERNIGHT = "GTT_OCO"
FALLBACK_EXIT = "LIMIT_THROUGH"

# Kept as the name the rest of the codebase and the harnesses already use. It
# means "the mechanism that carries the position", which is the GTT.
PRIMARY_STOP = PRIMARY_STOP_OVERNIGHT


# ── NAMING THE MECHANISM THAT FAILED ─────────────────────────────
#
# WHY THIS EXISTS. The halt reason and the operator alert on a failed stop both
# said "the OCO GTT would not place" regardless of which mechanism had actually
# been tried. That was true when there was one; since the day-0/day-1 split
# there are two, with different causes, and the first real failure would have
# sent the operator to the wrong system:
#
#   SL_DAY0   a regular order on POST /orders. Fails on order-level rules --
#             price bands, trigger range, series restrictions, margin.
#   GTT_OCO   a resting instruction on POST /gtt/triggers. Fails on GTT rules
#             -- the 500-pending cap, DDPI/TPIN authorisation, demat holdings.
#
# A diagnosis that names the wrong one costs the time it takes to rule it out,
# and that time is spent while a position is unprotected.
MECHANISM_LABELS = {
    "SL_DAY0":       ("day-0 stop-loss order", "orders", "DAY0_SL_FAILED"),
    "GTT_OCO":       ("overnight OCO GTT", "gtt", "GTT_FAILED"),
    "GTT_STOP_ONLY": ("overnight stop-only GTT", "gtt", "GTT_FAILED"),
    "GTT_SPLIT":     ("overnight GTT pair", "gtt", "GTT_FAILED"),
    "SL_RETIRED":    ("retired SL path (should be unreachable)", "orders",
                      "RETIRED_PATH_CALLED"),
    "NONE":          ("no mechanism (refused before any order)", "none",
                      "NO_MECHANISM"),
}


def mechanism_label(mechanism: str) -> tuple:
    """(human name, api, halt kind) for whatever protect() reported.

    UNKNOWN IS NAMED, NOT GUESSED. A mechanism string this does not recognise
    returns the string itself rather than being mapped onto the nearest known
    one -- an alert saying "GTT" about something that was not a GTT is the
    defect being fixed, and silently defaulting would reintroduce it the next
    time a mechanism is added.
    """
    m = str(mechanism or "").upper()
    if m in MECHANISM_LABELS:
        return MECHANISM_LABELS[m]
    return (f"unrecognised mechanism {mechanism!r}", "unknown",
            "STOP_FAILED_UNKNOWN_MECHANISM")


def assert_independent(primary: str = None,
                       fallback: str = FALLBACK_EXIT) -> None:
    """Raise unless the two can fail for different reasons.

    FOUR AXES ARE REQUIRED OF EVERY PAIR, each of which was collapsed on
    2026-10-07:

      order_type          SL-M and MARKET are both market-type orders and the
                          market-protection rule rejected both. Sharing the
                          type is sharing the rejection.
      trigger dependency  a resting trigger can be rejected, unaccepted or
                          simply not fire. A fallback that also needs one
                          inherits that.
      immediacy           a fallback that rests is not a fallback: the position
                          is already unprotected when it is called.
      market protection   the actual rule that did the damage. If both legs are
                          subject to it, one rule takes out both.

    AND A FIFTH, REQUIRED ONLY OF A GTT PRIMARY: a GTT fallback. Whatever makes
    /gtt/triggers refuse a two-leg instruction will almost certainly refuse
    another one a moment later, so a GTT may never be backed by a GTT.

    THE DAY-0 PAIR SHARES AN ENDPOINT, AND THAT IS STATED RATHER THAN HIDDEN.
    SL and LIMIT_THROUGH both go through POST /orders. They differ on the four
    required axes -- resting vs immediate, trigger vs none, SL vs LIMIT,
    neither subject to market protection -- so no RULE can reject both. What
    could take out both is the orders endpoint being down entirely, and in that
    case the position is genuinely unprotected and protect_or_exit reports
    exactly that and halts. A broker-wide outage is not a failure this pair can
    engineer away; pretending otherwise would be the real defect.
    """
    primary = primary or PRIMARY_STOP
    a, b = ORDER_SPECS[primary], ORDER_SPECS[fallback]
    # A PRIMARY MUST REST. A stop waits for a price it hopes never comes; a
    # mechanism that executes on placement is an EXIT, not protection. Without
    # this, MARKET + LIMIT_THROUGH passes every other axis -- different type,
    # no shared trigger, immediate fallback, only one market-protection order
    # -- while being two ways of saying "sell it now" and no stop at all.
    if a["immediate"]:
        raise AssertionError(
            f"{primary} executes on placement, so it cannot be a stop. A "
            f"primary must REST and wait for its trigger; something immediate "
            f"is an exit.")
    if a["order_type"] == b["order_type"]:
        raise AssertionError(
            f"exit fallback shares an order_type with the primary "
            f"({a['order_type']}). On 2026-10-07 SL-M and MARKET both failed "
            f"the market-protection rule and the position sat unprotected for "
            f"68 minutes. The fallback must fail for a different reason.")
    if a["needs_trigger"] and b["needs_trigger"]:
        raise AssertionError(
            f"both {primary} and {fallback} depend on a trigger price; a "
            f"fallback must not inherit the primary's trigger failure mode")
    if a["market_protection"] and b["market_protection"]:
        raise AssertionError(
            f"both {primary} and {fallback} are market-protection orders — "
            f"exactly the shared rejection of 2026-10-07")
    if not b["immediate"]:
        raise AssertionError(
            f"{fallback} rests rather than executing; a fallback is called "
            f"when the position is ALREADY unprotected and must not wait")
    if a.get("api") == "gtt" and b.get("api") == "gtt":
        raise AssertionError(
            f"{primary} and {fallback} both go through the GTT API; whatever "
            f"makes /gtt/triggers refuse one instruction will refuse the next, "
            f"which is how SL-M and MARKET collapsed into one layer")


# BOTH PAIRS, AT IMPORT. A bad pair breaks the module, not a position -- and
# both phases are checked, because day 0 and day 1 use different primaries and
# only checking one would leave the other unverified.
assert_independent(PRIMARY_STOP_DAY0)
assert_independent(PRIMARY_STOP_OVERNIGHT)


def stop_limit_price(trigger: float, direction: str) -> float:
    """The SL's limit price, set through the trigger so it fills like a market order.

    DIRECTION IS THE SIDE THAT CLOSES, not the side that opened. A long is closed
    by a SELL, so its limit sits BELOW the trigger -- selling lower guarantees the
    fill. A short is closed by a BUY, so its limit sits ABOVE. Getting this
    backwards produces an order that rests behind the market and never fills,
    which is the unprotected position again wearing a limit price.
    """
    t = float(trigger)
    if str(direction).upper() == "LONG":
        return round_tick(t * (1.0 - STOP_LIMIT_SLIP_PCT))
    return round_tick(t * (1.0 + STOP_LIMIT_SLIP_PCT))


def exit_limit_price(ltp: float, direction: str) -> float:
    """An emergency exit's limit, priced through the book from the last trade."""
    p = float(ltp)
    if str(direction).upper() == "LONG":
        return round_tick(p * (1.0 - EXIT_LIMIT_SLIP_PCT))
    return round_tick(p * (1.0 + EXIT_LIMIT_SLIP_PCT))


def round_tick(p: float) -> float:
    """Round to the exchange tick. A price off-tick is rejected outright."""
    return round(round(float(p) / TICK) * TICK, 2)


class StopWouldLoosen(ValueError):
    """A stop update that widens the stop. Refused, always, everywhere."""


def tighten_only(current: float, proposed: float, direction: str,
                 label: str = "stop") -> float:
    """The ONLY way a stop is allowed to change. -> the new stop, or raises.

    STOPS MOVE UP ONLY (for a long). This is absolute: a stop must never be
    moved down or widened, for any reason, under any condition.

    A STRUCTURAL GUARD, NOT A CONVENTION, and the difference is the whole
    point. "No code moves the stop down" is a property of today's code that the
    next edit can remove without anyone noticing -- which is exactly the shape
    of the 7 October failure, where the guarantee lived in another module's
    behaviour rather than in a local check. This REFUSES. Every stop update in
    the codebase goes through here, so loosening is not something a caller can
    choose to do wrong; it is something they cannot express.

    IT RAISES RATHER THAN RETURNING THE OLD VALUE. Silently keeping the old
    stop would make a loosening attempt invisible -- the caller would believe
    it had moved the stop and the log would show a successful trail. A raise
    stops the operation and names it.

    EQUAL IS ALLOWED and returns the current value: a trail that recomputes the
    same level is a no-op, not an error, and raising on it would make the
    nightly trail fail every time price did nothing.
    """
    d = str(direction).upper()
    cur, new = float(current), float(proposed)
    if d == "LONG":
        if new < cur:
            raise StopWouldLoosen(
                f"refusing to move a LONG {label} DOWN: {cur} -> {new}. "
                f"Stops move up only. A widened stop is a bigger loss "
                f"accepted after the fact, which is the one thing this engine "
                f"must never do.")
        return round_tick(new) if new > cur else round_tick(cur)
    # SHORT kept for symmetry. Shorts are disabled, not deleted, and a stop
    # guard that only understood longs would be the wrong thing to find here
    # if they are ever re-enabled.
    if new > cur:
        raise StopWouldLoosen(
            f"refusing to move a SHORT {label} UP: {cur} -> {new}. "
            f"Stops move toward the position only.")
    return round_tick(new) if new < cur else round_tick(cur)


def target_price(fill: float, stop: float, direction: str,
                 r: float = TARGET_R_MULTIPLE) -> float:
    """
    The 2R level, measured from the ACTUAL FILL, not from the signal's entry_ref.

    A MARKET order slips. Sizing was done against entry_ref, but risk per share
    after the fact is |fill - stop|, so a target computed from entry_ref is not
    2R of the risk actually taken. The stop stays where structure put it -- it is
    a price level, not an offset -- and only the target moves.
    """
    risk = abs(float(fill) - float(stop))
    if risk <= 0:
        raise ValueError(f"zero risk per share: fill {fill} == stop {stop}")
    return round_tick(fill + r * risk if str(direction).upper() == "LONG"
                      else fill - r * risk)


def stop_trigger(stop: float, direction: str) -> float:
    """Trigger just inside the stop, on the side price approaches from."""
    s = float(stop)
    return round_tick(s * (1 - STOP_TRIGGER_BUFFER) if str(direction).upper() == "LONG"
                      else s * (1 + STOP_TRIGGER_BUFFER))


def exit_side(direction: str) -> str:
    """The transaction that CLOSES the position."""
    return "SELL" if str(direction).upper() == "LONG" else "BUY"


# ══════════════════════════════════════════════════════════════════
# FILL DETECTION — blocking, in the same cycle as the entry
# ══════════════════════════════════════════════════════════════════

def await_fill(order_id: str, timeout: float = FILL_POLL_SECONDS,
               interval: float = FILL_POLL_INTERVAL,
               get_status=None, sleep=time.sleep) -> dict:
    """
    Block until the order reaches a terminal state. -> dict with `outcome`:

        FILLED        COMPLETE, with filled_qty and avg_price
        REJECTED      terminal, nothing was bought or sold
        INDETERMINATE the timeout elapsed, or the order is not in the book.
                      NOT the same as rejected: the order may still fill, so the
                      caller must treat it as a position that might exist.

    Blocking is the point. Detecting the fill on the next cycle would leave a
    minute of unprotected exposure on every single entry.
    """
    if get_status is None:
        from atlas.execution.broker import get_order_status as get_status

    deadline = time.monotonic() + float(timeout)
    last = {}
    seen = False
    while True:
        try:
            o = get_status(order_id) or {}
        except Exception as e:
            log.error(f"order status raised for {order_id}: {e}")
            o = {}
        if o:
            seen = True
            last = o
            st = str(o.get("status", "")).upper()
            if st in TERMINAL_OK:
                fq = int(float(o.get("filled_quantity") or 0))
                ap = float(o.get("average_price") or 0)
                if fq <= 0 or ap <= 0:
                    # COMPLETE with no quantity or no price is not a fill we can
                    # size a stop against.
                    return {"outcome": "INDETERMINATE", "order": o,
                            "reason": f"COMPLETE but filled_qty={fq} avg_price={ap}"}
                return {"outcome": "FILLED", "filled_qty": fq, "avg_price": ap,
                        "order": o}
            if st in TERMINAL_BAD:
                return {"outcome": "REJECTED", "order": o,
                        "reason": str(o.get("status_message") or st)}
        if time.monotonic() >= deadline:
            return {"outcome": "INDETERMINATE", "order": last,
                    "reason": (f"no terminal status within {timeout:.0f}s"
                               if seen else
                               f"order {order_id} never appeared in the order book")}
        sleep(interval)


# ══════════════════════════════════════════════════════════════════
# PROTECTION
# ══════════════════════════════════════════════════════════════════

def protect(symbol: str, direction: str, qty: int, product: str,
            fill_price: float, stop_price: float, kite=None,
            place_target: bool = None) -> dict:
    """
    Place the stop and the 2R target for a filled position.

    -> {"ok": bool, "legs": {...}, "mechanism": "GTT_OCO"|"SL", "reason": str}

    ok is False if the STOP could not be placed, whatever happened to the target:
    a position with a target and no stop has unbounded downside, which is the
    state this module exists to prevent. The caller must then exit at market.
    A target failure alone leaves the position protected and is reported, not
    escalated.
    """
    d = str(direction).upper()
    prod = str(product).upper()

    # ── LONGS ONLY, CNC ONLY. ONE MECHANISM. ──────────────────────
    #
    # This function used to dispatch on product: GTT for a CNC long, SL for an
    # MIS short. That branch is the complexity the 2026-10-12 simplification
    # removes, and it is where 7 October came from -- not because the SL path
    # was badly written, but because it existed at all.
    #
    # Anything that is not a CNC long is now REFUSED rather than quietly routed
    # somewhere. A refusal makes the caller exit the position; a silent reroute
    # would put it back on the path being retired.
    if d != "LONG":
        return {"ok": False, "mechanism": "NONE", "legs": {},
                "reason": f"ATLAS is long-only; refusing to protect a {d} "
                          f"position. Nothing should have opened it -- see "
                          f"ALLOW_SHORT_ENTRIES and the broker guard."}
    if prod != "CNC":
        return {"ok": False, "mechanism": "NONE", "legs": {},
                "reason": f"the only protection mechanism is a CNC OCO GTT; "
                          f"product is {prod}. GTT does not exist for MIS, "
                          f"which is the fact that forced SL-M on 2026-10-07."}

    if place_target is None:
        from atlas.config import ALLOW_AUTOMATED_TARGET
        place_target = ALLOW_AUTOMATED_TARGET

    # AN OCO NEEDS BOTH LEGS BY CONSTRUCTION. place_target=False would leave a
    # single resting stop and no OCO at all, so it is refused rather than
    # silently degraded -- the operator asked for the two-leg mechanism they
    # already use, and half of it is a different mechanism.
    if not place_target:
        return {"ok": False, "mechanism": "NONE", "legs": {},
                "reason": "ALLOW_AUTOMATED_TARGET is False, so there is no "
                          "target leg and no OCO. The protection mechanism is "
                          "a TWO-leg GTT; refusing to place half of it."}

    tgt = target_price(fill_price, stop_price, d)
    return _protect_cnc_gtt(symbol, d, qty, fill_price, stop_price, tgt, kite)


def gtt_leg_limit(trigger: float, direction: str) -> float:
    """The limit price for a GTT leg, placed BELOW its trigger for a sell.

    THIS FIXES A STOP THAT WOULD TRIGGER AND NOT FILL. The first version of
    the OCO priced the stop leg AT the stop while the trigger sat 0.1% inside
    it -- trigger 94.90, limit 95.00. Both legs are SELLs, so that put the
    limit ABOVE the trigger: price falls through 94.90, the GTT fires, and a
    SELL LIMIT at 95.00 rests above the market until price comes back up.

    Zerodha names this exact failure: "Your order's Last Traded Price (LTP)
    did not match the limit price you set... position your limit price more
    favorably (above trigger for buys, below trigger for sells)."
    Confirmed 2026-10-13 against the GTT support documentation.

    A STOP THAT TRIGGERS AND DOES NOT FILL IS WORSE THAN NO STOP, because the
    position looks protected. The GTT shows as triggered, the order sits
    unfilled, and nothing says the difference.

    APPLIED TO BOTH LEGS. For the target leg this means selling a little below
    the target trigger rather than exactly at it -- which fills at or above the
    limit in practice, so the realised price is around the target. An
    unfillable target is a missed exit; a marketable one is a slightly worse
    price. The second is strictly better.
    """
    t = float(trigger)
    if str(direction).upper() == "LONG":
        return round_tick(t * (1 - STOP_LIMIT_SLIP_PCT))     # SELL, below
    return round_tick(t * (1 + STOP_LIMIT_SLIP_PCT))         # BUY, above


def _protect_cnc_gtt(symbol, direction, qty, fill, stop, tgt, kite=None) -> dict:
    """ONE two-leg OCO GTT. Stop leg and target leg, either cancelling the other.

    THE MECHANISM THE OPERATOR ALREADY USES BY HAND. One leg firing cancels the
    other AT THE BROKER, which removes the orphan race our own reconciliation
    would otherwise have to win every cycle -- and removes the two-sibling
    bookkeeping that the old GTT_SINGLE fallback required.

    THE SINGLE-GTT FALLBACK IS GONE. It used to place two independent GTTs when
    two-leg was refused, and let reconcile_exits() pair them. That was correct
    and it was also a second mechanism on the SAME endpoint: whatever makes
    /gtt/triggers refuse a two-leg instruction is overwhelmingly likely to
    refuse two single ones a moment later. A fallback that shares its failure
    mode with the primary is exactly what 7 October was. The fallback is now
    the marketable LIMIT in protect_or_exit, on a different API.

    PAYLOAD CONFIRMED against https://kite.trade/docs/connect/v3/gtt/ on
    2026-10-12, not from memory:
        type              "two-leg"   (the SDK's trigger_type)
        condition         {exchange, tradingsymbol, trigger_values[2], last_price}
        orders            two order objects, each {exchange, tradingsymbol,
                          transaction_type, quantity, order_type, product, price}
    The Python SDK takes the condition fields as keyword arguments and
    assembles the condition object itself, which is why they are passed flat
    here rather than nested.

    TRIGGER ORDER MATTERS: trigger_values must be ascending, and the orders
    array is positional against it. For a long, the stop is BELOW and the
    target ABOVE, so [stop_trigger, target] pairs with [stop_leg, target_leg].
    Reversing them would arm the stop at the target price.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "mechanism": "GTT_OCO", "legs": {},
                "reason": "kite unavailable — cannot place the OCO GTT"}
    if tgt is None:
        return {"ok": False, "mechanism": "GTT_OCO", "legs": {},
                "reason": "no target price; an OCO requires two legs"}

    side = exit_side(direction)                      # SELL, for a long
    trig = stop_trigger(stop, direction)
    stop_px, tgt_px = round_tick(stop), round_tick(tgt)
    trig = round_tick(trig)

    # SANITY BEFORE SENDING. A long's stop must sit below its target or the two
    # trigger values are not ascending and the legs pair against the wrong
    # prices. Checked here rather than trusted, because the symptom at the
    # broker would be an accepted GTT armed inside out.
    if not (trig < tgt_px):
        return {"ok": False, "mechanism": "GTT_OCO", "legs": {},
                "reason": f"stop trigger {trig} is not below the target "
                          f"{tgt_px}; refusing to arm an inverted OCO"}

    common = {"exchange": "NSE", "tradingsymbol": symbol,
              "transaction_type": side, "quantity": int(qty),
              "order_type": "LIMIT", "product": "CNC"}
    # EACH LEG'S LIMIT SITS BELOW ITS OWN TRIGGER, because both legs are SELLs.
    # See gtt_leg_limit: pricing the stop leg AT the stop put the limit above
    # the trigger and produced a stop that fires and never fills.
    orders = [{**common, "price": gtt_leg_limit(trig, direction)},
              {**common, "price": gtt_leg_limit(tgt_px, direction)}]

    try:
        gid = kite.place_gtt(trigger_type="two-leg", tradingsymbol=symbol,
                             exchange="NSE",
                             trigger_values=[trig, tgt_px],
                             last_price=round_tick(fill), orders=orders)
        gid = gid.get("trigger_id") if isinstance(gid, dict) else gid
    except Exception as e:
        # NO SECOND GTT ATTEMPT. The caller exits the position on a different
        # API and halts.
        log.error(f"{symbol}: OCO GTT REFUSED ({type(e).__name__}: {e})")
        return {"ok": False, "mechanism": "GTT_OCO", "legs": {},
                "stop": stop_px, "target": tgt_px,
                "reason": f"OCO GTT failed: {type(e).__name__}: {e}"}

    if not gid:
        # A 200 with no trigger_id is not a placed GTT. Treated as a failure
        # rather than recorded as a leg we cannot later cancel or read back.
        log.error(f"{symbol}: place_gtt returned no trigger_id")
        return {"ok": False, "mechanism": "GTT_OCO", "legs": {},
                "stop": stop_px, "target": tgt_px,
                "reason": "place_gtt returned no trigger_id"}

    log.info(f"{symbol}: OCO GTT {gid} — stop trigger {trig} limit {stop_px} / "
             f"target {tgt_px}, {qty} {side} CNC")
    return {"ok": True, "mechanism": "GTT_OCO",
            "legs": {"oco_trigger_id": gid},
            "stop": stop_px, "target": tgt_px, "stop_trigger": trig,
            "reason": f"OCO GTT {gid} (stop {stop_px} / target {tgt_px})"}


def _protect_mis_sl(symbol, direction, qty, fill, stop, tgt, kite=None) -> dict:
    """RETIRED 2026-10-12. Unreachable, kept rather than deleted. DO NOT REVIVE.

    This placed an SL (stop-loss LIMIT) regular order with the limit set past
    the trigger, built in 954b68e to replace the SL-M that Kite refused on
    2026-10-07. It was correct for what it was: the stop an MIS short can have,
    because GTT is CNC-only.

    IT HAS NOTHING LEFT TO PROTECT. ATLAS is long-only and CNC-only, so there
    are no MIS positions, so there is no case where this would be called. The
    operator asked for it to be retired rather than deleted in this pass; it is
    listed in docs/ATLAS_LONG_ONLY.md under dead code.

    It REFUSES rather than placing. Leaving a working implementation here would
    be an invitation: the next person who wants to protect a short finds a
    ready-made path back to the mechanism that failed, and ALLOW_SHORT_ENTRIES
    is the only thing standing between them and it.
    """
    log.error(f"{symbol}: _protect_mis_sl is RETIRED and must not be called. "
              f"ATLAS is long-only and CNC-only; the only protection mechanism "
              f"is a two-leg OCO GTT. Nothing was placed.")
    return {"ok": False, "mechanism": "SL_RETIRED", "legs": {},
            "reason": "the SL stop path is retired — ATLAS is long-only, CNC "
                      "only, and protected by a two-leg OCO GTT"}


def protect_day0(symbol: str, direction: str, qty: int, fill_price: float,
                 stop_price: float, kite=None) -> dict:
    """DAY 0: a regular SL (stop-loss LIMIT) order. Not a GTT.

    WHY NOT A GTT TODAY. A GTT checks the DEMAT account, and shares bought
    today reach demat at T+1. A GTT stop triggering on the purchase day can be
    rejected for want of holdings even though the GTT itself was accepted --
    Zerodha: "If you do not have enough holdings of the specific stock in your
    demat account, the exchange will reject your GTT order." That is a stop
    that looks placed and is not.

    A REGULAR SL ORDER CHECKS THE ORDER BOOK, which knows about today's buy.
    Verified against the live broker rules 2026-10-13 before this was written:
    delivery shares appear under Positions on the purchase day explicitly so
    they can be traded before settlement, and "if your SL is triggered the same
    day, your trade will be considered intraday" -- i.e. it works, and the
    position simply becomes an intraday trade.

    IT DIES AT THE CLOSE, BY DESIGN. "All pending orders are cancelled at the
    end of the day" (equity: 16:00). The pre-close job places the GTTs at 15:20
    while this order is still live, so the GTT is resting before this expires
    and there is no window between them.

    THE LIMIT SITS BELOW THE TRIGGER. Same reason as the GTT legs: a SELL limit
    above its trigger rests above the market and does not fill.
    """
    if str(direction).upper() != "LONG":
        return {"ok": False, "mechanism": "NONE", "legs": {},
                "reason": f"ATLAS is long-only; refusing to protect a "
                          f"{direction} position"}
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "mechanism": "SL_DAY0", "legs": {},
                "reason": "kite unavailable — cannot place the day-0 SL"}

    trig = stop_trigger(stop_price, direction)
    lim = gtt_leg_limit(trig, direction)
    side = exit_side(direction)                      # SELL
    try:
        oid = kite.place_order(
            variety="regular", exchange="NSE", tradingsymbol=symbol,
            transaction_type=side, quantity=int(qty), product="CNC",
            order_type=DAY0_STOP_ORDER_TYPE, trigger_price=trig, price=lim,
            validity="DAY", tag="ATLAS_SL0")
    except Exception as e:
        log.error(f"{symbol}: day-0 SL REFUSED ({type(e).__name__}: {e})")
        return {"ok": False, "mechanism": "SL_DAY0", "legs": {},
                "stop": round_tick(stop_price), "stop_trigger": trig,
                "reason": f"day-0 SL failed: {type(e).__name__}: {e}"}
    if not oid:
        return {"ok": False, "mechanism": "SL_DAY0", "legs": {},
                "reason": "place_order returned no order_id"}

    log.info(f"{symbol}: day-0 SL {oid} — trigger {trig} limit {lim}, "
             f"{qty} {side} CNC, DAY validity")
    return {"ok": True, "mechanism": "SL_DAY0",
            "legs": {"day0_sl_order_id": oid},
            "stop": round_tick(stop_price), "stop_trigger": trig,
            "limit": lim, "target": None,
            "reason": f"day-0 SL {oid} (trigger {trig} / limit {lim})"}


def split_quantity(qty: int, oco_fraction: float = None,
                   odd_to: str = None) -> tuple:
    """(oco_qty, trailed_qty). The 50/50 split, with the odd share named.

    THE ODD SHARE GOES TO THE TRAILED HALF, because that is the half whose
    purpose is to run -- and because at qty=1 the alternative is a position
    with a target and no trailing leg, which is the opposite of the intent.

    AT qty=1 THERE IS NO OCO LEG AT ALL and the stop-only GTT carries the whole
    position. That is a stated outcome, not an edge case: the caller must not
    place a zero-quantity GTT, and place_overnight_gtts skips it.
    """
    from atlas.config import GTT_SPLIT_OCO_FRACTION, GTT_SPLIT_ODD_SHARE_TO
    frac = GTT_SPLIT_OCO_FRACTION if oco_fraction is None else oco_fraction
    odd = (GTT_SPLIT_ODD_SHARE_TO if odd_to is None else odd_to).lower()
    q = int(qty)
    if q <= 0:
        return 0, 0
    oco = int(q * frac)
    trailed = q - oco
    if odd != "trailed" and trailed > oco:
        # The other convention, kept reachable so the choice is config and not
        # a rewrite.
        oco, trailed = trailed, oco
    return oco, trailed


def place_overnight_gtts(symbol: str, direction: str, qty: int,
                         fill_price: float, stop_price: float,
                         kite=None) -> dict:
    """TWO GTTs for one position: an OCO on half, a stop-only GTT on half.

    -> {"ok", "legs", "oco_qty", "trailed_qty", "stop", "target", "reason"}

    WHY TWO AND NOT ONE OCO. An OCO forces a target to be named, which fights
    "winners are held and trailed" (engine/zone_entry.py). The operator's rule
    resolves it by doing both on the same position: half is booked at 2R, half
    runs behind a trailing stop. Zerodha's cap is 500 pending GTTs per client,
    so two per position is irrelevant -- it binds at 250 concurrent positions.

    ok IS TRUE ONLY IF EVERY LEG THAT SHOULD EXIST DOES. A position half
    protected is not protected: the caller treats a partial result as a failure
    and exits, because deciding which half to leave naked is not a decision
    this function is allowed to make.

    THE STOP IS THE SAME PRICE ON BOTH. They diverge later -- the trailed half
    moves up and the OCO half does not -- but they start together, so a
    stop-out before the first trail exits the whole position at one level.
    """
    if str(direction).upper() != "LONG":
        return {"ok": False, "legs": {},
                "reason": f"ATLAS is long-only; refusing {direction}"}
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "legs": {}, "reason": "kite unavailable"}

    oco_qty, trailed_qty = split_quantity(qty)
    if oco_qty + trailed_qty != int(qty):
        return {"ok": False, "legs": {},
                "reason": f"split {oco_qty}+{trailed_qty} does not sum to "
                          f"{qty}"}

    legs, failures = {}, []
    tgt = target_price(fill_price, stop_price, direction)

    # ── the OCO half: stop + target ───────────────────────────────
    if oco_qty > 0:
        r = _protect_cnc_gtt(symbol, direction, oco_qty, fill_price,
                             stop_price, tgt, kite)
        if r.get("ok"):
            legs["oco_trigger_id"] = (r.get("legs") or {}).get("oco_trigger_id")
            legs["oco_qty"] = oco_qty
        else:
            failures.append(f"OCO half ({oco_qty}): {r.get('reason')}")
    else:
        log.info(f"{symbol}: qty {qty} leaves no OCO half; the stop-only GTT "
                 f"carries the whole position")

    # ── the trailed half: stop only, single-leg ───────────────────
    if trailed_qty > 0:
        r2 = _gtt_stop_only(symbol, direction, trailed_qty, fill_price,
                            stop_price, kite)
        if r2.get("ok"):
            legs["trail_trigger_id"] = (r2.get("legs") or {}).get("stop_trigger_id")
            legs["trail_qty"] = trailed_qty
        else:
            failures.append(f"trailed half ({trailed_qty}): {r2.get('reason')}")

    ok = not failures
    if not ok:
        log.error(f"{symbol}: overnight GTTs INCOMPLETE — " + "; ".join(failures))
    return {"ok": ok, "mechanism": "GTT_SPLIT", "legs": legs,
            "oco_qty": oco_qty, "trailed_qty": trailed_qty,
            "stop": round_tick(stop_price), "target": round_tick(tgt),
            "reason": ("; ".join(failures) if failures else
                       f"OCO {oco_qty} (stop {round_tick(stop_price)} / target "
                       f"{round_tick(tgt)}) + trailed {trailed_qty} (stop "
                       f"{round_tick(stop_price)})")}


def _gtt_stop_only(symbol, direction, qty, fill, stop, kite=None) -> dict:
    """A single-leg GTT: stop only, nothing to orphan, free to be trailed.

    THIS IS THE HALF THAT RUNS. No target, so a winner is not capped -- the
    stop follows it up instead, which is what "winners are held and trailed"
    means in practice.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "mechanism": "GTT_STOP_ONLY", "legs": {},
                "reason": "kite unavailable"}
    trig = round_tick(stop_trigger(stop, direction))
    lim = gtt_leg_limit(trig, direction)
    order = {"exchange": "NSE", "tradingsymbol": symbol,
             "transaction_type": exit_side(direction), "quantity": int(qty),
             "order_type": "LIMIT", "product": "CNC", "price": lim}
    try:
        sid = kite.place_gtt(trigger_type="single", tradingsymbol=symbol,
                             exchange="NSE", trigger_values=[trig],
                             last_price=round_tick(fill), orders=[order])
        sid = sid.get("trigger_id") if isinstance(sid, dict) else sid
    except Exception as e:
        log.error(f"{symbol}: stop-only GTT REFUSED ({type(e).__name__}: {e})")
        return {"ok": False, "mechanism": "GTT_STOP_ONLY", "legs": {},
                "reason": f"stop-only GTT failed: {type(e).__name__}: {e}"}
    if not sid:
        return {"ok": False, "mechanism": "GTT_STOP_ONLY", "legs": {},
                "reason": "place_gtt returned no trigger_id"}
    log.info(f"{symbol}: stop-only GTT {sid} — trigger {trig} limit {lim}, "
             f"{qty} CNC, trailed")
    return {"ok": True, "mechanism": "GTT_STOP_ONLY",
            "legs": {"stop_trigger_id": sid},
            "stop": round_tick(stop), "stop_trigger": trig, "target": None,
            "reason": f"stop-only GTT {sid} (trigger {trig})"}


def next_stop(entry: float, current_stop: float, high_since_entry: float,
              last_close: float, swing_low: float | None,
              direction: str = "LONG") -> dict:
    """Where the stop should be after today's close. -> {"stop", "why", "moved"}

    THE RULE, and it is the operator's, verbatim:
        price up 3% from entry  ->  move the stop to the entry price (breakeven)
        thereafter              ->  trail to the most recent swing low as it forms
        price falls through the stop -> the stop fires. The trade was wrong.

    PURE. Takes numbers, returns a number. No broker, no clock, no database --
    so the rule can be tested exhaustively and the nightly job is only
    responsible for fetching the inputs and sending the modification.

    EVERY RESULT GOES THROUGH tighten_only(), so this function CANNOT return a
    looser stop even if the arithmetic above produced one. A swing low can form
    BELOW the current stop in a pullback, and the honest answer is "keep the
    stop where it is", not "follow the swing down".

    MEASURED FROM THE HIGH SINCE ENTRY, not from the last close. A position
    that ran 8% and came back to +1% has earned its breakeven stop; resetting
    on the pullback would hand the gain back.
    """
    d = str(direction).upper()
    from atlas.config import TRAIL_BREAKEVEN_AT_PCT
    e, cur = float(entry), float(current_stop)
    hi = float(high_since_entry)

    # The default message is set per-case below rather than once here: a
    # position already trailed to 103 is not "nothing has earned a move", and
    # a log line that says so misleads the person reading it at 16:00.
    proposed, why = cur, f"stop held at {cur}"

    if d == "LONG":
        gain = (hi / e - 1.0) if e else 0.0
        if gain >= TRAIL_BREAKEVEN_AT_PCT:
            # STAGE 1 -- breakeven. Earned once and never given back.
            if e > proposed:
                proposed, why = e, (f"high {hi} is +{gain*100:.1f}% from entry "
                                    f"{e} (>= {TRAIL_BREAKEVEN_AT_PCT*100:.0f}%) "
                                    f"-> stop to breakeven")
            # STAGE 2 -- the swing low, but only ABOVE breakeven. A swing low
            # below the entry is not a trail, it is a wider stop wearing a
            # structural name.
            if swing_low is not None and float(swing_low) > proposed:
                proposed, why = (float(swing_low),
                                 f"swing low {swing_low} is above the current "
                                 f"stop -> trail up")
            elif swing_low is not None and float(swing_low) <= cur:
                why = (f"stop held at {cur}: swing low {swing_low} is at or "
                       f"below it and is IGNORED — stops move up only")
        elif swing_low is not None and float(swing_low) > cur:
            why = (f"high {hi} is only +{gain*100:.1f}% from entry; breakeven "
                   f"needs +{TRAIL_BREAKEVEN_AT_PCT*100:.0f}% — stop held at "
                   f"{cur}")
    else:
        # Shorts are disabled, not deleted. Mirror the rule so the function is
        # not silently long-only if they return.
        gain = (1.0 - hi / e) if e else 0.0
        if gain >= TRAIL_BREAKEVEN_AT_PCT and e < proposed:
            proposed, why = e, "breakeven (short)"
        if swing_low is not None and float(swing_low) < proposed:
            proposed, why = float(swing_low), "trail down (short)"

    # THE GUARD IS THE LAST THING THAT TOUCHES THE NUMBER. Anything above that
    # produced a looser stop raises here rather than being sent to the broker.
    final = tighten_only(cur, proposed, d, label="trailing stop")
    return {"stop": final, "why": why, "moved": final != round_tick(cur)}


def swing_low_since(bars, bars_width: int = None) -> float | None:
    """The most recent CONFIRMED fractal swing low in `bars`. None if none.

    bars: a sequence of dicts or objects with .low / ["low"], oldest first.

    A 5-BAR FRACTAL CONFIRMS TWO SESSIONS AFTER IT FORMS, and that lag is real
    rather than a defect -- a "swing low" declared on the day it printed is
    just today's low, and it would be relabelled tomorrow. Same convention as
    radar/levels.swing_pivots.
    """
    from atlas.config import TRAIL_SWING_FRACTAL_BARS
    w = int(bars_width or TRAIL_SWING_FRACTAL_BARS)
    half = w // 2
    if half < 1 or bars is None or len(bars) < w:
        return None

    def low_of(b):
        try:
            return float(b["low"] if isinstance(b, dict) else b.low)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None

    lows = [low_of(b) for b in bars]
    if any(v is None for v in lows):
        return None
    found = None
    for i in range(half, len(lows) - half):
        window = lows[i - half:i + half + 1]
        if min(window) == lows[i] and window.index(min(window)) == half:
            found = lows[i]          # keep walking; the LAST one wins
    return round_tick(found) if found is not None else None


def protect_or_exit(symbol: str, direction: str, qty: int, product: str,
                    fill_price: float, stop_price: float, kite=None) -> dict:
    """The primary and its fallback, as ONE call. -> see keys below.

    WHY THIS EXISTS AS A FUNCTION. The relationship between the stop and the
    emergency exit IS the lesson of 2026-10-07: the fallback has to fail for
    reasons the primary cannot. When that relationship is spelled out at the call
    site -- "if not prot.ok: emergency_exit(...)" in atlas_entry -- it is a
    convention, and a convention is exactly what a future edit breaks silently.
    Here it is a unit: one place that chooses the primary, one place that chooses
    the fallback, one assert_independent() standing between them, and one thing
    to point a test at.

    THE ORDER OF THESE THREE LINES IS THE WHOLE DESIGN:
      1. assert the two mechanisms are independent     -- before anything is sent
      2. try the primary (a resting, triggered SL)
      3. on failure, try the fallback (an immediate, untriggered LIMIT)
    Step 1 cannot be skipped by a caller, because the caller no longer chooses the
    pair.

    -> {"protected": bool,   the GTT is resting -- nothing further is needed
        "exited":    bool,   no GTT, but the position is closed
        "unprotected": bool, open with no GTT AND no exit -- operator incident
        "must_halt": bool,   TRUE whenever the primary failed, even if the
                             fallback worked. See below.
        "prot": {...},       what protect() returned
        "exit": {...}|None,  what emergency_exit() returned, if it was reached
        "mechanism": str, "reason": str}

    MUST_HALT IS SET WHENEVER THE GTT DID NOT PLACE, successful fallback or
    not. A failed GTT means the only protection mechanism is not working, and
    the next entry would be opened into the same condition -- so ATLAS stops
    taking entries even though this particular position was closed cleanly.
    Halting only when BOTH failed, which is what the previous shape did, treats
    a working fallback as evidence that the primary is fine.

    It does not alert and does not halt ITSELF. The flag is returned and the
    caller acts on it, because only the caller knows the trade id and what to
    write to the ledger. A flag rather than a convention: a caller that ignores
    it is a test failure, not a silent omission.
    """
    # BEFORE EITHER ORDER IS BUILT. A pair that shares a failure mode must break
    # here, where no position exists yet, rather than at the moment the fallback
    # is needed -- which is the one moment there is no time to notice.
    # BOTH PHASES CHECKED. This runs on fill, so the primary here is the DAY-0
    # mechanism -- but the overnight pair is asserted too, because a GTT that
    # cannot be paired safely must break the entry rather than be discovered at
    # 15:20 with a position already open.
    assert_independent(PRIMARY_STOP_DAY0)
    assert_independent(PRIMARY_STOP_OVERNIGHT)

    # DAY 0 IS AN ORDER, NOT A GTT. The GTT is placed before the close by
    # atlas/execution/preclose_gtt.py, while this order is still live.
    prot = protect_day0(symbol=symbol, direction=direction, qty=qty,
                        fill_price=fill_price, stop_price=stop_price, kite=kite)
    if prot.get("ok"):
        return {"protected": True, "exited": False, "unprotected": False,
                "must_halt": False, "prot": prot, "exit": None,
                "mechanism": prot.get("mechanism", ""),
                "reason": ""}

    # THE STOP COULD NOT BE PLACED. The fallback runs now, not after a retry: an
    # unprotected open position is an unbounded loss in a system nobody is
    # watching, and the bad exit costs a known amount once.
    #
    # IT IS A DIFFERENT KIND OF ORDER, NOT THE SAME ORDER AGAIN. On 2026-10-07
    # the stop was SL-M and the fallback was MARKET: both market-type, both
    # refused by the same rule, for the same reason, seconds apart -- two layers
    # of protection that were one layer twice. What follows shares none of the
    # primary's three dependencies: no trigger price, no resting order, and no
    # market-protection requirement.
    # NAMES THE ACTUAL PRIMARY. This said "rather than the gtt API that just
    # refused us" regardless of which mechanism had failed -- wrong on day 0,
    # where the primary is an SL on the orders API. A log line that misnames
    # the thing that broke sends the next reader to the wrong place.
    _p = ORDER_SPECS[PRIMARY_STOP_DAY0]
    _f = ORDER_SPECS[FALLBACK_EXIT]
    _same_api = _p["api"] == _f["api"]
    log.error(
        f"{symbol}: DAY-0 STOP FAILED ({prot.get('reason')}) — exiting NOW "
        f"with an immediate {_f['order_type']} through the book. The "
        f"{_p['order_type']} that refused us needed a trigger and rested; this "
        f"needs neither"
        + (f", though both go through the {_f['api']} API — see "
           f"assert_independent" if _same_api else
           f", and goes through the {_f['api']} API rather than "
           f"{_p['api']}")
        + ". ATLAS will self-halt: the protection mechanism is not working.")
    ex = emergency_exit(symbol, direction, qty, product,
                        why=f"stop placement failed: {prot.get('reason')}",
                        kite=kite)
    if ex.get("ok"):
        return {"protected": False, "exited": True, "unprotected": False,
                "must_halt": True, "prot": prot, "exit": ex,
                "mechanism": f"{prot.get('mechanism', '?')}->FALLBACK_EXIT",
                "reason": prot.get("reason", "")}
    return {"protected": False, "exited": False, "unprotected": True,
            "must_halt": True, "prot": prot, "exit": ex, "mechanism": "NONE",
            "reason": (f"the OCO GTT failed ({prot.get('reason')}) AND the "
                       f"fallback exit failed ({ex.get('reason')})")}


def emergency_exit(symbol: str, direction: str, qty: int, product: str,
                   why: str, kite=None) -> dict:
    """
    Close with a marketable LIMIT because the position could not be protected.

    DELIBERATELY NOT A RETRY. An unprotected open position is worse than a bad
    exit: the exit costs a known amount once, the unprotected position is an
    unbounded loss in a system nobody is watching. The exit price is not the thing
    being optimised.

    If the exit ITSELF fails there is nothing further this function can do -- the
    position is open, unprotected, and the broker is not accepting orders. That is
    an operator incident: it returns ok=False and the caller halts new entries and
    alerts. It does not loop.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "reason": "kite unavailable — POSITION IS OPEN "
                                       "AND UNPROTECTED"}
    # ── WHAT THIS DOES NOT SHARE WITH THE PRIMARY ───────────────────────
    # The stop is an SL: a RESTING order, with a TRIGGER price, limit-priced.
    # This is an immediate LIMIT: no trigger, nothing resting, and not a
    # market-type order, so the market-protection rule that rejected both legs on
    # 2026-10-07 cannot apply to it. assert_independent() enforces all three
    # differences at import, so a future edit that turns this back into a MARKET
    # order -- or gives it a trigger -- breaks the module instead of the position.
    #
    # It is PRICED THROUGH THE BOOK rather than at the last trade: a limit AT the
    # LTP is a resting order in disguise. 1% through, wider than the stop's 0.5%,
    # because this runs when the position is already unprotected and a missed fill
    # is the thing being escaped.
    assert_independent()
    side = exit_side(direction)
    try:
        from atlas.execution.broker import get_ltp
        # THE SAME SESSION THAT WILL PLACE THE ORDER. The limit price and the
        # order must come from one broker session or the exit can be priced by a
        # session that is not the one placing it.
        ltp = float(get_ltp(symbol, kite=kite) or 0)
    except Exception as e:
        ltp = 0.0
        log.error(f"{symbol}: LTP unreadable for the emergency exit ({e})")
    if ltp <= 0:
        # NO PRICE, NO LIMIT ORDER. Falling back to MARKET here would reinstate
        # the shared failure mode this function exists to remove, so it reports
        # the position as unprotected and lets the caller alert a human.
        log.critical(f"{symbol}: EMERGENCY EXIT CANNOT PRICE ITSELF — no LTP. "
                     f"POSITION IS OPEN AND UNPROTECTED")
        return {"ok": False, "order_id": None,
                "reason": "emergency exit could not read an LTP to price a limit "
                          "order — POSITION IS OPEN AND UNPROTECTED"}
    lim = exit_limit_price(ltp, direction)
    log.error(f"{symbol}: EMERGENCY EXIT — {side} {qty} LIMIT {lim} "
              f"(ltp {ltp}, {EXIT_LIMIT_SLIP_PCT*100:.1f}% through) ({why})")
    try:
        oid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty),
                               product=str(product).upper(),
                               order_type=ORDER_SPECS[FALLBACK_EXIT]["order_type"],
                               price=lim, validity="DAY",
                               tag="ATLAS_EMERGENCY")
        return {"ok": True, "order_id": oid, "limit_price": lim, "ltp": ltp,
                "reason": f"exited at LIMIT {lim} through the book: {why}"}
    except Exception as e:
        log.critical(f"{symbol}: EMERGENCY EXIT FAILED ({e}) — POSITION IS OPEN "
                     f"AND UNPROTECTED")
        return {"ok": False, "order_id": None,
                "reason": f"emergency exit failed: {e} — POSITION IS OPEN AND "
                          f"UNPROTECTED"}


# ══════════════════════════════════════════════════════════════════
# SIBLING RECONCILIATION — driven by broker quantity, never by our record
# ══════════════════════════════════════════════════════════════════

def discover_legs(symbol: str, orders: list, atlas_gtts: list) -> dict:
    """
    The resting exit legs for `symbol`, found at the BROKER rather than in our row.

    TWO SOURCES, because the two mechanisms are recognisable in different ways:

      regular orders  their TAG survives in the order book, so ATLAS_SL and
                      ATLAS_TGT identify our MIS legs directly.
      GTTs            Kite's get_gtts() OMITS the tag field entirely, so a GTT can
                      only be recognised via atlas_trades.gtt_trigger_id --
                      gtt.list_atlas_gtts() does that join, and its output is what
                      `atlas_gtts` must be. NEVER infer ownership of a GTT from the
                      symbol alone: the account may hold that stock for reasons
                      that have nothing to do with ATLAS, and cancelling a human's
                      stop is worse than leaving our own orphan.

    Nothing is persisted per-leg deliberately. The broker is the authority for what
    is resting, and a record of legs written before a fill is exactly the record
    that goes stale when something closes the position another way.
    """
    legs = {}
    for o in orders or []:
        if str(o.get("tradingsymbol")) != str(symbol):
            continue
        if str(o.get("status", "")).upper() in ("COMPLETE", "CANCELLED", "REJECTED"):
            continue
        tag = str(o.get("tag") or "")
        if tag == "ATLAS_SL":
            legs["stop_order_id"] = o.get("order_id")
        elif tag == "ATLAS_TGT":
            legs["target_order_id"] = o.get("order_id")
    for g in atlas_gtts or []:
        cond = g.get("condition") or {}
        if str(cond.get("tradingsymbol") or g.get("tradingsymbol")) != str(symbol):
            continue
        legs["stop_trigger_id"] = g.get("id")
    return legs


def reconcile_exits(trades: list, kite=None, positions=None, orders=None,
                    atlas_gtts=None) -> dict:
    """
    For each protected position, cancel the leg that is now orphaned.

    DRIVEN BY BROKER POSITION QUANTITY, not by which leg we think fired. If the
    target fills and we cancel "the stop" from our own record, we are trusting a
    record written before the fill; if the position was closed some other way --
    manually, or by the exchange squaring off MIS at 15:20 -- our record says
    nothing fired and both legs are left resting. An orphaned trigger can
    RE-ENTER a symbol that is already closed, which is the worst outcome
    available here, so quantity at the broker is the only authority.

    Returns counts plus a list of things an operator has to know about.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    out = {"checked": 0, "closed": 0, "cancelled": 0, "partial": 0,
           "external": 0, "alerts": []}
    if kite is None:
        out["alerts"].append("kite unavailable — exits not reconciled this cycle")
        return out

    try:
        pos = positions if positions is not None else kite.positions().get("net", [])
    except Exception as e:
        out["alerts"].append(f"positions unreadable ({e}) — exits not reconciled")
        return out
    try:
        book = orders if orders is not None else kite.orders()
    except Exception as e:
        book = []
        out["alerts"].append(f"order book unreadable ({e}) — leg status unknown")

    qty_by = {}
    for p in pos or []:
        qty_by[str(p.get("tradingsymbol"))] = int(float(p.get("quantity") or 0))
    status_by = {str(o.get("order_id")): str(o.get("status", "")).upper()
                 for o in book or []}

    for t in trades or []:
        sym = str(t.get("symbol"))
        out["checked"] += 1
        held = qty_by.get(sym)
        if held is None:
            # Not in the position book at all. On the day of entry that means
            # flat; we do NOT infer it means flat in general, because an
            # unreadable book is handled above and a missing symbol after a
            # closed session is normal.
            held = 0
        # Prefer what the BROKER is showing; fall back to whatever the caller
        # passed. A restart loses our in-memory legs and must still reconcile.
        legs = discover_legs(sym, book, atlas_gtts or [])
        if not legs:
            legs = {k: v for k, v in (t.get("exit_legs") or {}).items() if v}
        if held != 0:
            # Still open. If one leg has already filled, the other must be
            # REDUCED to what remains rather than cancelled -- cancelling would
            # strip protection from the residual position.
            filled = [k for k, v in legs.items()
                      if status_by.get(str(v)) == "COMPLETE"]
            if filled:
                out["partial"] += 1
                out["alerts"].append(
                    f"{sym}: {', '.join(filled)} filled but {abs(held)} still "
                    f"open — remaining quantity needs its leg resized, not cancelled")
            continue

        # Flat at the broker: every remaining leg is an orphan.
        out["closed"] += 1
        any_filled = any(status_by.get(str(v)) == "COMPLETE" for v in legs.values())
        if not any_filled and legs:
            out["external"] += 1
            out["alerts"].append(
                f"{sym}: flat at the broker with no leg filled — closed "
                f"externally (manual, or MIS square-off). Cancelling both legs.")
        for name, ident in legs.items():
            if status_by.get(str(ident)) in ("COMPLETE", "CANCELLED", "REJECTED"):
                continue
            try:
                if "trigger_id" in name:
                    kite.delete_gtt(trigger_id=ident)
                else:
                    kite.cancel_order(variety="regular", order_id=ident)
                out["cancelled"] += 1
            except Exception as e:
                # An orphan that will not cancel can re-enter a closed position.
                out["alerts"].append(
                    f"{sym}: could not cancel {name}={ident} ({e}) — an orphaned "
                    f"trigger can RE-ENTER a closed position. Cancel it by hand.")
    return out


# ══════════════════════════════════════════════════════════════════
# MIS TIME EXIT
# ══════════════════════════════════════════════════════════════════

# FROM CONFIG, NOT FROM HERE. These were 15, 15 and "15:20" -- a time three
# minutes PAST the broker's real 15:12 cutoff, and a backstop time that had moved
# twice without this line hearing about it. The segment cutoffs, the margin and
# the resulting exit time now live in atlas/config.py with the date they were
# verified, so there is one place to change and one place to read.
from atlas.config import DAY0_STOP_ORDER_TYPE  # noqa: E402
from atlas.config import (MIS_EXIT_HOUR, MIS_EXIT_MIN, MIS_EXIT_TIME,
                          MIS_BROKER_CUTOFF, MIS_EXIT_MARGIN_MIN)

MIS_BROKER_SQUAREOFF = MIS_BROKER_CUTOFF   # Zerodha's, at market


def squareoff_mis(trades: list, now_ist=None, kite=None) -> dict:
    """
    Close every MIS position at MIS_EXIT_TIME, before the broker's cutoff.

    NOT COSMETIC. Past the cutoff Zerodha squares off at market and we lose both
    the exit price and the audit trail -- the fill appears with no order of ours
    behind it, so the trade's own record cannot say why it closed or at what.
    Exiting with margin keeps the decision, and the reason for it, ours.

    THE CUTOFF IS ALSO AN ORDER CUTOFF, which is what the old 15:15 missed: after
    it, an MIS exit order is REJECTED, so arriving late is not "a few minutes of
    extra risk", it is no exit at all. out["late"] says the window was missed
    whatever the attempts then return.

    Intended to run from its own timer, NOT from inside the market-hours loop: a
    crashed loop must not be able to strand a short into broker liquidation.
    """
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = now_ist or datetime.now(IST)
    out = {"due": False, "late": False, "exited": 0, "failed": 0, "alerts": []}
    if (now.hour, now.minute) < (MIS_EXIT_HOUR, MIS_EXIT_MIN):
        return out
    out["due"] = True

    # PAST THE CUTOFF. Still attempt -- a non-CAS equity has until 15:25 and the
    # attempt costs nothing -- but say so first and unconditionally, because a
    # rejected exit here looks identical to a rejected exit for any other reason
    # and the distinction is the whole lesson of 2026-10-07.
    cutoff = tuple(int(x) for x in MIS_BROKER_CUTOFF.split(":"))
    if (now.hour, now.minute) >= cutoff:
        out["late"] = True
        out["alerts"].append(
            f"MIS square-off ran at {now:%H:%M} IST, AT OR PAST the "
            f"{MIS_BROKER_CUTOFF} broker cutoff (it should run at "
            f"{MIS_EXIT_TIME}, {MIS_EXIT_MARGIN_MIN}min early). Exit orders may "
            f"be rejected outright with \"Intraday orders (MIS) are allowed only "
            f"till {MIS_BROKER_CUTOFF}\" -- attempting anyway; any open position "
            f"is now the broker's to liquidate.")
    for t in trades or []:
        if str(t.get("product", "")).upper() != "MIS":
            continue
        r = emergency_exit(str(t.get("symbol")), str(t.get("direction")),
                           int(t.get("qty") or 0), "MIS",
                           why=f"MIS time exit before the {MIS_BROKER_SQUAREOFF} "
                               f"broker square-off", kite=kite)
        if r.get("ok"):
            out["exited"] += 1
        else:
            out["failed"] += 1
            out["alerts"].append(f"{t.get('symbol')}: MIS time exit FAILED "
                                 f"({r.get('reason')}) — the broker will square "
                                 f"it off at {MIS_BROKER_SQUAREOFF} at market")
    return out
