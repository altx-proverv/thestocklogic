#!/usr/bin/env python3
"""
THE HOLIDAY CALENDAR MUST BE FETCHED, NEVER REMEMBERED
=======================================================
The guard mechanism was sound on 2026-10-22. The data behind it was wrong, and
that is the harder failure because nothing throws:

  2026-10-20 was DUSSEHRA and the hand-written list called it a session. ATLAS
             traded a closed market and made 342 entry decisions against a
             pre-open snapshot. 01b then requested a bhavcopy that does not
             exist, NSE's legacy endpoint served the previous session under its
             name, and a duplicate 20 Oct landed in all 744 parquets.

  2026-10-21 and 2026-10-22 were REAL SESSIONS the list called holidays. ATLAS
             stood down for both. NSE serves genuine bhavcopies for them.

Reconciled against NSE's holiday-master API across the four years we hold data
for, the shipped list was wrong on 52 dates: 36 real holidays missing and 16
dates that are not holidays at all.

So this file asserts the SOURCE, not the contents:

  no literal        no ISO date may be hardcoded in the trading-day path
                    inside the range the API serves. A list only a human can
                    check is a list nobody checks.
  one owner         no other module may carry its own holiday set. 01b used to,
                    and the two had already diverged.
  fails closed      with no cache every date reads as NOT a trading day, which
                    stops the pipeline instead of trading blind.
  both directions   reconcile() must report missing AND extra. Only reporting
                    missing would have hidden 2026-10-21 and 2026-10-22.
"""
import ast
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from tests._srcutil import executable_source                     # noqa: E402

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:24]:<26} {'ok' if ok else 'FAIL':<6} {note}")


def head(t):
    print()
    print(t)
    print("-" * 112)


import engine.trading_calendar as TC                              # noqa: E402

CAL = ROOT / "engine" / "trading_calendar.py"
ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


# ══════════════════════════════════════════════════════════════════
head("NO HARDCODED DATE IN THE TRADING-DAY PATH")
# ══════════════════════════════════════════════════════════════════
# EXECUTABLE SOURCE ONLY. This module's own docstrings name 2026-10-20 and
# 2026-10-21 in order to explain why they must not be hardcoded, and a raw
# text scan would flag the explanation as the defect -- the exact trap
# tests/_srcutil.py exists for.
src = executable_source(CAL)
literals = sorted({m.group(0) for m in ISO.finditer(src)})
inside = [d for d in literals if TC.API_FIRST_YEAR <= int(d[:4]) <= TC.API_LAST_VERIFIED_YEAR]
check("trading_calendar holds no date literal inside the fetched range",
      inside, [], f"{len(literals)} literal(s) in executable source")
check("  and the comments are not what is being judged",
      "2026-10-20" in CAL.read_text() and "2026-10-20" not in src, True,
      "named in prose, absent from code")

# ONE OWNER. 01b_download_bhavcopy used to carry a second copy and the two had
# diverged -- 01b had 2025-02-26 which the calendar lacked, the calendar had
# 2025-01-26 which 01b lacked. Neither was checkable.
owners = []
for d in ("engine", "atlas", "radar", "tools"):
    for f in sorted((ROOT / d).rglob("*.py")):
        if "legacy" in f.parts or "__pycache__" in f.parts:
            continue
        s = executable_source(f)
        # a container of ISO dates assigned to a holiday-shaped name
        for m in re.finditer(r"(\w*HOLIDAY\w*)\s*=\s*[\{\(\[]", s):
            seg = s[m.end():m.end() + 4000]
            if ISO.search(seg):
                owners.append(f"{f.relative_to(ROOT)}:{m.group(1)}")
check("no module defines a literal holiday set", sorted(set(owners)), [])

check("UNVERIFIED_HISTORICAL is empty", dict(TC.UNVERIFIED_HISTORICAL), {},
      "the API floor is below our data, so nothing is asserted from memory")
bad_hist = [k for k in TC.UNVERIFIED_HISTORICAL if int(k[:4]) >= TC.API_FIRST_YEAR]
check("  and may never hold a date the API covers", bad_hist, [])


# ══════════════════════════════════════════════════════════════════
head("THE CACHE IS THE SOURCE, AND ITS AGE IS VISIBLE")
# ══════════════════════════════════════════════════════════════════
st = TC.cache_state()
check("a cache exists and is readable", st["ok"], True, st["path"])
check("  it records when it was fetched", bool(st["fetched_at"]), True,
      str(st["fetched_at"]))
