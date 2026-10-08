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
ORDER_SPECS = {
    # resting at the exchange, fires on a trigger, limit-priced
    "SL":           {"order_type": "SL",     "needs_trigger": True,
                     "market_protection": False, "immediate": False},
    # immediate, priced through the book, no trigger and no market protection
    "LIMIT_THROUGH": {"order_type": "LIMIT", "needs_trigger": False,
                      "market_protection": False, "immediate": True},
    # kept only so the independence check can name it as the thing NOT to pair
    "SLM":          {"order_type": "SL-M",   "needs_trigger": True,
                     "market_protection": True, "immediate": False},
    "MARKET":       {"order_type": "MARKET", "needs_trigger": False,
                     "market_protection": True, "immediate": True},
}

# What protects a position, and what is used when that fails.
PRIMARY_STOP = "SL"
FALLBACK_EXIT = "LIMIT_THROUGH"


def assert_independent(primary: str = PRIMARY_STOP,
                       fallback: str = FALLBACK_EXIT) -> None:
    """Raise unless the two can fail for different reasons.

    THREE WAYS THEY MUST DIFFER, each of which was collapsed on 2026-10-07:

      order_type          SL-M and MARKET are both market-type orders and the
                          market-protection rule rejected both. Sharing the type
                          is sharing the rejection.
      trigger dependency  a resting trigger can be rejected, unaccepted or simply
                          not fire. A fallback that also needs one inherits that.
      immediacy           a fallback that rests is not a fallback: the position is
                          already unprotected when it is called.
    """
    a, b = ORDER_SPECS[primary], ORDER_SPECS[fallback]
    if a["order_type"] == b["order_type"]:
        raise AssertionError(
            f"exit fallback shares an order_type with the primary "
            f"({a['order_type']}). On 2026-10-07 SL-M and MARKET both failed the "
            f"market-protection rule and the position sat unprotected for 68 "
            f"minutes. The fallback must fail for a different reason.")
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
            f"{fallback} rests rather than executing; a fallback is called when "
            f"the position is ALREADY unprotected and must not wait")


assert_independent()      # at import: a bad pair breaks the module, not a trade


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
    if place_target is None:
        from atlas.config import ALLOW_AUTOMATED_TARGET
        place_target = ALLOW_AUTOMATED_TARGET
    # None means stop only: the position is held and trailed rather than capped
    # at 2R. See ALLOW_AUTOMATED_TARGET -- a strategy choice, not a safety one.
    tgt = target_price(fill_price, stop_price, d) if place_target else None
    if str(product).upper() == "CNC":
        return _protect_cnc_gtt(symbol, d, qty, fill_price, stop_price, tgt, kite)
    return _protect_mis_sl(symbol, d, qty, fill_price, stop_price, tgt, kite)


