#!/usr/bin/env python3
"""THE OBSERVATION LEDGER: T+3 IS SESSIONS, SKIPS ARE REASONS, NOTHING RENDERS FIRST.

1. T+3 IS THREE NSE TRADING SESSIONS. A Friday observation scores against
   Wednesday; a window that crosses a holiday gets longer, never shorter. Both
   are asserted over EVERY qualifying date the fetched holiday master covers,
   not one hand-picked example, and the expected values are derived from the
   calendar rather than typed in. Then the same assertions are run against the
   two wrong implementations a person would actually write -- calendar-day
   arithmetic and weekdays-only -- and must FAIL on them. An assertion that
   passes a wrong implementation is not testing the calendar.

2. A SKIP IS A REASON, NOT A NUMBER. Each skip reason fires on the condition it
   names, and a clean window scores net of the 0.23% floor with the sign of the
   claimed direction.

3. NOTHING RENDERS BEFORE IT IS RECORDED. Every module that publishes a
   symbol-level row a page renders is listed; each listed function must call
   observations.record() before its first write to Supabase. A new publisher
   fails this suite until it is listed.

4. THE MIGRATION AND THE CODE AGREE on the type and skip-reason vocabularies,
   and the SQL carries every enforcement layer tools/prove_ledger_append_only.py
   proves in Postgres.
"""
import ast
import re
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests._srcutil import executable_source                  # noqa: E402
from engine import trading_calendar as tc                     # noqa: E402
from engine import observations as obs                        # noqa: E402
from engine import score_observations as so                   # noqa: E402

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:22]:<24} {'ok' if ok else 'FAIL':<6} {note}")


def head(title):
    print()
    print(title)
    print("-" * 112)


# ══════════════════════════════════════════════════════════════════
head("1. T+3 IS THREE TRADING SESSIONS, FROM THE FETCHED HOLIDAY MASTER")
# ══════════════════════════════════════════════════════════════════
st = tc.cache_state()
check("the fetched calendar is readable", st["ok"], True,
      "fails closed: no cache means every date is closed")
years = [y for y in st["years"] if y >= tc.API_FIRST_YEAR]
print(f"  covered years {years}, fetched {st['fetched_at']}")


def all_days():
    d = date(years[0], 1, 1)
    # stop 15 days short of the last covered year so every T+3 is resolvable
    end = date(years[-1], 12, 31) - timedelta(days=15)
    while d <= end:
        yield d
        d += timedelta(days=1)


def fridays_with_clean_week():
    """Fridays whose next Monday, Tuesday and Wednesday are all sessions."""
    for d in all_days():
        if d.weekday() == 4 and tc.is_trading_day(d) and all(
                tc.is_trading_day(d + timedelta(days=k)) for k in (3, 4, 5)):
            yield d


def holiday_windows():
    """Sessions whose naive weekday T+3 window contains a weekday holiday."""
    for d in all_days():
        if not tc.is_trading_day(d):
            continue
        k, wd = d, []
        while len(wd) < 3:
            k += timedelta(days=1)
            if k.weekday() < 5:
                wd.append(k)
        if any(not tc.is_trading_day(x) for x in wd):
            yield d, wd[-1]


def friday_violations(t_plus):
    bad = []
    for f in fridays_with_clean_week():
        t3 = t_plus(f)
        if t3 != f + timedelta(days=5) or t3.weekday() != 2:
            bad.append((f, t3))
    return bad


def holiday_violations(t_plus):
    bad = []
    for d, naive in holiday_windows():
        t3 = t_plus(d)
        between = sum(tc.is_trading_day(d + timedelta(days=i))
                      for i in range(1, (t3 - d).days)) if t3 else -1
        if t3 is None or not t3 > naive or not tc.is_trading_day(t3) or between != 2:
            bad.append((d, naive, t3))
    return bad


FRIDAYS = list(fridays_with_clean_week())
HOLIDAY_WINDOWS = list(holiday_windows())
check("there are clean-week Fridays to test", len(FRIDAYS) > 100, True,
      f"{len(FRIDAYS)} of them")
check("there are holiday-spanning windows to test", len(HOLIDAY_WINDOWS) > 50, True,
      f"{len(HOLIDAY_WINDOWS)} of them")
