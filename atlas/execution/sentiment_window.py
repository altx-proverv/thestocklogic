"""
THE ROLLING WINDOW: WHAT MAY PERMIT AN ENTRY, AND WHAT STANDS THE DAY DOWN
=========================================================================

WHY THIS EXISTS. 8172f33 made Gate 1 a live read of the NIFTY near-month
future. Replaying six sessions showed the raw read is an honest observation and
a poor permission: a median of 4 state changes a session, 24 on 2026-10-07, and
on 2026-10-05 fourteen crossings in and out of bullish. Authorising on the raw
read is authorising on a number that crossed zero.

TWO MECHANISMS, FOUR CONSTANTS, operator-approved 2026-10-14.

  ESTABLISHED STATE   at least SENTIMENT_ESTABLISH_MIN of the last
                      SENTIMENT_ESTABLISH_WINDOW cycles reading one state
                      (24 of 30). bullish, bearish or mixed can all be
                      established. Only a confirmed SIDE -- bullish or bearish
                      -- can permit an entry; a confirmed mixed permits nothing,
                      and neither does a window that has not established.

  REVERSAL BREAKER    once a SIDE is established for the session, at least
                      SENTIMENT_REVERSAL_MIN of the last
                      SENTIMENT_REVERSAL_WINDOW cycles reading the OPPOSITE side
                      (36 of 45) stands ATLAS down for the rest of the session.
                      Both sides. One-way: it does not re-arm if the market
                      turns back.

NO OTHER SMOOTHING. No hysteresis, no cooldown, no confirmation delay, no
weighting of recent cycles. The four constants in atlas/config.py are the whole
policy, so changing the policy means changing them and nothing else.

MIXED NEVER REVERSES ANYTHING. A reversal needs a confirmed opposite SIDE.
Drifting into mixed already stops entries by failing to establish a side, so
treating it as a reversal would stand the session down for the ordinary case of
an unclear market.

ONE OBSERVATION PER CYCLE, NOT PER CANDIDATE. This is the trap this module is
shaped to avoid. Gate 1 is evaluated once per CANDIDATE -- nine or ten times a
cycle -- so observing inside the gate would fill a 30-cycle window in three
cycles and "30 cycles of agreement" would mean three minutes, not thirty.
observe_cycle() is called once, at the top of the loop, and is idempotent per
cycle number; the gate calls current(), which never mutates.

THE STAND-DOWN SURVIVES A RESTART. It has to: the deploy watchdog restarts the
process when the SHA moves, and a forgotten breaker re-arms trading. The window
itself is rebuilt from scratch after a restart, which fails SAFE on its own --
an empty window establishes nothing and permits nothing for 30 cycles.

WHY THE STATE IS ALSO ON DISK. tools/atlas_switches.py and the 08:30 report run
in their own processes. They cannot read this one's memory, and the operator
asked to be able to watch a reversal building before it fires.
"""

import os
import json
import logging
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("ATLAS-SENTIMENT-WINDOW")
IST = timezone(timedelta(hours=5, minutes=30))

# ── REASON CODES ──────────────────────────────────────────────────
# Distinct from each other and from LIVE_SENTIMENT_UNAVAILABLE, because they
# call for different actions and were previously one bucket:
#
#   FORMING          before ~09:50 there are not 30 cycles yet. Expected every
#                    single session; a DECLARED STATE, not an error.
#   NOT_ESTABLISHED  30+ cycles exist and no state reached 24 of them. The
#                    market is genuinely undecided.
#   STAND_DOWN       the reversal breaker fired. Nothing re-arms it today.
REASON_FORMING = "SENTIMENT_WINDOW_FORMING"
REASON_NOT_ESTABLISHED = "SENTIMENT_NOT_ESTABLISHED"
REASON_STAND_DOWN = "SENTIMENT_REVERSAL_STAND_DOWN"

SIDES = ("bullish", "bearish")
OPPOSITE = {"bullish": "bearish", "bearish": "bullish"}


def _cfg():
    from atlas.config import (SENTIMENT_ESTABLISH_WINDOW, SENTIMENT_ESTABLISH_MIN,
                              SENTIMENT_REVERSAL_WINDOW, SENTIMENT_REVERSAL_MIN,
                              SENTIMENT_WINDOW_FILE)
    return (int(SENTIMENT_ESTABLISH_WINDOW), int(SENTIMENT_ESTABLISH_MIN),
            int(SENTIMENT_REVERSAL_WINDOW), int(SENTIMENT_REVERSAL_MIN),
            Path(SENTIMENT_WINDOW_FILE))


