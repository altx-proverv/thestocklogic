"""
THE RANGE-BREAK BREAKERS — MARKET LEVEL AND STOCK LEVEL
=======================================================

THE MECHANIC, BOTH LEVELS. A box is drawn around the previous 45 minutes of
price. Price printing outside that box, on the side that goes AGAINST the
position, is a confirmed change of character. One print is enough.

A STRUCTURAL FACT, NOT A VOTE. A level either printed through or it did not.
There is no threshold here and none is to be added -- the 24-of-30 sentiment
vote deleted on 2026-10-15 was a vote over a continuous reading, which is what
made it tunable and therefore arbitrary.

────────────────────────────────────────────────────────────────────
THE WINDOW EXCLUDES THE CURRENT READING
────────────────────────────────────────────────────────────────────
This is the part most likely to be silently wrong, so it is worth stating
twice. A box that CONTAINS the current price can never be broken: its high is
by construction at least the current price and its low at most. Such a breaker
would read "all clear" forever and nobody would notice.

So `box()` takes the 45 candles STRICTLY BEFORE the current minute, and the
current minute is the thing tested against them. candles_used and the box's
own time span are reported so the exclusion can be checked from the outside
rather than trusted.

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
    from atlas.config import (BREAKER_WINDOW_CYCLES, BREAKER_ARMS_AT,
                              BREAKER_BLOCKED_FILE)
    return int(BREAKER_WINDOW_CYCLES), str(BREAKER_ARMS_AT), Path(BREAKER_BLOCKED_FILE)


def _now_ist():
    return datetime.now(IST)


def armed(now=None) -> tuple:
    """(is_armed, why). Nothing can fire before BREAKER_ARMS_AT."""
    _, arms_at, _ = _cfg()
    now = now or _now_ist()
    hh, mm = (int(x) for x in arms_at.split(":"))
    if (now.hour, now.minute) < (hh, mm):
        return False, f"not armed until {arms_at} IST (now {now:%H:%M})"
    return True, f"armed since {arms_at} IST"


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


def box(instrument_key: str, now=None, candles: list = None) -> dict:
    """The prior-N-candle box, EXCLUDING the current minute.

    -> {ok, high, low, candles_used, first, last, probe_high, probe_low, ...}

    `candles` is injectable so the replay harness and the tests run the same
    arithmetic the engine runs, against a session's real history.
    """
    n, arms_at, _ = _cfg()
    now = now or _now_ist()
    out = {"ok": False, "reason_code": REASON_CANNOT_EVALUATE, "detail": "",
           "high": None, "low": None, "candles_used": 0, "window": n,
           "first": None, "last": None, "probe_high": None, "probe_low": None,
           "current_minute": now.strftime("%H:%M"),
           "instrument": instrument_key}
    try:
        cs = candles if candles is not None else _candles(instrument_key)
    except Exception as e:
        out["detail"] = f"candles unavailable: {type(e).__name__}: {e}"
        return out
    if not cs:
        out["detail"] = ("the intraday endpoint returned no candles — nothing "
                         "to build a box from")
        return out

    cur_key = now.strftime("%Y-%m-%dT%H:%M")

    # THE EXCLUSION. Everything strictly before the current minute is eligible
    # for the box; the current minute is the thing tested against it. A box
    # containing the current price cannot be broken.
    prior = [c for c in cs if str(c[0])[:16] < cur_key]
    current = [c for c in cs if str(c[0])[:16] == cur_key]

    if len(prior) < n:
        out["candles_used"] = len(prior)
        out["detail"] = (f"cannot evaluate: {len(prior)} of {n} candles — the "
                         f"NSE session opens at 09:15, so a {n}-candle box is "
                         f"not complete until about "
                         f"{9 + (15 + n) // 60:02d}:{(15 + n) % 60:02d}")
        return out

    win = prior[-n:]
    out.update({
        "ok": True, "reason_code": None,
        "high": max(float(c[2]) for c in win),
        "low": min(float(c[3]) for c in win),
        "candles_used": len(win),
        "first": str(win[0][0])[11:16], "last": str(win[-1][0])[11:16],
    })
    # THE PROBE is the current minute's own high and low -- a wick through the
    # level inside this minute is a break, and the candle carries it where a
    # last-traded-price sample would not.
    if current:
        out["probe_high"] = max(float(c[2]) for c in current)
        out["probe_low"] = min(float(c[3]) for c in current)
        out["probe_source"] = "current 1-minute candle"
    else:
        out["probe_source"] = "none — no candle for the current minute yet"
    out["detail"] = (f"box {out['low']:.2f}-{out['high']:.2f} from "
                     f"{out['candles_used']} candles {out['first']}-{out['last']}")
    return out


def with_last_price(b: dict, last_price) -> dict:
    """Widen the probe with the live last price.

    THE CANDLE CAN LAG. The current minute's candle is incomplete and the
    endpoint may not have published it yet at the instant a cycle runs, so the
    live last price is folded in: whichever reaches further outside the box is
    the probe. Belt and braces, and it never narrows the probe.
    """
    if last_price in (None, 0):
        return b
    lp = float(last_price)
    b = dict(b)
    b["last_price"] = lp
    b["probe_high"] = lp if b.get("probe_high") is None else max(b["probe_high"], lp)
    b["probe_low"] = lp if b.get("probe_low") is None else min(b["probe_low"], lp)
    if b.get("probe_source") and "last price" not in b["probe_source"]:
        b["probe_source"] += " + live last price"
    return b


def verdict(b: dict, side: str) -> dict:
    """Did the box break AGAINST `side`? Pure. The direction logic lives here.

    LONG  -> the FLOOR is the level that matters. A break of the ceiling is a
             breakout in ATLAS's favour and must NOT fire.
    SHORT -> the CEILING matters. A break of the floor is in its favour.

    Getting this backwards would stop ATLAS on its winners, which is the single
    worst outcome available here, so it is one function, tested in all four
    combinations, and nothing else decides direction.
    """
    out = {"broke": False, "level": None, "price": None, "side": side,
           "favourable_break": False}
    if not b.get("ok"):
        return out
    s = str(side or "").upper()
    if s == "LONG":
        if b.get("probe_low") is not None and b["probe_low"] < b["low"]:
            out.update({"broke": True, "level": "floor", "price": b["probe_low"]})
        elif b.get("probe_high") is not None and b["probe_high"] > b["high"]:
            # IN ATLAS'S FAVOUR. Recorded so the replay can count these and
            # prove the direction logic held, and explicitly NOT a break.
            out["favourable_break"] = True
    elif s == "SHORT":
        if b.get("probe_high") is not None and b["probe_high"] > b["high"]:
            out.update({"broke": True, "level": "ceiling",
                        "price": b["probe_high"]})
        elif b.get("probe_low") is not None and b["probe_low"] < b["low"]:
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
        probe = (b.get("probe_low") if side == "LONG" else b.get("probe_high"))
        dist = (None if probe is None else
                (probe - edge) if side == "LONG" else (edge - probe))
        out.update({"state": "clear",
                    "detail": (f"{b['detail']}; holding {side}, watching the "
                               f"{'floor' if side == 'LONG' else 'ceiling'} "
                               f"{edge:.2f}"
                               + (f", price {probe:.2f} is {dist:.2f} away"
                                  if dist is not None else "")
                               + (" — FAVOURABLE break of the other side, "
                                  "ignored" if v["favourable_break"] else ""))})
        return out

    detail = (f"holding {side}; the {v['level']} of the "
              f"{b['candles_used']}-minute box broke. box "
              f"{b['low']:.2f}-{b['high']:.2f} from {b['first']}-{b['last']}, "
              f"broken at {v['price']:.2f} at {b['current_minute']} IST "
              f"({b['trading_symbol']})")
    SD.stand_down(REASON_MARKET_BREAK, detail)
    out.update({"fired": True, "state": REASON_MARKET_BREAK, "detail": detail})
    return out


# ══════════════════════════════════════════════════════════════════
# THE STOCK BREAKER
# ══════════════════════════════════════════════════════════════════

def _blocked_load() -> dict:
    day = _now_ist().date().isoformat()
    _, _, path = _cfg()
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
    _, _, path = _cfg()
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
                               + (" — favourable break of the other side, "
                                  "ignored" if v["favourable_break"] else ""))})
        return out

    detail = (f"{side}; the {v['level']} of the {b['candles_used']}-minute box "
              f"broke. box {b['low']:.2f}-{b['high']:.2f} from "
              f"{b['first']}-{b['last']}, broken at {v['price']:.2f} at "
              f"{b['current_minute']} IST")
    block_symbol(symbol, detail)
    out.update({"broke": True, "state": REASON_SYMBOL_BREAK, "detail": detail})
    return out


def line(m: dict = None) -> str:
    """One line for the cycle log, the inventory and the 08:30 report."""
    m = m or {"state": "unknown", "detail": ""}
    nb = len(blocked_symbols())
    head = f"market breaker {m.get('state') or '—'}"
    tail = f" | {nb} symbol(s) blocked by a stock break" if nb else ""
    return f"{head}: {str(m.get('detail') or '')[:200]}{tail}"
