#!/usr/bin/env python3
"""
THE SESSION REPORTS — 11:30, 13:30, 15:30 IST, and a day-end at 15:50
=====================================================================

WHY THESE EXIST. ATLAS used to tell you about the session by talking
constantly: an alert per blocked symbol per cycle, the paused state repeated
every cycle it stayed paused, a line for every range break it watched and did
not act on. At 360 cycles a day that is a channel nobody reads, and the things
that matter -- an entry, a halt, a protection failure -- arrived in the same
stream as the noise. On 2026-10-15 a position was entered with no stop, no
target and no GTT, and the channel that should have screamed had been
scrolling blocked-symbol notices all morning.

So the routine status is pulled out of the cycle and scheduled. Four reports,
each CUMULATIVE SINCE 09:15, each answering the same questions:

    what was entered, and is each stop and target CONFIRMED AT THE BROKER
    what is open, and what is it worth right now
    what closed, and for how much
    the day's P&L
    the gate state -- sentiment, both breakers, the halt file and the mode
    how many candidates were evaluated, and the one reason most of them died

CONFIRMED MEANS CONFIRMED. Every protective leg in these reports is verified by
reading it back from the broker -- an open SL order or an active GTT trigger --
never from what ATLAS recorded having attempted. The 2026-10-15 failure was
four mechanisms that all REPORTED placing a stop and all had their orders
rejected; a report built from intent would have said the position was
protected. If the broker cannot be read, the answer is UNKNOWN and never
"confirmed", and UNKNOWN counts as unprotected for the warning at the top.

THE 15:50 REPORT IS DIFFERENT IN ONE WAY. It names, for every open long, which
overnight GTT was placed and whether it confirmed -- and if any position is
open without one, that goes in the FIRST LINE of the message in plain words.
That is the 7 October failure class: the exchange flushes the day-0 SL at
16:00, and a position whose GTT never confirmed is naked from then until the
next open. It must not be the fifth bullet of a long message.

NOTHING HERE PLACES, MODIFIES OR CANCELS AN ORDER. Reads only: the broker's
orders, GTTs and positions, atlas_trades, atlas_entry_log, and the gate state
files. It is safe to run while halted, and it is MOST useful then.

    python3 -m atlas.reporting.session_report --slot 1130
    python3 -m atlas.reporting.session_report --slot dayend
"""

import os
import sys
import logging
import argparse
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from atlas.reporting.telegram import send                       # noqa: E402
from atlas.reporting.daily_report import (_get, _inr, _esc,     # noqa: E402
                                          _fit, OPEN_STATUSES)

log = logging.getLogger("session_report")

IST = timezone(timedelta(hours=5, minutes=30))

# The slots, as the operator named them. The key is what --slot takes; the
# label is what the message header says.
SLOTS = {
    "1130": ("11:30", False),
    "1330": ("13:30", False),
    "1530": ("15:30", False),
    "dayend": ("15:50 DAY-END", True),
}

SESSION_START = "09:15"

# An SL order resting at the broker is one of these. ANYTHING ELSE IS NOT
# PROTECTION: COMPLETE means it already fired, CANCELLED and REJECTED mean it
# is not there, and REJECTED is exactly what 2026-10-15 produced four times
# while every mechanism reported success.
LIVE_ORDER_STATUS = ("OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED")


def _now_ist():
    return datetime.now(IST)


def _today():
    return _now_ist().date().isoformat()


# ══════════════════════════════════════════════════════════════════
# WHAT THE RECORD SAYS
# ══════════════════════════════════════════════════════════════════

def entries_today() -> list:
    """Every atlas_trades row stamped with today's entry_date, open or closed.

    entry_date and not created_at: a row reserved at 09:19 and filled at 09:21
    belongs to today either way, and created_at is a timestamptz that would
    need the IST conversion done in the query.
    """
    return _get(f"/rest/v1/atlas_trades?entry_date=eq.{_today()}"
                f"&select=id,symbol,direction,qty,entry_price,stop_price,"
                f"target_price,published_stop,stop_source,publication_kind,"
                f"status,product,order_id,exit_legs,exit_mechanism,exit_price,"
                f"pnl,pnl_net,exit_reason,created_at,notes"
                f"&order=created_at.asc", "today's trades") or []


