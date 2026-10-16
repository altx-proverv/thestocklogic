"""
THE SESSION STAND-DOWN — A MECHANISM WAITING FOR ITS TRIGGER
============================================================

WHAT THIS FILE WAS, AND WHAT WAS DELETED FROM IT, 2026-10-15.

It held a rolling vote over the per-cycle sentiment read: a side had to hold 24
of the last 30 cycles before Gate 1 would permit an entry, and 36 of the last
45 reading the opposite side stood the session down. Operator decision: the
vote is gone. Gate 1 is back to the instantaneous three-way read shipped in
8172f33 and permits or refuses on that alone, with no warm-up and no minimum
cycle count, from 09:20.

Removed: the 24-of-30 establishment, the 36-of-45 reversal, the four constants
in atlas/config.py, the per-cycle persisted window, fold(), observe_cycle(),
the counts and shortfall reporting, and the SENTIMENT_WINDOW_FORMING and
SENTIMENT_NOT_ESTABLISHED reason codes.

WHY THE 2026-10-14 SESSION SETTLED IT. 360 cycles, 2,233 candidate
evaluations, and 623 of the skips were the window's own two codes -- while the
raw read was never once bullish that day. The warm-up blocked nothing the read
would not have blocked anyway, and would have blocked the first thirty cycles
of any day that did open bullish.

────────────────────────────────────────────────────────────────────
WHAT SURVIVES, AND WHY IT HAS NO TRIGGER
────────────────────────────────────────────────────────────────────
The STAND-DOWN mechanism is kept whole: block new entries for the rest of the
session, persist across a process restart, alert the operator once, and leave
every exit alone.

NOTHING CALLS stand_down() TODAY. That is deliberate, not an oversight, and
not a dangling reference either -- the function is complete and tested, and no
code path reaches it. THE RANGE-BREAK CHECK, shipping tomorrow, is what will
fire it. Until that lands, no condition in ATLAS can stand the day down.

If you are reading this because you are wiring that check: call
stand_down(kind, detail) once, from wherever the range break is detected. The
persistence, the one-shot alert, the reason code and the entry block are all
already here and need nothing from the caller.

THE STAND-DOWN IS AN ENTRY CONTROL AND ONLY AN ENTRY CONTROL. Stops, targets,
trailing and the 15:20 GTT handover continue through it. A breaker that also
stopped protecting open positions would be worse than no breaker, and the
alert says so in as many words. tests/test_sentiment_standdown.py asserts that
no exit path references this module.

PERSISTENCE IS NOT OPTIONAL. The deploy watchdog restarts this process when the
deployed SHA moves. A stand-down held only in memory would be forgotten by that
restart and trading would re-arm -- a fail-OPEN on the one control whose job is
to stop it. The file is written only when a stand-down actually fires; there is
no per-cycle write any more.
"""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("ATLAS-SENTIMENT-STANDDOWN")
IST = timezone(timedelta(hours=5, minutes=30))

# THE REASON CODE, RENAMED 2026-10-15 WHEN THE TRIGGER ARRIVED.
#
# It was SENTIMENT_REVERSAL_STAND_DOWN, from the 24-of-30 sentiment vote it was
# built for. That vote was deleted and the thing that fires this is now a RANGE
# BREAK -- price printing outside the previous 45 minutes' box, against the side
# ATLAS holds. A code naming sentiment would have been the fourth time this
# project shipped a message naming the wrong mechanism, after the GTT alert that
# fired on an SL failure, "operator PAUSED" for a self-halt, and the 361-of-746
# figure that did not reproduce.
#
# Distinct from SENTIMENT (a readable market permitting no side) and
# LIVE_SENTIMENT_UNAVAILABLE (the feed would not answer): those two mean wait
# and investigate. This one means the day is over for entries whatever the
# market does next.
REASON_STAND_DOWN = "RANGE_BREAK_STAND_DOWN"

CANONICAL_FILE = "/var/lib/atlas/sentiment_standdown.json"


def _path() -> Path:
    from atlas.config import SENTIMENT_STANDDOWN_FILE
    return Path(SENTIMENT_STANDDOWN_FILE)


def _now_ist():
    return datetime.now(IST)


def _blank(day: str) -> dict:
    return {"session_date": day, "stood_down": False, "at": None,
            "kind": None, "detail": ""}


