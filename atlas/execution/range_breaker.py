"""
THE RANGE-BREAK BREAKERS — MARKET LEVEL AND STOCK LEVEL
=======================================================

THE MECHANIC, BOTH LEVELS. The box is the OPENING RANGE -- the high and low of
09:15 to 10:00, computed once when that range completes and held unchanged for
the rest of the session. A one-minute CLOSE outside it, on the side that goes
AGAINST the position, is a confirmed change of character. One close is enough.

A STRUCTURAL FACT, NOT A VOTE. A level either printed through or it did not.
There is no threshold here and none is to be added -- the 24-of-30 sentiment
vote deleted on 2026-10-15 was a vote over a continuous reading, which is what
made it tunable and therefore arbitrary.

────────────────────────────────────────────────────────────────────
A FIXED RANGE, NOT A ROLLING BOX  (changed 2026-10-15)
────────────────────────────────────────────────────────────────────
This shipped as a rolling 45-cycle box and was wrong in both directions at
once. A rolling box descends with the price, so it MISSES A SLOW GRIND -- the
box follows the decline down and is never broken -- and it FIRES ON AN ORDINARY
PULLBACK, because by mid-morning the box has risen and a normal retrace prints
below it. Replayed over eight sessions it fired on 8 of 8, which is a
description of normal intraday movement rather than of a change of character.

The opening range is a far higher bar: a move has to undo the whole morning to
break it. It is also how a trader actually reads a day, and it does not need a
"does the box contain the current price" caveat, because the range closed at
10:00 and everything tested against it happened afterwards.

COMPUTED ONCE AND PERSISTED. The levels cannot be recomputed after the fact
from a live feed that only serves today, so losing them to a process restart
would silently disarm the breaker for the rest of the session. They are written
to BREAKER_RANGE_FILE keyed to the session date; a file from another date is
discarded.

────────────────────────────────────────────────────────────────────
CLOSE-BASIS, NOT A WICK  (changed 2026-10-15)
────────────────────────────────────────────────────────────────────
A break requires the one-minute CLOSE to be outside the range. Touching
outside and closing back inside is not a break:

    floor 25,090:  low 25,085 close 25,096  ->  no break
                   low 25,085 close 25,087  ->  break

WHY: a wick below an obvious low is usually stops being swept, and THIS
CODEBASE'S OWN SMC LOGIC READS THAT AS BULLISH -- bull_liq_sweep is a scoring
input in engine/03b_score.py and one of the conditions that rescues a long from
bear_regime_no_reversal. The breaker was treating the same event as a reason to
stop the day.

The probe is the close of the most recent COMPLETED minute, so the breaker is
up to a minute slower than a wick test. Accepted: the stop-loss order protects
the position, the breaker only stops new entries. The live last price is read
for DISPLAY -- how far price is from the edge -- and deliberately not for the
break test, because folding it in would reintroduce wick behaviour.

STILL ONE OCCURRENCE. No counting, no vote, no threshold. What changed is what
counts as an occurrence, not how many are needed.

────────────────────────────────────────────────────────────────────
THE ORB MACHINERY THAT WAS ALREADY HERE
────────────────────────────────────────────────────────────────────
Asked to look before building: index_state.get_first15_range() computes a
09:15-09:30 high/low from the same intraday endpoint, and
data/orb_levels_today.json holds an empty {}.

THE EMPTY FILE MEANS UNUSED, NOT BROKEN. Nothing in the tree writes it --
it was committed once (6c7dbfc) as {} and no code path has ever touched it.
get_first15_range() and classify_index() have no callers at all, and
index_state is not in any cron line or unit; the only live import from that
module is get_market_direction(), the sector_heatmap fallback. So it is
never-run code rather than a mechanism that fails.

WHAT WAS REUSED: the approach, which is sound -- pull the intraday 1-minute
series once, filter by minute-of-day in IST, take max(high) and min(low).
opening_range() below is that same shape over 09:15-10:00 instead of
09:15-09:30. It is not called directly because its window is fixed at 15
minutes and it is index-keyed, while this needs the future and individual
stocks. The dead ORB code and the empty artifact are left alone in this pass
and are worth deleting in their own.

────────────────────────────────────────────────────────────────────
THE DATA, AS IT ACTUALLY IS
────────────────────────────────────────────────────────────────────
Probed against the live Upstox v2 API, 2026-10-15:

  /historical-candle/intraday/{key}/1minute   HTTP 200 for the NIFTY future,
      the Nifty 50 index AND an individual NSE_EQ stock. Shape
      [ts, open, high, low, close, volume, oi]. One call returns the whole of
      today's 1-minute series, so the prior-45 box costs ONE call per symbol
      per cycle, not 45.
  Per-stock 1-minute history is real: RELIANCE 2026-10-13 returned 375
      candles, 09:15 to 15:29, with volume on every bar.
  THERE IS NO BATCH CANDLE ENDPOINT. Two keys to the intraday endpoint
      returns HTTP 400 UDAPI100011 "Invalid Instrument key". market-quote
      /quotes batches 500 keys in one call; candles are one key per call.
  Cost measured: 28ms per intraday fetch, 5 sequential in 0.14s.

WHAT THAT COSTS AT A REALISTIC CANDIDATE COUNT. 2026-10-14 ran 360 cycles and
2,233 candidate evaluations, about 6.2 candidates a cycle. With one open
position that is ~7 symbols plus the NIFTY future = 8 calls a cycle, ~0.22s of
wall clock, and about 2,880 calls over a session. The market box is fetched
once per cycle and shared; stock boxes are fetched per symbol because there is
no way to batch them.

THE ONE PREMISE I COULD NOT TEST. The market was closed while this was built,
so the intraday endpoint returned HTTP 200 with ZERO candles for every
instrument. That it serves PARTIAL-day candles during a session is the
documented behaviour and the shape is right, but it is unverified here, and
nothing else in this repo exercises it (index_state.get_first15_range does,
and data/orb_levels_today.json has been an empty {} since August). If it turns
out not to serve partial days, every evaluation reports
BREAKER_CANNOT_EVALUATE and nothing fires -- which is the fail-open direction,
so the failure is loud and harmless rather than silent and dangerous. Check it
at about 10:05 on any session with:

    venv/bin/python -c "from atlas.execution.range_breaker import market_box; \\
        print(market_box())"

────────────────────────────────────────────────────────────────────
FAIL OPEN, DELIBERATELY, AND DO NOT REVERSE THIS
────────────────────────────────────────────────────────────────────
Insufficient candles, a feed failure, a gap in the history: the breaker does
NOT fire and does NOT report all-clear. It reports BREAKER_CANNOT_EVALUATE as
its own distinct state, in the log and in the inventory.

THE REASONING, so nobody flips it by accident: this breaker is ONE-WAY. A
stand-down lasts the rest of the session and does not re-arm. Failing closed
would mean a single feed hiccup at 10:03 permanently ends a good trading day,
and the feed has already been observed returning 401s overnight and an empty
row for a contract it had answered for minutes earlier. The stop-loss order is
the real protection for an open position; this breaker is a refinement on top
of it, and a refinement that can self-destruct the day on a dropped packet is
worse than no refinement. Fail open, say so loudly, and let the stop do its job.

────────────────────────────────────────────────────────────────────
IT NEVER EXITS A POSITION
────────────────────────────────────────────────────────────────────
Neither level closes anything. The stop-loss order owns that decision; two
systems racing to close one position is how this goes wrong. This module
contains no reference to place_order, place_gtt, modify_gtt, cancel_order or
any exit path, and tests/test_range_breaker.py asserts it.

The market breaker blocks NEW ENTRIES for the session. The stock breaker blocks
NEW ENTRIES IN ONE SYMBOL for the session. A stock-level break does not stand
the day down -- only the market breaker does that.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("ATLAS-RANGE-BREAKER")
IST = timezone(timedelta(hours=5, minutes=30))

NIFTY_FUT = None          # resolved per day from live_sentiment

# ── REASON CODES ──────────────────────────────────────────────────
# NAMED FOR WHAT ACTUALLY HAPPENS. The code the market breaker fires used to be
# SENTIMENT_REVERSAL_STAND_DOWN, from the vote it replaced -- and this project
# has now been bitten three times by a message naming the wrong mechanism: a
# GTT alert that fired on an SL failure, "operator PAUSED" for a self-halt, and
# the 361-of-746 figure that did not reproduce.
REASON_MARKET_BREAK = "RANGE_BREAK_STAND_DOWN"
REASON_SYMBOL_BREAK = "SYMBOL_RANGE_BREAK"
REASON_CANNOT_EVALUATE = "BREAKER_CANNOT_EVALUATE"
REASON_DORMANT = "BREAKER_DORMANT"
REASON_NOT_ARMED = "BREAKER_NOT_ARMED"


def _cfg():
    from atlas.config import (OPENING_RANGE_START, OPENING_RANGE_END,
                              BREAKER_OBSERVE_ONLY, BREAKER_RANGE_FILE,
                              BREAKER_BLOCKED_FILE)
    return (str(OPENING_RANGE_START), str(OPENING_RANGE_END),
            bool(BREAKER_OBSERVE_ONLY), Path(BREAKER_RANGE_FILE),
            Path(BREAKER_BLOCKED_FILE))


def _hm(s: str) -> tuple:
    h, m = (int(x) for x in str(s).split(":"))
    return h, m


def observe_only() -> bool:
    return _cfg()[2]


def _now_ist():
    return datetime.now(IST)


def armed(now=None) -> tuple:
    """(is_armed, why). The breaker arms when the opening range COMPLETES.

    Not before: until 10:00 the range is still being set and there is no level
    to break. This is the honest arming time -- the previous version armed at
    09:50 and then reported "cannot evaluate" for ten minutes because its box
    was not full yet.
    """
    start, end, _, _, _ = _cfg()
    now = now or _now_ist()
    if (now.hour, now.minute) < _hm(end):
        return False, (f"not armed until {end} IST — the {start}-{end} opening "
                       f"range is still forming (now {now:%H:%M})")
    return True, f"armed: the {start}-{end} opening range is complete"


# ══════════════════════════════════════════════════════════════════
# THE BOX
# ══════════════════════════════════════════════════════════════════

def _candles(instrument_key: str) -> list:
    """Today's 1-minute candles, oldest first. [] on any failure."""
    from engine.upstox_ws import BASE_URL, get_headers
    import requests
    r = requests.get(
        f"{BASE_URL}/historical-candle/intraday/{instrument_key}/1minute",
        headers=get_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} from the intraday endpoint")
    c = r.json().get("data", {}).get("candles", []) or []
    return sorted(c, key=lambda x: x[0])