def _protect_cnc_gtt(symbol, direction, qty, fill, stop, tgt, kite=None) -> dict:
    """
    A LONG held overnight needs a stop that survives the close, so: GTT.

    TWO-LEG OCO IF THE ACCOUNT HAS IT. One leg firing cancels the other at the
    broker, which removes the race our own reconciliation would otherwise have to
    win every cycle. If the API refuses two-leg, fall back to two SINGLE GTTs and
    let reconcile_exits() pair them -- correct, just with a window.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "reason": "kite unavailable", "legs": {}}

    side = exit_side(direction)
    trig = stop_trigger(stop, direction)
    common = {"exchange": "NSE", "tradingsymbol": symbol,
              "transaction_type": side, "quantity": int(qty),
              "order_type": "LIMIT", "product": "CNC"}
    # A GTT leg is a LIMIT order placed when the trigger fires. The stop leg is
    # priced AT the stop rather than at the trigger, so a gap through the trigger
    # still works the order at the level structure defined.
    legs = [{**common, "price": round_tick(stop)}]
    if tgt is not None:
        legs.append({**common, "price": round_tick(tgt)})

    if tgt is None:
        # Stop only: a single GTT, nothing to pair with and nothing to cancel.
        try:
            sid = kite.place_gtt(trigger_type="single", tradingsymbol=symbol,
                                 exchange="NSE", trigger_values=[trig],
                                 last_price=round_tick(fill), orders=legs)
            sid = sid.get("trigger_id") if isinstance(sid, dict) else sid
            return {"ok": True, "mechanism": "GTT_STOP_ONLY",
                    "legs": {"stop_trigger_id": sid},
                    "stop": round_tick(stop), "target": None,
                    "reason": f"stop GTT {sid}; no target (held and trailed)"}
        except Exception as e:
            return {"ok": False, "mechanism": "GTT_STOP_ONLY", "legs": {},
                    "stop": round_tick(stop), "target": None,
                    "reason": f"stop GTT failed: {e}"}

    try:
        gid = kite.place_gtt(trigger_type="two-leg", tradingsymbol=symbol,
                             exchange="NSE", trigger_values=[trig, round_tick(tgt)],
                             last_price=round_tick(fill), orders=legs)
        gid = gid.get("trigger_id") if isinstance(gid, dict) else gid
        log.info(f"{symbol}: OCO GTT {gid} stop {stop:.2f} / target {tgt:.2f}")
        return {"ok": True, "mechanism": "GTT_OCO", "legs": {"oco_trigger_id": gid},
                "stop": round_tick(stop), "target": round_tick(tgt),
                "reason": f"OCO GTT {gid}"}
    except Exception as e:
        log.warning(f"{symbol}: two-leg GTT refused ({e}) — falling back to "
                    f"two single GTTs")

    out = {"mechanism": "GTT_SINGLE", "legs": {},
           "stop": round_tick(stop), "target": round_tick(tgt)}
    try:
        sid = kite.place_gtt(trigger_type="single", tradingsymbol=symbol,
                             exchange="NSE", trigger_values=[trig],
                             last_price=round_tick(fill), orders=[legs[0]])
        out["legs"]["stop_trigger_id"] = (sid.get("trigger_id")
                                          if isinstance(sid, dict) else sid)
    except Exception as e:
        out["ok"] = False
        out["reason"] = f"stop GTT failed: {e}"
        return out
    try:
        tid = kite.place_gtt(trigger_type="single", tradingsymbol=symbol,
                             exchange="NSE", trigger_values=[round_tick(tgt)],
                             last_price=round_tick(fill), orders=[legs[1]])
        out["legs"]["target_trigger_id"] = (tid.get("trigger_id")
                                            if isinstance(tid, dict) else tid)
    except Exception as e:
        # Stop is placed; the position is protected. Not an escalation.
        out["reason"] = f"stop placed, target GTT failed: {e}"
        out["ok"] = True
        return out
    out["ok"] = True
    out["reason"] = "stop and target GTTs placed"
    return out


def _protect_mis_sl(symbol, direction, qty, fill, stop, tgt, kite=None) -> dict:
    """
    A MIS short: SL stop (limit on trigger) plus a LIMIT target, both DAY validity.

    WAS SL-M, AND THE OLD REASONING IS KEPT HERE BECAUSE IT WAS NOT WRONG, ONLY
    UNAVAILABLE. The argument for SL-M was that an SL is a limit order on trigger
    and can go unfilled in exactly the fast move it exists to escape. That is true.
    But BSE has discontinued SL-M across equity, equity derivatives, currency and
    commodity, Zerodha blocks it for index options, and on 2026-10-07 this exact
    call was rejected -- so the choice was never between SL-M and SL, it was
    between SL and nothing.

    THE UNFILLED-LIMIT RISK IS ANSWERED BY THE LIMIT PRICE, not by the order type.
    stop_limit_price() sets the limit 0.5% THROUGH the trigger on the closing side,
    which is Zerodha's own published guidance for making an SL behave like an
    SL-M. A fill 0.5% worse than the trigger is a cost; the band is there so the
    order is not rejected by Limit Price Protection.

    NO market_protection HERE. It is a parameter of MARKET and SL-M only. Sending
    it on an SL is sending a field the order type does not take.

    DAY validity is sufficient here and only here: the position cannot outlive the
    session, because squareoff_mis() closes it before the broker's cutoff.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "reason": "kite unavailable", "legs": {}}

    side = exit_side(direction)
    trig = stop_trigger(stop, direction)
    lim = stop_limit_price(trig, direction)
    out = {"mechanism": "SL", "legs": {}, "stop": round_tick(stop),
           "trigger": trig, "stop_limit": lim,
           "target": round_tick(tgt) if tgt is not None else None}
    try:
        sid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty), product="MIS",
                               order_type=ORDER_SPECS[PRIMARY_STOP]["order_type"],
                               trigger_price=trig, price=lim,
                               validity="DAY", tag="ATLAS_SL")
        out["legs"]["stop_order_id"] = sid
    except Exception as e:
        out["ok"] = False
        out["reason"] = f"SL stop failed: {e}"
        return out
    if tgt is None:
        out["ok"] = True
        out["reason"] = "SL stop placed; no target (held and trailed)"
        return out
    try:
        tid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty), product="MIS",
                               order_type="LIMIT", price=round_tick(tgt),
                               validity="DAY", tag="ATLAS_TGT")
        out["legs"]["target_order_id"] = tid
    except Exception as e:
        out["reason"] = f"SL stop placed, target LIMIT failed: {e}"
        out["ok"] = True
        return out
    out["ok"] = True
    out["reason"] = "SL stop and LIMIT target placed"
    return out


