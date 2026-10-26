#!/usr/bin/env python3
"""TWO SILENT-FAILURE PATHS, CLOSED AND KEPT CLOSED.

Both defects here reported success while producing nothing real, which is the
one failure shape that cannot be caught by reading output.

1. A FEED THAT DIES AND NOTHING NOTICES. 2026-10-20: the WebSocket connected,
   subscribed 498 tokens, received exactly 498 ticks -- the opening snapshot --
   and the heartbeat printed "ticks: 498" 360 times. The number was reported and
   never differenced. In the same session market_open's NIFTY quote read
   22894.00 in cycle 1 and 22894.00 in cycle 360, and the existing
   timestamp-based age check logged "[quote 0.0s old]" throughout, because
   Upstox stamps a current time onto a stale price.

2. A GUARD EACH PROCESS HAD TO REMEMBER. rbe_engine.market_is_open() tested
   `weekday() >= 5` and the clock and never the holiday list, so 2026-10-21 and
   2026-10-22 -- both NSE holidays -- ran as full sessions and wrote
   breadth_snapshots, heavy_quiet and live_signals rows for days that never
   opened. market_flash then published a sentence about the breadth of a closed
   market.

THE REGISTRY AT THE BOTTOM IS THE POINT OF THIS FILE. Eleven writers had no
trading-day check and four more each had their own spelling of one. Listing the
writers and asserting the gate is what stops the twelfth from being forgotten:
a new module that POSTs to Supabase fails this suite until it is classified as
gated or explicitly exempt with a reason.
"""
import ast
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from tests._srcutil import executable_source          # noqa: E402