check("every clean-week Friday scores against the following Wednesday",
      friday_violations(so.t_plus), [])
check("every holiday-spanning window ends AFTER the weekday-count end",
      holiday_violations(so.t_plus), [],
      "extends, never shortens; exactly 2 sessions between")

# The two named cases, each from the calendar's own facts.
check("2026-10-09 is a Friday and 12/13/14 Oct are sessions",
      (date(2026, 10, 9).weekday(),
       [tc.is_trading_day(date(2026, 10, d)) for d in (12, 13, 14)]),
      (4, [True, True, True]))
check("  so Fri 2026-10-09 scores against Wed 2026-10-14",
      so.t_plus(date(2026, 10, 9)), date(2026, 10, 14))
check("Tue 2026-10-20 is a weekday the fetched master calls a holiday",
      (date(2026, 10, 20).weekday(), tc.is_trading_day(date(2026, 10, 20))),
      (1, False), "Dussehra")
check("  so Fri 2026-10-16 runs to Thu 2026-10-22, not Wed 2026-10-21",
      so.t_plus(date(2026, 10, 16)), date(2026, 10, 22))

# THE ASSERTIONS ABOVE MUST FAIL ON A WRONG CALENDAR. If either passes one of
# these, it is not testing what it claims.
def calendar_days(d):          # "three days later"
    return d + timedelta(days=3)


def weekdays_only(d):          # skips weekends, knows no holidays
    out = d
    for _ in range(3):
        out += timedelta(days=1)
        while out.weekday() >= 5:
            out += timedelta(days=1)
    return out


def off_by_one(d):             # counts T0 as session one
    days = tc.next_n_trading_days(d, 2)
    return days[-1] if len(days) == 2 else None


print()
check("BITE: the Friday assertion FAILS calendar-day arithmetic",
      bool(friday_violations(calendar_days)), True)
check("BITE: the holiday assertion FAILS weekdays-only",
      bool(holiday_violations(weekdays_only)), True)
check("BITE:   while weekdays-only PASSES the Friday one (so each test "
      "covers its own case)", friday_violations(weekdays_only), [])
check("BITE: both FAIL an off-by-one",
      (bool(friday_violations(off_by_one)), bool(holiday_violations(off_by_one))),
      (True, True))

# DUE MEANS THE T+3 BAR HAS LANDED, not that the clock passed it.
o_fri = {"observation_type": "signal", "session_date": "2026-10-09",
         "symbol": "X", "direction": "LONG", "reference_basis": "close_T0"}
_, nd, _ = so.score_batch([o_fri], as_of=date(2026, 10, 13),
                          prices=lambda s: None, results_fetch=lambda a, b: {})
check("a Friday observation is NOT due when the latest bar is Tuesday", nd, 1)
sc, nd, _ = so.score_batch([o_fri],
                           as_of=date(2026, 10, 14), prices=lambda s: None,
                           results_fetch=lambda a, b: {})
check("  and IS due once Wednesday's bar has landed",
      (nd, sc[0][1]["t3_session"] if sc else None), (0, "2026-10-14"))


# RADAR IS NEVER SCORED -- not scored, and not skipped either, since a skip
# row is still a row in the accuracy record.
_r = {**o_fri, "observation_type": "radar_event", "direction": None}
sc, nd, unres = so.score_batch([_r], as_of=date(2026, 10, 14), prices=lambda s: None,
                               results_fetch=lambda a, b: {})
check("a due radar event produces no score row, no skip row, no 'not due'",
      (len(sc), nd, len(unres)), (0, 0, 0))
check("  and the unscored types are radar_event and atlas_decision",
      sorted(obs.UNSCORED_TYPES), ["atlas_decision", "radar_event"])


# ══════════════════════════════════════════════════════════════════
head("2. A SKIP IS A REASON; A SCORE IS NET OF THE FLOOR, SIGNED")
# ══════════════════════════════════════════════════════════════════
import pandas as pd                                            # noqa: E402

T0, T3 = date(2026, 10, 16), date(2026, 10, 22)
SESS = so.window_sessions(T0, T3)
check("the window is T0 plus three sessions", [str(s) for s in SESS],
      ["2026-10-16", "2026-10-19", "2026-10-21", "2026-10-22"])