def _now_ist():
    return datetime.now(IST)


def _blank(day: str) -> dict:
    return {"session_date": day, "observations": [], "last_cycle": None,
            "raw": None, "established": None, "established_at": None,
            "session_side": None, "session_side_at": None,
            "stood_down": False, "stand_down_at": None,
            "stand_down_detail": "", "reversal_from": None,
            "reversal_to": None}


def _load() -> dict:
    """The stored state for TODAY, or a blank one. Never raises."""
    _, _, _, _, path = _cfg()
    day = _now_ist().date().isoformat()
    try:
        if path.exists():
            d = json.loads(path.read_text())
            # A FILE FROM ANOTHER DAY IS NOT STATE, IT IS HISTORY. The
            # stand-down is per session, so a stale file must not carry
            # yesterday's breaker into today.
            if d.get("session_date") == day:
                return {**_blank(day), **d}
            log.info(f"sentiment window file is from {d.get('session_date')}, "
                     f"today is {day} — starting a fresh window")
    except Exception as e:
        log.error(f"could not read the sentiment window at {path} ({e}) — "
                  f"starting a fresh window; entries stay refused until it "
                  f"establishes")
    return _blank(day)


def _save(st: dict) -> None:
    _, _, _, _, path = _cfg()
    # A DRILL MUST NOT WRITE THE LIVE WINDOW. Same fence as breaker.halt, and
    # for the same reason: tests/test_market_hours_loop calls the real cycle(),
    # cycle() advances this window, and on 2026-10-14 running the suite injected
    # an observation into /var/lib/atlas/sentiment_window.json. tests/run_all
    # declares the drill and redirects the path; this refuses the canonical file
    # even if something forgets to.
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(path) == "/var/lib/atlas/sentiment_window.json":
        log.warning(f"DRILL ({why}) — refusing to write the live sentiment "
                    f"window at {path}")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(path)                  # atomic: a torn file reads as absent
    except Exception as e:
        log.error(f"could not persist the sentiment window to {path} ({e}) — "
                  f"a restart would forget "
                  f"{'A FIRED STAND-DOWN' if st.get('stood_down') else 'the window'}")


def _counts(obs: list, n: int) -> dict:
    """Counts of each state over the last n observations."""
    tail = obs[-n:] if n > 0 else []
    c = Counter(tail)
    return {"n": len(tail), "bullish": c.get("bullish", 0),
            "bearish": c.get("bearish", 0), "mixed": c.get("mixed", 0),
            "unavailable": c.get("unavailable", 0)}


def fold(verdicts: list, thresholds: tuple = None) -> dict:
    """THE POLICY, AS A PURE FUNCTION OF A SEQUENCE OF RAW VERDICTS.

    One implementation, used by the live path (over the stored observations)
    and by tools/replay_live_sentiment.py (over a replayed session). The replay
    cannot drift from the engine because there is nothing to drift from -- same
    function, same constants.

    -> {established, session_side, session_side_index, stood_down,
        stand_down_index, reversal_from, reversal_to, trace}

    `trace` carries one entry per step, which is what lets the replay say when
    a side established and how close a session came to reversing.
    """
    ew, em, rw, rm = thresholds or _cfg()[:4]
    established = None
    session_side = None
    session_side_i = None
    stood_down = False
    stand_down_i = None
    rev_from = rev_to = None
    worst_opposite = 0          # the closest approach to a reversal
    worst_opposite_i = None
    trace = []

    for i in range(len(verdicts)):
        seen = verdicts[:i + 1]
        est = _counts(seen, ew)
        rev = _counts(seen, rw)

        established = None
        if est["n"] >= ew:
            for name in ("bullish", "bearish", "mixed"):
                if est[name] >= em:
                    established = name
                    break

        # THE SESSION SIDE IS THE FIRST SIDE ESTABLISHED, and it does not move.
        # Following the latest establishment would let the reversal reference
        # chase the market, and the breaker could never fire.
        if established in SIDES and session_side is None:
            session_side, session_side_i = established, i

        opp_count = 0
        if session_side in SIDES:
            opp_count = rev[OPPOSITE[session_side]]
            if opp_count > worst_opposite:
                worst_opposite, worst_opposite_i = opp_count, i
            if not stood_down and rev["n"] >= rw and opp_count >= rm:
                stood_down, stand_down_i = True, i
                rev_from, rev_to = session_side, OPPOSITE[session_side]

        trace.append({
            "i": i, "raw": verdicts[i], "established": established,
            "session_side": session_side, "stood_down": stood_down,
            "permits": (None if (stood_down or established not in SIDES)
                        else established),
            "est": est, "rev": rev, "opposite": opp_count,
        })

    return {"established": established, "session_side": session_side,
            "session_side_index": session_side_i, "stood_down": stood_down,
            "stand_down_index": stand_down_i, "reversal_from": rev_from,
            "reversal_to": rev_to, "worst_opposite": worst_opposite,
            "worst_opposite_index": worst_opposite_i, "trace": trace}


