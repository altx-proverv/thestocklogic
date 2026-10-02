"""
MERIDIAN — the F&O underlying list, resolved from the exchange every night.

NEVER HARDCODED. NSE revises the F&O-eligible list periodically -- names are
added and removed -- and a hardcoded list would record a shrinking universe
without saying so, which is the quiet version of the failure this whole recorder
is designed against.

TWO SOURCES, AND THE DIFF IS REPORTED RATHER THAN RECONCILED
------------------------------------------------------------
  Upstox instrument master   AUTHORITATIVE, because it is the list of what we can
                             actually fetch. Derived from the exchange, refreshed
                             daily around 06:00, with expired contracts and
                             delisted names dropped from the next BOD file.
  NSE's own F&O list         A completeness CHECK. If NSE lists a symbol Upstox
                             has no instrument key for, we cannot record it and
                             should know; if Upstox carries one NSE has dropped,
                             the same.

Neither source silently wins. The diff goes into meridian_recorder_runs.list_diff
so an exchange revision shows up as a line in the run record on the night it
happens, instead of as a gap noticed months later.

The NSE fetch is best-effort: it is a cross-check, so its failure degrades the
report rather than the recording. The Upstox fetch is not -- without it there is
no universe and the run fails.
"""

import io
import gzip
import json
import logging
from datetime import datetime, timezone, timedelta

import requests

from meridian.config import UPSTOX_INSTRUMENTS_NSE, INDEX_SYMBOLS

IST = timezone(timedelta(hours=5, minutes=30))

log = logging.getLogger("MERIDIAN-UNIV")