def _range_load() -> dict:
    day = _now_ist().date().isoformat()
    _, _, _, path, _ = _cfg()
    blank = {"session_date": day, "ranges": {}, "observed": []}
    try:
        if path.exists():
            d = json.loads(path.read_text())
            if d.get("session_date") == day:
                return {**blank, **d}
            log.info(f"range file is from {d.get('session_date')}, today is "
                     f"{day} — starting fresh")
    except Exception as e:
        log.error(f"could not read {path} ({e}) — the range will be recomputed")
    return blank


def _range_save(d: dict) -> None:
    _, _, _, path, _ = _cfg()
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(path) == "/var/lib/atlas/breaker_range.json":
        log.warning(f"DRILL ({why}) — refusing to write the live range file")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)
    except Exception as e:
        log.error(f"could not persist the opening range to {path} ({e}) — a "
                  f"restart would lose the morning's levels and silently "
                  f"disarm the breaker")


def opening_range(instrument_key: str, now=None, candles: list = None,
                  use_cache: bool = True) -> dict:
    """The 09:15-10:00 high/low, computed once and held for the session.

    -> {ok, high, low, candles_used, first, last, probe_close, reason_code, ...}

    SAME SHAPE AS index_state.get_first15_range: pull the intraday series once,
    filter by minute-of-day in IST, take max(high) and min(low). That window is
    fixed at 09:15-09:30 and index-keyed, so it is not called directly.
    """
    start, end, _, _, _ = _cfg()
    now = now or _now_ist()
    out = {"ok": False, "reason_code": REASON_CANNOT_EVALUATE, "detail": "",
           "high": None, "low": None, "candles_used": 0,
           "range_start": start, "range_end": end,
           "first": None, "last": None, "probe_close": None,
           "probe_minute": None, "current_minute": now.strftime("%H:%M"),
           "instrument": instrument_key, "from_cache": False}

    if (now.hour, now.minute) < _hm(end):
        out["reason_code"] = REASON_NOT_ARMED
        out["detail"] = (f"the {start}-{end} opening range is still forming "
                         f"(now {now:%H:%M})")
        return out

    # THE RANGE IS COMPUTED ONCE. After 10:00 it never changes, so a cached
    # value is not an optimisation, it is the definition -- and it is what
    # survives a restart, since a live feed cannot rebuild this afternoon.
    cached = None
    if use_cache:
        cached = _range_load().get("ranges", {}).get(instrument_key)

    cs = candles
    if cs is None:
        try:
            cs = _candles(instrument_key)
        except Exception as e:
            if cached:
                cs = []          # fall through to the cached levels
            else:
                out["detail"] = f"candles unavailable: {type(e).__name__}: {e}"
                return out

    if cached:
        out.update({"high": float(cached["high"]), "low": float(cached["low"]),
                    "candles_used": int(cached.get("candles_used", 0)),
                    "first": cached.get("first"), "last": cached.get("last"),
                    "from_cache": True, "ok": True, "reason_code": None})
    else:
        if not cs:
            out["detail"] = ("the intraday endpoint returned no candles — no "
                             "opening range to compute")
            return out
        s_hm, e_hm = _hm(start), _hm(end)
        win = [c for c in cs
               if s_hm <= _hm(str(c[0])[11:16]) < e_hm]
        if not win:
            out["detail"] = (f"no candles inside {start}-{end} — the session "
                             f"may not have opened")
            return out
        out.update({"high": max(float(c[2]) for c in win),
                    "low": min(float(c[3]) for c in win),
                    "candles_used": len(win),
                    "first": str(win[0][0])[11:16],
                    "last": str(win[-1][0])[11:16],
                    "ok": True, "reason_code": None})
        d = _range_load()
        d.setdefault("ranges", {})[instrument_key] = {
            "high": out["high"], "low": out["low"],
            "candles_used": out["candles_used"],
            "first": out["first"], "last": out["last"],
            "computed_at": now.isoformat(timespec="seconds")}
        _range_save(d)

    # THE PROBE IS THE LAST COMPLETED MINUTE'S CLOSE. The current minute is
    # still forming, so its "close" is just the last trade -- using it would be
    # a wick test by another name, which is the thing being removed.
    cur_key = now.strftime("%Y-%m-%dT%H:%M")
    done = [c for c in (cs or []) if str(c[0])[:16] < cur_key]
    if done:
        out["probe_close"] = float(done[-1][4])
        out["probe_minute"] = str(done[-1][0])[11:16]
    out["detail"] = (f"range {out['low']:.2f}-{out['high']:.2f} from "
                     f"{out['candles_used']} candles "
                     f"{out['first']}-{out['last']}"
                     + (" (cached)" if out["from_cache"] else "")
                     + (f"; last close {out['probe_close']:.2f} at "
                        f"{out['probe_minute']}"
                        if out["probe_close"] is not None else
                        "; no completed minute yet"))
    return out