def open_trades() -> list:
    """Open rows regardless of entry date -- an overnight CNC long entered
    yesterday is open today and needs its protection confirmed just as much."""
    return _get(f"/rest/v1/atlas_trades?status=in.({','.join(OPEN_STATUSES)})"
                f"&select=id,symbol,direction,qty,entry_price,stop_price,"
                f"target_price,status,product,exit_legs,exit_mechanism,"
                f"entry_date,created_at&order=created_at.asc",
                "open trades") or []


def closed_today() -> list:
    return _get(f"/rest/v1/atlas_trades?status=eq.CLOSED&exit_date=eq.{_today()}"
                f"&select=symbol,direction,qty,entry_price,exit_price,pnl,"
                f"pnl_net,exit_reason,publication_kind", "closed today") or []


def decisions_today() -> tuple:
    """(evaluated, Counter of blocking reasons, most common).

    atlas_entry_log is the decision record -- one row per evaluation including
    the skips -- which is why the per-cycle BLOCKED alert could be silenced
    without losing anything: the reason was always written here too.
    """
    rows = _get(f"/rest/v1/atlas_entry_log?run_date=eq.{_today()}"
                f"&select=symbol,status,reason&limit=5000", "decisions") or []
    blocked = Counter()
    for r in rows:
        st = str(r.get("status") or "")
        if st in ("ENTERED", "WOULD_ENTER"):
            continue
        # The status is the machine-readable gate; the reason is prose that
        # varies per symbol (it carries prices), so grouping on the reason
        # would give one bucket per symbol and name nothing.
        blocked[st or "UNKNOWN"] += 1
    top = blocked.most_common(1)
    return len(rows), blocked, (top[0] if top else None)


# ══════════════════════════════════════════════════════════════════
# WHAT THE BROKER SAYS — the only source that counts for "confirmed"
# ══════════════════════════════════════════════════════════════════

def broker_protection() -> dict:
    """-> {"ok": bool, "why": str, "orders": {...}, "gtts": {...}}

    Reads resting SL orders and active GTTs, keyed by symbol. ok=False means
    the broker could not be read, and then NOTHING may be reported as
    confirmed -- an unreadable broker is indistinguishable from an unprotected
    position, and the safe reading of the two is the same.
    """
    out = {"ok": False, "why": "", "orders": {}, "gtts": {}}
    try:
        from atlas.execution.broker import get_kite
        kite = get_kite()
        if not kite:
            out["why"] = "no broker session (token missing or expired)"
            return out
    except Exception as e:
        out["why"] = f"could not reach the broker: {type(e).__name__}: {e}"
        return out

    try:
        for o in kite.orders() or []:
            if str(o.get("status", "")).upper() not in LIVE_ORDER_STATUS:
                continue
            if str(o.get("order_type", "")).upper() not in ("SL", "SL-M"):
                continue
            out["orders"].setdefault(o.get("tradingsymbol"), []).append({
                "order_id": o.get("order_id"),
                "trigger": o.get("trigger_price"),
                "price": o.get("price"),
                "qty": o.get("quantity"),
                "type": o.get("order_type"),
                "status": o.get("status"),
                "side": o.get("transaction_type")})
    except Exception as e:
        out["why"] = f"orders unreadable: {type(e).__name__}: {e}"
        return out

    try:
        for g in kite.get_gtts() or []:
            if str(g.get("status", "")).lower() != "active":
                continue
            cond = g.get("condition") or {}
            sym = cond.get("tradingsymbol")
            legs = []
            for leg in (g.get("orders") or []):
                legs.append({"price": leg.get("price"),
                             "qty": leg.get("quantity"),
                             "side": leg.get("transaction_type")})
            out["gtts"].setdefault(sym, []).append({
                "trigger_id": g.get("id"),
                "type": g.get("type"),
                "triggers": cond.get("trigger_values") or [],
                "last_price": cond.get("last_price"),
                "legs": legs})
    except Exception as e:
        out["why"] = f"GTTs unreadable: {type(e).__name__}: {e}"
        return out

    out["ok"] = True
    return out