def _evaluate(st: dict) -> dict:
    """Apply the policy to the stored observations and stamp the clock."""
    ew, em, rw, rm, _ = _cfg()
    obs = st["observations"]
    f = fold(obs)
    rev_counts = _counts(obs, rw)

    if f["established"] and st.get("established") != f["established"]:
        st["established_at"] = _now_ist().isoformat(timespec="seconds")
    st["established"] = f["established"]

    if f["session_side"] and st.get("session_side") is None:
        st["session_side"] = f["session_side"]
        st["session_side_at"] = _now_ist().isoformat(timespec="seconds")

    if f["stood_down"] and not st.get("stood_down"):
        st["stood_down"] = True
        st["stand_down_at"] = _now_ist().isoformat(timespec="seconds")
        st["reversal_from"] = f["reversal_from"]
        st["reversal_to"] = f["reversal_to"]
        opp = f["reversal_to"]
        st["stand_down_detail"] = (
            f"established {f['reversal_from']} at "
            f"{str(st.get('session_side_at'))[11:19]}, then {rev_counts[opp]} "
            f"of the last {rev_counts['n']} cycles read {opp} "
            f"(threshold {rm}/{rw})")
    return st


def _shortfalls(st: dict) -> dict:
    """How many cycles short of each threshold we are, right now."""
    ew, em, rw, rm, _ = _cfg()
    obs = st["observations"]
    est = _counts(obs, ew)
    rev = _counts(obs, rw)
    out = {"establish": {}, "reversal": None,
           "cycles_to_window": max(0, ew - est["n"])}
    for name in ("bullish", "bearish", "mixed"):
        out["establish"][name] = max(0, em - est[name])
    side = st.get("session_side")
    if side in SIDES:
        opp = OPPOSITE[side]
        out["reversal"] = {"opposite": opp, "count": rev[opp], "of": rev["n"],
                           "needs": rm, "short_by": max(0, rm - rev[opp])}
    return out


def state() -> dict:
    """The full picture, with counts and shortfalls. Read-only. Never raises."""
    ew, em, rw, rm, path = _cfg()
    try:
        st = _load()
    except Exception as e:
        st = _blank(_now_ist().date().isoformat())
        log.error(f"sentiment window unreadable ({e})")
    obs = st["observations"]
    est, rev = _counts(obs, ew), _counts(obs, rw)
    sf = _shortfalls(st)

    if st.get("stood_down"):
        permits, code = None, REASON_STAND_DOWN
    elif est["n"] < ew:
        permits, code = None, REASON_FORMING
    elif st.get("established") in SIDES:
        permits, code = st["established"], None
    elif st.get("established") == "mixed":
        permits, code = None, "SENTIMENT"
    else:
        permits, code = None, REASON_NOT_ESTABLISHED

    return {
        "session_date": st["session_date"], "cycles": est["n"],
        "observed_total": len(obs), "last_cycle": st.get("last_cycle"),
        "raw": st.get("raw"), "established": st.get("established"),
        "established_at": st.get("established_at"),
        "session_side": st.get("session_side"),
        "session_side_at": st.get("session_side_at"),
        "stood_down": bool(st.get("stood_down")),
        "stand_down_at": st.get("stand_down_at"),
        "stand_down_detail": st.get("stand_down_detail", ""),
        "reversal_from": st.get("reversal_from"),
        "reversal_to": st.get("reversal_to"),
        "permits": permits, "reason_code": code,
        "establish_counts": est, "reversal_counts": rev, "shortfalls": sf,
        "thresholds": {"establish_min": em, "establish_window": ew,
                       "reversal_min": rm, "reversal_window": rw},
        "file": str(path),
    }


