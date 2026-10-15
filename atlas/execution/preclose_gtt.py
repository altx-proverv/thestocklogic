#!/usr/bin/env python3
"""
ATLAS — the pre-close handover: day-0 SL orders become resting GTTs.

    python3 -m atlas.execution.preclose_gtt              # act
    python3 -m atlas.execution.preclose_gtt --dry-run    # report only
    python3 -m atlas.execution.preclose_gtt --trail-only # skip new positions

WHY THIS RUNS BEFORE THE CLOSE AND NOT NEXT MORNING
---------------------------------------------------
A GTT checks the DEMAT account. Shares bought today reach demat at T+1, so a
GTT stop triggering on the purchase day can be rejected for want of holdings
even though the GTT was accepted. That is why day 0 is protected by a regular
SL order, which checks the ORDER BOOK.

But that SL order has DAY validity and dies at the 16:00 flush -- "all pending
orders are cancelled at the end of the day". If the GTT were placed the next
morning the position would be naked overnight, which is exactly when gaps
happen.

So the GTT goes in at 15:20, while the SL order is still live. A GTT placed
then cannot fire today -- continuous trading is over -- and by the next open the
shares are credited. The two mechanisms overlap rather than leaving a window.

    09:20-15:20   day-0 SL order resting        (order book)
    15:20         both GTTs placed             (overlap)
    16:00         the SL order is flushed
    next open     the GTTs carry the position   (demat)

WHAT IT DOES, IN ORDER
----------------------
  1. positions opened TODAY with a day-0 SL  ->  place the two GTTs
  2. positions opened EARLIER                ->  run the trail, modify the GTTs

THE TRAIL RUNS ONCE, HERE, AFTER THE CLOSE. No intraday churn: a stop that
moves on every tick is a stop that gets hit by noise, and every modification is
an API call that can fail halfway.

EVERY STOP CHANGE GOES THROUGH exits.tighten_only(). This module cannot loosen
a stop even if its own arithmetic asked it to -- that is enforced there, not
here, and deliberately so.
"""

from __future__ import annotations

import sys
import logging
import argparse
from pathlib import Path
from datetime import date, datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

import requests                                                   # noqa: E402
from atlas.config import (SUPABASE_URL, SUPABASE_KEY, OPEN_STATUSES,  # noqa: E402
                          PRECLOSE_GTT_TIME, log_halt_state)