def func_src(path, name):
    """Executable source of ONE function, by name. Never the whole module.

    WHY THIS EXISTS. The first version of this file asserted
    `"is_session_today()" in rbe_source`, which passed after the guard was
    deleted from market_is_open() because main() still called it elsewhere in
    the file. Four of the seven assertions here were weak the same way -- the
    substring survived the mutation because it also occurred somewhere
    innocent. A module-wide `in` test cannot say WHERE a call is, and where it
    is was the entire point.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = [n for n in node.body
                    if not (isinstance(n, ast.Expr)
                            and isinstance(n.value, ast.Constant)
                            and isinstance(n.value.value, str))]
            return "\n".join(ast.unparse(n) for n in body)
    raise AssertionError(f"{path} has no function named {name!r} — the test is "
                         f"asserting against something that moved")

def calls_gate(path, gate="require_trading_day"):
    """True if the module actually CALLS the gate somewhere.

    An `in` test over the source passes on the import line alone. Deleting the
    `if not require_trading_day(...)` from market_flash left
    `from engine.trading_calendar import require_trading_day` behind, and the
    first version of the registry below reported that module as gated.
    """
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = (getattr(f, "id", None)
                    or getattr(f, "attr", None))
            if name == gate:
                return True
    return False


IST = timezone(timedelta(hours=5, minutes=30))
failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:24]:<26} {'ok' if ok else 'FAIL':<6} {note}")


def head(title):
    print()
    print(title)
    print("-" * 112)


# ══════════════════════════════════════════════════════════════════
head("THE MONITOR DIFFERENCES THE VALUE, WHICH IS THE WHOLE IDEA")
# ══════════════════════════════════════════════════════════════════
from atlas.freshness import (Monitor, fingerprint,             # noqa: E402
                             MAX_IDENTICAL_INTERVALS,
                             DERIVED_FOR_INTERVAL_SECONDS)

m = Monitor("probe")
check("a monitor with one observation is not stale",
      m.observe(100)["stale"], False, "it is a baseline, not evidence")
check("  and is not armed either", m.state()["armed"], False)

# A FEED THAT NEVER STARTS IS NOT A FEED THAT DIED. This is the 09:15 case:
# before the first tick the value cannot have changed, and standing the day
# down every morning would be a worse bug than the one being fixed.
m2 = Monitor("never starts", baseline=0)
for _ in range(MAX_IDENTICAL_INTERVALS + 5):
    v = m2.observe(0)                # still at the baseline: nothing arrived
check(f"unchanged from the first observation x{MAX_IDENTICAL_INTERVALS + 6} "
      f"is STILL not stale", v["stale"], False, "unarmed: never moved once")

# Now the real shape: it moves, then stops. That is 2026-10-20.
#
# THE SEQUENCE IS THE PRODUCTION ONE, NOT A CONVENIENT ONE. This block used to
# call observe(0) then observe(498), hand-feeding the monitor a transition. The
# heartbeat loop never does that: the 498 opening ticks land before its first
# observation, so its FIRST sample is already 498 and no change is ever
# observed. Forcing the condition end to end showed the monitor sitting unarmed
# and silent through six intervals -- the original defect, reproduced inside its
# own guard. Arming is on the baseline now, and this test replays the real
# order so it cannot drift back.
m3 = Monitor("rbe ticks", baseline=0)
m3.observe(498)                      # first sample ALREADY has the snapshot
check("a first sample above the baseline arms it", m3.state()["armed"], True,
      "no transition was observed")
check("  and is not yet stale", m3.state()["stale"], False)
verdicts = [m3.observe(498)["stale"] for _ in range(MAX_IDENTICAL_INTERVALS)]
check(f"stale on the {MAX_IDENTICAL_INTERVALS}th identical interval",
      verdicts, [False] * (MAX_IDENTICAL_INTERVALS - 1) + [True],
      f"N={MAX_IDENTICAL_INTERVALS}")
check("  and the detail names the frozen value",
      "498" in m3.state()["detail"], True)
check("  and says it is a stopped feed, not a quiet market",
      "quiet market" in m3.state()["detail"], True)

# A single moving interval clears the run -- a transient must not stand the
# session down.
m4 = Monitor("transient", baseline=0)
m4.observe(10)
for _ in range(MAX_IDENTICAL_INTERVALS - 1):
    m4.observe(10)
m4.observe(11)
check("one change resets the identical run", m4.state()["identical"], 0)
check("  so a transient gap never reaches stale", m4.state()["stale"], False)

# THE REAL MONDAY SERIES MUST NOT TRIP IT. Derived, not invented: these are the
# five smallest per-interval deltas from reports/rbe.log on 2026-10-19, the
# live session immediately before the outage.
MONDAY_SMALLEST_DELTAS = [12075, 14208, 14424, 14432, 14570]
m5 = Monitor("monday", baseline=0)
cum = 0
mon_stale = False
for d in MONDAY_SMALLEST_DELTAS * 4:
    cum += d
    if m5.observe(cum)["stale"]:
        mon_stale = True
check("Monday's own tightest 20 intervals never read stale", mon_stale, False,
      f"min delta {min(MONDAY_SMALLEST_DELTAS)} ticks/min")

# Tuesday, replayed exactly: 498 then nothing.
m6 = Monitor("tuesday", baseline=0)
tue_at = None
for i in range(0, 361):              # every sample is 498, as it was
    if m6.observe(498)["stale"]:
        tue_at = i
        break
# AN ABSOLUTE BOUND, NOT A RELATIVE ONE. This said `tue_at ==
# MAX_IDENTICAL_INTERVALS`, which follows the constant: raising N to 90 kept the
# assertion green while detection slipped to an hour and a half. The bound has
# to come from the evidence instead -- no live session on record has a single
# 60s interval without new ticks, so N has no measured reason to exceed a
# handful, and a feed death must be caught in minutes.
TUESDAY_MUST_BE_CAUGHT_WITHIN = 5          # intervals == minutes at 60s
check("Tuesday's series trips", tue_at is not None, True)
check(f"  and within {TUESDAY_MUST_BE_CAUGHT_WITHIN} minutes, not 360",
      tue_at <= TUESDAY_MUST_BE_CAUGHT_WITHIN, True, f"caught at {tue_at}")
check("  N stays inside the band the evidence supports",
      1 <= MAX_IDENTICAL_INTERVALS <= TUESDAY_MUST_BE_CAUGHT_WITHIN, True,
      f"N={MAX_IDENTICAL_INTERVALS}, observed max zero-run 0")

check("the quote fingerprint changes when one price moves",
      fingerprint({"A": 1.0, "B": 2.0}) == fingerprint({"A": 1.0, "B": 2.5}),
      False)
check("  and is equal for an identical map",
      fingerprint({"A": 1.0, "B": 2.0}) == fingerprint({"A": 1.0, "B": 2.0}),
      True)
check("  and changes when the map loses a symbol",
      fingerprint({"A": 1.0, "B": 2.0}) == fingerprint({"A": 1.0}), False)


# ══════════════════════════════════════════════════════════════════
head("THE THRESHOLD IS DERIVED AND SAYS SO")
# ══════════════════════════════════════════════════════════════════
# A threshold with no evidence beside it is a guess, and a guess that happens to
# be wrong reads as a product bug. The numbers must be IN the module.
fsrc = (ROOT / "atlas" / "freshness.py").read_text()
check("the measured session count is stated", "47" in fsrc, True)
check("the observed zero-interval count is stated",
      re.search(r"zero new ticks\s+0", fsrc) is not None, True)
check("the smallest live delta ever seen is stated", "3,156" in fsrc, True)
check("Monday's distribution is stated", "12,075" in fsrc and "15,983" in fsrc,
      True)
check("Tuesday's 360-of-360 is stated", "360 of 360" in fsrc, True)
check("the sampling interval the evidence belongs to is named",
      DERIVED_FOR_INTERVAL_SECONDS, 60)
check("N is above every observed zero-run", MAX_IDENTICAL_INTERVALS > 0, True)

# A monitor sampling at another cadence is off-evidence and must say so.
import logging                                                  # noqa: E402
_warned = []
_h = logging.Handler(); _h.emit = lambda r: _warned.append(r.getMessage())
logging.getLogger("ATLAS-FRESHNESS").addHandler(_h)
Monitor("fast sampler", interval_seconds=5)
check("a different sampling interval warns that N is not evidence",
      any("not evidence at this cadence" in w for w in _warned), True)
logging.getLogger("ATLAS-FRESHNESS").removeHandler(_h)


# ══════════════════════════════════════════════════════════════════
head("at_zone REFUSES A STALE PRICE RATHER THAN ANSWERING WELL")
# ══════════════════════════════════════════════════════════════════
import atlas.signal.market_open as MO                            # noqa: E402

SIG = {"entry_low": 100.0, "entry_high": 110.0}
ok_fresh, why_fresh = MO.at_zone(SIG, 105.0, "LONG")
check("a fresh price inside the band is an entry", ok_fresh, True, why_fresh[:34])
ok_stale, why_stale = MO.at_zone(SIG, 105.0, "LONG", fresh=False)
check("the SAME price, stale, is refused", ok_stale, False)
check("  and the reason names staleness, not the zone",
      "stale" in why_stale and "zone" not in why_stale.split("—")[0], True)
# The refusal must come BEFORE the arithmetic, so a stale price cannot be
# reported as mitigated either -- "the setup died" and "we cannot see" are
# different facts about a session.
ok_m, why_m = MO.at_zone(SIG, 50.0, "LONG", fresh=False)
check("a stale price below the band is not called mitigated",
      "mitigated" in why_m, False, "refusal precedes the comparison")
check("  default stays fresh=True, so existing callers are unchanged",
      MO.at_zone(SIG, 105.0, "LONG")[0], True)


# ══════════════════════════════════════════════════════════════════
head("A FROZEN FEED IS NOT A RANGE BREAK")
# ══════════════════════════════════════════════════════════════════
from atlas.execution import sentiment_window as SW                # noqa: E402

check("there is a distinct reason code for a stale feed",
      SW.REASON_FEED_STALE, "FEED_STALE_STAND_DOWN")
check("  and it differs from the range-break code",
      SW.REASON_FEED_STALE != SW.REASON_STAND_DOWN, True)

# reason_code must TRAVEL FROM the kind the caller fired with. It lands in
# atlas_entry_log.blocked_by; hardcoding it made a dead feed indistinguishable
# from a market reversal in every count over that column.
# BEHAVIOURAL, not textual. `st.get("kind")` already appeared in state() on
# another key, so a substring test passed with the reason_code hardcoded.
# BEHAVIOURAL, not textual. `st.get("kind")` already appeared in state() on
# another key, so a substring test passed with the reason_code hardcoded.
#
# AND THE CODE IS NOT THE KIND. `kind` is a human label, reason_code is the
# countable bucket that lands in atlas_entry_log.blocked_by. Conflating them
# was the first attempt here and tests/test_sentiment_standdown.py caught it:
# it fires with kind="RANGE_BREAK" and requires the code to still read
# RANGE_BREAK_STAND_DOWN.
def _loaded(**kw):
    base = {"session_date": SW._now_ist().date().isoformat(), "stood_down": True,
            "at": "09:18:00", "kind": "X", "detail": "d", "reason_code": None}
    return lambda: {**base, **kw}


_real_load = SW._load
try:
    SW._load = _loaded(kind="FEED_STALE", reason_code=SW.REASON_FEED_STALE)
    check("state() reports the code the caller fired with",
          SW.state()["reason_code"], "FEED_STALE_STAND_DOWN",
          "lands in atlas_entry_log.blocked_by")
    SW._load = _loaded(kind="RANGE_BREAK", reason_code=SW.REASON_STAND_DOWN)
    check("  and a range break still reports a range break",
          SW.state()["reason_code"], "RANGE_BREAK_STAND_DOWN")
    # An old file, written before reason_code existed, was always a range break.
    SW._load = _loaded(kind="RANGE_BREAK")
    check("  a pre-existing file with no code reads as a range break",
          SW.state()["reason_code"], "RANGE_BREAK_STAND_DOWN",
          "every stand-down was one until 2026-10-22")
finally:
    SW._load = _real_load

_stood = {}
_orig_save, _orig_alert = SW._save, SW._alert
SW._save = lambda st: _stood.update(st)
SW._alert = lambda s: None
try:
    SW._load = lambda: {"session_date": SW._now_ist().date().isoformat(),
                        "stood_down": False, "at": None, "kind": None,
                        "detail": ""}
    # The same call shape rbe_engine.feed_is_dead uses: a label plus the code.
    SW.stand_down("FEED_STALE", "the feed stopped at 09:18",
                  reason_code=SW.REASON_FEED_STALE)
    check("a feed stand-down persists its own code",
          _stood.get("reason_code"), "FEED_STALE_STAND_DOWN")
    check("  with the label kept separate", _stood.get("kind"), "FEED_STALE")
finally:
    SW._save, SW._alert = _orig_save, _orig_alert


# ══════════════════════════════════════════════════════════════════
head("ONE TRADING-DAY GATE, AND IT FAILS CLOSED")
# ══════════════════════════════════════════════════════════════════
from engine.trading_calendar import (require_trading_day,         # noqa: E402
                                     is_trading_day, NSE_HOLIDAYS)

# REPOINTED 2026-10-22, AND THESE THREE WERE WRONG WHEN WRITTEN. They asserted
# that 20 Oct was a session and 21-22 were holidays, which is what the
# hand-maintained NSE_HOLIDAYS said. NSE's own holiday-master API says the
# opposite: 2026-10-20 is DUSSEHRA, and 21 and 22 October were real sessions
# with genuine bhavcopies of 3,708 and 3,649 rows. The gate mechanism tested
# here was never the problem; the calendar behind it was, and these assertions
# encoded the bad data rather than catching it.
check("20 Oct 2026 is DUSSEHRA — refused",
      require_trading_day(date(2026, 10, 20), "test"), False,
      "ATLAS traded this closed market")
check("21 Oct 2026 was a real session — allowed",
      require_trading_day(date(2026, 10, 21), "test"), True,
      "ATLAS stood down for it")
check("22 Oct 2026 was a real session — allowed",
      require_trading_day(date(2026, 10, 22), "test"), True)
check("a Saturday is refused", require_trading_day(date(2026, 10, 24), "test"),
      False)
check("  the holiday is the one the API names",
      {"2026-10-20"} <= NSE_HOLIDAYS, True)
check("  and the two sessions are not in it",
      bool({"2026-10-21", "2026-10-22"} & set(NSE_HOLIDAYS)), False)


class _Boom:
    def __contains__(self, k):
        raise RuntimeError("calendar unreadable")


import engine.trading_calendar as TC                              # noqa: E402
_real = TC.NSE_HOLIDAYS
TC.NSE_HOLIDAYS = _Boom()
try:
    check("an unreadable calendar FAILS CLOSED",
          require_trading_day(date(2026, 10, 20), "test"), False,
          "a publisher that cannot tell the day must not publish")
finally:
    TC.NSE_HOLIDAYS = _real

# THE GATE MUST READ THE DAY IN IST, NOT IN THE BOX'S UTC. Comparing
# TC._today_ist() against datetime.now(IST).date() cannot fail on a machine
# whose local date already equals IST -- it passed with the body replaced by
# date.today(). So the assertion is on the construction: an explicit +05:30,
# and no naive local-date call.
tsrc = func_src(ROOT / "engine" / "trading_calendar.py", "_today_ist")
check("the gate builds its date in IST explicitly",
      "hours=5" in tsrc and "minutes=30" in tsrc, True)
check("  and never uses the box's naive local date",
      "date.today()" in tsrc, False, "the box runs UTC")
check("  and the value agrees with IST now", TC._today_ist(),
      datetime.now(IST).date())


# ══════════════════════════════════════════════════════════════════
head("rbe_engine NO LONGER TREATS A HOLIDAY AS A SESSION")
# ══════════════════════════════════════════════════════════════════
RBE = ROOT / "engine" / "rbe_engine.py"
rsrc = executable_source(RBE)

# SCOPED TO THE FUNCTION. market_is_open() is the one the writers call; a
# holiday check anywhere else in the file does not protect them.
mio = func_src(RBE, "market_is_open")
check("market_is_open itself consults the session gate",
      "is_session_today()" in mio, True, "not merely somewhere in the file")
check("  and no longer decides the day on weekday() alone",
      "weekday()" in mio, False)
check("the session gate calls the shared calendar",
      "require_trading_day" in func_src(RBE, "is_session_today"), True)
check("  there is no second holiday list in rbe_engine",
      "NSE_HOLIDAYS" in rsrc, False)

# THE HEARTBEAT MUST DIFFERENCE. Asserted on the loop's own source, and on the
# subtraction, because the previous version of this check passed while `delta`
# survived only inside the log f-string.
mainsrc = func_src(RBE, "main")
check("the heartbeat computes a delta by subtraction",
      "count - prev_count" in mainsrc, True)
check("  and feeds the observation to the monitor",
      "ticks.observe(count)" in mainsrc, True)
check("  and acts on the verdict",
      "feed_is_dead" in mainsrc, True)
check("  the shared monitor owns the threshold, not rbe_engine",
      "MAX_IDENTICAL" in rsrc, False, "no local copy of N")
check("a dead feed uses the stale reason code, not the range-break one",
      "REASON_FEED_STALE" in func_src(RBE, "feed_is_dead"), True)

# THE BASELINE MUST BE DECLARED AT THE CALL SITE, and this assertion exists
# because its absence is invisible everywhere else. A Monitor built without
# `baseline` arms only on an observed change, and the heartbeat never observes
# one on the sequence that matters -- the opening ticks land before its first
# sample. Dropping `baseline=0` therefore disables the entire guard while every
# other test in this file still passes.
check("the heartbeat's monitor declares its baseline",
      "baseline=0" in mainsrc.replace(" ", ""), True,
      "without it the guard never arms")
check("the quote monitor declares its baseline too",
      "baseline=" in func_src(ROOT / "atlas" / "signal" / "market_open.py",
                              "__init__").replace(" ", ""), True)
check("  the startup refuses a non-trading day before connecting",
      rsrc.index("is_session_today()") < rsrc.index("KiteTicker"), True)

# THE ORDER INSIDE feed_is_dead IS LOAD-BEARING: stand down, then alert, then
# stop. Only the first protects money.
fid = func_src(RBE, "feed_is_dead")
check("feed_is_dead stands down before it alerts",
      fid.index("stand_down") < fid.index("send"), True)
check("  and closes the socket last",
      fid.index("send") < fid.index("kws.close"), True)


# ══════════════════════════════════════════════════════════════════
head("THE REGISTRY — EVERY PUBLIC-TABLE WRITER IS CLASSIFIED")
# ══════════════════════════════════════════════════════════════════
# GATED: writes a claim about a market session. Must call the gate.
GATED = {
    "engine/rbe_engine.py":         "breadth_snapshots heavy_quiet intraday_prices live_signals",
    "engine/market_flash.py":       "market_flash",
    "engine/index_quotes.py":       "index_quotes",
    "engine/publish_orb.py":        "orb_breaks",
    "engine/publish_closes.py":     "daily_closes",
    "engine/07_sector_momentum.py": "sector_heatmap",
    "engine/upstox_ws.py":          "live_prices sector_heatmap",
    "engine/btst_engine.py":        "live_signals live_prices",
    "engine/surveillance_snapshot.py": "surveillance_snapshots surveillance_captures",
}

# EXEMPT, each with the reason it is exempt. An exemption is a decision and has
# to be written down; "it does not need one" is not a reason.
EXEMPT = {
    "atlas/execution/atlas_entry.py":
        "writes atlas_trades on a real fill; market_open gates the session above it",
    "atlas/execution/preclose_gtt.py":
        "already gates, and is an EXIT path -- must run whatever the calendar says",
    "atlas/execution/reconcile.py":
        "exit reconciliation; a stop that filled must be recorded on any day",
    "atlas/execution/trade_management.py":
        "DEAD CODE — nothing imports it (broker.py:438 says so). Listed here so "
        "the completeness check stays green; it writes nothing because it never "
        "runs. Exempt by being unreachable, not by being an exit path.",
    "atlas/execution/trade_outcome_checker.py":
        "exit path; reconciles fills that already happened",
    "atlas/execution/index_state.py":
        "atlas_market_state is intraday engine state, not a published session",
    "atlas/execution/zerodha_login.py":
        "broker_tokens: auth must refresh on any day",
    "engine/upstox_auto_login.py":
        "upstox_tokens: auth must refresh on any day",
    "atlas/reporting/directives.py":
        "operator commands; must answer on any day",
    "atlas/risk/breaker.py":
        "atlas_state halt/health; must be writable on any day",
    "atlas/signal/market_open.py":
        "already gates the whole session in should_run()",
    "engine/06_push_supabase.py":
        "publishes a batch stamped with the SESSION it was built from, never today",
    "engine/mark_signals.py":
        "already imports is_trading_day; marks a named past session",
    "engine/update_outcomes.py":
        "already imports is_trading_day; scores named past sessions",
    "engine/excursions.py":
        "signal_excursions over historical sessions, stamped from the data",
    "engine/reject_sample.py":
        "reject_sample is a diagnostic over a named batch",
    "engine/learning_loop.py":
        "learning_hypotheses/results are analysis, not a session claim",
    "engine/publish_protection.py":
        "protection of OPEN positions is real on a holiday and must be checked",
    "radar/store.py":
        "radar_events; radar/bars.py gates the session upstream",
}

for mod, tables in sorted(GATED.items()):
    check(f"GATED  {mod}", calls_gate(ROOT / mod), True, tables[:44])

# AND THE RESULT MUST BE USED. A gate whose return value is dropped --
# `require_trading_day(...)` as a bare statement -- reads exactly like a guard
# and guards nothing, which is this whole file's subject.
#
# NOT "inside an if". rbe_engine legitimately routes the gate through
# is_session_today(), which memoises it because market_is_open() is reached from
# the tick path; the `if` is one call further out. Demanding a literal `if`
# failed that correct shape, so the assertion is on the result being consumed
# rather than on where the branch sits.
def gate_result_used(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        f = node.value.func
        if (getattr(f, "id", None) or getattr(f, "attr", None)) == "require_trading_day":
            return False          # a bare call: result thrown away
    return True


for mod in sorted(GATED):
    check(f"  result is used  {Path(mod).name}",
          gate_result_used(ROOT / mod), True, "not a discarded bare call")

# COMPLETENESS. Every module that POSTs to Supabase must appear in exactly one
# list. This is the assertion that stops the next writer being forgotten --
# without it, the eleven unguarded publishers would simply have become twelve.
POST = re.compile(r"requests\.(post|patch|put|delete)|session\.(post|patch)")
discovered = []
for d in ("atlas", "engine", "radar"):
    for f in sorted((ROOT / d).rglob("*.py")):
        if "legacy" in f.parts or "__pycache__" in f.parts:
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        if "rest/v1" in txt and POST.search(txt):
            discovered.append(str(f.relative_to(ROOT)))

classified = set(GATED) | set(EXEMPT)
unclassified = sorted(set(discovered) - classified)
check("every Supabase writer is classified", unclassified, [],
      f"{len(discovered)} writers found")
stale_entries = sorted(classified - set(discovered))
check("  and the registry lists no module that no longer writes",
      stale_entries, [], "a dead entry hides a live gap")
check("  no module is both gated and exempt",
      sorted(set(GATED) & set(EXEMPT)), [])

print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED — feed freshness and the trading-day gate "
      f"({len(discovered)} Supabase writers classified, "
      f"{len(GATED)} gated, {len(EXEMPT)} exempt)")