# Kept as the name the rest of the module and the tests call.
box = opening_range


def with_last_price(b: dict, last_price) -> dict:
    """Attach the live last price FOR DISPLAY ONLY.

    DELIBERATELY NOT PART OF THE BREAK TEST. It used to widen the probe so a
    tick outside the box counted; the break is close-basis now, and folding the
    live price back in would reintroduce exactly the wick behaviour that was
    removed. It is carried so the inventory can say how far price is from the
    edge.
    """
    if last_price in (None, 0):
        return b
    b = dict(b)
    b["last_price"] = float(last_price)
    return b


def verdict(b: dict, side: str) -> dict:
    """Did a one-minute CLOSE land outside the range, AGAINST `side`? Pure.

    THE DIRECTION LOGIC LIVES HERE AND NOWHERE ELSE.

    LONG  -> the FLOOR is the level that matters. A close above the ceiling is
             a breakout in ATLAS's favour and must NOT fire.
    SHORT -> the CEILING matters. A close below the floor is in its favour.

    Getting this backwards would stop ATLAS on its winners, which is the single
    worst outcome available here, so it is one function, tested in every
    combination, and nothing else decides direction.

    CLOSE-BASIS. `probe_close` is the close of the last COMPLETED minute. A low
    that pierced the floor and recovered does not reach this function at all --
    that is a stop sweep, which this codebase's own SMC logic scores as bullish.
    """
    out = {"broke": False, "level": None, "price": None, "side": side,
           "favourable_break": False}
    if not b.get("ok") or b.get("probe_close") is None:
        return out
    c = float(b["probe_close"])
    s = str(side or "").upper()
    if s == "LONG":
        if c < b["low"]:
            out.update({"broke": True, "level": "floor", "price": c})
        elif c > b["high"]:
            # IN ATLAS'S FAVOUR. Recorded so the replay can count these and
            # prove the direction logic held, and explicitly NOT a break.
            out["favourable_break"] = True
    elif s == "SHORT":
        if c > b["high"]:
            out.update({"broke": True, "level": "ceiling", "price": c})
        elif c < b["low"]:
            out["favourable_break"] = True
    return out