def confirm_legs(trade: dict, prot: dict) -> dict:
    """Is this trade's stop confirmed live, and its target? -> dict of verdicts.

    Verdicts are exactly three: CONFIRMED (read back from the broker), NONE
    (the broker was readable and it is not there) and UNKNOWN (the broker could
    not be read). There is deliberately no "attempted" and no "placed" -- those
    are claims about ATLAS, and the question is about the exchange.
    """
    sym = trade.get("symbol")
    entry = float(trade.get("entry_price") or 0)
    long_ = str(trade.get("direction") or "").upper() == "LONG"
    if not prot["ok"]:
        return {"stop": "UNKNOWN", "target": "UNKNOWN", "detail": prot["why"],
                "gtt_ids": [], "stop_at": None, "target_at": None}

    stop_at, target_at, ids, bits = None, None, [], []

    for o in prot["orders"].get(sym, []):
        stop_at = o["trigger"]
        bits.append(f"SL order {o['order_id']} trigger Rs{o['trigger']}")

    for g in prot["gtts"].get(sym, []):
        ids.append(g["trigger_id"])
        for t in g["triggers"]:
            t = float(t)
            # Which side a GTT trigger is protecting is decided by where it
            # sits relative to the ENTRY, not by leg order: a two-leg OCO puts
            # the stop below and the target above for a long, and reading them
            # positionally broke the moment one leg was modified by the trail.
            is_stop = (t < entry) if long_ else (t > entry)
            if is_stop:
                stop_at = t if stop_at is None else stop_at
                bits.append(f"GTT {g['trigger_id']} stop trigger Rs{t}")
            else:
                target_at = t if target_at is None else target_at
                bits.append(f"GTT {g['trigger_id']} target trigger Rs{t}")

    return {"stop": "CONFIRMED" if stop_at is not None else "NONE",
            "target": "CONFIRMED" if target_at is not None else "NONE",
            "detail": "; ".join(bits), "gtt_ids": ids,
            "stop_at": stop_at, "target_at": target_at}


def live_prices(symbols: list) -> dict:
    """LTP per symbol from the broker, for running P&L. {} if unreadable."""
    if not symbols:
        return {}
    try:
        from atlas.execution.broker import get_kite
        kite = get_kite()
        if not kite:
            return {}
        keys = [f"NSE:{s}" for s in symbols]
        q = kite.ltp(keys) or {}
        return {k.split(":", 1)[1]: float(v.get("last_price") or 0)
                for k, v in q.items()}
    except Exception as e:
        log.warning(f"LTP read failed: {e}")
        return {}


# ══════════════════════════════════════════════════════════════════
# THE GATE STATE
# ══════════════════════════════════════════════════════════════════

def gate_state() -> list:
    """The gates, each read from its own source, each failing to a named
    unknown rather than to an optimistic default."""
    out = []

    try:
        from atlas.execution import live_sentiment as LS
        s = LS.read()
        v = s.get("verdict") or "?"
        out.append(f"sentiment    {v}  ({LS.legs_line(s)})")
    except Exception as e:
        out.append(f"sentiment    UNREADABLE — {type(e).__name__}: {e}")

    try:
        from atlas.execution import range_breaker as RB
        out.append(f"breakers     {RB.line(RB.market_box())}")
        nb = RB.blocked_symbols()
        out.append(f"             stock breaker: {len(nb)} symbol(s) blocked"
                   + (f" — {', '.join(sorted(nb)[:8])}" if nb else ""))
    except Exception as e:
        out.append(f"breakers     UNREADABLE — {type(e).__name__}: {e}")

    try:
        from atlas.execution import sentiment_window as SW
        out.append(f"stand-down   {SW.line()}")
    except Exception as e:
        out.append(f"stand-down   UNREADABLE — {type(e).__name__}: {e}")

    try:
        from atlas.risk import breaker
        halted, why = breaker.is_halted()
        out.append(f"halt file    {'HALTED' if halted else 'clear'}"
                   + (f" — {str(why)[:160]}" if halted else ""))
    except Exception as e:
        out.append(f"halt file    UNREADABLE — {type(e).__name__}: {e}")

    try:
        rows = _get("/rest/v1/atlas_state?limit=1&order=updated_at.desc",
                    "agent state")
        mode = (rows or [{}])[0].get("mode") or "?"
        out.append(f"mode         {mode}")
    except Exception as e:
        out.append(f"mode         UNREADABLE — {type(e).__name__}: {e}")

    return out


# ══════════════════════════════════════════════════════════════════
# THE MESSAGE
# ══════════════════════════════════════════════════════════════════

def _pnl(v: float) -> str:
    """Rs, Indian grouping, and an EXPLICIT SIGN. _inr() renders a gain as "95"
    and a loss as "-95", which differ by one leading character in a message
    read on a phone -- and the sign is the only part of a P&L figure that
    cannot be misread without consequence."""
    v = float(v or 0)
    return f"{'+' if v >= 0 else '-'}Rs{_inr(abs(v))}"


