"""
THE LONG PILOT -- the long book's entry rules (operator, 2026-11-10).
=====================================================================

Five names, first come first served, sized by atlas/sizing.py against
LONG_PILOT_CAPITAL; tranche 1 a LIMIT at the zone, T2/T3 resting BUY GTTs once
T1 has filled (atlas/execution/tranches.py); NO stop orders at the broker
(atlas/books.py). This module holds what is specific to the pilot:

  cap_check()       the 5-entry cap, counted from atlas_trades (a restart
                    cannot reset it); in watch-only, today's WOULD_PLACE
                    symbols count too, so Wednesday's preview stops at five
  preflight()       every precondition, each ON THIS CALL, before any order:
                    funds were already read at Gate 5; here the GTT list is
                    read, order_path_events is written and read back, sizing
                    is confirmed connected, and the book switch is confirmed
                    to be stops-off for longs and stops-on for shorts. Any
                    failure -> place nothing.
  record_intent()   the decision written to the observation ledger BEFORE the
                    order; if the ledger will not take it, no order (these
                    positions must count as evidence)
  ledger_decision() every long-book refusal and would-enter, once per symbol
                    per status per session, with the price and its source
  after_fill()      what happens once T1 has FILLED: published stop/target,
                    T2/T3 GTTs, the row, the ledger, the alert

FEWER ORDERS IS THE SAFE DIRECTION. Every function here that cannot establish
its answer refuses.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta

log = logging.getLogger("long-pilot")
IST = timezone(timedelta(hours=5, minutes=30))
SOURCE = "atlas/execution/long_pilot.py"

_preflight_ok: dict = {}        # session -> True once order_path_events read back
_ledgered: set = set()          # (session, symbol, status) already ATTEMPTED this process
_pending: list = []             # refusal rows queued this cycle, written by flush_decisions()


def _now():
    return datetime.now(IST)


def _svc():
    from atlas.config import SUPABASE_URL
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return (os.environ.get("SUPABASE_URL") or SUPABASE_URL).rstrip("/"), key


def _hdr(key, extra=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _ev(*a, **k):
    try:
        from atlas.execution.order_events import record
        return record(*a, **k)
    except Exception as e:                                   # pragma: no cover
        log.error(f"order event not recorded: {e}")
        return False


# ── THE CAP ───────────────────────────────────────────────────────

def entries_used(getter=None) -> tuple:
    """(readable, n) -- long-pilot rows in atlas_trades, any status but CANCELLED
    (a released reservation). Never (True, 0) on a failed read."""
    from atlas.config import LONG_PILOT_SIZING_BASIS
    url, key = _svc()
    try:
        import requests
        get = getter or requests.get
        r = get(f"{url}/rest/v1/atlas_trades?sizing_basis=eq.{LONG_PILOT_SIZING_BASIS}"
                f"&status=neq.CANCELLED&select=id,symbol,status",
                headers=_hdr(key), timeout=10)
        if r.status_code != 200:
            return False, None
        return True, len(r.json())
    except Exception as e:
        log.warning(f"pilot entries unreadable: {e}")
        return False, None


def would_place_today(getter=None) -> tuple:
    """(readable, {symbols}) with a WOULD_PLACE event this session."""
    url, key = _svc()
    try:
        import requests
        get = getter or requests.get
        r = get(f"{url}/rest/v1/order_path_events?session_date=eq.{_now().date().isoformat()}"
                f"&event=eq.WOULD_PLACE&select=symbol", headers=_hdr(key), timeout=10)
        if r.status_code != 200:
            return False, None
        return True, {x.get("symbol") for x in r.json() if x.get("symbol")}
    except Exception as e:
        log.warning(f"would-place events unreadable: {e}")
        return False, None


def cap_check(symbol: str, dry_run: bool, getter=None) -> tuple:
    """-> (ok, reason, code). Unreadable is a refusal."""
    from atlas.config import LONG_PILOT_MAX_ENTRIES as CAP
    ok, used = entries_used(getter)
    if not ok:
        return False, "pilot entry count unreadable — refusing rather than risk a sixth", "PILOT_COUNT_UNREADABLE"
    extra = 0
    if dry_run:
        ok2, seen = would_place_today(getter)
        if not ok2:
            return False, "today's would-place events unreadable — cannot apply the cap", "PILOT_COUNT_UNREADABLE"
        extra = len(seen - {symbol})
    if used + extra >= CAP:
        return False, (f"pilot is full: {used} entr{'y' if used == 1 else 'ies'} made"
                       + (f" + {extra} would-place today" if dry_run else "")
                       + f", cap {CAP}"), "PILOT_FULL"
    return True, f"{used + extra} of {CAP} used", None


# ── PRECONDITIONS ─────────────────────────────────────────────────

def _orderevents_writable(poster=None) -> tuple:
    """Write ONE PREFLIGHT row per session and READ IT BACK. Cached per session."""
    sess = _now().date().isoformat()
    if _preflight_ok.get(sess):
        return True, "order_path_events written and read back earlier this session"
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = True, "breaker unavailable"
    if drilling and poster is None:
        return False, f"drill ({why}) — the production table is not written from a test"
    url, key = _svc()
    if not key:
        return False, "SUPABASE_SERVICE_KEY unset — order_path_events cannot be written"
    row = {"at": _now().isoformat(timespec="seconds"), "session_date": sess,
           "event": "PREFLIGHT", "source": SOURCE, "symbol": None,
           "code": "ORDER_EVENTS_WRITABLE",
           "reason": "long pilot preflight: proving order_path_events accepts a row "
                     "before any order this session", "detail": {}}
    try:
        import requests
        post = poster or requests.post
        r = post(f"{url}/rest/v1/order_path_events",
                 headers=_hdr(key, {"Prefer": "return=representation"}),
                 json=row, timeout=10)
        body = r.json() if r.status_code in (200, 201) else None
        if not body or not (body[0] if isinstance(body, list) else body).get("id"):
            return False, f"order_path_events refused the preflight row (HTTP {r.status_code})"
    except Exception as e:
        return False, f"order_path_events unwritable ({type(e).__name__}: {e})"
    _preflight_ok[sess] = True
    return True, "order_path_events written and read back"


def preflight(gtt_reader=None, poster=None) -> tuple:
    """-> (ok, [failures]). Funds are Gate 5's read; this is everything else."""
    fails = []
    # THE BOOK SWITCH, AS THE CODE SEES IT NOW: stops off for longs, on for shorts.
    from atlas.books import broker_stops
    if broker_stops("LONG", "CNC"):
        fails.append("the long book still places broker stops (LONG_BOOK_BROKER_STOPS)")
    if not broker_stops("SHORT", "MIS"):
        fails.append("the short book would place NO stops — refusing everything")
    # SIZING CONNECTED: the module and its capital resolve.
    try:
        from atlas.config import LONG_PILOT_CAPITAL
        from atlas import sizing as _sz                                  # noqa: F401
        if not float(LONG_PILOT_CAPITAL) > 0:
            fails.append(f"LONG_PILOT_CAPITAL is {LONG_PILOT_CAPITAL!r}")
    except Exception as e:
        fails.append(f"sizing not connected: {e}")
    # THE GTT LIST: None is unreadable, [] is a real empty book.
    try:
        if gtt_reader is None:
            from atlas.execution.gtt import list_active_gtts as gtt_reader
        if gtt_reader() is None:
            fails.append("the GTT list is unreadable — an orphaned trigger could not be seen")
    except Exception as e:
        fails.append(f"the GTT list read raised ({e})")
    ok, why = _orderevents_writable(poster)
    if not ok:
        fails.append(why)
    return (not fails), fails


