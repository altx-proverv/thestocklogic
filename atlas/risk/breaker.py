"""
ATLAS — Failure Breaker
=======================
Stops ATLAS entering when the failure in front of it will not fix itself.

WHY A BREAKER AND NOT A RETRY
-----------------------------
Under the 09:37 cron a failed entry was one failed entry; a human saw it in the
morning report. A market-hours loop retries every 60 seconds, so a failure that
is deterministic is not retried once, it is retried for the rest of the
session. The canonical case is already in this repo's history: _log_intent
POSTed gtt_trigger_id to a table that had no such column, PostgREST answered
400 PGRST204, and requests.post does not raise on 4xx. That rejection was
identical on every attempt and would have stayed identical all day.

So the trigger is the failure's CLASS, not a count of it:

    4xx from the ledger      schema, constraint, RLS. Deterministic. Halt at
                             once -- the next 359 attempts fail the same way.
    5xx / timeout            transient. Retry, halt after two in a row.
    completion write failed  a position may exist and the ledger does not say
                             so. Halt at once.
    order rejected           ordinary (margin, circuit limit). Cool the symbol
                             down; halt only on a run of them, which means the
                             session is broken rather than the trade.

COUNTS ARE CALIBRATED TO OPPORTUNITY, NOT TO TIME
-------------------------------------------------
Reads happen every cycle -- roughly 360 a day -- so a consecutive-count rule
sees enough samples to separate a blip from an outage: at a ~0.5% transient
rate, isolated failures are a couple a day and three in a row is well clear of
noise while still catching a real outage inside three minutes.

Ledger WRITES happen per order attempt, which is rare. Most cycles find
nothing. A consecutive-count rule on a rare event could take days to trip,
which is why writes key off status class instead. Using one rule for both would
either halt on noise or never halt at all.

THE HALT MUST SURVIVE A RESTART
-------------------------------
A supervisor with Restart=always erases an in-memory breaker, and the loop
resumes into the same failure -- the grind, with extra steps. So a halt is
written down twice:

  atlas_state.mode = PAUSED   visible to the kill switch, /atlas, the report
                              and the operator's /normal, and it survives the
                              box being replaced.
  a local sentinel file       for when the thing that is broken is the write
                              path itself.

Mostly the first works even during a ledger failure, because a rejected write
to atlas_trades says nothing about atlas_state -- the PGRST204 case rejected
one table's writes while everything else was fine. And if Supabase is wholly
unreachable, kill_switch already fails closed and no entry happens anyway. The
sentinel covers the gap between those two.

PAUSED IS REUSED DELIBERATELY. It means an operator cannot tell a self-halt
from their own /pause except by the alert and the note. The alternative was a
distinct mode threaded through AGENT_MODES, HALT_MODES, directives and a resume
command. The ambiguity is self-correcting: resuming into a failure that has not
been fixed trips this again within about three minutes.
"""

import os
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

from atlas.config import SUPABASE_URL, SUPABASE_KEY

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Outside the repo on purpose. The box deploys with `git reset --hard`, and
# while that leaves untracked files alone, a halt marker should not depend on
# that staying true.
SENTINEL = Path(os.environ.get(
    "ATLAS_HALT_FILE", str(Path.home() / ".atlas" / "halt")))

# Consecutive transient failures tolerated before halting. Reads get three
# because they have 360 chances a day; ledger writes get two because they are
# rare and each one blocks a real trade.
MAX_CONSECUTIVE_READ_FAILURES  = 3
MAX_CONSECUTIVE_WRITE_FAILURES = 2
MAX_CONSECUTIVE_ORDER_REJECTS  = 5

_consecutive = {}


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════
# HALT STATE
# ══════════════════════════════════════════════════════════════════

def is_halted() -> tuple:
    """(halted, reason). Reads the local sentinel only.

    The atlas_state side is NOT read here -- kill_switch.check() already does
    that, fails closed, and is called on every entry. Reading it twice would
    double the request count for no extra safety.
    """
    try:
        if SENTINEL.exists():
            return True, SENTINEL.read_text().strip()[:400]
    except Exception as e:
        # Cannot read our own halt marker. Refuse rather than assume clear.
        log.error(f"cannot read halt sentinel {SENTINEL}: {e}")
        return True, f"halt sentinel unreadable: {e}"
    return False, ""