def _mark(v: str) -> str:
    return {"CONFIRMED": "✅", "NONE": "❌", "UNKNOWN": "❓"}.get(v, "❓")


def build(slot: str = "1130") -> tuple:
    """-> (message, naked) where naked is the list of unprotected open rows."""
    label, day_end = SLOTS.get(slot, SLOTS["1130"])
    prot = broker_protection()
    trades = entries_today()
    opens = open_trades()
    closed = closed_today()
    evaluated, blocked, top = decisions_today()

    syms = sorted({t["symbol"] for t in opens})
    ltps = live_prices(syms)

    # ── the unprotected check, done FIRST because it decides the first line ──
    naked = []
    legs = {}
    for t in opens:
        legs[t["id"]] = confirm_legs(t, prot)
        if legs[t["id"]]["stop"] != "CONFIRMED":
            naked.append(t)

    L = []
    L.append(f"<b>ATLAS SESSION REPORT — {label} IST</b>")
    L.append(f"cumulative since {SESSION_START}  ·  {_today()}")

    # THE FIRST LINE, when it needs to be. Plain words, no jargon, before any
    # of the detail -- this is the 7 October failure class and burying it in a
    # list of positions is how it went unnoticed for a session.
    if naked:
        L.append("")
        L.append("🚨 <b>A POSITION IS OPEN WITH NO CONFIRMED STOP AT THE "
                 "BROKER.</b>")
        for t in naked:
            how = legs[t["id"]]
            L.append(f"   <b>{_esc(t['symbol'])}</b> {t['qty']} "
                     f"{t.get('product') or '?'} — stop {how['stop']}"
                     + (f", target {how['target']}" if day_end else ""))
        if day_end:
            L.append("   The exchange flushes day orders at 16:00. Unless a "
                     "GTT is resting, this position is unprotected overnight "
                     "and needs a manual stop NOW.")
        if not prot["ok"]:
            L.append(f"   (the broker could not be read: {_esc(prot['why'])} "
                     f"— so this may be a reporting failure rather than an "
                     f"unprotected position, and must be checked by hand)")

    # ── entries ───────────────────────────────────────────────────
    L.append("")
    L.append(f"<b>ENTRIES TODAY — {len(trades)}</b>")
    if not trades:
        L.append("  none")
    for t in trades:
        when = str(t.get("created_at") or "")[11:16]
        lg = legs.get(t["id"]) or confirm_legs(t, prot)
        kind = t.get("publication_kind") or "?"
        src = t.get("stop_source") or "?"
        L.append(f"  <b>{_esc(t['symbol'])}</b> {t.get('direction')} "
                 f"{t.get('qty')} @ Rs{float(t.get('entry_price') or 0):,.2f}"
                 f"  {when}  [{kind}]")
        L.append(f"     stop   Rs{float(t.get('stop_price') or 0):,.2f} "
                 f"({src})  {_mark(lg['stop'])} {lg['stop']}"
                 + (f" at Rs{lg['stop_at']}" if lg["stop_at"] else ""))
        tgt = t.get("target_price")
        L.append(f"     target "
                 + (f"Rs{float(tgt):,.2f}" if tgt else "—")
                 + f"  {_mark(lg['target'])} {lg['target']}"
                 + (f" at Rs{lg['target_at']}" if lg["target_at"] else ""))
        if t.get("published_stop") is not None and src not in ("published", "?"):
            L.append(f"     published stop was "
                     f"Rs{float(t['published_stop']):,.2f} — derived from {src}")

    # ── open positions ────────────────────────────────────────────
    unreal = 0.0
    L.append("")
    L.append(f"<b>OPEN — {len(opens)}</b>")
    if not opens:
        L.append("  none")
    for t in opens:
        ltp = ltps.get(t["symbol"])
        entry = float(t.get("entry_price") or 0)
        qty = int(t.get("qty") or 0)
        if ltp:
            sign = 1 if str(t.get("direction")).upper() == "LONG" else -1
            pnl = (ltp - entry) * qty * sign
            unreal += pnl
            pl = f"LTP Rs{ltp:,.2f}  P&L {_pnl(pnl)}"
        else:
            pl = "LTP unavailable — P&L not computed"
        L.append(f"  {_esc(t['symbol'])} {t.get('direction')} {qty} "
                 f"@ Rs{entry:,.2f}  {pl}")

    # ── closed ────────────────────────────────────────────────────
    real = sum(float(c.get("pnl_net") or c.get("pnl") or 0) for c in closed)
    L.append("")
    L.append(f"<b>CLOSED TODAY — {len(closed)}</b>")
    if not closed:
        L.append("  none")
    for c in closed:
        L.append(f"  {_esc(c['symbol'])} {c.get('direction')} "
                 f"{c.get('qty')} @ Rs{float(c.get('entry_price') or 0):,.2f} "
                 f"→ Rs{float(c.get('exit_price') or 0):,.2f}  "
                 f"{_pnl(float(c.get('pnl_net') or c.get('pnl') or 0))}  "
                 f"({_esc(c.get('exit_reason') or '—')})")

    L.append("")
    L.append(f"<b>DAY P&L  {_pnl(real + unreal)}</b>  "
             f"(realised {_pnl(real)} · open {_pnl(unreal)})")
    if not ltps and opens:
        L.append("  open P&L excludes positions whose LTP could not be read")

    # ── gates ─────────────────────────────────────────────────────
    L.append("")
    L.append("<b>GATES</b>")
    for g in gate_state():
        L.append(f"  {_esc(g)}")

    # ── candidates ────────────────────────────────────────────────
    L.append("")
    L.append(f"<b>CANDIDATES EVALUATED — {evaluated}</b>")
    if top:
        L.append(f"  most common blocker: <b>{_esc(top[0])}</b> "
                 f"({top[1]} of {sum(blocked.values())} blocked)")
    elif evaluated:
        L.append("  nothing blocked")
    else:
        L.append("  no decisions recorded yet today")

    # ── the day-end GTT handover ──────────────────────────────────
    if day_end:
        longs = [t for t in opens
                 if str(t.get("direction") or "").upper() == "LONG"]
        L.append("")
        L.append(f"<b>OVERNIGHT GTT HANDOVER — {len(longs)} open long(s)</b>")
        if not longs:
            L.append("  nothing to hand over")
        for t in longs:
            lg = legs.get(t["id"]) or confirm_legs(t, prot)
            recorded = t.get("exit_legs") or {}
            rec_ids = [v for k, v in recorded.items()
                       if "trigger_id" in k and v]
            live_ids = lg["gtt_ids"]
            # RECORDED AND LIVE ARE DIFFERENT QUESTIONS. A trigger_id in
            # exit_legs says ATLAS believes it placed one; the id coming back
            # from get_gtts() as active is the only thing that says it exists.
            missing = [i for i in rec_ids if i not in live_ids]
            if live_ids and not missing:
                verdict = f"✅ CONFIRMED — GTT {', '.join(map(str, live_ids))}"
            elif live_ids and missing:
                verdict = (f"⚠️ PARTIAL — live {live_ids}, recorded but NOT "
                           f"live {missing}")
            elif rec_ids:
                verdict = (f"❌ RECORDED BUT NOT LIVE — ATLAS recorded "
                           f"{rec_ids}; the broker has no active GTT")
            elif not prot["ok"]:
                verdict = f"❓ UNKNOWN — {prot['why']}"
            else:
                verdict = "❌ NO GTT PLACED"
            L.append(f"  {_esc(t['symbol'])} {t.get('qty')} "
                     f"{t.get('product') or '?'} "
                     f"[{t.get('exit_mechanism') or 'none'}] — {verdict}")

    body = "\n".join(L)
    return body, naked


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", default="1130", choices=sorted(SLOTS))
    ap.add_argument("--print", action="store_true",
                    help="build and print, send nothing")
    a = ap.parse_args()

    body, naked = build(a.slot)

    # THE DRILL FENCE. is_drill() is true when the halt sentinel is not the
    # canonical path or ATLAS_DRILL is set, which is exactly the condition
    # under which the suite runs -- so a test that builds a report cannot put
    # one in the operator's channel.
    try:
        from atlas.risk import breaker
        drilling, why = breaker.is_drill()
    except Exception:
        drilling, why = False, ""

    if a.print or drilling:
        print(body)
        if drilling:
            print(f"\n[not sent: drill fence — {why}]")
        return 0

    ok = send(_fit(body, ""))
    if not ok:
        log.error("SESSION REPORT NOT DELIVERED — Telegram send failed or is "
                  "unconfigured. The report exists only in this journal:\n"
                  + body)
    if naked:
        log.error(f"{len(naked)} open position(s) with no confirmed stop: "
                  + ", ".join(t["symbol"] for t in naked))
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