log = logging.getLogger("ATLAS-PRECLOSE")
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def open_longs() -> tuple:
    """(readable, rows). FAILS CLOSED-ISH: unreadable returns readable=False.

    Concluding "nothing open" from a failed read is how a position goes
    unprotected overnight. The caller alerts instead.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?status=in.({','.join(OPEN_STATUSES)})&direction=eq.LONG"
            f"&select=id,symbol,direction,qty,product,entry_price,entry_date,"
            f"stop_price,exit_legs",
            headers=_headers(), timeout=20)
        if r.status_code != 200:
            log.error(f"open longs read failed: HTTP {r.status_code} "
                      f"{r.text[:140]}")
            return False, []
        return True, r.json() or []
    except Exception as e:
        log.error(f"open longs read failed: {e}")
        return False, []


def _patch_trade(trade_id, fields: dict) -> bool:
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{trade_id}",
            headers={**_headers(), "Prefer": "return=minimal"},
            json=fields, timeout=20)
        if r.status_code not in (200, 204):
            log.error(f"trade {trade_id}: PATCH HTTP {r.status_code} "
                      f"{r.text[:140]}")
            return False
        return True
    except Exception as e:
        log.error(f"trade {trade_id}: PATCH failed ({e})")
        return False


def _bars_for(symbol: str, through: date, n: int = 40):
    """Recent daily bars from the signals chain's parquets. [] if unavailable.

    READ-ONLY, and the only thing this module takes from the signals side. The
    trail needs a swing low and bhavcopy is where one comes from; if the file
    is missing the trail reports that and moves nothing, which is the correct
    failure -- a stop held is always safe.
    """
    try:
        import pandas as pd
        p = ROOT / "data/processed/stocks" / f"{symbol}.parquet"
        if not p.exists():
            return []
        df = pd.read_parquet(p)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df[df["date"] <= through].sort_values("date").tail(n)
        return [{"date": r["date"], "high": float(r["high"]),
                 "low": float(r["low"]), "close": float(r["close"])}
                for _, r in df.iterrows()]
    except Exception as e:
        log.warning(f"{symbol}: bars unavailable ({e})")
        return []


def handover(trade: dict, kite, dry: bool = False) -> dict:
    """A position opened TODAY: place the two overnight GTTs."""
    from atlas.execution import exits as X
    sym = trade["symbol"]
    qty = int(trade.get("qty") or 0)
    entry = float(trade.get("entry_price") or 0)
    stop = float(trade.get("stop_price") or 0)
    if not (qty and entry and stop):
        return {"ok": False, "symbol": sym,
                "reason": f"incomplete row: qty={qty} entry={entry} stop={stop}"}

    oco_q, trail_q = X.split_quantity(qty)
    if dry:
        return {"ok": True, "symbol": sym, "dry": True,
                "reason": f"would place OCO {oco_q} + stop-only {trail_q} at "
                          f"stop {stop}"}

    out = X.place_overnight_gtts(symbol=sym, direction="LONG", qty=qty,
                                 fill_price=entry, stop_price=stop, kite=kite)
    if not out.get("ok"):
        # A POSITION HALF PROTECTED IS NOT PROTECTED. The day-0 SL is still
        # live until 16:00, so there is a little time -- but the overnight
        # mechanism is broken and ATLAS must not open more positions into it.
        return {"ok": False, "symbol": sym, "reason": out.get("reason"),
                "legs": out.get("legs") or {}}

    legs = {**(trade.get("exit_legs") or {}), **(out.get("legs") or {})}
    _patch_trade(trade["id"], {"exit_legs": legs,
                               "exit_mechanism": "GTT_SPLIT",
                               "target_price": out.get("target")})
    return {"ok": True, "symbol": sym, "reason": out.get("reason"),
            "legs": out.get("legs")}


def trail(trade: dict, kite, today: date, dry: bool = False) -> dict:
    """A position opened EARLIER: recompute the stop and modify the GTTs.

    ONLY THE TRAILED HALF MOVES. The OCO half keeps its original stop, because
    moving it would mean modifying a two-leg instruction whose other leg is a
    target -- and the whole point of the split is that one half is booked and
    one half runs.
    """
    from atlas.execution import exits as X
    sym = trade["symbol"]
    entry = float(trade.get("entry_price") or 0)
    cur_stop = float(trade.get("stop_price") or 0)
    legs = trade.get("exit_legs") or {}
    tid = legs.get("trail_trigger_id")
    trail_q = int(legs.get("trail_qty") or 0)

    if not tid or not trail_q:
        # THIS RETURNED ok=True AND THAT WAS THE TRAP. "No recorded leg" was
        # treated as "nothing to do", which is true when the position is
        # protected by something else and false when it is protected by
        # nothing -- and the two were indistinguishable because this read
        # exit_legs, the column that is empty precisely when placement failed.
        # ATHERENERG, entered 15 October, took this branch on 16 October and
        # the job reported success while the position was naked.
        #
        # So ask the broker. Protected by something we did not record is fine
        # (nothing to trail, nothing to fix). Protected by nothing is a
        # FAILURE, and the caller repairs it.
        try:
            from atlas.execution import protection as PR
            prot = PR.broker_protection(kite=kite)
            legs_live = PR.confirm_legs(trade, prot)
        except Exception as e:
            return {"ok": False, "symbol": sym, "moved": False,
                    "needs_protection": True,
                    "reason": f"no recorded leg and the broker could not be "
                              f"read to check for one ({type(e).__name__}: {e})"}
        if legs_live["stop"] == PR.CONFIRMED:
            return {"ok": True, "symbol": sym, "moved": False,
                    "reason": f"no stop-only GTT recorded to trail, but a stop "
                              f"IS confirmed at the broker "
                              f"({legs_live['detail'][:120]}) — nothing to move"}
        return {"ok": False, "symbol": sym, "moved": False,
                "needs_protection": True,
                "reason": (f"NOTHING IS PROTECTING THIS POSITION. No stop-only "
                           f"GTT recorded and the broker shows no stop "
                           f"({legs_live['stop']}: {legs_live['detail'] or prot.get('why')})")}
    if not (entry and cur_stop):
        return {"ok": False, "symbol": sym, "moved": False,
                "reason": f"incomplete row: entry={entry} stop={cur_stop}"}

    ed = trade.get("entry_date")
    if isinstance(ed, str):
        try:
            ed = date.fromisoformat(ed)
        except ValueError:
            ed = None
    bars = _bars_for(sym, today)
    if not bars:
        return {"ok": True, "symbol": sym, "moved": False,
                "reason": "no bars — stop held, which is always the safe answer"}

    since = [b for b in bars if ed is None or b["date"] >= ed]
    if not since:
        since = bars[-1:]
    high = max(b["high"] for b in since)
    last_close = since[-1]["close"]
    swing = X.swing_low_since(since)

    try:
        nxt = X.next_stop(entry, cur_stop, high, last_close, swing, "LONG")
    except X.StopWouldLoosen as e:
        # THE GUARD FIRED. That is a bug upstream, not a condition to handle --
        # report it loudly and move nothing.
        log.error(f"{sym}: trail produced a LOOSER stop and was refused: {e}")
        return {"ok": False, "symbol": sym, "moved": False, "reason": str(e)}

    if not nxt["moved"]:
        return {"ok": True, "symbol": sym, "moved": False,
                "reason": nxt["why"], "stop": nxt["stop"]}
    if dry:
        return {"ok": True, "symbol": sym, "moved": True, "dry": True,
                "stop": nxt["stop"], "reason": nxt["why"]}

    new_stop = nxt["stop"]
    # THE INSTRUMENT'S OWN TICK. This was X.round_tick(...) on the 0.05
    # constant, which is one of the four prices the broker refused on
    # 2026-10-15 -- the trail writes a GTT trigger, so it is broker-bound.
    try:
        _tick, _ = X._tick_or_raise(sym)
    except Exception as e:
        log.error(f"{sym}: cannot price a trailed stop ({e}) — leaving the "
                  f"existing GTT alone")
        return {"ok": False, "symbol": sym, "moved": False,
                "reason": f"cannot price a stop: {e}"}
    trig = X.stop_trigger(new_stop, "LONG", tick=_tick, symbol=sym)
    lim = X.gtt_leg_limit(trig, "LONG", tick=_tick, symbol=sym)
    order = {"exchange": "NSE", "tradingsymbol": sym,
             "transaction_type": "SELL", "quantity": trail_q,
             "order_type": "LIMIT", "product": "CNC", "price": lim}
    try:
        kite.modify_gtt(trigger_id=tid, trigger_type="single",
                        tradingsymbol=sym, exchange="NSE",
                        trigger_values=[trig], last_price=last_close,
                        orders=[order])
    except Exception as e:
        log.error(f"{sym}: could not modify trailing GTT {tid} "
                  f"({type(e).__name__}: {e}) — the OLD stop is still resting")
        return {"ok": False, "symbol": sym, "moved": False,
                "reason": f"modify_gtt failed: {type(e).__name__}: {e}"}

    _patch_trade(trade["id"], {"stop_price": new_stop})
    log.info(f"{sym}: trailed stop {cur_stop} -> {new_stop} "
             f"(trigger {trig} limit {lim}) — {nxt['why']}")
    return {"ok": True, "symbol": sym, "moved": True, "stop": new_stop,
            "reason": nxt["why"]}


def run(dry: bool = False, trail_only: bool = False,
        now_ist: datetime = None) -> int:
    now = now_ist or datetime.now(IST)
    log_halt_state(log)

    try:
        sys.path.insert(0, str(ROOT / "engine"))
        from trading_calendar import is_trading_day
        if not is_trading_day(now.date()):
            log.info(f"{now.date()} is not an NSE trading day — nothing to do")
            return 0
    except Exception as e:
        log.warning(f"trading calendar unreadable ({e}) — proceeding")

    readable, rows = open_longs()
    if not readable:
        _alert("PRECLOSE BLIND",
               "Could not read open positions before the close. Any position "
               "opened today still has its day-0 SL, which DIES AT 16:00 — if "
               "this is not fixed before then it will be naked overnight.")
        return 1
    if not rows:
        log.info("no open long positions")
        return 0

    kite = None
    if not dry:
        from atlas.execution.broker import get_kite
        kite = get_kite()
        if kite is None:
            _alert("PRECLOSE NO BROKER",
                   "No broker session before the close. GTTs were NOT placed; "
                   "any position opened today loses its stop at 16:00.")
            return 1

    today = now.date()

    # ── WHAT IS ACTUALLY PROTECTED, read from the broker ──────────
    #
    # THIS USED TO SPLIT ON entry_date ALONE: entered today -> handover,
    # entered earlier -> trail. That routing assumes a position older than
    # today already HAS its GTTs, which is true only when its own handover
    # succeeded. When one fails, the position is "older" the next morning and
    # routes to a trail that finds nothing to trail -- and no path in the
    # system ever places the protection that was missed. That is how
    # ATHERENERG went a session and a night naked.
    #
    # Age now decides only the MECHANISM. Whether to act at all is decided by
    # what the broker is holding.
    from atlas.execution import protection as PR
    prot = PR.broker_protection(kite=kite) if not dry else {"ok": False,
            "why": "dry run — the broker was not read", "orders": {}, "gtts": {}}

    new_today, older, unprotected = [], [], []
    for t in rows:
        ed = t.get("entry_date")
        if isinstance(ed, str):
            try:
                ed = date.fromisoformat(ed)
            except ValueError:
                ed = None
        protected = prot.get("ok") and PR.is_protected(t, prot)
        if not protected and not dry:
            unprotected.append(t)
        elif ed == today:
            new_today.append(t)
        else:
            older.append(t)

    log.info(f"{len(rows)} open long(s): {len(unprotected)} UNPROTECTED "
             f"(repair), {len(new_today)} opened today (handover), "
             f"{len(older)} older (trail)")

    failures = []

    # ── THE REPAIR, FIRST ─────────────────────────────────────────
    #
    # Before the handover and before the trail, because a position with no
    # protection at all is a different order of problem from a stop that could
    # be a little tighter. At the pre-close every repair uses a GTT regardless
    # of the position's age: a day-0 SL placed at 15:20 is flushed at 16:00,
    # which is the window this whole job exists to close.
    if unprotected and not dry:
        print(f"\n── REPAIR — open with NO protection at the broker ──")
        for t in unprotected:
            r = PR.ensure_protected(t, kite=kite, prot=prot, now_ist=now,
                                    at_preclose=True)
            flg = "ok " if r["ok"] else "FAIL"
            print(f"  [{flg}] {r['symbol']:<12} {r['action']:<20} "
                  f"{str(r.get('reason', ''))[:70]}")
            if r["ok"] and r.get("legs"):
                _patch_trade(t["id"], {"exit_legs": {**(t.get("exit_legs") or {}),
                                                     **(r.get("legs") or {})},
                                       "exit_mechanism": r.get("mechanism") or "",
                                       **({"target_price": r["target"]}
                                          if r.get("target") else {})})
            if not r["ok"]:
                failures.append(f"{r['symbol']}: {r.get('reason')}")

    if not trail_only:
        print(f"\n── HANDOVER — positions opened {today} ──")
        for t in new_today:
            r = handover(t, kite, dry=dry)
            flag = "ok " if r["ok"] else "FAIL"
            print(f"  [{flag}] {r['symbol']:<12} {r.get('reason','')[:92]}")
            if not r["ok"]:
                failures.append(f"{r['symbol']}: {r.get('reason')}")

    print(f"\n── TRAIL — positions opened before {today} ──")
    moved = 0
    for t in older:
        r = trail(t, kite, today, dry=dry)
        flag = "ok " if r["ok"] else "FAIL"
        mv = "MOVED" if r.get("moved") else "held "
        print(f"  [{flag}] {mv} {r['symbol']:<12} "
              f"{('-> ' + str(r.get('stop'))) if r.get('moved') else '':<12}"
              f"{r.get('reason','')[:72]}")
        moved += 1 if r.get("moved") else 0
        if not r["ok"]:
            failures.append(f"{r['symbol']}: {r.get('reason')}")

    print(f"\n  {len(new_today)} handover(s), {moved} stop(s) moved, "
          f"{len(failures)} failure(s)")

    if failures and not dry:
        # A FAILED HANDOVER IS AN OVERNIGHT NAKED POSITION -- and it is one
        # SYMBOL's naked position. This used to call breaker.halt(), which on
        # 2026-10-15 took the whole account down over ATHERENERG's tick size
        # and refused 443 published rows the next morning in every sector.
        #
        # Each failing symbol is blocked for the session and registered as
        # unprotected, so the alert repeats every 15 minutes until a stop is
        # confirmed. Nothing stands the day down, and nothing survives it.
        from atlas.risk import symbol_block as SB
        from atlas.execution import protection as PR
        _by_sym = {str(t.get("symbol")): t for t in rows}
        for f in failures:
            _sym = str(f).split(":", 1)[0].strip()
            SB.block(_sym, SB.REASON_PROTECTION_FAILED,
                     f"pre-close protection failed: {str(f)[:300]}")
            _t = _by_sym.get(_sym)
            if _t:
                PR.register(_t, f"pre-close protection failed: {str(f)[:300]}",
                            wanted_stop=_t.get("stop_price"))
        _alert("PRECLOSE HANDOVER FAILED",
               "The day-0 SL order dies at 16:00 and the overnight GTT is NOT "
               "in place for:\n" + "\n".join(f"· {f}" for f in failures[:6])
               + "\n\nTHIS NEEDS A HUMAN BEFORE 16:00 — place these stops by "
                 "hand. Those symbols are blocked for the rest of the session; "
                 "ATLAS has NOT stood the day down and every other symbol is "
                 "unaffected. Send /pause if you want entries stopped.")
        return 1
    return 0


def _alert(kind: str, text: str) -> None:
    log.error(f"[{kind}] {text}")
    try:
        from atlas.reporting.telegram import send
        if not send(f"<b>ATLAS — {kind}</b>\n{text}"):
            log.error(f"ALERT NOT DELIVERED [{kind}] — Telegram failed or is "
                      f"unconfigured. This alert exists only in this log.")
    except Exception as e:
        log.error(f"ALERT NOT DELIVERED [{kind}] ({e})")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--trail-only", action="store_true")
    a = ap.parse_args()
    sys.exit(run(dry=a.dry_run, trail_only=a.trail_only))
