"""
THE STOCK LOGIC — Trading Calendar
===================================
THE HOLIDAY LIST IS A FETCHED FACT, NOT A MAINTAINED OPINION.

WHAT THE MAINTAINED VERSION COST. Until 2026-10-22 this file carried a
hand-written set. Checked against NSE's own holiday-master API it was wrong in
BOTH directions for 2026: fourteen real holidays missing and eight dates listed
that NSE does not observe. The consequences were not theoretical.

  2026-10-20 was DUSSEHRA and we called it a session. ATLAS traded a closed
             market, made 342 entry decisions against a pre-open snapshot, and
             01b requested a bhavcopy that does not exist -- NSE's legacy
             endpoint served 19-Oct content under the 20-Oct name, which put a
             duplicate 20 Oct into all 744 parquets and built a signals batch
             on it.

  2026-10-21 and 2026-10-22 were REAL SESSIONS and we called them holidays.
             ATLAS stood down for both. NSE serves genuine bhavcopies for them
             (3,708 and 3,649 rows); we simply never asked.

The guard mechanism was sound. The data behind it was wrong, and a list that
only a human can check is a list nobody checks. So it is fetched, cached with
its fetch time, and reconciled.

HOW IT RESOLVES, in order:

  1. the cache      data/reference/nse_holidays.json, written by a successful
                    fetch and carrying fetched_at. This is the normal path.
  2. a stale cache  used, with its age named in a Telegram alert. A holiday is
                    published months ahead, so yesterday's answer is still a
                    good answer -- but silence about its age is not.
  3. nothing        is_trading_day returns False for every date and
                    require_trading_day refuses. NEVER a hardcoded list, and
                    never open into trading: a calendar we cannot read is a day
                    we must not trade.

THE API COVERS 2019 THROUGH 2026, measured 2026-10-22 by asking for each year
in turn (2027 returns an empty set -- not yet published). Our own data begins
2023-01-02, so every date the pipeline touches is inside the authoritative
range and nothing needs to be asserted from memory.

UNVERIFIED_HISTORICAL exists for dates BEFORE the API's floor and is empty,
which is the honest state rather than a convenience. If the dataset is ever
extended before 2019 the holidays for those years go there, explicitly labelled
as unverified, and tests/test_calendar_source.py will still refuse any hardcoded
date that falls inside the fetched range.
"""

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from collections.abc import Set
from typing import List, Optional

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# WHERE THE CACHE LIVES, and why it is not in git. The box hard-resets from
# origin/main every five minutes, so a tracked cache would be reverted to
# whatever was committed and a box-side refresh would survive at most five
# minutes. Untracked files survive `git reset --hard`, which is exactly the
# property reference data needs.
CACHE_PATH = Path(os.environ.get(
    "NSE_HOLIDAY_CACHE",
    str(Path(__file__).resolve().parents[1] / "data" / "reference"
        / "nse_holidays.json")))

# A holiday is gazetted months ahead, so an old cache is usually still correct.
# Seven days is long enough to ride out an API outage and short enough that a
# new year's list cannot be missed.
MAX_CACHE_AGE_DAYS = 7

NSE_API = "https://www.nseindia.com/api/holiday-master"
NSE_HOME = "https://www.nseindia.com/"
# CM is the capital-market (equities) segment. The payload carries twelve
# segments and they differ -- CD and IRD list 24 days where CM lists 20 -- so
# naming the one we trade is part of the answer, not a detail.
SEGMENT = "CM"

# Measured, not assumed: each year was requested in turn on 2026-10-22.
API_FIRST_YEAR = 2019
API_LAST_VERIFIED_YEAR = 2026

# ── DATES THE API CANNOT SERVE ────────────────────────────────────
# Empty by design. The API's floor is 2019 and the dataset starts 2023-01-02,
# so there is no date the pipeline touches that has to be asserted from memory.
# Anything added here is UNVERIFIED and is used only below API_FIRST_YEAR;
# tests/test_calendar_source.py fails if an entry lands inside the fetched
# range, which is the mistake this whole change exists to make impossible.
UNVERIFIED_HISTORICAL: dict = {}


def _now_ist() -> datetime:
    return datetime.now(IST)


