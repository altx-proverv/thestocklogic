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
# A reporting write gets more rope than a risk input and NEVER halts -- see
# record_reporting_write. Four cycles is about twenty minutes of the market-hours
# loop: long enough that a Supabase blip stays quiet, short enough that a broken
# schema is reported inside the first session rather than the next audit.
MAX_CONSECUTIVE_REPORTING_FAILURES = 4

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
            # NAME THE FILE. The reason alone reads as a live condition, so an
            # operator who has already fixed the cause looks for a cause that is
            # gone. The halt is a FILE, it outlives a restart and an atlas_state
            # edit, and the message has to say so or the next hour is spent the
            # way 2026-10-01 was.
            return True, (f"{SENTINEL.read_text().strip()[:320]} "
                          f"[halt file {SENTINEL} — send /resume, or delete it]")
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


def clear() -> bool:
    """Operator action: resume. Clears BOTH halves of a halt. -> fully cleared.

    IT USED TO CLEAR ONLY THE FILE, and /normal set only atlas_state, so neither
    action on its own resumed anything. On 2026-10-01 the operator set mode NORMAL
    and the loop stayed paused for an hour: paused() checks the sentinel FIRST and
    returns before it ever reads the mode, so the file silently won and nothing in
    the log said a file was involved.

    A halt is one state with two records of it -- a local file that survives a
    restart and a row the dashboard can see. Clearing one is not resuming.

    Order matters. The sentinel goes first: it is local, it cannot fail for the
    reason we are most likely halted, and it is what the next cycle reads. If the
    atlas_state patch then fails the loop is already able to trade, and the mode
    row is stale in the SAFE direction -- it says PAUSED while entries are
    enabled, which an operator will notice, rather than the reverse.
    """
    ok = True
    try:
        existed = SENTINEL.exists()
        SENTINEL.unlink(missing_ok=True)
        log.info(f"halt sentinel {'cleared' if existed else 'was not present'} "
                 f"({SENTINEL})")
    except Exception as e:
        log.error(f"could not clear halt sentinel {SENTINEL}: {e}")
        ok = False

    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/atlas_state?id=eq.1",
            headers=_headers(),
            json={"mode": "NORMAL",
                  "notes": f"Resumed at {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
                           f" — halt sentinel cleared"},
            timeout=10)
        if r.status_code not in (200, 204):
            log.error(f"sentinel cleared but atlas_state still PAUSED: "
                      f"HTTP {r.status_code} {r.text[:160]}")
            ok = False
    except Exception as e:
        log.error(f"sentinel cleared but atlas_state not reset: {e}")
        ok = False
    return ok


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
    """Report a per-cycle read (positions, funds, state). Returns may-continue.

    NOT IMPLEMENTED, AND WANTED: the deterministic/transient split that
    record_ledger_write above already makes.
    ---------------------------------------------------------------------------
    On 2026-10-01 a schema error tripped this after three consecutive failures
    and the session stayed halted until the operator fixed the schema by hand.
    Both halves of that were wrong in opposite directions:

      TOO SLOW. A missing column answers identically on every attempt. Waiting
      for three failures to establish that spends two cycles learning something
      the first response already said. record_ledger_write treats a 4xx as
      deterministic and halts at once, with the reason naming why -- "will not
      self-heal". A read has the same classes of failure and does not use them.

      TOO FINAL. A timeout or a 5xx is the opposite case: it usually clears on
      its own, and three of them inside three minutes is a Supabase blip rather
      than a broken system. Halting on that costs a session and needs a human to
      clear a sentinel, for something that had already fixed itself.

    The shape, when it is built:

      4xx, PGRST1xx/2xx, "does not exist", "column", "schema"
                     -> deterministic. Halt on the FIRST one. The message
                        already contains the remedy; a counter adds nothing.
      timeout, connection reset, 5xx, 429
                     -> transient. Retry with backoff -- 1s, 4s, 15s -- and only
                        halt if a read is still failing after the window. A
                        retried read that succeeds must clear the counter.
      anything unclassified
                     -> count as today, which is the safe default: an unfamiliar
                        failure should not get the permissive branch.

    Deliberately NOT done in the same change as the live view, because this
    changes when ATLAS stops trading and that deserves its own commit and its own
    test. Note the asymmetry it does not touch: market_open.push_live_zones does
    not call this, because a reporting write that cannot reach Supabase must not
    count toward a trading halt. As of 2026-10-05 it calls
    record_reporting_write instead, which alerts without halting -- the silence
    was the problem, not the absence of a halt.
    """
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