# ══════════════════════════════════════════════════════════════════
# THE MARKET BREAKER
# ══════════════════════════════════════════════════════════════════

def held_side(rows: list = None) -> tuple:
    """(side, detail) — the side ATLAS currently holds, or None.

    DORMANT WHEN FLAT. With nothing open there is nothing to protect and the
    breaker must not fire: a range break against a position ATLAS does not
    have is not an event.

    Holding both sides returns None as well, and says so. ATLAS is long-only
    today so it cannot arise, but a breaker that silently picked one side of a
    hedge would be choosing which position to protect.
    """
    if rows is None:
        try:
            from atlas.execution.preclose_gtt import OPEN_STATUSES
            from atlas.config import SUPABASE_URL, SUPABASE_KEY
            import requests
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/atlas_trades"
                f"?status=in.({','.join(OPEN_STATUSES)})"
                f"&select=symbol,direction", headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}"}, timeout=15)
            if r.status_code != 200:
                return None, f"open positions unreadable: HTTP {r.status_code}"
            rows = r.json() or []
        except Exception as e:
            return None, f"open positions unreadable: {type(e).__name__}: {e}"
    sides = {str(x.get("direction", "")).upper() for x in rows}
    sides = {s for s in sides if s in ("LONG", "SHORT")}
    if not sides:
        return None, "no open positions — dormant"
    if len(sides) > 1:
        return None, f"both sides open ({sorted(sides)}) — dormant, deliberately"
    side = sides.pop()
    return side, f"{len(rows)} open {side} position(s)"