def _fetch_year(session, year: int) -> dict:
    """{iso_date: description} for one year from NSE. Raises on failure."""
    r = session.get(NSE_API, params={"type": "trading", "year": str(year)},
                    timeout=30,
                    headers={"Referer": "https://www.nseindia.com/resources/"
                                        "exchange-communication-holidays",
                             "Accept": "application/json"})
    r.raise_for_status()
    rows = r.json().get(SEGMENT) or []
    out = {}
    for row in rows:
        try:
            d = datetime.strptime(row["tradingDate"], "%d-%b-%Y").date()
        except (KeyError, ValueError):
            continue
        if d.year != year:
            # The API has answered with a different year before. A row we did
            # not ask for is not evidence about the year we did.
            continue
        out[d.isoformat()] = str(row.get("description", "")).strip()
    if not out:
        raise ValueError(f"NSE returned no {SEGMENT} holidays for {year}")
    return out


def refresh_from_nse(write: bool = True) -> dict:
    """Fetch every covered year and write the cache. Raises on failure.

    WEEKEND HOLIDAYS ARE KEPT. NSE lists them (2026-02-15 is a Sunday) and they
    change nothing, because is_trading_day rejects weekends first. They are
    facts from the source and dropping them would mean editing what was
    fetched, which is the habit being removed.

    ONE KNOWN IMPRECISION IN THE SOURCE, recorded rather than silently handled.
    The CM list mixes closures with special SESSIONS: across 2019-2026 exactly
    one entry is of the second kind, "2024-03-02 Special Live Trading", a
    Saturday on which NSE did trade. Reading it as a closure costs nothing
    because the weekend check answers first -- but it means a special Saturday
    session is invisible to this calendar, and a backtest that wanted one would
    not get it from here. "2024-01-22 Special Holiday" is a genuine closure
    (the Ram Mandir consecration) despite the similar wording, so the two
    cannot be separated by keyword.
    """
    import requests
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9"})
    try:
        # A cookie is needed before the API will answer. The homepage itself
        # may return 403 and still set it, so its status is not checked.
        s.get(NSE_HOME, timeout=20)
    except Exception as e:
        log.warning(f"NSE homepage priming failed ({e}) — trying the API anyway")

    last = _now_ist().year + 1
    years, failed = {}, []
    for y in range(API_FIRST_YEAR, last + 1):
        try:
            got = _fetch_year(s, y)
            years[str(y)] = got
        except Exception as e:
            # A year with no published list yet (next year, early) is not a
            # failure. A year inside the verified range is.
            (failed if y <= API_LAST_VERIFIED_YEAR else []).append((y, str(e)[:60]))
    if failed:
        raise RuntimeError(f"holiday fetch incomplete: {failed}")
    if not years:
        raise RuntimeError("holiday fetch returned nothing")

    payload = {
        "source": NSE_API, "segment": SEGMENT,
        "fetched_at": _now_ist().isoformat(timespec="seconds"),
        "years": sorted(int(y) for y in years),
        "holidays": years,
    }
    if write:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        tmp.replace(CACHE_PATH)          # atomic: a torn file reads as absent
        log.info(f"holiday cache written: {CACHE_PATH} — "
                 f"{sum(len(v) for v in years.values())} dates across "
                 f"{len(years)} year(s)")
    return payload


def _alert(msg: str) -> None:
    """Tell the operator. Never raises, never blocks the caller."""
    log.error(msg)
    try:
        from atlas.reporting.telegram import send
        send(f"📅 <b>NSE CALENDAR</b>\n{msg}")
    except Exception as e:
        log.error(f"could not send the calendar alert: {e}")


_cache = None
_cache_warned = False