check("  it names its source", st["source"], TC.NSE_API)
check("  and the segment, because the segments differ",
      st["segment"], "CM", "CD and IRD list 24 days where CM lists 20")
check("  it is not stale", st["stale"], False,
      f"age {st['age_days']}d, limit {TC.MAX_CACHE_AGE_DAYS}d")
check("  and covers the API's whole range",
      st["years"], list(range(TC.API_FIRST_YEAR, TC.API_LAST_VERIFIED_YEAR + 1)))
check("the cache is not tracked by git",
      "data/reference/" in (ROOT / ".gitignore").read_text(), True,
      "a tracked cache would be reverted by the box's 5-minute reset")


# ══════════════════════════════════════════════════════════════════
head("THE FIX ITSELF — the three dates that cost a session each")
# ══════════════════════════════════════════════════════════════════
check("2026-10-20 is a holiday (Dussehra)",
      TC.is_trading_day(date(2026, 10, 20)), False, "we traded it")
check("2026-10-21 IS a trading day", TC.is_trading_day(date(2026, 10, 21)), True,
      "we stood down; NSE serves 3,708 rows for it")
check("2026-10-22 IS a trading day", TC.is_trading_day(date(2026, 10, 22)), True,
      "we stood down; NSE serves 3,649 rows for it")
check("  and the description travels with the date",
      "Dussehra" in TC.reconcile(set())["descriptions"].get("2026-10-20", ""),
      True)
for d, why in ((date(2026, 11, 8), "Diwali Laxmi Pujan"),
               (date(2026, 2, 15), "Mahashivratri"),
               (date(2026, 9, 14), "Ganesh Chaturthi")):
    check(f"{d} is a holiday ({why})", TC.is_trading_day(d), False,
          "missing from the shipped list")


# ══════════════════════════════════════════════════════════════════
head("RECONCILE REPORTS BOTH DIRECTIONS")
# ══════════════════════════════════════════════════════════════════
# Only reporting what we were missing would have hidden 2026-10-21 and
# 2026-10-22 entirely -- the direction that cost two whole sessions.
shipped_2026 = {"2026-01-26", "2026-03-18", "2026-04-02", "2026-04-06",
                "2026-04-14", "2026-04-17", "2026-05-01", "2026-08-15",
                "2026-10-02", "2026-10-21", "2026-10-22", "2026-11-05",
                "2026-11-20", "2026-12-25"}
r = TC.reconcile(shipped_2026, years=[2026])
check("the 2026 reconciliation finds 14 missing", len(r["missing"]), 14)
check("  and 8 extra", len(r["extra"]), 8)
check("  2026-10-20 is among the missing", "2026-10-20" in r["missing"], True)
check("  2026-10-21 and -22 are among the extra",
      {"2026-10-21", "2026-10-22"} <= set(r["extra"]), True)
check("  only 6 of the 14 shipped dates were right", len(r["agree"]), 6)


# ══════════════════════════════════════════════════════════════════
head("NO CACHE MEANS NO TRADING, NOT A GUESS")
# ══════════════════════════════════════════════════════════════════
_real_load, _real_cache, _real_warn = TC._load, TC._cache, TC._cache_warned
_alerts = []
_real_alert = TC._alert
TC._alert = lambda m: _alerts.append(m)
try:
    TC._cache = {}
    TC._cache_warned = True            # suppress the Telegram path in the test
    TC._load = lambda: {}
    check("with no calendar, a weekday is NOT a trading day",
          TC.is_trading_day(date(2026, 10, 19)), False,
          "fails closed: we cannot tell, so we must not trade")
    check("  calendar_is_readable says so plainly",
          TC.calendar_is_readable(), False)
    check("  and require_trading_day refuses",
          TC.require_trading_day(date(2026, 10, 19), "test"), False)
finally:
    TC._load, TC._cache, TC._cache_warned = _real_load, _real_cache, _real_warn
    TC._alert = _real_alert

check("back to normal after the injection",
      TC.is_trading_day(date(2026, 10, 19)), True)

# A YEAR THE CACHE DOES NOT COVER IS REFUSED, not assumed to be all-sessions.
check("an uncovered year is not a trading day",
      TC.is_trading_day(date(2027, 6, 15)), False,
      "2027 is unpublished; assuming sessions is the original defect")

