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

TERMINAL_OK = ("COMPLETE",)
TERMINAL_BAD = ("REJECTED", "CANCELLED")


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

    -> {"ok": bool, "legs": {...}, "mechanism": "GTT_OCO"|"SLM", "reason": str}

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
    return _protect_mis_slm(symbol, d, qty, fill_price, stop_price, tgt, kite)


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


def _protect_mis_slm(symbol, direction, qty, fill, stop, tgt, kite=None) -> dict:
    """
    A MIS short: SL-M stop plus a LIMIT target, both DAY validity.

    SL-M, not SL. An SL is a limit order on trigger and can go unfilled in exactly
    the fast move it exists to escape; SL-M becomes a market order and gets out.
    Slippage on a stop is a cost, an unfilled stop is an open-ended loss.

    DAY validity is sufficient here and only here: the position cannot outlive the
    session, because squareoff_mis() closes it at 15:15 and the exchange would do
    it at 15:20 regardless.
    """
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        return {"ok": False, "reason": "kite unavailable", "legs": {}}

    side = exit_side(direction)
    trig = stop_trigger(stop, direction)
    out = {"mechanism": "SLM", "legs": {}, "stop": round_tick(stop),
           "target": round_tick(tgt) if tgt is not None else None}
    try:
        sid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty), product="MIS",
                               order_type="SL-M", trigger_price=trig,
                               validity="DAY", tag="ATLAS_SL")
        out["legs"]["stop_order_id"] = sid
    except Exception as e:
        out["ok"] = False
        out["reason"] = f"SL-M failed: {e}"
        return out
    if tgt is None:
        out["ok"] = True
        out["reason"] = "SL-M stop placed; no target (held and trailed)"
        return out
    try:
        tid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty), product="MIS",
                               order_type="LIMIT", price=round_tick(tgt),
                               validity="DAY", tag="ATLAS_TGT")
        out["legs"]["target_order_id"] = tid
    except Exception as e:
        out["reason"] = f"SL-M placed, target LIMIT failed: {e}"
        out["ok"] = True
        return out
    out["ok"] = True
    out["reason"] = "SL-M stop and LIMIT target placed"
    return out


# ══════════════════════════════════════════════════════════════════
# THE FAILURE PATH
# ══════════════════════════════════════════════════════════════════

def emergency_exit(symbol: str, direction: str, qty: int, product: str,
                   why: str, kite=None) -> dict:
    """
    Close at MARKET because the position could not be protected.

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
    side = exit_side(direction)
    log.error(f"{symbol}: EMERGENCY EXIT at market ({why})")
    try:
        oid = kite.place_order(variety="regular", exchange="NSE",
                               tradingsymbol=symbol, transaction_type=side,
                               quantity=int(qty),
                               product=str(product).upper(),
                               order_type="MARKET", validity="DAY",
                               tag="ATLAS_EMERGENCY")
        return {"ok": True, "order_id": oid,
                "reason": f"exited at market: {why}"}
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

MIS_EXIT_HOUR, MIS_EXIT_MIN = 15, 15     # ours
MIS_BROKER_SQUAREOFF = "15:20"           # Zerodha's, at market


def squareoff_mis(trades: list, now_ist=None, kite=None) -> dict:
    """
    Close every MIS position at 15:15, before the broker does it at 15:20.

    NOT COSMETIC. After 15:20 Zerodha squares off at market and we lose both the
    exit price and the audit trail -- the fill appears with no order of ours behind
    it, so the trade's own record cannot say why it closed. Exiting five minutes
    early keeps the decision, and the reason for it, ours.

    Intended to run from its own timer, NOT from inside the market-hours loop: a
    crashed loop must not be able to strand a short into broker liquidation, and
    the loop currently stops at 15:20 anyway.
    """
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now = now_ist or datetime.now(IST)
    out = {"due": False, "exited": 0, "failed": 0, "alerts": []}
    if (now.hour, now.minute) < (MIS_EXIT_HOUR, MIS_EXIT_MIN):
        return out
    out["due"] = True
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