def _load() -> dict:
    """The cached calendar, or {} when there is none. Never raises."""
    global _cache, _cache_warned
    if _cache is not None:
        return _cache
    try:
        payload = json.loads(CACHE_PATH.read_text())
        hol = payload.get("holidays") or {}
        if not hol:
            raise ValueError("cache carries no holidays")
    except Exception as e:
        if not _cache_warned:
            _cache_warned = True
            _alert(f"NO USABLE HOLIDAY CACHE at {CACHE_PATH} ({e}). Every date "
                   f"now reads as NOT a trading day, so nothing will trade or "
                   f"publish until `python -m engine.trading_calendar "
                   f"--refresh` succeeds. This fails CLOSED on purpose.")
        _cache = {}
        return _cache

    # AGE IS REPORTED, NOT INFERRED. The cache carries the time it was
    # fetched; nothing here guesses from an mtime, which a copy would change.
    try:
        age = (_now_ist() - datetime.fromisoformat(payload["fetched_at"])).days
    except Exception:
        age = None
    if not _cache_warned and (age is None or age > MAX_CACHE_AGE_DAYS):
        _cache_warned = True
        _alert(f"HOLIDAY CACHE IS STALE — fetched "
               f"{payload.get('fetched_at', 'at an unknown time')}"
               + (f", {age} days ago" if age is not None else "")
               + f" (limit {MAX_CACHE_AGE_DAYS}d). Still being used, because a "
                 f"gazetted holiday does not move; refresh with `python -m "
                 f"engine.trading_calendar --refresh`.")
    _cache = payload
    return _cache


def cache_state() -> dict:
    """(read-only) what the calendar is resolving from, for reports and tests."""
    p = _load()
    hol = p.get("holidays") or {}
    try:
        age = (_now_ist() - datetime.fromisoformat(p["fetched_at"])).days
    except Exception:
        age = None
    return {"path": str(CACHE_PATH), "ok": bool(hol),
            "fetched_at": p.get("fetched_at"), "age_days": age,
            "years": sorted(int(y) for y in hol),
            "dates": sum(len(v) for v in hol.values()),
            "stale": (age is None or age > MAX_CACHE_AGE_DAYS) if hol else True,
            "source": p.get("source"), "segment": p.get("segment")}


def _holiday_map() -> dict:
    """{iso_date: description} across every cached year, plus the unverified
    pre-API set. Empty when the calendar cannot be read at all."""
    p = _load()
    hol = p.get("holidays") or {}
    if not hol:
        return {}
    out = {}
    for y, dates in hol.items():
        out.update(dates)
    # Below the API floor only. Inside it, fetched data is the only authority.
    for iso, why in UNVERIFIED_HISTORICAL.items():
        if int(iso[:4]) < API_FIRST_YEAR:
            out.setdefault(iso, f"UNVERIFIED: {why}")
    return out


class _Holidays(Set):
    """A real Set view of the cached holidays, so every existing caller keeps
    working while the data behind it is fetched.

    collections.abc.Set rather than a bare __contains__: callers already use
    `in`, a comprehension, and `<=` against a literal set, and inheriting the
    mixin gives all of those plus &, |, - for free. The first version
    implemented only the three dunders and tests/test_feed_freshness.py failed
    on `{...} <= NSE_HOLIDAYS` with a TypeError -- a narrower object than the
    thing it replaced.
    """

    def __contains__(self, iso) -> bool:
        return str(iso) in _holiday_map()

    def __iter__(self):
        return iter(_holiday_map())

    def __len__(self) -> int:
        return len(_holiday_map())

    def __repr__(self) -> str:
        st = cache_state()
        return (f"<NSE_HOLIDAYS {len(self)} dates, years {st['years']}, "
                f"fetched {st['fetched_at']}>")


NSE_HOLIDAYS = _Holidays()

# Kept for callers that print it. It is now the FETCHED range rather than a
# promise about a hand-written list.
CALENDAR_COVERAGE = (API_FIRST_YEAR, API_LAST_VERIFIED_YEAR)


def calendar_is_readable() -> bool:
    """False when nothing can be resolved, which must block trading."""
    return bool(_holiday_map())


def reconcile(other: set, years=None) -> dict:
    """Compare a date set against the fetched calendar. {missing, extra, ...}

    `missing` are holidays the fetched calendar has and `other` does not --
    days we would have traded. `extra` are dates `other` calls holidays and NSE
    does not -- real sessions we would have sat out. Both directions matter and
    the 2026 reconciliation found fourteen of the first and eight of the second.
    """
    fetched = _holiday_map()
    yrs = set(years) if years else {int(d[:4]) for d in fetched}
    theirs = {str(d) for d in other if int(str(d)[:4]) in yrs}
    ours = {d for d in fetched if int(d[:4]) in yrs}
    return {"years": sorted(yrs),
            "fetched": len(ours), "compared": len(theirs),
            "missing": sorted(ours - theirs),
            "extra": sorted(theirs - ours),
            "agree": sorted(ours & theirs),
            "descriptions": {d: fetched[d] for d in sorted(ours - theirs)}}