# The set-like view must keep working for every existing caller.
check("NSE_HOLIDAYS still supports `in`",
      "2026-10-20" in TC.NSE_HOLIDAYS, True)
check("  and reports a real length", len(TC.NSE_HOLIDAYS) > 100, True,
      f"{len(TC.NSE_HOLIDAYS)} dates")
check("  and no longer contains 2026-10-21",
      "2026-10-21" in TC.NSE_HOLIDAYS, False)

# ══════════════════════════════════════════════════════════════════
head("THE FETCH ITSELF — stubbed, so no test depends on NSE being up")
# ══════════════════════════════════════════════════════════════════
# THESE EXIST BECAUSE THREE MUTATIONS SLIPPED THROUGH. Asserting the cache's
# contents cannot catch a change to the code that WRITES the cache: breaking
# fetched_at, reading the wrong segment, or disabling the staleness limit all
# left the already-written file intact and every assertion above still passed.
# So the fetch is driven directly against a canned payload.
import requests as _rq                                            # noqa: E402

def _payload_for(year):
    """Year-aware, because _fetch_year correctly drops rows from other years.
    A fixed payload made every year but one come back empty and the fetch
    raised -- the stub was wrong, not the code."""
    return {
        "CM": [{"tradingDate": f"26-Jan-{year}", "description": "Republic Day"},
               {"tradingDate": f"20-Oct-{year}", "description": "Dussehra"},
               {"tradingDate": "02-Jan-2099", "description": "wrong year, dropped"}],
        # A different segment with different dates, so reading the wrong one shows.
        "CD": [{"tradingDate": f"15-Aug-{year}", "description": "only in CD"}],
    }


class _FakeResp:
    status_code = 200

    def __init__(self, year): self._year = year
    def raise_for_status(self): pass
    def json(self): return _payload_for(self._year)


class _FakeSession:
    def __init__(self): self.headers = {}

    def get(self, url, **kw):
        y = (kw.get("params") or {}).get("year", "2026")
        return _FakeResp(y)


_real_session = _rq.Session
_rq.Session = _FakeSession
try:
    pay = TC.refresh_from_nse(write=False)
    years = pay["holidays"]
    check("the fetch records when it ran", bool(pay.get("fetched_at")), True,
          str(pay.get("fetched_at")))
    check("  and it parses as a timestamp",
          isinstance(datetime.fromisoformat(pay["fetched_at"]), datetime), True)
    check("the fetch records which segment it read", pay.get("segment"), "CM")
    check("  and takes CM's dates, not CD's",
          ("2026-10-20" in years.get("2026", {}),
           "2026-08-15" in years.get("2026", {})), (True, False),
          "2026-08-15 exists only in the CD segment of the stub")
    check("  the description is kept", years["2026"].get("2026-10-20"), "Dussehra")
    check("a row for a year we did not ask for is dropped",
          any("2099" in d for y in years.values() for d in y), False)
    check("the fetch names its source", pay.get("source"), TC.NSE_API)
finally:
    _rq.Session = _real_session

# A refusal must raise rather than write an empty calendar, which would read as
# "no holidays" and trade every day of the year.
class _EmptyResp(_FakeResp):
    def json(self): return {"CM": []}


class _EmptySession(_FakeSession):
    def get(self, url, **kw): return _EmptyResp()


_rq.Session = _EmptySession
try:
    raised = False
    try:
        TC.refresh_from_nse(write=False)
    except Exception:
        raised = True
    check("an empty payload RAISES instead of caching nothing", raised, True,
          "an empty calendar means every day trades")
finally:
    _rq.Session = _real_session

# DERIVED BAND, not a magic number: a gazetted holiday does not move, so the
# limit must be long enough to ride out an API outage; it must also be short
# enough that a newly published year cannot go unnoticed for a quarter.
check("the staleness limit is within a defensible band",
      1 <= TC.MAX_CACHE_AGE_DAYS <= 31, True, f"{TC.MAX_CACHE_AGE_DAYS} days")
# An old cache must report stale. Injected rather than waited for.
TC._cache = {"holidays": {"2026": {"2026-10-20": "Dussehra"}},
             "fetched_at": "2026-01-01T00:00:00+05:30"}
try:
    _old = TC.cache_state()
    check("  a cache fetched in January reports stale", _old["stale"], True,
          f"age {_old['age_days']}d")
finally:
    TC._cache = None     # reload the real file for anything after this

print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — the calendar is a fetched fact, fails closed, and reconciles "
      "both ways")
