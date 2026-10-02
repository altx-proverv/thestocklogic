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

import requests

from meridian.config import UPSTOX_INSTRUMENTS_NSE, STOCK_EXPIRY_KEYWORD

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


def fetch_upstox_fo_underlyings() -> dict:
    """{symbol: instrument_key} for every NSE_FO stock option underlying.

    Options carry underlying_key and underlying_symbol, so the set of distinct
    underlyings IS the F&O list as Upstox can serve it. Indices are excluded here
    and handled explicitly in config.INDICES, because their instrument keys and
    expiry keywords differ and are worth stating by hand rather than inferring.
    """
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

    out = {}
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
        # Index options share this segment; they are named explicitly in config.
        if str(key).startswith("NSE_INDEX|"):
            continue
        out[str(sym).strip().upper()] = str(key)

    if len(out) < 50:
        # The list has run ~200 for years. Fifty would mean a format change or a
        # truncated download, and recording a tenth of the universe silently is
        # exactly the outcome this guard exists for.
        raise UniverseUnavailable(
            f"only {len(out)} F&O underlyings parsed from the instrument master "
            f"-- expected ~200. Refusing to record a truncated universe.")
    log.info(f"Upstox instrument master: {len(out)} F&O stock underlyings")
    return out


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


def resolve_universe() -> tuple:
    """(underlyings, diff) -- what to record tonight, and where the sources differ.

    underlyings is a list of dicts ready for the recorder:
        {"symbol", "key", "kind", "expiry"}
    with the four indices first, because if a run is going to be cut short by a
    rate limit or an outage, the indices are the series that matter most.
    """
    from meridian.config import INDICES

    stocks = fetch_upstox_fo_underlyings()          # raises if unusable
    nse = fetch_nse_fo_symbols()                    # {} on failure

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

    underlyings = [{"symbol": s, "key": v["key"], "kind": "index",
                    "expiry": v["expiry"]} for s, v in INDICES.items()]
    underlyings += [{"symbol": s, "key": k, "kind": "stock",
                     "expiry": STOCK_EXPIRY_KEYWORD}
                    for s, k in sorted(stocks.items())]
    return underlyings, diff