def _record_observed(scope: str, detail: str) -> None:
    """Persist a would-be fire while BREAKER_OBSERVE_ONLY is on.

    SO THE INVENTORY CANNOT READ AS AN ALL-CLEAR. "nothing fired" and
    "something fired and was ignored" are different facts, and while observing
    the second one is the whole point of the exercise.
    """
    d = _range_load()
    rec = {"scope": scope, "detail": detail,
           "at": _now_ist().isoformat(timespec="seconds")}
    if any(o.get("scope") == scope and o.get("detail") == detail
           for o in d.get("observed", [])):
        return
    d.setdefault("observed", []).append(rec)
    _range_save(d)
    log.error("=" * 70)
    log.error(f"OBSERVE-ONLY — WOULD HAVE FIRED ({scope}): {detail[:300]}")
    log.error("Nothing was blocked. BREAKER_OBSERVE_ONLY is True.")
    log.error("=" * 70)
    try:
        from atlas.reporting.telegram import send
        send(f"👁 <b>RANGE BREAK — OBSERVED, NOT ACTED ON</b>\n"
             f"<code>{detail[:320]}</code>\n\n"
             f"<b>Nothing was blocked.</b> BREAKER_OBSERVE_ONLY is True, so "
             f"this is evidence only — entries continue as normal and the day "
             f"is NOT stood down.\n"
             f"EXITS ARE UNAFFECTED either way: the stop-loss order owns the "
             f"exit decision.")
    except Exception as e:
        log.error(f"could not send the observed-break alert: {e}")


def observed() -> list:
    """Would-be fires recorded today while observing. Read-only."""
    return list(_range_load().get("observed", []))