def observe_cycle(raw_verdict: str, cycle_n: int = None) -> dict:
    """Record ONE cycle's raw read and re-evaluate. -> the new state.

    Idempotent per cycle_n: called twice for the same cycle, the second call
    changes nothing. The window counts CYCLES, and the gate it feeds runs once
    per candidate.
    """
    ew, em, rw, rm, _ = _cfg()
    st = _load()
    if cycle_n is not None and st.get("last_cycle") == cycle_n:
        return state()

    v = str(raw_verdict or "unavailable").lower()
    if v not in ("bullish", "bearish", "mixed"):
        # AN UNREADABLE CYCLE IS RECORDED, NOT SKIPPED. Dropping it would let a
        # dead feed hold a stale established side indefinitely: the window would
        # stop advancing and the last good reading would keep permitting entries.
        # Recorded as its own value, it counts toward neither threshold and ages
        # the established state out.
        v = "unavailable"
    st["observations"] = (st["observations"] + [v])[-max(ew, rw) * 2:]
    st["raw"] = v
    st["last_cycle"] = cycle_n
    was_down = bool(st.get("stood_down"))
    st = _evaluate(st)
    _save(st)
    s = state()

    if s["stood_down"] and not was_down:
        _alert_stand_down(s)
    return s


def _alert_stand_down(s: dict) -> None:
    """Tell the operator the breaker fired. Never raises."""
    rc = s["reversal_counts"]
    t = s["thresholds"]
    try:
        from atlas.reporting.telegram import send
        send("🔄 <b>ATLAS STOOD DOWN — SENTIMENT REVERSAL</b>\n"
             f"Established <b>{s['reversal_from']}</b> at "
             f"{str(s['session_side_at'])[11:19]} IST, now confirmed "
             f"<b>{s['reversal_to']}</b>.\n\n"
             f"<code>reversal window: {rc[s['reversal_to']]} of the last "
             f"{rc['n']} cycles read {s['reversal_to']} "
             f"(threshold {t['reversal_min']}/{t['reversal_window']})\n"
             f"establish window: bullish {s['establish_counts']['bullish']}, "
             f"bearish {s['establish_counts']['bearish']}, "
             f"mixed {s['establish_counts']['mixed']} of "
             f"{s['establish_counts']['n']} "
             f"(threshold {t['establish_min']}/{t['establish_window']})</code>\n\n"
             "No new entries either side for the rest of the session. This is "
             "one-way — it does not re-arm if the market turns back.\n"
             "EXITS ARE UNAFFECTED: stops, targets, trailing and the 15:20 GTT "
             "handover all continue.")
    except Exception as e:
        log.error(f"could not send the stand-down alert: {e}")
    log.error("=" * 70)
    log.error(f"SENTIMENT REVERSAL — stood down for the session. "
              f"{s['stand_down_detail']}")
    log.error("=" * 70)


def line(s: dict = None) -> str:
    """One line for the cycle log, the inventory and the 08:30 report.

    Carries the confirmed state, the raw read, both window counts and the
    distance to each threshold -- so a reversal can be watched building rather
    than only reported once it has fired.
    """
    s = s or state()
    t, ec, rc, sf = s["thresholds"], s["establish_counts"], s["reversal_counts"], s["shortfalls"]
    head = (f"confirmed {s['established'] or '—'}"
            f" (raw {s['raw'] or '—'})")
    est = (f"establish {ec['bullish']}B/{ec['bearish']}S/{ec['mixed']}M"
           + (f"/{ec['unavailable']}U" if ec["unavailable"] else "")
           + f" of {ec['n']}, need {t['establish_min']}/{t['establish_window']}")
    if sf["cycles_to_window"]:
        est += f" ({sf['cycles_to_window']} more cycle(s) before it can establish)"
    else:
        # bullish=B, bearish=S, mixed=M -- matching the count display above.
        # First-letter abbreviation gave bullish and bearish both "B".
        abbr = {"bullish": "B", "bearish": "S", "mixed": "M"}
        short = ", ".join(f"{abbr[k]}+{v}" for k, v in
                          sf["establish"].items() if v)
        if short and not s["established"]:
            est += f" (short by {short})"
    rev = "reversal n/a (no side established yet)"
    if sf["reversal"]:
        r = sf["reversal"]
        rev = (f"reversal {r['count']} {r['opposite']} of last {r['of']}, "
               f"need {r['needs']}/{t['reversal_window']}"
               + (f" — {r['short_by']} short" if r["short_by"] else " — FIRED"))
    tail = ""
    if s["stood_down"]:
        tail = f" | STOOD DOWN at {str(s['stand_down_at'])[11:19]}"
    elif s["session_side"]:
        tail = (f" | session side {s['session_side']} from "
                f"{str(s['session_side_at'])[11:19]}")
    return f"{head} | {est} | {rev}{tail}"


def reset_for_tests(path: Path = None) -> None:
    """Drop the stored window. Tests only; never called by the engine."""
    _, _, _, _, p = _cfg()
    (path or p).unlink(missing_ok=True)