def halt(kind: str, detail: str) -> None:
    """Stop entering, durably, and say so. Safe to call more than once."""
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    note = f"ATLAS self-halt at {now} — {kind}: {detail}"
    log.error("=" * 66)
    log.error(f"HALT — {note}")
    log.error("=" * 66)

    # 1. The sentinel first: it is local, it cannot fail for the reason we are
    #    most likely halting, and it is what a restart reads before doing
    #    anything.
    try:
        SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        SENTINEL.write_text(note + "\n")
    except Exception as e:
        log.error(f"could not write halt sentinel {SENTINEL}: {e}")

    # 2. atlas_state, so the kill switch, the dashboard and the operator all
    #    see it, and so it survives this box.
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/atlas_state?id=eq.1",
            headers=_headers(),
            json={"mode": "PAUSED", "notes": note},
            timeout=10)
        if r.status_code not in (200, 204):
            log.error(f"could not set PAUSED: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"could not set PAUSED: {e}")

    # 3. Tell the operator. A halt that nobody hears about is a stopped engine
    #    with no explanation.
    try:
        from atlas.reporting.telegram import send
        send("🛑 <b>ATLAS HALTED</b>\n"
             f"{kind}\n<code>{detail[:300]}</code>\n\n"
             f"No further entries this session.\n"
             f"Fix the cause, clear <code>{SENTINEL}</code>, then /normal.")
    except Exception as e:
        log.error(f"could not send halt alert: {e}")


def clear() -> None:
    """Operator action. Does not touch atlas_state -- /normal does that."""
    try:
        SENTINEL.unlink(missing_ok=True)
        log.info(f"halt sentinel cleared ({SENTINEL})")
    except Exception as e:
        log.error(f"could not clear halt sentinel: {e}")


# ══════════════════════════════════════════════════════════════════
# FAILURE CLASSIFICATION
# ══════════════════════════════════════════════════════════════════

def record_ledger_write(ok: bool, status_code: int = None, detail: str = "",
                        phase: str = "reserve") -> bool:
    """
    Report the outcome of a ledger write. Returns True if ATLAS may continue.

    `phase` is "reserve" (the row before the order) or "complete" (the row
    after it). They fail differently: a failed reserve means no order was
    placed and nothing is at risk, while a failed complete means a position may
    exist that the ledger describes as PENDING rather than OPEN.
    """
    key = f"write:{phase}"
    if ok:
        _consecutive[key] = 0
        return True

    if phase == "complete":
        # The order is already out. The row exists and still blocks the symbol,
        # so this is not a duplicate risk -- it is a known-incomplete ledger,
        # and continuing to trade on one is how the book and the broker part
        # company.
        halt("ledger completion failed",
             f"{detail} — a position may exist recorded as PENDING")
        return False

    if status_code is not None and 400 <= status_code < 500:
        # Deterministic. PGRST204 on a missing column answered identically on
        # every attempt and would do so again.
        halt("ledger rejected the row (4xx)",
             f"HTTP {status_code} {detail} — deterministic, will not self-heal")
        return False

    n = _consecutive.get(key, 0) + 1
    _consecutive[key] = n
    log.error(f"ledger write failed ({n}/{MAX_CONSECUTIVE_WRITE_FAILURES}): {detail}")
    if n >= MAX_CONSECUTIVE_WRITE_FAILURES:
        halt("ledger unwritable",
             f"{n} consecutive failures — last: {detail}")
        return False
    return True


def record_read(ok: bool, what: str, detail: str = "") -> bool:
    """Report a per-cycle read (positions, funds, state). Returns may-continue."""
    key = f"read:{what}"
    if ok:
        _consecutive[key] = 0
        return True
    n = _consecutive.get(key, 0) + 1
    _consecutive[key] = n
    log.error(f"{what} read failed ({n}/{MAX_CONSECUTIVE_READ_FAILURES}): {detail}")
    if n >= MAX_CONSECUTIVE_READ_FAILURES:
        halt(f"{what} unreadable",
             f"{n} consecutive failures — last: {detail}")
        return False
    return True


def record_order_reject(symbol: str, reason: str) -> bool:
    """A broker refusal. Ordinary once; a run of them means the session is bad."""
    key = "order:reject"
    n = _consecutive.get(key, 0) + 1
    _consecutive[key] = n
    log.warning(f"order rejected for {symbol} "
                f"({n}/{MAX_CONSECUTIVE_ORDER_REJECTS}): {reason}")
    if n >= MAX_CONSECUTIVE_ORDER_REJECTS:
        halt("broker refusing orders",
             f"{n} consecutive rejections — last: {symbol} {reason}")
        return False
    return True


def record_order_ok() -> None:
    _consecutive["order:reject"] = 0


def reset() -> None:
    """Clear the in-memory counters. For tests and for a clean session start."""
    _consecutive.clear()


def status() -> dict:
    halted, reason = is_halted()
    return {"halted": halted, "reason": reason,
            "sentinel": str(SENTINEL), "counters": dict(_consecutive)}


if __name__ == "__main__":
    # Self-test. Writes only to a temporary sentinel and never touches Supabase
    # or Telegram -- both are stubbed, because what is under test is the
    # decision to halt, not the delivery of the news.
    import tempfile
    logging.basicConfig(level=logging.CRITICAL)
    SENTINEL = Path(tempfile.mkdtemp()) / "halt"
    requests.patch = lambda *a, **k: type("R", (), {"status_code": 204, "text": ""})()
    import atlas.reporting.telegram as _tg
    _tg.send = lambda *a, **k: True

    def fresh():
        SENTINEL.unlink(missing_ok=True)
        reset()

    cases, ok = [], True

    fresh(); record_ledger_write(False, 400, "PGRST204 missing column", phase="reserve")
    cases.append(("4xx ledger rejection (deterministic)", is_halted()[0], True))

    fresh(); record_ledger_write(False, 503, "gateway", phase="reserve")
    cases.append(("1 transient write failure", is_halted()[0], False))
    record_ledger_write(False, 503, "gateway", phase="reserve")
    cases.append(("2 consecutive transient writes", is_halted()[0], True))

    fresh()
    record_ledger_write(False, 503, "x", phase="reserve")
    record_ledger_write(True, phase="reserve")
    record_ledger_write(False, 503, "x", phase="reserve")
    cases.append(("a success breaks the run", is_halted()[0], False))

    fresh(); record_ledger_write(False, None, "trade 7", phase="complete")
    cases.append(("completion failed (position may exist)", is_halted()[0], True))

    fresh()
    for _ in range(MAX_CONSECUTIVE_READ_FAILURES - 1):
        record_read(False, "positions", "timeout")
    cases.append((f"{MAX_CONSECUTIVE_READ_FAILURES - 1} consecutive read failures",
                  is_halted()[0], False))
    record_read(False, "positions", "timeout")
    cases.append((f"{MAX_CONSECUTIVE_READ_FAILURES} consecutive read failures",
                  is_halted()[0], True))

    fresh()
    for _ in range(MAX_CONSECUTIVE_ORDER_REJECTS - 1):
        record_order_reject("X", "margin")
    cases.append((f"{MAX_CONSECUTIVE_ORDER_REJECTS - 1} order rejections",
                  is_halted()[0], False))
    record_order_reject("X", "margin")
    cases.append((f"{MAX_CONSECUTIVE_ORDER_REJECTS} order rejections",
                  is_halted()[0], True))

    # The halt outlives the process, or a supervisor with Restart=always
    # simply resumes into the same failure.
    fresh(); record_ledger_write(False, 400, "deterministic", phase="reserve")
    _consecutive.clear()                       # everything in memory is gone
    cases.append(("halt survives losing all memory", is_halted()[0], True))

    for label, got, want in cases:
        good = got == want
        ok &= good
        print(f"  {label:<44} halts={str(got):<6} "
              f"{'ok' if good else '** expected ' + str(want) + ' **'}")
    SENTINEL.unlink(missing_ok=True)
    print("\nBREAKER:", "correct" if ok else "*** DEFECTIVE ***")