def bars(n_before=300, t3_close=101.0, gap_on=None):
    """Sessions ending at T3, flat at 100 except where told otherwise."""
    ds, d = [], T3
    while len(ds) < n_before + 3:
        if tc.is_trading_day(d):
            ds.append(d)
        d -= timedelta(days=1)
    ds.reverse()
    rows = []
    for x in ds:
        c = t3_close if x == T3 else 100.0
        o = 100.0
        if gap_on and x >= gap_on:
            o = c = 40.0 if x != T3 else t3_close * 0.4
        rows.append({"date": x, "open": o, "high": max(o, c), "low": min(o, c),
                     "close": c, "prev_close": rows[-1]["close"] if rows else 100.0,
                     "volume": 1000})
    return pd.DataFrame(rows)


CLEAN = {s: set() for s in SESS}
o_long = {"observation_type": "signal", "session_date": str(T0), "symbol": "X",
          "direction": "LONG", "reference_basis": "close_T0"}
o_short = {**o_long, "direction": "SHORT"}

s = so.classify(o_long, bars(), T3, SESS, CLEAN, {})
check("LONG, 100 -> 101: scored, move +1.0", (s["skip_reason"], s["move_pct"]),
      (None, 1.0))
check("  net is move minus the 0.23% floor", s["net_pct"], round(1.0 - 0.23, 4))
check("  outcome follows net, not move", s["outcome"], "net_positive")
s = so.classify(o_short, bars(), T3, SESS, CLEAN, {})
check("SHORT, 100 -> 101: move -1.0, net -1.23",
      (s["move_pct"], s["net_pct"], s["outcome"]), (-1.0, -1.23, "net_negative"))
s = so.classify(o_long, bars(t3_close=100.2), T3, SESS, CLEAN, {})
check("a move smaller than the floor nets negative",
      (s["move_pct"], s["outcome"]), (0.2, "net_negative"))

cases = [
    ("lookback too short (signal needs 252)",
     so.classify(o_long, bars(n_before=100), T3, SESS, CLEAN, {}), "lookback_too_short"),
    ("a results meeting inside [T0, T+3]",
     so.classify(o_long, bars(), T3, SESS, CLEAN, {"X": [date(2026, 10, 19)]}),
     "result_in_window"),
    ("  one on a WEEKEND inside the window still counts",
     so.classify(o_long, bars(), T3, SESS, CLEAN, {"X": [date(2026, 10, 18)]}),
     "result_in_window"),
    ("GSM/ASM on one session of the window",
     so.classify(o_long, bars(), T3, SESS, {**CLEAN, SESS[2]: {"X"}}, {}), "gsm_asm"),
    ("a price-basis discontinuity inside the window",
     so.classify(o_long, bars(gap_on=SESS[2]), T3, SESS, CLEAN, {}),
     "price_basis_discontinuity"),
    ("a session with no surveillance snapshot",
     so.classify(o_long, bars(), T3, SESS, {**CLEAN, SESS[1]: None}, {}),
     "surveillance_unknown"),
    ("the results calendar could not be read",
     so.classify(o_long, bars(), T3, SESS, CLEAN, None),
     "results_calendar_unavailable"),
    ("an observation with no direction",
     so.classify({**o_long, "observation_type": "heavy_quiet", "direction": None},
                 bars(), T3, SESS, CLEAN, {}), "not_directional"),
    ("no bar at T+3",
     so.classify(o_long, bars().iloc[:-1], T3, SESS, CLEAN, {}), "no_price_data"),
    ("T0 on a holiday",
     so.classify({**o_long, "session_date": "2026-10-20"}, bars(), T3, SESS, CLEAN, {}),
     "not_a_trading_session"),
]
for label, s, want in cases:
    check(label, s["skip_reason"], want)
    if s["move_pct"] is not None or s["outcome"] is not None:
        failures.append(f"{label}: a skip carried a number")
s = so.classify(o_long, bars(), T3, SESS, CLEAN, {"X": [date(2026, 10, 23)]})
check("a results meeting the day AFTER T+3 does not skip", s["skip_reason"], None)
s = so.classify(o_long, bars(n_before=100), T3, SESS, {**CLEAN, SESS[1]: {"X"}},
                {"X": [T0]})