def _warn_if_uncovered(d: date):
    """Say it once per year, loudly. An uncovered year is refused, not guessed."""
    if d.year in _warned_years:
        return
    _warned_years.add(d.year)
    st = cache_state()
    _alert(f"HOLIDAY CALENDAR DOES NOT COVER {d.year}. The cache holds "
           f"{st['years']} (fetched {st['fetched_at']}). Every {d.year} date "
           f"now reads as NOT a trading day. If NSE has published that year, "
           f"run `python -m engine.trading_calendar --refresh`; if it has "
           f"not, this is the correct answer.")


def is_trading_day(d: date) -> bool:
    """Is d an NSE trading day? FAILS CLOSED when the calendar is unreadable.

    A weekend needs no calendar, so it is answered first and for free.

    EVERYTHING ELSE NEEDS THE FETCHED LIST, and when there is none this returns
    False for every date. That stops the pipeline, which is the point: on
    2026-10-20 a wrong calendar let ATLAS trade a closed market, and "we cannot
    tell" must behave like "do not trade" rather than like "carry on". The
    alert in _load() names the cause so the stop is never mysterious.

    A YEAR THE CACHE DOES NOT COVER IS ALSO NOT A TRADING DAY. Treating an
    uncovered year as all-sessions is how a missing holiday becomes a traded
    holiday -- it is the exact failure above, one level up.
    """
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    hol = _holiday_map()
    if not hol:
        return False              # unreadable calendar: refuse, do not assume
    if not _year_is_covered(d.year):
        _warn_if_uncovered(d)
        return False
    return d.isoformat() not in hol


_warned_years = set()


def _year_is_covered(year: int) -> bool:
    p = _load()
    return str(year) in (p.get("holidays") or {}) or (
        year < API_FIRST_YEAR and any(
            int(k[:4]) == year for k in UNVERIFIED_HISTORICAL))


# ── THE ONE GATE EVERY SESSION-SCOPED WRITER CALLS ────────────────
#
# is_trading_day() above is a pure predicate. This is the GATE: it fails
# closed, it logs in one voice, and it is the single thing a publisher has to
# call. The difference matters because the predicate can be forgotten and a
# forgotten predicate is silent.
#
# WHAT IT COST TO NOT HAVE THIS. rbe_engine.market_is_open() checked
# `weekday() >= 5` and the clock and never the holiday list -- a weekend guard
# wearing a trading-day guard's name. On 2026-10-21 and 2026-10-22, both NSE
# holidays, it therefore ran a full session: 5,920,860 and 6,007,262 ticks,
# 89 signals fired against a closed market, and rows written to
# breadth_snapshots and heavy_quiet for two days that never opened. market_flash
# then read them and published "breadth deteriorated sharply with 13.1%
# advancing; 644 declining issues" about a market that did not trade.
#
# Four other processes already had the check and had it four different ways --
# `from engine.trading_calendar import`, `from trading_calendar import` behind a
# sys.path.insert, inside a try/except that swallowed the ImportError. A guard
# that each process has to remember, and spell differently, is the bug. This is
# the spelling.
def require_trading_day(day: Optional[date] = None, what: str = "",
                        logger=None) -> bool:
    """True if `day` (default: today, IST) is an NSE trading day.

    FAILS CLOSED. An unreadable calendar returns False -- a publisher that
    cannot tell what day it is must not publish a claim about the session. The
    opposite default is how 2026-10-21 got published.

    `what` names the caller in the log line, so a skipped run says which
    publisher skipped it rather than appearing as an unexplained silence.
    """
    lg = logger or log
    label = f"{what}: " if what else ""
    try:
        d = day or _today_ist()
        if is_trading_day(d):
            return True
        lg.info(f"{label}{d} is not an NSE trading day — nothing to publish")
        return False
    except Exception as e:
        lg.error(f"{label}trading calendar unreadable ({e}) — treating the day "
                 f"as CLOSED rather than publishing a session that may not "
                 f"exist")
        return False


def _today_ist() -> date:
    """Today in IST, not in the box's timezone.

    The box runs UTC. date.today() there rolls over at 05:30 IST, which is
    before the 09:15 open and so happens to be harmless for an entry gate -- but
    it is wrong for any publisher that stamps session_date, and relying on
    "harmless for now" is how the next defect gets written.
    """
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


