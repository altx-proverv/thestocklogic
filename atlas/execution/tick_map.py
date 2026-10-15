"""
TICK SIZE, PER INSTRUMENT, CACHED DAILY
=======================================

WHY THIS EXISTS. atlas/execution/exits.py carried `TICK = 0.05` as a module
constant and rounded every protective price through it. ATHERENERG trades on a
0.10 tick, so on 2026-10-15 the broker refused the day-0 SL, the emergency exit
LIMIT, the OCO GTT and the stop-only GTT -- four mechanisms designed so that no
single fault could defeat them all, defeated by one constant they shared.

THERE IS NO SAFE BLANKET VALUE. Across the 745-symbol universe the master
reports six distinct ticks -- 0.01 (166), 0.05 (289), 0.10 (244), 0.50 (21),
1.00 (13), 5.00 (10) -- and price does not determine tick: the 0.01/0.05,
0.05/0.10 and 0.10/0.50 price bands all overlap. The only rounding valid on
every one is 5.00, which on a Rs240 stock is a 2% error. So the tick has to be
looked up, not guessed.

────────────────────────────────────────────────────────────────────
THE LOOKUP CHAIN, AND WHY IT ENDS IN A REFUSAL
────────────────────────────────────────────────────────────────────
  1  today's cached map          one file, written once a day
  2  a live refresh from kite.instruments("NSE")   if today's is missing
  3  THE LAST KNOWN TICK FOR THAT SYMBOL, from any earlier day's cache.
     Tick size changes rarely, and a symbol in a published batch has
     necessarily been ingested on an earlier day, so this covers essentially
     every real case -- a broker outage at 09:20 included.
  4  nothing known -> REFUSE THAT ONE CANDIDATE.

STEP 4 IS NOT "REFUSING BECAUSE OF TICK SIZE". It is refusing because ATLAS
cannot compute a price the broker will accept, and therefore cannot protect the
position it is about to open. Same principle as refusing to enter when exit
management is off: a flag that removes all protection is not an entry path, and
neither is an unknown tick. Sending a price that will be rejected is what left
a position unprotected on 2026-10-15; one skipped candidate is cheaper.

It should essentially never fire, so it logs loudly and alerts when it does.

THE REFRESH LIVES IN THE 08:30 JOB. atlas/execution/zerodha_morning.py already
runs before the open with a live broker session, which is exactly what the
master needs. Nothing else has to remember.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("ATLAS-TICK-MAP")
IST = timezone(timedelta(hours=5, minutes=30))

# The reason code for step 4. Distinct from every sizing or gate code because
# it calls for a different action: find out why a symbol has no tick.
REASON_NO_TICK = "NO_TICK_SIZE_CANNOT_PRICE"

_MEM = {"day": None, "map": None}


def _path() -> Path:
    from atlas.config import TICK_MAP_FILE
    return Path(TICK_MAP_FILE)


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def _load() -> dict:
    """The whole file: {"days": {date: {symbol: tick}}, "last_known": {...}}."""
    path = _path()
    blank = {"days": {}, "last_known": {}}
    try:
        if path.exists():
            d = json.loads(path.read_text())
            return {**blank, **d}
    except Exception as e:
        log.error(f"could not read the tick map at {path} ({e})")
    return blank


def _save(d: dict) -> None:
    path = _path()
    # A DRILL MUST NOT WRITE PRODUCTION STATE. Third instance of this fence,
    # after the suite pausing the engine and the suite writing the sentiment
    # window; the runner declares the drill and this refuses the canonical path
    # even if a suite forgets to redirect.
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(path) == "/var/lib/atlas/tick_map.json":
        log.warning(f"DRILL ({why}) — refusing to write the live tick map")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(path)
    except Exception as e:
        log.error(f"could not persist the tick map to {path} ({e})")


def refresh(kite=None) -> dict:
    """Pull tick_size for every NSE equity and persist it under today's date.

    -> {"ok", "symbols", "ticks", "day", "detail"}. Never raises.

    LAST_KNOWN IS UPDATED, NOT REPLACED. It accumulates across days so a symbol
    that drops out of one refresh keeps its previous tick rather than becoming
    unknown.
    """
    out = {"ok": False, "symbols": 0, "ticks": {}, "day": _today(), "detail": ""}
    try:
        if kite is None:
            from atlas.execution.broker import get_kite
            kite = get_kite()
        if not kite:
            out["detail"] = "no Kite session"
            return out
        rows = kite.instruments("NSE")
    except Exception as e:
        out["detail"] = f"instruments() failed: {type(e).__name__}: {e}"
        log.error(f"tick map refresh failed: {out['detail']}")
        return out

    fresh = {}
    for r in rows:
        if r.get("segment") != "NSE" or r.get("instrument_type") != "EQ":
            continue
        t = r.get("tick_size")
        sym = str(r.get("tradingsymbol") or "")
        if sym and t:
            fresh[sym] = float(t)
    if not fresh:
        out["detail"] = "the master returned no NSE equity rows"
        return out

    d = _load()
    d["days"] = {k: v for k, v in d.get("days", {}).items()
                 if k >= (datetime.now(IST).date()
                          - timedelta(days=7)).isoformat()}
    d["days"][out["day"]] = fresh
    d.setdefault("last_known", {}).update(fresh)
    _save(d)
    _MEM.update({"day": out["day"], "map": fresh})

    from collections import Counter
    out.update({"ok": True, "symbols": len(fresh),
                "ticks": dict(sorted(Counter(fresh.values()).items())),
                "detail": f"{len(fresh)} NSE equity symbols"})
    log.info(f"tick map refreshed: {len(fresh)} symbols, "
             f"ticks {out['ticks']}")
    return out


def tick_for(symbol: str, kite=None, allow_refresh: bool = True) -> tuple:
    """(tick, source) for one symbol, or (None, reason) if nothing is known.

    source is one of: "today", "refreshed", "last_known:<date>", or None.
    """
    sym = str(symbol or "").upper()
    day = _today()

    if _MEM.get("day") == day and _MEM.get("map") and sym in _MEM["map"]:
        return _MEM["map"][sym], "today"

    d = _load()
    today_map = d.get("days", {}).get(day) or {}
    if sym in today_map:
        _MEM.update({"day": day, "map": today_map})
        return float(today_map[sym]), "today"

    if allow_refresh and not today_map:
        r = refresh(kite=kite)
        if r["ok"]:
            m = _load().get("days", {}).get(day) or {}
            if sym in m:
                return float(m[sym]), "refreshed"

    # THE LAST KNOWN TICK. Any earlier day's value, newest first.
    days = d.get("days", {})
    for k in sorted(days, reverse=True):
        if sym in days[k]:
            log.warning(f"{sym}: no tick in today's map — using the last known "
                        f"value {days[k][sym]} from {k}")
            return float(days[k][sym]), f"last_known:{k}"
    lk = d.get("last_known", {})
    if sym in lk:
        log.warning(f"{sym}: no dated tick — using last_known {lk[sym]}")
        return float(lk[sym]), "last_known:accumulated"

    # NOTHING KNOWN. The caller must refuse this candidate.
    log.error("=" * 66)
    log.error(f"{sym}: NO TICK SIZE KNOWN, from today's map, a refresh, or any "
              f"cached day. ATLAS cannot compute a price the broker will "
              f"accept and therefore cannot protect a position in it.")
    log.error(f"This candidate is skipped under {REASON_NO_TICK}. It is not a "
              f"refusal about tick size -- it is a refusal to open something "
              f"that cannot be protected.")
    log.error("=" * 66)
    try:
        from atlas.reporting.telegram import send
        send(f"⚠️ <b>{sym} SKIPPED — NO TICK SIZE</b>\n"
             f"<code>{REASON_NO_TICK}</code>\n\n"
             f"No tick size is known for {sym} from today's instrument master, "
             f"a live refresh, or any cached day. ATLAS cannot compute a price "
             f"the broker will accept, so it will not open a position it "
             f"cannot protect.\n"
             f"This should essentially never happen — worth looking at why.")
    except Exception as e:
        log.error(f"could not send the no-tick alert: {e}")
    return None, REASON_NO_TICK


def state() -> dict:
    """What the map holds. Read-only, for the inventory."""
    d = _load()
    day = _today()
    today_map = d.get("days", {}).get(day) or {}
    from collections import Counter
    return {"file": str(_path()), "day": day,
            "today_symbols": len(today_map),
            "today_ticks": dict(sorted(Counter(today_map.values()).items())),
            "cached_days": sorted(d.get("days", {})),
            "last_known_symbols": len(d.get("last_known", {}))}


def reset_for_tests() -> None:
    _MEM.update({"day": None, "map": None})
    _path().unlink(missing_ok=True)