check("several reasons: the first is recorded, all are kept",
      (s["skip_reason"], s["all_reasons"]),
      ("lookback_too_short", ["lookback_too_short", "result_in_window", "gsm_asm"]))


# ══════════════════════════════════════════════════════════════════
head("3. NOTHING RENDERS BEFORE IT IS RECORDED")
# ══════════════════════════════════════════════════════════════════
# Every symbol-level table a page renders, and every function that publishes
# to one. pages: signals.html reads signals and live_signals; home.html reads
# orb_breaks, radar_events and heavy_quiet.
RENDERED = ("signals", "live_signals", "orb_breaks", "radar_events", "heavy_quiet")
PUBLISHERS = {
    ("engine/06_push_supabase.py", "push_signals"):     "signals (signal)",
    ("engine/06_push_supabase.py", "push_candidates"):  "signals (candidate)",
    ("engine/rbe_engine.py", "fire_signal"):            "live_signals (detection)",
    ("engine/rbe_engine.py", "_write_heavy_quiet"):     "heavy_quiet",
    ("engine/btst_engine.py", "push_btst_signals"):     "live_signals (btst)",
    ("engine/upstox_ws.py", "push_live_signals"):       "live_signals (never called)",
    ("engine/publish_orb.py", "run"):                   "orb_breaks",
    ("radar/store.py", "upsert_events"):                "radar_events",
}
# Writers to those tables that publish no new observation, with the reason.
NOT_PUBLISHERS = {
    "engine/mark_signals.py":
        "reads signals; posts signal_marks / market_marks -- outcomes of "
        "published signals, not new observations",
    "engine/update_outcomes.py":
        "reads signals; posts signal_outcomes",
    "atlas/signal/market_open.py":
        "reads signals; posts atlas_entry_log and atlas_live_zones -- ATLAS's "
        "live view of a batch already recorded when 06_push published it",
}