# ══════════════════════════════════════════════════════════════════
# THE FAILURE PATH
# ══════════════════════════════════════════════════════════════════

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

    -> {"protected": bool,   the stop is resting -- nothing further is needed
        "exited":    bool,   no stop, but the position is closed
        "unprotected": bool, open with no stop AND no exit -- operator incident
        "prot": {...},       what protect() returned
        "exit": {...}|None,  what emergency_exit() returned, if it was reached
        "mechanism": str, "reason": str}

    It does not alert and does not halt. Those are the caller's, because only the
    caller knows the trade id and what to write to the ledger.
    """
    # BEFORE EITHER ORDER IS BUILT. A pair that shares a failure mode must break
    # here, where no position exists yet, rather than at the moment the fallback
    # is needed -- which is the one moment there is no time to notice.
    assert_independent()

    prot = protect(symbol=symbol, direction=direction, qty=qty, product=product,
                   fill_price=fill_price, stop_price=stop_price, kite=kite)
    if prot.get("ok"):
        return {"protected": True, "exited": False, "unprotected": False,
                "prot": prot, "exit": None,
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
    log.error(f"{symbol}: stop placement FAILED ({prot.get('reason')}) — "
              f"falling back to an immediate "
              f"{ORDER_SPECS[FALLBACK_EXIT]['order_type']} exit, which shares "
              f"no failure mode with the "
              f"{ORDER_SPECS[PRIMARY_STOP]['order_type']} that just failed")
    ex = emergency_exit(symbol, direction, qty, product,
                        why=f"stop placement failed: {prot.get('reason')}",
                        kite=kite)
    if ex.get("ok"):
        return {"protected": False, "exited": True, "unprotected": False,
                "prot": prot, "exit": ex,
                "mechanism": f"{prot.get('mechanism', '?')}->FALLBACK_EXIT",
                "reason": prot.get("reason", "")}
    return {"protected": False, "exited": False, "unprotected": True,
            "prot": prot, "exit": ex, "mechanism": "NONE",
            "reason": (f"stop failed ({prot.get('reason')}) AND the fallback "
                       f"exit failed ({ex.get('reason')})")}


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