def market_box(now=None, candles: list = None, last_price=None) -> dict:
    """The NIFTY near-month future's box. The same instrument Gate 1 reads."""
    from atlas.execution.live_sentiment import near_month_future
    try:
        fut = near_month_future()
    except Exception as e:
        return {"ok": False, "reason_code": REASON_CANNOT_EVALUATE,
                "detail": f"could not resolve the NIFTY future: {e}",
                "instrument": None}
    b = box(fut["key"], now=now, candles=candles)
    b["trading_symbol"] = fut["symbol"]
    if last_price is not None:
        b = with_last_price(b, last_price)
    return b


def market_check(now=None, rows=None, candles=None, last_price=None) -> dict:
    """Evaluate the market breaker and stand the day down if it broke.

    -> a state dict. Never raises.
    """
    from atlas.execution import sentiment_window as SD
    out = {"fired": False, "state": None, "detail": "", "box": None,
           "side": None, "verdict": None}
    already = SD.state()
    if already["stood_down"]:
        out.update({"state": "stood_down",
                    "detail": f"already stood down: {already['detail'][:160]}"})
        return out

    ok_armed, why_armed = armed(now)
    if not ok_armed:
        out.update({"state": REASON_NOT_ARMED, "detail": why_armed})
        return out

    side, side_detail = held_side(rows)
    out["side"] = side
    if side is None:
        out.update({"state": REASON_DORMANT, "detail": side_detail})
        return out

    b = market_box(now=now, candles=candles, last_price=last_price)
    out["box"] = b
    if not b.get("ok"):
        out.update({"state": REASON_CANNOT_EVALUATE, "detail": b["detail"]})
        return out

    v = verdict(b, side)
    out["verdict"] = v
    if not v["broke"]:
        edge = (b["low"] if side == "LONG" else b["high"])
        probe = b.get("probe_close")
        dist = (None if probe is None else
                (probe - edge) if side == "LONG" else (edge - probe))
        out.update({"state": "clear",
                    "detail": (f"{b['detail']}; holding {side}, watching the "
                               f"{'floor' if side == 'LONG' else 'ceiling'} "
                               f"{edge:.2f}"
                               + (f", last close is {dist:+.2f} from it"
                                  if dist is not None else "")
                               + (" — FAVOURABLE close beyond the other side, "
                                  "ignored" if v["favourable_break"] else ""))})
        return out

    detail = (f"holding {side}; a one-minute CLOSE broke the {v['level']} of "
              f"the {b['range_start']}-{b['range_end']} opening range. range "
              f"{b['low']:.2f}-{b['high']:.2f} ({b['candles_used']} candles "
              f"{b['first']}-{b['last']}), closed at {v['price']:.2f} at "
              f"{b['probe_minute']} IST ({b['trading_symbol']})")
    if observe_only():
        # OBSERVING: compute, log, alert, block nothing.
        _record_observed("market", detail)
        out.update({"fired": False, "observed": True,
                    "state": "OBSERVING_WOULD_STAND_DOWN", "detail": detail})
        return out
    SD.stand_down(REASON_MARKET_BREAK, detail)
    out.update({"fired": True, "state": REASON_MARKET_BREAK, "detail": detail})
    return out


# ══════════════════════════════════════════════════════════════════
# THE STOCK BREAKER
# ══════════════════════════════════════════════════════════════════

def _blocked_load() -> dict:
    day = _now_ist().date().isoformat()
    _, _, _, _, path = _cfg()
    blank = {"session_date": day, "symbols": {}}
    try:
        if path.exists():
            d = json.loads(path.read_text())
            if d.get("session_date") == day:
                return {**blank, **d}
            log.info(f"blocked-symbol file is from {d.get('session_date')}, "
                     f"today is {day} — ignoring it")
    except Exception as e:
        # FAIL OPEN, same reasoning as the box: an unreadable file cannot prove
        # a symbol broke, and the stop-loss is the real protection.
        log.error(f"could not read {path} ({e}) — no symbols treated as blocked")
    return blank


def _blocked_save(d: dict) -> None:
    _, _, _, _, path = _cfg()
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(path) == "/var/lib/atlas/breaker_blocked.json":
        log.warning(f"DRILL ({why}) — refusing to write the live "
                    f"blocked-symbol file at {path}")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)
    except Exception as e:
        log.error(f"could not persist blocked symbols to {path} ({e})")


def blocked_symbols() -> dict:
    """{symbol: detail} blocked by a stock-level break today. Read-only."""
    return dict(_blocked_load().get("symbols", {}))