def next_trading_day(from_date: Optional[date] = None) -> date:
    """
    Return the next trading day after from_date.
    If from_date is None, uses today.
    """
    d = (from_date or date.today()) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
        if (d - (from_date or date.today())).days > 14:
            raise ValueError(f"Could not find next trading day within 14 days of {from_date}")
    return d


def prev_trading_day(from_date: Optional[date] = None) -> date:
    """Return the most recent trading day before from_date."""
    d = (from_date or date.today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
        if ((from_date or date.today()) - d).days > 14:
            raise ValueError(f"Could not find prev trading day within 14 days of {from_date}")
    return d


def next_n_trading_days(from_date: date, n: int = 5) -> List[date]:
    """Return the next n trading days after from_date."""
    days = []
    d = from_date + timedelta(days=1)
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
        if (d - from_date).days > 60:
            break
    return days


def trading_days_between(start: date, end: date) -> int:
    """
    Count trading days between start and end (exclusive of start, inclusive of end).
    """
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def is_friday_signal(signal_date: date) -> bool:
    """
    Return True if signal_date is a Friday.
    Friday signals carry gap risk over the weekend.
    BTST trades from Friday close require extra caution.
    """
    return signal_date.weekday() == 4


def friday_gap_risk_flag(signal_date: date) -> dict:
    """
    Returns a risk flag dict for Friday signals.
    Gap risk: 2 calendar days of news before Monday open.
    """
    if not is_friday_signal(signal_date):
        return {"has_gap_risk": False, "reason": None}

    return {
        "has_gap_risk":   True,
        "reason":         "Friday signal — weekend gap risk. 2 days of news before Monday open.",
        "recommendation": "Reduce position size by 50% or skip. Set wider SL for gap protection.",
        "next_open":      next_trading_day(signal_date).isoformat(),
    }


def days_until_next_trading_day(from_date: Optional[date] = None) -> int:
    """Return calendar days until the next trading day."""
    d = from_date or date.today()
    nxt = next_trading_day(d)
    return (nxt - d).days


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if "--refresh" in sys.argv:
        pay = refresh_from_nse()
        print(f"fetched {sum(len(v) for v in pay['holidays'].values())} dates "
              f"across years {pay['years']} at {pay['fetched_at']}")
        print(f"cache: {CACHE_PATH}")
        sys.exit(0)

    if "--state" in sys.argv:
        import json as _j
        print(_j.dumps(cache_state(), indent=1))
        sys.exit(0)

    if "--reconcile" in sys.argv:
        # Compare a comma-separated set, or the set a previous build shipped,
        # against the fetched calendar. Prints BOTH directions.
        arg = [a for a in sys.argv if a.startswith("--against=")]
        other = set(arg[0].split("=", 1)[1].split(",")) if arg else set()
        yrs = [int(a.split("=", 1)[1]) for a in sys.argv if a.startswith("--year=")]
        r = reconcile(other, years=yrs or None)
        print(f"years compared     : {r['years']}")
        print(f"fetched holidays   : {r['fetched']}")
        print(f"compared set size  : {r['compared']}")
        print(f"agree              : {len(r['agree'])}")
        print(f"MISSING from the compared set ({len(r['missing'])}) "
              f"— days we would have TRADED a holiday:")
        for d in r["missing"]:
            print(f"    + {d}  {r['descriptions'].get(d, '')}")
        print(f"EXTRA in the compared set ({len(r['extra'])}) "
              f"— real sessions we would have SAT OUT:")
        for d in r["extra"]:
            print(f"    - {d}")
        sys.exit(0)

    today = date.today()
    print(f"Today:               {today} ({'trading day' if is_trading_day(today) else 'NOT a trading day'})")
    print(f"Next trading day:    {next_trading_day(today)}")
    print(f"Prev trading day:    {prev_trading_day(today)}")
    print(f"Next 5 trading days: {next_n_trading_days(today, 5)}")
    print(f"Trading days this week so far: {trading_days_between(today - timedelta(days=today.weekday()), today)}")
    print(f"Friday gap risk:     {friday_gap_risk_flag(today)}")
    print(f"Days to next open:   {days_until_next_trading_day(today)}")