def record_reporting_write(ok: bool, what: str, detail: str = "") -> bool:
    """Report a write that feeds the SITE, not the trading decision. Never halts.

    WHY THIS IS NOT record_read. market_open.push_live_zones deliberately avoided
    the breaker because a reporting table that cannot be written must not stop
    ATLAS trading -- record_read halts after three consecutive failures, and the
    live view being broken is not a reason to stop taking setups. That reasoning
    still holds and this function does not change it.

    WHAT IT FIXES IS THE OTHER HALF. Not calling the breaker at all meant the
    failure was invisible: atlas_live_zones was missing seven columns, PostgREST
    rejected every payload whole, and the write failed on every cycle of every
    session from the day the live view shipped until 2026-10-05 -- 0 rows, no
    alert, nothing in any audit. "Logged once per cycle and dropped" is only
    adequate if somebody reads the log.

    So: count, alert ONCE at the threshold, alert again when it recovers, and
    always return True. The counter is per `what`, so two reporting writes failing
    for different reasons do not mask each other.
    """
    key = f"report:{what}"
    if ok:
        n = _consecutive.get(key, 0)
        _consecutive[key] = 0
        if n >= MAX_CONSECUTIVE_REPORTING_FAILURES:
            # RECOVERY IS ALSO NEWS. An alert that never closes trains the
            # operator to ignore the channel.
            log.info(f"{what} write recovered after {n} consecutive failures")
            try:
                from atlas.reporting.telegram import send
                send(f"✅ <b>{what}</b> write recovered after {n} failed cycles.")
            except Exception as e:
                log.warning(f"could not send recovery alert: {e}")
        return True

    n = _consecutive.get(key, 0) + 1
    _consecutive[key] = n
    log.error(f"{what} write failed ({n}): {detail}")
    if n == MAX_CONSECUTIVE_REPORTING_FAILURES:
        # EXACTLY ONCE, on the crossing. A per-cycle alert for a broken schema is
        # a hundred messages a session and gets muted, which returns us to silence
        # by a different route.
        log.error(f"{what} has failed {n} consecutive cycles — alerting")
        try:
            from atlas.reporting.telegram import send
            send(f"⚠️ <b>{what}</b> write has failed {n} cycles in a row.\n"
                 f"<code>{detail[:300]}</code>\n\n"
                 f"Reporting only — ATLAS is NOT halted and continues to trade. "
                 f"The site will be stale until this is fixed.")
        except Exception as e:
            log.warning(f"could not send reporting-write alert: {e}")
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

    # A REPORTING WRITE MUST NEVER HALT, however long it fails for. The live view
    # was unwritable for a month because its failure was silent; the fix for that
    # is an alert, NOT a halt. A broken site table stopping ATLAS from trading
    # would be a worse bug than the one being fixed.
    fresh()
    for _ in range(MAX_CONSECUTIVE_REPORTING_FAILURES * 3):
        record_reporting_write(False, "atlas_live_zones", "column does not exist")
    cases.append((f"{MAX_CONSECUTIVE_REPORTING_FAILURES * 3} reporting-write failures",
                  is_halted()[0], False))
    # and it still reports may-continue on every one of them
    cases.append(("reporting write returns may-continue",
                  not record_reporting_write(False, "x", "y"), False))
    # recovery clears the counter, so the next outage alerts again
    fresh()
    for _ in range(MAX_CONSECUTIVE_REPORTING_FAILURES):
        record_reporting_write(False, "atlas_live_zones", "boom")
    record_reporting_write(True, "atlas_live_zones")
    cases.append(("recovery clears the reporting counter",
                  _consecutive.get("report:atlas_live_zones", 0) != 0, False))
    # the counter is per-target, so two broken writes do not mask each other
    fresh()
    record_reporting_write(False, "a", "x")
    record_reporting_write(False, "b", "x")
    cases.append(("reporting counters are per-target",
                  _consecutive.get("report:a") != 1
                  or _consecutive.get("report:b") != 1, False))

    for label, got, want in cases:
        good = got == want
        ok &= good
        print(f"  {label:<44} halts={str(got):<6} "
              f"{'ok' if good else '** expected ' + str(want) + ' **'}")
    SENTINEL.unlink(missing_ok=True)
    print("\nBREAKER:", "correct" if ok else "*** DEFECTIVE ***")