def tranche_rows(getter=None):
    """Every long-pilot row with exit_legs, ANY status -- a row closed by the
    outcome checker can still have a resting trigger. None on a failed read."""
    from atlas.config import LONG_PILOT_SIZING_BASIS
    url, key = _svc()
    try:
        import requests
        get = getter or requests.get
        r = get(f"{url}/rest/v1/atlas_trades?sizing_basis=eq.{LONG_PILOT_SIZING_BASIS}"
                f"&exit_legs=not.is.null&select=id,symbol,status,exit_legs",
                headers=_hdr(key), timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        log.warning(f"tranche rows unreadable: {e}")
        return None


# ── THE LEDGER ────────────────────────────────────────────────────

def _obs_row(kind, symbol, ref_price, values, qualifier=""):
    from engine import observations as O
    return O.build(kind, symbol=symbol, session_date=_now().date().isoformat(),
                   direction="LONG", reference_price=ref_price,
                   computed_values=values, source_file=SOURCE, qualifier=qualifier)


def record_intent(symbol, status, ltp, ltp_source, plan, stop, extra=None,
                  recorder=None) -> bool:
    """The decision to enter (or, in watch-only, would-enter), to the ledger,
    BEFORE any order. False -> the caller places nothing."""
    try:
        from engine import observations as O
        rec = recorder or O.record
        row = _obs_row("atlas_decision", symbol, ltp, {
            "status": status, "price": ltp, "price_source": ltp_source,
            "published_stop": stop, "plan": plan_summary(plan), **(extra or {})},
            qualifier=status)
        ok = bool(rec([row]))
        if ok:
            _ledgered.add((_now().date().isoformat(), symbol, status))
        return ok
    except Exception as e:
        log.error(f"{symbol}: ledger intent not recorded ({e})")
        return False


def ledger_decision(signal: dict, result: dict) -> bool:
    """QUEUE a long-book refusal (status + reason + price + source) for the
    ledger, once per symbol per status per session. Written by flush_decisions()
    at the end of the cycle, in one batched call.

    ATTEMPTED ONCE, NOT RETRIED EVERY CYCLE. The key is marked on queueing, so
    a ledger that refuses (e.g. the atlas types not yet applied) costs one
    batched attempt per new decision -- not 20-80 round trips per 60-second
    cycle inside the entry loop. Never raises; nothing is blocked by a refusal
    that cannot be ledgered -- no order is at stake for a refusal."""
    try:
        if str(signal.get("direction") or "").upper() != "LONG":
            return False
        status = str(result.get("status") or "?")
        if status in ("WATCH_ONLY", "WOULD_ENTER", "ENTERED"):
            return False            # the intent / entry rows are written on that path
        sym = signal.get("symbol")
        k = (_now().date().isoformat(), sym, status)
        if k in _ledgered:
            return False
        _ledgered.add(k)
        ltp = signal.get("ltp")
        _pending.append(_obs_row("atlas_decision", sym, ltp, {
            "status": status, "reason": str(result.get("reason") or "")[:500],
            "price": ltp, "price_source": "signal.ltp (Upstox batch, market_open)",
            "zone": [signal.get("entry_low"), signal.get("entry_high")],
            "blocked_by": result.get("blocked_by")}, qualifier=status))
        return True
    except Exception as e:
        log.error(f"decision not queued: {e}")
        return False


def flush_decisions(recorder=None) -> int:
    """Write the cycle's queued refusals in ONE ledger call. -> rows sent."""
    global _pending
    rows, _pending = _pending, []
    if not rows:
        return 0
    try:
        from engine import observations as O
        ok = (recorder or O.record)(rows)
        if not ok:
            log.error(f"{len(rows)} long-book decision(s) not ledgered (refused); "
                      f"not retried this session")
        return len(rows) if ok else 0
    except Exception as e:
        log.error(f"decisions not ledgered ({e})")
        return 0


def plan_summary(plan: dict) -> dict:
    if not plan:
        return {}
    return {k: plan.get(k) for k in ("mcap_cr", "band_edge_cr", "ceiling_pct_of_book",
                                     "long_book", "ceiling", "median_traded_value",
                                     "liquidity_cap", "bound", "position", "atr",
                                     "shares_quarter", "shares_filed")} | {
        "tranches": [{k: t.get(k) for k in ("price", "source", "qty", "amount",
                                             "pct_below_entry", "atr_below_entry",
                                             "rounded_up")}
                     for t in plan.get("tranches") or []]}


def would_place(symbol, plan, limit_price, ltp, ltp_source, trade_ref=None) -> None:
    """Watch-only: the exact orders that WOULD go out, to order_path_events."""
    t = plan["tranches"]
    _ev("WOULD_PLACE", "atlas_entry.enter_trade", symbol, "LONG_PILOT",
        f"WOULD BUY {t[0]['qty']} {symbol} LIMIT {limit_price} (Rs{t[0]['qty'] * limit_price:,.0f}); "
        f"then GTT T2 {t[1]['qty']} @ {t[1]['price']} ({t[1]['source']}), "
        f"T3 {t[2]['qty']} @ {t[2]['price']} ({t[2]['source']})",
        {"t1": {"qty": t[0]["qty"], "limit": limit_price,
                "value": round(t[0]["qty"] * limit_price, 2)},
         "t2": {"qty": t[1]["qty"], "price": t[1]["price"], "source": t[1]["source"]},
         "t3": {"qty": t[2]["qty"], "price": t[2]["price"], "source": t[2]["source"]},
         "ltp": ltp, "ltp_source": ltp_source, "plan": plan_summary(plan)})


# ── AFTER T1 HAS FILLED ───────────────────────────────────────────

def after_fill(row_id, symbol, intent, plan, avg_price, filled_qty, stop,
               last_price, patch, alert, kite=None) -> dict:
    """The long book's post-fill: NO stop to the broker. Publish the levels,
    place T2/T3, record the row and the ledger, tell the operator."""
    from atlas.execution import exits as X
    from atlas.execution import tranches as TR
    from atlas.config import PROFIT_FLAG_PCT
    try:
        target = X.target_price(avg_price, stop, "LONG", symbol=symbol)
    except Exception as e:
        log.warning(f"{symbol}: target not computed ({e})")
        target = None
    tr = TR.place(row_id, symbol, plan["tranches"], last_price, kite=kite)
    legs = dict(tr["legs"])
    legs["published_only"] = True
    intent.update({"entry_price": round(avg_price, 2), "qty": filled_qty,
                   "target_price": target, "stop_confirmed": False,
                   "target_confirmed": False, "exit_legs": legs,
                   "exit_mechanism": "NONE: long book, levels published, no broker stops"})
    ok = patch(row_id, {
        "status": "OPEN", "entry_price": round(avg_price, 2), "qty": filled_qty,
        "stop_price": stop, "target_price": target, "exit_legs": legs,
        "exit_mechanism": intent["exit_mechanism"], "stop_confirmed": False,
        "target_confirmed": False,
        "notes": (f"LONG PILOT: no broker stop by mandate. Published stop {stop}, "
                  f"target {target}. T2/T3 GTTs placed {tr['placed']}/2"
                  + (f"; {'; '.join(tr['failed'])}" if tr["failed"] else "")
                  + f". Flag at +{PROFIT_FLAG_PCT:g}% on blended cost.")[:900]},
        "pilot fill")
    try:
        from engine import observations as O
        O.record([_obs_row("atlas_entry", symbol, avg_price, {
            "trade_id": row_id, "filled_qty": filled_qty, "fill_price": avg_price,
            "price_source": "broker fill average (await_fill)", "limit_price":
            intent.get("entry_limit_price"), "published_stop": stop,
            "published_target": target, "plan": plan_summary(plan), "tranche_gtts": legs})])
    except Exception as e:
        log.error(f"{symbol}: entry not ledgered ({e})")
    alert("LONG PILOT ENTRY",
          f"{symbol} BUY {filled_qty} filled @ Rs{avg_price:,.2f} (trade {row_id}). "
          f"No stop at the broker by mandate; published stop {stop}, target {target}. "
          f"T2/T3 BUY GTTs placed: {tr['placed']} of 2"
          + (f" — {'; '.join(tr['failed'])}" if tr["failed"] else "") + ".")
    return {"status": "ENTERED", "recorded": ok, "trade_id": row_id,
            "tranches_placed": tr["placed"], "tranche_failures": tr["failed"], **intent}