def writes_in(fn):
    """Line numbers of every Supabase write call in a function."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("post", "delete", "patch", "put") \
                and getattr(n.func.value, "id", None) == "requests":
            out.append(n.lineno)
    return out


def record_lines(fn):
    return [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "record"
            and getattr(n.func.value, "id", None) == "observations"]


def find_fn(path, name):
    tree = ast.parse(executable_source(ROOT / path))
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def ordering_violations(publishers=PUBLISHERS, source=None):
    bad = []
    for (path, name) in sorted(publishers):
        fn = (source or {}).get((path, name)) or find_fn(path, name)
        if fn is None:
            bad.append(f"{path}:{name} not found")
            continue
        w, r = writes_in(fn), record_lines(fn)
        if not w:
            bad.append(f"{path}:{name} has no Supabase write — repoint the registry")
        elif not r:
            bad.append(f"{path}:{name} never calls observations.record")
        elif min(r) > min(w):
            bad.append(f"{path}:{name} writes before it records")
    return bad


check("every publisher records before its first Supabase write",
      ordering_violations(), [])
for (path, name), what in sorted(PUBLISHERS.items()):
    fn = find_fn(path, name)
    w, r = (writes_in(fn), record_lines(fn)) if fn else ([], [])
    print(f"    {path + ':' + name:<52} record@{min(r) if r else '-':<5} "
          f"first write@{min(w) if w else '-':<5} {what}")

# COMPLETENESS: any module that names a rendered table AND posts must be
# classified, or the next publisher is born unrecorded.
POSTS = re.compile(r"requests\.post\(")
TABLE = re.compile(r"rest/v1/(" + "|".join(RENDERED) + r")\b")
found = set()
for d in ("engine", "atlas", "radar", "meridian", "tools"):
    for f in sorted((ROOT / d).rglob("*.py")):
        if "legacy" in f.parts or "__pycache__" in f.parts:
            continue
        try:
            src = executable_source(f)
        except SyntaxError:
            continue
        if TABLE.search(src) and POSTS.search(src):
            found.add(str(f.relative_to(ROOT)))
classified = {p for p, _ in PUBLISHERS} | set(NOT_PUBLISHERS)
check("every module that posts to a rendered table is classified",
      sorted(found - classified), [], f"{len(found)} found")
check("  and nothing classified has stopped matching",
      sorted(classified - found), [], "a dead entry hides a live gap")

# BITE: a publisher with the record removed, and one with it moved after the
# post, must both be caught.
_probe = ast.parse(
    "def a():\n    requests.post(u)\n"
    "def b():\n    requests.post(u)\n    observations.record(x)\n"
    "def c():\n    observations.record(x)\n    requests.delete(u)\n    requests.post(u)\n")
_fns = {(f"p{n.name}", n.name): n for n in _probe.body}
check("BITE: missing record, and record after the post, are both caught",
      len(ordering_violations({k: "" for k in _fns}, _fns)), 2)


# ══════════════════════════════════════════════════════════════════
head("4. THE WRITER DECIDES ORIGINAL / CORRECTION / NOTHING, AND FAILS CLOSED")
# ══════════════════════════════════════════════════════════════════
def row(otype, vals):
    return obs.build(otype, symbol="X", session_date="2026-10-16", direction="LONG",
                     computed_values=vals, source_file="t")


r1 = row("signal", {"a": 1})
check("a new key is an original", [x.get("corrects_observation_id") for x in
                                   obs.plan([r1], [])], [None])
ex = [{"observation_id": "o1", "natural_key": r1["natural_key"],
       "values_sha256": r1["values_sha256"], "corrects_observation_id": None}]
check("the same values again are nothing", obs.plan([r1], ex), [])
r2 = row("signal", {"a": 2})
p = obs.plan([r2], ex)
check("different values on a 'correct' type append a correction of the original",
      [(x["corrects_observation_id"], bool(x["correction_reason"])) for x in p],
      [("o1", True)])
ex2 = ex + [{"observation_id": "c1", "natural_key": r1["natural_key"],
             "values_sha256": r2["values_sha256"], "corrects_observation_id": "o1"}]
check("  and repeating the corrected values is nothing", obs.plan([r2], ex2), [])
h1, h2 = row("heavy_quiet", {"r": 1}), row("heavy_quiet", {"r": 2})
exh = [{"observation_id": "o", "natural_key": h1["natural_key"],
        "values_sha256": h1["values_sha256"], "corrects_observation_id": None}]
check("a live re-send on an 'ignore' type keeps the first sighting",
      obs.plan([h2], exh), [])
check("heavy_quiet carries no direction even if handed one", h1["direction"], None)
_radar = obs.build("radar_event", symbol="X", session_date="2026-10-16",
                   direction="up", computed_values={"direction": "up"},
                   source_file="t")
check("radar 'up' is NOT a direction claim: the column stays NULL",
      _radar["direction"], None, "radar describes a move; it does not call one")
check("  the move it describes is kept, in computed_values",
      _radar["computed_values"]["direction"], "up")

import os                                                      # noqa: E402
import tempfile                                                # noqa: E402
import requests                                                # noqa: E402
from engine import ledger_alerts                               # noqa: E402
# Every refusal below sends an alert. Capture it here: a suite that reaches the
# real Telegram is an alarm crying wolf.
_sent = []
ledger_alerts.send = lambda m: (_sent.append(m) or (True, 1, 1))
ledger_alerts.STATE_DIR = Path(tempfile.mkdtemp())
ledger_alerts.HOLD_MINUTES = 0
_key = os.environ.pop("SUPABASE_SERVICE_KEY", None)
check("no service key: record() refuses", obs.record([r1]), False)
os.environ["SUPABASE_SERVICE_KEY"] = "test-key-not-real"
_get = requests.get


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


def _boom(*a, **k):
    raise requests.ConnectionError("no network in the suite")


requests.get = _boom
check("the ledger unreachable: record() refuses, does not raise", obs.record([r1]), False)
_post = requests.post
requests.get = lambda *a, **k: _Resp(200, [])
requests.post = lambda *a, **k: _Resp(201, None)
check("a 201 with nothing readable back: record() refuses", obs.record([r1]), False,
      "the read-back, not the status, is the evidence")
requests.get = lambda *a, **k: _Resp(404, {"message": "relation does not exist"})
check("the table missing (migration not applied): record() refuses",
      obs.record([r1]), False, "so no publisher runs ahead of the migration")
check("each of those four refusals sent exactly one alert", len(_sent), 4,
      "fail-closed is never silent")
check("  and every alert names the publisher and the symbol",
      all(f"LEDGER REFUSED — {r1['source_file']} did NOT publish" in m
          and f"\nSymbols (1): {r1['symbol']}\n" in m for m in _sent), True)
requests.get, requests.post = _get, _post
os.environ.pop("SUPABASE_SERVICE_KEY", None)
if _key is not None:
    os.environ["SUPABASE_SERVICE_KEY"] = _key


# ══════════════════════════════════════════════════════════════════
head("5. THE MIGRATION AND THE CODE SPEAK THE SAME VOCABULARY")
# ══════════════════════════════════════════════════════════════════
sql = (ROOT / "migrations" / "20261103181336_observation_ledger.sql").read_text()
sql_code = re.sub(r"--[^\n]*", "", sql)
# LATER MIGRATIONS REDEFINE THE VOCABULARY (20261110_observation_types_atlas.sql
# adds ATLAS's two types). The check that matters is against what the database
# now enforces, so the NEWEST definition of each wins: the type CHECK and the
# never-score function are read from the last migration that states them.
_later = sorted(p for p in (ROOT / "migrations").glob("2026*.sql")
                if p.name > "20261103181336_observation_ledger.sql")
_newest_type_check = _newest_scorable = None
for _p in _later:
    _c = re.sub(r"--[^\n]*", "", _p.read_text())
    _m1 = re.search(r"observations_observation_type_check\s+CHECK \(observation_type IN \(([^)]*)\)\)", _c, re.S)
    if _m1:
        _newest_type_check = sorted(re.findall(r"'([^']+)'", _m1.group(1)))
    if re.search(r"FUNCTION public\.observation_scores_scorable\(\)", _c):
        _newest_scorable = _c


def check_list(column):
    m = re.search(column + r"\s+text[^,]*?CHECK \(" + column + r" IN \(([^)]*)\)\)",
                  sql_code, re.S)
    return sorted(re.findall(r"'([^']+)'", m.group(1))) if m else None


check("observation_type CHECK == observations.TYPES",
      _newest_type_check or check_list("observation_type"), sorted(obs.TYPES))
check("skip_reason CHECK == score_observations.SKIP_ORDER",
      check_list("skip_reason"), sorted(so.SKIP_ORDER))
for tbl in ("observations", "observation_scores", "observation_scorer_runs"):
    check(f"{tbl}: row trigger refuses UPDATE OR DELETE",
          bool(re.search(r"BEFORE UPDATE OR DELETE ON public\." + tbl +
                         r"\s+FOR EACH ROW EXECUTE FUNCTION "
                         r"public\.observation_ledger_append_only", sql_code)), True)
    check(f"{tbl}: statement trigger refuses TRUNCATE",
          bool(re.search(r"BEFORE TRUNCATE ON public\." + tbl +
                         r"\s+FOR EACH STATEMENT EXECUTE FUNCTION "
                         r"public\.observation_ledger_append_only", sql_code)), True)
    check(f"{tbl}: service_role loses UPDATE, DELETE, TRUNCATE",
          bool(re.search(r"REVOKE UPDATE, DELETE, TRUNCATE ON public\." + tbl +
                         r"\s+FROM service_role", sql_code)), True)
_m = re.search(r"FUNCTION public\.observation_scores_scorable\(\).*?observation_type IN \(([^)]*)\)",
               _newest_scorable or sql_code, re.S)
check("the database's never-score list == observations.UNSCORED_TYPES",
      sorted(re.findall(r"'([^']+)'", _m.group(1))) if _m else None,
      sorted(obs.UNSCORED_TYPES))
check("  and that function is wired as a BEFORE INSERT trigger on scores",
      bool(re.search(r"BEFORE INSERT ON public\.observation_scores\s+FOR EACH ROW "
                     r"EXECUTE FUNCTION public\.observation_scores_scorable", sql_code)), True)
check("there is no `scored` column to flip (it is derived)",
      bool(re.search(r"^\s*scored\s+bool", sql_code, re.M)), False)

print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print(f"PASSED — T+3 over {len(FRIDAYS)} Fridays and {len(HOLIDAY_WINDOWS)} holiday "
      f"windows, {len(PUBLISHERS)} publishers record first")