def is_blocked(symbol: str) -> tuple:
    """(blocked, detail) for one symbol."""
    d = _blocked_load().get("symbols", {})
    if symbol in d:
        return True, str(d[symbol])
    return False, ""


def block_symbol(symbol: str, detail: str) -> None:
    """Block new entries in one symbol for the rest of the session."""
    d = _blocked_load()
    if symbol in d.get("symbols", {}):
        return
    d.setdefault("symbols", {})[symbol] = detail
    _blocked_save(d)
    try:
        from atlas.reporting.telegram import send
        send(f"⛔ <b>{symbol} — RANGE BREAK</b>\n"
             f"<code>{detail[:320]}</code>\n\n"
             f"No new entries in {symbol} for the rest of the session. Other "
             f"symbols are unaffected and the day is NOT stood down.\n"
             f"EXITS ARE UNAFFECTED: the stop-loss order owns the exit "
             f"decision, and this breaker never closes a position.")
    except Exception as e:
        log.error(f"could not send the {symbol} break alert: {e}")
    log.error(f"SYMBOL RANGE BREAK — {symbol}: {detail[:300]}")


def stock_check(symbol: str, side: str, instrument_key: str = None,
                now=None, candles=None, last_price=None) -> dict:
    """Evaluate one symbol's own box against its own side. Never raises.

    -> {state, broke, detail, box}. Blocks the symbol on a break; never exits.
    """
    out = {"symbol": symbol, "side": side, "broke": False, "state": None,
           "detail": "", "box": None}
    blocked, why = is_blocked(symbol)
    if blocked:
        out.update({"state": REASON_SYMBOL_BREAK,
                    "detail": f"already blocked today: {why[:160]}"})
        return out
    ok_armed, why_armed = armed(now)
    if not ok_armed:
        out.update({"state": REASON_NOT_ARMED, "detail": why_armed})
        return out
    if instrument_key is None:
        try:
            from engine.universe import INSTRUMENT_KEYS
            instrument_key = INSTRUMENT_KEYS.get(symbol)
        except Exception:
            instrument_key = None
    if not instrument_key:
        out.update({"state": REASON_CANNOT_EVALUATE,
                    "detail": f"no instrument key for {symbol}"})
        return out

    b = box(instrument_key, now=now, candles=candles)
    if last_price is not None:
        b = with_last_price(b, last_price)
    out["box"] = b
    if not b.get("ok"):
        out.update({"state": REASON_CANNOT_EVALUATE, "detail": b["detail"]})
        return out

    v = verdict(b, side)
    if not v["broke"]:
        edge = b["low"] if str(side).upper() == "LONG" else b["high"]
        out.update({"state": "clear",
                    "detail": (f"{symbol} {b['detail']}; {side}, watching "
                               f"{edge:.2f}"
                               + (" — favourable close beyond the other side, "
                                  "ignored" if v["favourable_break"] else ""))})
        return out

    detail = (f"{symbol} {side}; a one-minute CLOSE broke the {v['level']} of "
              f"the {b['range_start']}-{b['range_end']} opening range. range "
              f"{b['low']:.2f}-{b['high']:.2f} ({b['candles_used']} candles "
              f"{b['first']}-{b['last']}), closed at {v['price']:.2f} at "
              f"{b['probe_minute']} IST")
    if observe_only():
        _record_observed(f"symbol:{symbol}", detail)
        out.update({"broke": False, "observed": True,
                    "state": "OBSERVING_WOULD_BLOCK", "detail": detail})
        return out
    block_symbol(symbol, detail)
    out.update({"broke": True, "state": REASON_SYMBOL_BREAK, "detail": detail})
    return out


def line(m: dict = None) -> str:
    """One line for the cycle log, the inventory and the 08:30 report."""
    m = m or {"state": "unknown", "detail": ""}
    nb = len(blocked_symbols())
    nobs = len(observed())
    head = f"market breaker {m.get('state') or '—'}"
    tail = ""
    if observe_only():
        tail += " | OBSERVE-ONLY: blocks nothing"
    if nobs:
        tail += f" | {nobs} would-be fire(s) observed today"
    if nb:
        tail += f" | {nb} symbol(s) blocked"
    return f"{head}: {str(m.get('detail') or '')[:200]}{tail}"
