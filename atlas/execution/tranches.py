"""
TRANCHES 2 AND 3 -- resting BUY GTTs for the long book (operator, 2026-11-10).
==============================================================================

WHAT IT DOES. After tranche 1 has FILLED, two single-leg BUY GTTs are placed at
the ladder prices atlas/sizing.py computed: T2 and T3, each a LIMIT buy that
fires when the price falls to its trigger. They average into a position the
operator already holds.

IF T1 NEVER FILLS, NOTHING HERE RUNS. Called only from the filled branch of
atlas_entry; a no-fill, a rejection or an unconfirmed fill places no GTT. The
ladder exists to average into a held position -- resting cash against one that
never opened would tie up the reserve for nothing.

PRICES. trigger = the ladder price on the tick grid (rounded DOWN: a buy below
market should not trigger early); limit = trigger + STOP_LIMIT_SLIP_PCT rounded
UP, because Zerodha's own guidance for a GTT buy is a limit ABOVE the trigger --
a limit at or below it can trigger and not fill.

WHERE THE IDS LIVE. atlas_trades.exit_legs gets t2_trigger_id / t3_trigger_id
with their prices and quantities. NOT gtt_trigger_id: that column belongs to
the stop machinery, and gtt.list_atlas_gtts() + exits.reconcile_exits() would
treat a tranche as a stop leg (they call every ATLAS GTT "stop_trigger_id").
The long book is therefore kept OUT of reconcile_exits and cleaned up here.

WHAT HAPPENS TO A RESTING BUY GTT WHEN ITS POSITION IS CLOSED. Zerodha does not
cancel it: a BUY GTT is independent of holdings, rests up to 365 days, and when
the price reaches it, it BUYS -- re-entering a closed symbol (GODREJAGRO). So
cleanup() runs every reconcile cycle (every 5 minutes in session, watch-only
included) and, for every long-book trade with a tranche GTT still active,
cancels it once the broker shows NO shares -- positions AND holdings, because a
CNC long held past settlement is in holdings, not positions. A trigger that
fires between the sale and the next cycle can still buy; the window is at most
one reconcile interval, and the alarm below fires if the list cannot be read.

READS THAT FAIL ARE NOT ZERO. held_qty() returns (False, None) when either book
cannot be read, and cleanup() then cancels nothing and raises an
ORPHAN_UNCHECKED exit alarm instead -- never "flat, so cancel".
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("tranches")
IST = timezone(timedelta(hours=5, minutes=30))
LEG_NAMES = ("t2", "t3")


def _ev(*a, **k):
    try:
        from atlas.execution.order_events import record
        return record(*a, **k)
    except Exception as e:                                   # pragma: no cover
        log.error(f"order event not recorded: {e}")
        return False


def gtt_prices(symbol: str, ladder_price: float, tick: float = None) -> tuple:
    """-> (trigger, limit). Raises exits.TickUnknown when no tick is known."""
    from atlas.execution import exits as X
    trig = X.price_for(symbol, float(ladder_price), "stop", "LONG", tick=tick)   # DOWN
    lim = X.price_for(symbol, trig * (1 + X.STOP_LIMIT_SLIP_PCT), "entry", "LONG",
                      tick=tick)                                                 # UP
    return trig, lim


def place(trade_id, symbol: str, plan: list, last_price: float, kite=None) -> dict:
    """Place T2 and T3. plan = sizing's tranches list (T1, T2, T3).

    -> {"legs": {...exit_legs keys...}, "placed": n, "failed": [reasons]}
    Never raises. A tranche with zero shares is skipped and recorded.
    """
    out = {"legs": {}, "placed": 0, "failed": []}
    if kite is None:
        from atlas.execution.broker import get_kite
        kite = get_kite()
    if kite is None:
        out["failed"].append("no broker session")
        return out
    for name, tr in zip(LEG_NAMES, plan[1:3]):
        qty = int(tr.get("qty") or 0)
        if qty < 1:
            why = f"{name.upper()} of Rs{tr.get('amount', 0):,.0f} buys no share at {tr.get('price')}"
            out["failed"].append(why)
            _ev("REFUSED", "tranches.place", symbol, f"{name.upper()}_ZERO_QTY", why,
                {"tranche": tr}, trade_id=trade_id)
            continue
        try:
            trig, lim = gtt_prices(symbol, tr["price"])
        except Exception as e:
            why = f"{name.upper()} cannot be priced: {e}"
            out["failed"].append(why)
            _ev("REFUSED", "tranches.place", symbol, "NO_TICK", why, {}, trade_id=trade_id)
            continue
        order = {"exchange": "NSE", "tradingsymbol": symbol, "transaction_type": "BUY",
                 "quantity": qty, "order_type": "LIMIT", "product": "CNC", "price": lim}
        try:
            r = kite.place_gtt(trigger_type="single", tradingsymbol=symbol,
                               exchange="NSE", trigger_values=[trig],
                               last_price=float(last_price), orders=[order])
            tid = r.get("trigger_id") if isinstance(r, dict) else r
        except Exception as e:
            tid, err = None, f"{type(e).__name__}: {e}"
        else:
            err = "place_gtt returned no trigger_id"
        if not tid:
            why = f"{name.upper()} GTT refused: {err}"
            out["failed"].append(why)
            _ev("REFUSED", "tranches.place", symbol, f"{name.upper()}_GTT_REFUSED", why,
                {"trigger": trig, "limit": lim, "qty": qty}, trade_id=trade_id)
            continue
        out["legs"].update({f"{name}_trigger_id": str(tid), f"{name}_trigger": trig,
                            f"{name}_limit": lim, f"{name}_qty": qty,
                            f"{name}_source": tr.get("source")})
        out["placed"] += 1
        _ev("GTT_SENT", "tranches.place", symbol, f"{name.upper()}_BUY_GTT",
            f"{name.upper()} BUY GTT {tid}: trigger {trig}, limit {lim}, {qty} CNC "
            f"({tr.get('source')} level {tr.get('price')})",
            {"trigger_id": str(tid), "trigger": trig, "limit": lim, "qty": qty,
             "ladder_price": tr.get("price"), "source": tr.get("source"),
             "last_price": last_price}, trade_id=trade_id)
    return out


def read_books(kite) -> tuple:
    """(net positions, holdings) from the broker, or (None, None) if either
    cannot be read. One read serves every symbol in a cycle."""
    try:
        pos = kite.positions()
        net = pos.get("net") if isinstance(pos, dict) else None
        hold = kite.holdings()
        if net is None or hold is None:
            return None, None
        return net, hold
    except Exception as e:
        log.warning(f"positions/holdings unreadable ({e})")
        return None, None


def position_from(symbol: str, net: list, hold: list) -> tuple:
    """(shares, blended average cost) for a CNC long across BOTH books.

    THE ONE DEFINITION, used by the cleanup, the +16% flag and the 15:50 report.
    Today's buys sit in positions (CNC, quantity > 0, average_price); settled
    shares sit in holdings (quantity + t1_quantity, average_price). A position
    with a T1 from last week and a T2 filled today is in BOTH, so the books are
    added, weighted by quantity -- a CNC buy today cannot already be in holdings,
    so nothing is counted twice.
    """
    q = v = 0.0
    for x in net or []:
        if str(x.get("tradingsymbol")) == symbol and str(x.get("product", "")).upper() == "CNC":
            n = float(x.get("quantity") or 0)
            if n > 0:
                q += n
                v += n * float(x.get("average_price") or 0)
    for x in hold or []:
        if str(x.get("tradingsymbol")) == symbol:
            n = float(x.get("quantity") or 0) + float(x.get("t1_quantity") or 0)
            if n > 0:
                q += n
                v += n * float(x.get("average_price") or 0)
    return int(q), (v / q if q else None)


def held_qty(symbol: str, kite) -> tuple:
    """(readable, shares held) from positions AND holdings. Never (True, 0) on a
    failed read."""
    net, hold = read_books(kite)
    if net is None:
        log.warning(f"{symbol}: positions/holdings unreadable")
        return False, None
    return True, position_from(symbol, net, hold)[0]


def tranche_ids(t: dict) -> dict:
    legs = (t or {}).get("exit_legs") or {}
    return {n: str(legs[f"{n}_trigger_id"]) for n in LEG_NAMES if legs.get(f"{n}_trigger_id")}


def flag_cancel(trades: list, kite, active_gtts, ltp_of=None, now=None) -> dict:
    """At +PROFIT_FLAG_PCT on blended cost, cancel the position's remaining
    tranche GTTs (operator, 2026-11-10).

    The position is in exit territory; averaging into something about to be sold
    contradicts the exit. Runs every cycle in the engine (watch-only included) --
    NOT in a report, which never places or cancels. Cancelled tranches are not
    re-placed if the price falls back: the position stays without them.

    Any input that cannot be read cancels NOTHING and says so: the flag is a
    measurement, and a GTT buy below market can only fire on a fall, so leaving
    it for one more cycle is not the exposure an unmeasured cancel would hide.
    """
    from atlas.config import PROFIT_FLAG_PCT
    out = {"checked": 0, "cancelled": 0, "flagged": [], "alerts": []}
    if active_gtts is None:
        return out                    # cleanup() has already said the list is unreadable
    active = {str(g.get("id")) for g in active_gtts}
    live = [(t, {n: i for n, i in tranche_ids(t).items() if i in active}) for t in trades or []]
    live = [(t, ids) for t, ids in live if ids]
    if not live:
        return out
    net, hold = read_books(kite)
    if net is None:
        out["alerts"].append("+16% check skipped: positions/holdings unreadable")
        return out
    if ltp_of is None:
        def ltp_of(syms):
            q = kite.ltp([f"NSE:{s}" for s in syms]) or {}
            return {k.split(":", 1)[1]: float(v.get("last_price") or 0) for k, v in q.items()}
    try:
        ltps = ltp_of(sorted({str(t.get("symbol")) for t, _ in live}))
    except Exception as e:
        out["alerts"].append(f"+16% check skipped: LTP unreadable ({e})")
        return out
    for t, ids in live:
        sym = str(t.get("symbol"))
        out["checked"] += 1
        qty, avg = position_from(sym, net, hold)
        ltp = ltps.get(sym)
        if not qty or not avg or not ltp:
            continue                  # flat (cleanup's job) or unpriced: no measurement, no cancel
        ch = (ltp / avg - 1) * 100
        if ch < PROFIT_FLAG_PCT:
            continue
        out["flagged"].append((sym, round(ch, 2)))
        for name, tid in ids.items():
            try:
                kite.delete_gtt(trigger_id=tid)
                out["cancelled"] += 1
                _ev("GTT_CANCELLED", "tranches.flag_cancel", sym, f"{name.upper()}_PROFIT_FLAG",
                    f"{name.upper()} BUY GTT {tid} cancelled: {sym} is {ch:+.2f}% on blended "
                    f"cost Rs{avg:,.2f} (LTP {ltp}), at or past the +{PROFIT_FLAG_PCT:g}% flag",
                    {"trigger_id": tid, "blended_cost": round(avg, 4), "ltp": ltp,
                     "qty": qty, "change_pct": round(ch, 4)}, trade_id=t.get("id"))
            except Exception as e:
                out["alerts"].append(f"{sym}: +{PROFIT_FLAG_PCT:g}% reached but tranche GTT {tid} "
                                     f"could not be cancelled ({e}) — cancel it by hand")
    return out


def cleanup(trades: list, kite, active_gtts) -> dict:
    """Cancel the tranche GTTs of every long-book trade the broker shows flat.

    trades: long-book rows with exit_legs (ANY status -- a row closed by the
    outcome checker can still have a live trigger). active_gtts: the broker's
    active GTT list, or None if it could not be read.
    """
    out = {"checked": 0, "cancelled": 0, "alerts": []}
    if active_gtts is None:
        for t in trades or []:
            if tranche_ids(t):
                out["alerts"].append(f"{t.get('symbol')}: tranche GTTs could not be "
                                     f"checked — the GTT list is unreadable")
        return out
    active = {str(g.get("id")) for g in active_gtts}
    for t in trades or []:
        ids = {n: i for n, i in tranche_ids(t).items() if i in active}
        if not ids:
            continue
        sym = str(t.get("symbol"))
        out["checked"] += 1
        ok, held = held_qty(sym, kite)
        if not ok:
            out["alerts"].append(f"{sym}: holdings unreadable — tranche GTTs "
                                 f"{sorted(ids.values())} left resting, not checked")
            try:
                from atlas.execution.exit_alarms import raise_alarm
                d = datetime.now(IST).date().isoformat()
                raise_alarm(f"orphan:{sym}:{d}", "ORPHAN_UNCHECKED", sym,
                            "holdings could not be read, so whether its resting BUY "
                            "GTTs are orphaned is unknown; an orphaned buy trigger "
                            "re-enters a closed symbol", t.get("id"))
            except Exception:
                pass
            continue
        if held > 0:
            continue
        for name, tid in ids.items():
            try:
                kite.delete_gtt(trigger_id=tid)
                out["cancelled"] += 1
                _ev("GTT_CANCELLED", "tranches.cleanup", sym, f"{name.upper()}_ORPHAN",
                    f"{name.upper()} BUY GTT {tid} cancelled: no {sym} shares at the "
                    f"broker (positions and holdings), so it would re-enter a closed "
                    f"position", {"trigger_id": tid}, trade_id=t.get("id"))
            except Exception as e:
                out["alerts"].append(f"{sym}: could not cancel tranche GTT {tid} ({e}) "
                                     f"— it can RE-ENTER a closed position. Cancel it by hand.")
    return out
