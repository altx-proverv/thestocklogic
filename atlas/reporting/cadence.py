"""
WHICH MESSAGES GO TO TELEGRAM IMMEDIATELY, AND WHICH WAIT FOR A REPORT
======================================================================

The channel had one cadence: everything, the moment it happened, 360 cycles a
day. An entry and a blocked-symbol notice arrived the same way, so the channel
became something to scroll past -- and on 2026-10-15 a position was entered
with no stop while the morning's traffic was blocked-symbol notices.

THE DIVIDING LINE IS EVENT VERSUS STATUS, not importance.

An EVENT happened once, at a moment, and is gone if you miss it: an entry, a
fill, a self-halt, a protective order that failed, PRECLOSE BLIND. Those still
fire immediately and are untouched.

STATUS is a condition that is still true five minutes from now and can
therefore be read from a report instead: which gate is refusing, that ATLAS is
still paused, that a stock broke its range in a session where the breaker
blocks nothing. Repeating it per cycle adds no information after the first
time, and every repeat costs the attention the events need.

SILENCED HERE MEANS "NOT SENT TO TELEGRAM". It does not mean discarded:
market_open.alert() still writes the full line to the journal, still counts it
into the session's failure tally, and atlas_entry_log still gets a row per
evaluation. Everything silenced here is reported by
atlas/reporting/session_report.py at 11:30, 13:30, 15:30 and 15:50 -- the
blocked counts as "candidates evaluated and the most common blocker", the
pause as the halt and mode lines of the gate block, the observed range breaks
as the breaker lines.

NOTHING ON THE KEEP LIST IS REACHABLE FROM HERE. The four classes the operator
named -- entry placed/filled, self-halt, protection failure, PRECLOSE BLIND --
do not route through alert() at all, or do so under kinds not listed below:
notify_entry() calls send() directly and is documented as never throttled,
breaker.halt() sends its own message, and the protection failures are raised
and alerted by exits.py and preclose_gtt.py under their own names.
"""

# Exact alert kinds that are status, not events.
SILENCED_KINDS = {
    "PAUSED": (
        "The observe-only state, re-alerted every cycle it persists. It is "
        "throttled per reason, but a halt that lasts a session still produces "
        "a message every ALERT_COOLDOWN_SECONDS for hours, all saying the same "
        "thing. The halt file and the mode are the first two lines of every "
        "report's gate block, which is where a persistent state belongs."),
    "RECONCILE": (
        "'N position(s) adopted at startup' -- a startup census, not an event. "
        "RECONCILE FAILED is a different kind and still fires."),
}

# Kinds matched by prefix. BLOCKED_* is per symbol per cycle and is by far the
# largest source of traffic: 103 candidates that all fail the same gate is 103
# messages saying one thing.
SILENCED_PREFIXES = {
    "BLOCKED_": (
        "One message per blocked symbol per cycle. The gate and the reason are "
        "written to atlas_entry_log for every evaluation, so the information "
        "survives in full; the report names the count and the single most "
        "common blocker, which is the form the question is actually asked in."),
}

# Non-alert() sends that are status. Checked explicitly at their call site.
SILENCED_SENDS = {
    "RANGE_BREAK_OBSERVED": (
        "'RANGE BREAK — OBSERVED, NOT ACTED ON'. By construction nothing was "
        "done, and in observe-only mode nothing will be: it is a measurement "
        "of what the breaker WOULD have blocked. The count and the symbol list "
        "are in every report's breaker lines. The stock breaker actually "
        "firing is a different send and still goes out immediately."),
}


def telegram_silenced(kind: str) -> tuple:
    """(silenced, why). `kind` is an alert kind or a SILENCED_SENDS key."""
    k = str(kind or "")
    if k in SILENCED_KINDS:
        return True, SILENCED_KINDS[k]
    if k in SILENCED_SENDS:
        return True, SILENCED_SENDS[k]
    for p, why in SILENCED_PREFIXES.items():
        if k.startswith(p):
            return True, why
    return False, ""


def inventory() -> list:
    """Every silenced message type and why. For tools/atlas_switches.py and
    for answering "what did you turn off" without reading the diff."""
    rows = [(k, "alert", v) for k, v in sorted(SILENCED_KINDS.items())]
    rows += [(p + "*", "alert", v) for p, v in sorted(SILENCED_PREFIXES.items())]
    rows += [(k, "send", v) for k, v in sorted(SILENCED_SENDS.items())]
    return rows