NSE_FO_LIST_URLS = (
    # Lot sizes double as the canonical machine-readable F&O list.
    "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
    "https://archives.nseindia.com/content/fo/fo_mktlots.csv",
)
NSE_HEADERS = {
    # NSE rejects an unadorned client. This mirrors what engine/tier1_fetch.py
    # already has to do for the announcements endpoint.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Accept": "text/csv,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


class UniverseUnavailable(Exception):
    """The Upstox instrument master could not be read. No universe, no run."""


def _download_master() -> list:
    try:
        r = requests.get(UPSTOX_INSTRUMENTS_NSE, timeout=120)
    except Exception as e:
        raise UniverseUnavailable(f"instrument master unreachable: "
                                  f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise UniverseUnavailable(f"instrument master HTTP {r.status_code}")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as fh:
            rows = json.load(fh)
    except Exception as e:
        # FAILS LOUD. A master we cannot parse is not an empty universe -- it is a
        # broken input, and treating it as "no symbols today" would write a run
        # that looks like a quiet market.
        raise UniverseUnavailable(f"instrument master unparseable: "
                                  f"{type(e).__name__}: {e}") from e
    if not isinstance(rows, list) or not rows:
        raise UniverseUnavailable("instrument master parsed to nothing")
    return rows


def parse_master(rows: list, today=None) -> tuple:
    """(stocks, index_keys, expiries) from the instrument master.

    stocks      {symbol: underlying_key} for every F&O stock option underlying
    index_keys  {symbol: underlying_key} for the four indices, READ from the
                master rather than hardcoded -- "NSE_INDEX|NIFTY MID SELECT" is
                not a string worth retyping from memory
    expiries    {symbol: [date, ...]} ascending, FUTURE EXPIRIES ONLY

    WHY EXPIRIES ARE RESOLVED HERE AND NOT LEFT TO A KEYWORD. The recorder used
    Upstox's relative keywords -- current_week for NIFTY, current_month for
    everything else. On Friday 2026-10-02 NIFTY returned "chain carried no
    strikes" while all 216 other underlyings succeeded, because current_week
    resolves to the expiry inside the current CALENDAR week: Tuesday
    2026-09-29, which had already expired. Upstox drops expired contracts from
    the next BOD master, so the contract the keyword named no longer had any
    strikes to return.

    That is not an edge case. NIFTY weeklies expire Tuesday, so the keyword
    would have failed every Wednesday, Thursday and Friday -- three sessions in
    five, on the single most important underlying in the recorder, silently, in a
    series that cannot be back-filled.

    The master already carries every contract's expiry and is already downloaded
    for the universe, so the nearest live expiry is a fact we can read instead of
    a keyword semantic we have to guess. It also removes two other guesses: the
    weekly/monthly split per underlying disappears, and so does any day-of-week
    arithmetic -- NSE shifts expiries off Tuesday around holidays, and this
    master currently holds a Monday weekly (2026-10-19) and a Monday monthly
    (2026-11-23) that no weekday rule would have found.

    STRICTLY AFTER TODAY. An option expiring today has almost no time value left,
    so its IV is degenerate and would enter the series as a spike that means
    nothing about volatility. Taking the next expiry instead produces a visible
    tenor jump, which days_to_expiry records, rather than an invisible bad number.
    """
    today = today or datetime.now(IST).date()
    stocks, index_keys = {}, {}
    exp = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("segment") != "NSE_FO":
            continue
        if row.get("instrument_type") not in ("CE", "PE"):
            continue
        sym = row.get("underlying_symbol")
        key = row.get("underlying_key")
        if not sym or not key:
            continue
        sym = str(sym).strip().upper()
        key = str(key)

        if sym in INDEX_SYMBOLS:
            index_keys[sym] = key
        elif key.startswith("NSE_INDEX|"):
            # An index we do not record. Not a stock; skipped rather than
            # silently added to the stock universe.
            continue
        else:
            stocks[sym] = key

        raw = row.get("expiry")
        if raw is None:
            continue
        try:
            # milliseconds since epoch, read in IST because an expiry is a
            # trading date and UTC would roll it back a day for an evening value
            d = datetime.fromtimestamp(int(raw) / 1000, IST).date()
        except Exception:
            continue
        if d > today:
            exp.setdefault(sym, set()).add(d)

    if len(stocks) < 50:
        # The list has run ~200 for years. Fifty would mean a format change or a
        # truncated download, and recording a tenth of the universe silently is
        # exactly the outcome this guard exists for.
        raise UniverseUnavailable(
            f"only {len(stocks)} F&O underlyings parsed from the instrument "
            f"master -- expected ~200. Refusing to record a truncated universe.")

    expiries = {k: sorted(v) for k, v in exp.items()}
    log.info(f"instrument master: {len(stocks)} F&O stock underlyings, "
             f"{len(index_keys)} indices, expiries resolved for {len(expiries)}")
    return stocks, index_keys, expiries


def fetch_upstox_fo_underlyings() -> dict:
    """{symbol: instrument_key} for the F&O stock underlyings. Kept as the
    narrow entry point the tests and any future caller use."""
    stocks, _, _ = parse_master(_download_master())
    return stocks


def fetch_nse_fo_symbols() -> set:
    """The exchange's own F&O list, for cross-checking. Empty set on failure.

    BEST EFFORT, deliberately. This is a completeness check on a list we already
    have from an authoritative source, so its failure must degrade the REPORT and
    not the recording. Returning an empty set makes the diff say "not checked"
    rather than "NSE lists nothing", which the caller distinguishes.
    """
    for url in NSE_FO_LIST_URLS:
        try:
            r = requests.get(url, headers=NSE_HEADERS, timeout=30)
            if r.status_code != 200:
                log.warning(f"NSE F&O list {url} -> HTTP {r.status_code}")
                continue
            text = r.text
            if "," not in text or len(text) < 200:
                log.warning(f"NSE F&O list {url} -> not CSV ({len(text)} bytes)")
                continue
            syms = set()
            for line in text.splitlines()[1:]:
                parts = [p.strip() for p in line.split(",")]
                if len(parts) < 2:
                    continue
                s = parts[1].upper()
                if s and s.isalnum() or ("&" in s or "-" in s):
                    syms.add(s)
            # Drop index names; we only cross-check stocks.
            syms -= {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                     "NIFTYNXT50", "SYMBOL", ""}
            if len(syms) >= 50:
                log.info(f"NSE F&O list: {len(syms)} symbols")
                return syms
            log.warning(f"NSE F&O list {url} parsed to {len(syms)} symbols — "
                        f"treating as unavailable")
        except Exception as e:
            log.warning(f"NSE F&O list {url} failed: {type(e).__name__}: {e}")
    log.warning("NSE F&O cross-check unavailable — recording from the Upstox "
                "master alone and saying so in the run record")
    return set()


def resolve_universe(today=None) -> tuple:
    """(underlyings, diff) -- what to record tonight, and where the sources differ.

    Each underlying carries an EXPLICIT expiry date, resolved from the master:
        {"symbol", "key", "kind", "expiry", "expiry_source"}

    Indices first, because if a run is cut short by an outage the index series
    are the ones that matter most. An underlying with no future expiry in the
    master is dropped WITH A REASON rather than fetched and failed -- that is a
    contract gap, not a network problem, and the two should not look alike in the
    failure list.
    """
    rows = _download_master()
    stocks, index_keys, expiries = parse_master(rows, today=today)
    nse = fetch_nse_fo_symbols()                    # set() on failure

    diff = {"checked": bool(nse),
            "upstox_count": len(stocks),
            "nse_count": len(nse)}
    if nse:
        diff["in_nse_not_upstox"] = sorted(nse - set(stocks))[:60]
        diff["in_upstox_not_nse"] = sorted(set(stocks) - nse)[:60]
        n_missing = len(nse - set(stocks))
        if n_missing:
            # Named, not resolved. A symbol the exchange lists and Upstox cannot
            # serve is a gap in the history we are about to start accumulating,
            # and the operator should see it on the night it appears.
            log.warning(f"{n_missing} symbol(s) in the NSE F&O list have no "
                        f"Upstox instrument key — they will not be recorded: "
                        f"{sorted(nse - set(stocks))[:12]}")

    missing_expiry = []
    underlyings = []
    for sym in INDEX_SYMBOLS:
        key = index_keys.get(sym)
        if not key:
            missing_expiry.append({"symbol": sym, "reason": "not in master"})
            continue
        exps = expiries.get(sym) or []
        if not exps:
            missing_expiry.append({"symbol": sym, "reason": "no future expiry"})
            continue
        underlyings.append({"symbol": sym, "key": key, "kind": "index",
                            "expiry": exps[0].isoformat(),
                            "expiry_source": "master:nearest_future"})
    for sym, key in sorted(stocks.items()):
        exps = expiries.get(sym) or []
        if not exps:
            missing_expiry.append({"symbol": sym, "reason": "no future expiry"})
            continue
        underlyings.append({"symbol": sym, "key": key, "kind": "stock",
                            "expiry": exps[0].isoformat(),
                            "expiry_source": "master:nearest_future"})

    if missing_expiry:
        diff["no_future_expiry"] = missing_expiry[:40]
        log.warning(f"{len(missing_expiry)} underlying(s) have no future expiry "
                    f"in the master and are not recorded: "
                    f"{[m['symbol'] for m in missing_expiry[:10]]}")
    return underlyings, diff