def _load() -> dict:
    """Today's stand-down, or a blank. Never raises."""
    day = _now_ist().date().isoformat()
    path = _path()
    try:
        if path.exists():
            d = json.loads(path.read_text())
            # A FILE FROM ANOTHER DAY IS HISTORY, NOT STATE. The stand-down is
            # per session; yesterday's must not carry into today.
            if d.get("session_date") == day:
                return {**_blank(day), **d}
            log.info(f"stand-down file is from {d.get('session_date')}, today "
                     f"is {day} — ignoring it")
    except Exception as e:
        # FAIL OPEN HERE, AND ONLY HERE. An unreadable file means we cannot
        # prove a stand-down fired; it does not mean one did. Treating it as
        # fired would disarm the engine on a corrupt byte, and the entry gate
        # has its own independent refusals above this.
        log.error(f"could not read the stand-down file at {path} ({e}) — "
                  f"treating the session as NOT stood down")
    return _blank(day)


def _save(st: dict) -> None:
    path = _path()
    # A DRILL MUST NOT WRITE PRODUCTION STATE. Same fence as breaker.halt, for
    # the same reason it was needed twice before: on 2026-10-13 the suite paused
    # the live engine, and on 2026-10-14 it wrote the live sentiment window.
    # tests/run_all declares the drill and redirects the path; this refuses the
    # canonical file even if something forgets to.
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(path) == CANONICAL_FILE:
        log.warning(f"DRILL ({why}) — refusing to write the live stand-down "
                    f"file at {path}")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(path)          # atomic: a torn file reads as absent
    except Exception as e:
        log.error(f"could not persist the stand-down to {path} ({e}) — a "
                  f"restart would forget it and trading would re-arm")


def state() -> dict:
    """(read-only) Is the session stood down, and why. Never raises."""
    st = _load()
    return {"session_date": st["session_date"],
            "stood_down": bool(st.get("stood_down")),
            "at": st.get("at"), "kind": st.get("kind"),
            "detail": st.get("detail", ""),
            "reason_code": REASON_STAND_DOWN if st.get("stood_down") else None,
            "file": str(_path())}


def stand_down(kind: str, detail: str) -> dict:
    """Stop entering for the rest of the session. Both sides. One-way.

    CALLED BY range_breaker.market_check(), live as of 2026-10-16. This said
    "NO CALLER TODAY" while BREAKER_OBSERVE_ONLY was True.

    THIS IS THE ONE SELF-INFLICTED STAND-DOWN LEFT IN ATLAS. Ten other sites
    that used to halt the account now only alert (see breaker.flag), because
    they were reacting to ATLAS's own machinery failing. This one is exempt by
    ruling: a market-wide reversal is a fact about the market, it applies to
    every symbol at once, and stopping is the right answer to it.

    Safe to call more than once -- the alert fires on the first, and a second
    call does not re-stamp the time.
    """
    st = _load()
    if st.get("stood_down"):
        return state()
    st.update({"stood_down": True,
               "at": _now_ist().isoformat(timespec="seconds"),
               "kind": str(kind), "detail": str(detail)})
    _save(st)
    s = state()
    _alert(s)
    return s


def _alert(s: dict) -> None:
    """Tell the operator. Never raises."""
    try:
        from atlas.reporting.telegram import send
        send("🔄 <b>ATLAS STOOD DOWN FOR THE SESSION</b>\n"
             f"{s['kind']}\n<code>{str(s['detail'])[:300]}</code>\n\n"
             "No new entries either side for the rest of the session. This is "
             "one-way — it does not re-arm if the market turns back.\n"
             "EXITS ARE UNAFFECTED: stops, targets, trailing and the 15:20 GTT "
             "handover all continue.")
    except Exception as e:
        log.error(f"could not send the stand-down alert: {e}")
    log.error("=" * 70)
    log.error(f"STOOD DOWN — {s['kind']}: {str(s['detail'])[:300]}")
    log.error("=" * 70)


def line(s: dict = None) -> str:
    """One line for the inventory. No counts: there is nothing to count."""
    s = s or state()
    if s["stood_down"]:
        return (f"STOOD DOWN at {str(s['at'])[11:19]} — {s['kind']}: "
                f"{str(s['detail'])[:160]}")
    return ("armed, nothing has fired it — the market range breaker "
            "(range_breaker.market_check) is what fires this")


def reset_for_tests(path: Path = None) -> None:
    """Drop the stored stand-down. Tests only; never called by the engine."""
    (path or _path()).unlink(missing_ok=True)
