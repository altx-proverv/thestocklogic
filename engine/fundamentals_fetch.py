"""
TSL — Fundamentals Fetch
========================
Builds the annual-facts store the fundamentals gate reads, one symbol at a time,
resumably.

    python3 -m engine.fundamentals_fetch --all            # every universe symbol
    python3 -m engine.fundamentals_fetch --symbol RELIANCE
    python3 -m engine.fundamentals_fetch --limit 40
    python3 -m engine.fundamentals_fetch --distribution   # report, no fetching

WHY IT IS RESUMABLE AND SLOW ON PURPOSE
---------------------------------------
Four requests per symbol -- one for the filing index, three for the annual XBRL
files -- so 460 symbols is ~1,800 requests against nseindia.com. That is a
long-running job against someone else's infrastructure, so it sleeps between
requests and it writes after every symbol.

Writing as it goes means a failure halfway costs the remaining symbols, not the
finished ones, and a re-run skips what it already has. The alternative -- hold
everything in memory and write once -- loses an hour of work to one timeout.

WHAT IT STORES, AND WHAT IT DOES NOT DECIDE
-------------------------------------------
Raw normalised facts per filing year, plus the parse notes. No verdicts: the
rules live in engine/fundamentals.py and are applied on top, so a threshold
change re-reads the store rather than re-fetching NSE. That separation is the
point -- the calibration question ("what should the ROE floor be") must be
answerable without another 1,800 requests.

CONSOLIDATED IS PREFERRED OVER STANDALONE. A holding company's standalone
accounts can show trivial revenue and a large investment line, which makes ROE
and leverage meaningless. Where both exist the consolidated filing is taken, and
which one was used is recorded per year.

TIER 1 NEEDS TWO MORE SOURCES -- the shareholding pattern for pledge and
promoter %, and corporate announcements for auditor events -- which are a second
pass. Until they exist every Tier 1 evaluation is UNPARSEABLE, so the gate
blocks everything, which is the correct state for a gate that is not finished.
"""

from __future__ import annotations

import sys
import json
import time
import logging
import argparse
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

log = logging.getLogger("TSL-FUND-FETCH")

STORE = ROOT / "data/processed/fundamentals_annual.json"

NSE_RESULTS = ("https://www.nseindia.com/api/corporates-financial-results"
               "?index=equities&symbol={symbol}&period=Annual")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Consecutive symbols returning nothing usable before the run gives up. NSE
# degrades its payload under rapid requests rather than refusing outright, so a
# rate-limited run does not fail -- it quietly marks every symbol unparseable and
# poisons the distribution the whole exercise exists to produce. Ten in a row is
# far past coincidence for a universe of large caps.
MAX_CONSECUTIVE_EMPTY = 10

YEARS_WANTED = 3        # tier 2 needs 3 for ROE, tier 1 needs 3 for rising D/E
REQUEST_DELAY = 0.35    # between requests, to nseindia.com
TIMEOUT = 40
RETRIES = 2



def _q(symbol: str) -> str:
    """
    URL-ENCODE THE SYMBOL. NSE tickers contain ampersands -- M&M, M&MFIN,
    J&KBANK, ARE&M, GVT&D -- and interpolating one raw into a query string ends
    the symbol parameter and starts a junk one: ?symbol=M&M asks for symbol "M".
    NSE answers 200 with an empty or wrong payload rather than an error, so the
    symbol simply reports "no filings" and looks like a company that does not
    file. Five symbols were blocked this way before it was caught.
    """
    return quote(symbol, safe="")

def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
    return s


def _get(s, url: str, tries: int = RETRIES, quiet: bool = False):
    """
    None on failure. Retries only what retrying can fix.

    A 404 will not become a 200, so retrying a 4xx triples the request count
    against someone else's server for nothing. Only 5xx and network errors are
    retried.
    """
    last = None
    for attempt in range(tries + 1):
        try:
            r = s.get(url, timeout=TIMEOUT)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < tries:
            time.sleep(1.0 + attempt)
    if not quiet:
        log.warning(f"gave up on {url[:80]}: {last}")
    return None


def _is_url(v) -> bool:
    """
    The xbrl field is not always a URL.

    NSE returns rows whose xbrl is a placeholder, and the placeholder is not
    empty -- it is a well-formed URL ending in a bare dash:

        https://nsearchives.nseindia.com/corporate/xbrl/-

    So `if row.get("xbrl")` accepts it AND so does a startswith("http") check;
    only the filename reveals it. A real one ends in .xml
    (INDAS_121277_1705282_30072026051753.xml). Same shape as "No Band": a
    sentinel that passes every cheap test you would think to write.
    """
    return (isinstance(v, str) and v.startswith("http")
            and v.rstrip("/").lower().endswith(".xml"))


def load_store(path: Path = STORE) -> dict:
    if not path.exists():
        return {"schema": 1, "symbols": {}}
    try:
        d = json.loads(path.read_text())
        d.setdefault("symbols", {})
        return d
    except Exception as e:
        log.error(f"store unreadable ({e}) — starting a new one")
        return {"schema": 1, "symbols": {}}


def save_store(store: dict, path: Path = STORE) -> None:
    store["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=1, sort_keys=True))
    tmp.replace(path)


def fetch_symbol(s, symbol: str) -> dict:
    """
    {years: [facts...], status, note}. Never raises.

    status is "ok" when at least one year parsed, "no_filings" when NSE lists
    none, "unreachable" when the index could not be read, and "unparseable" when
    filings exist but none yielded usable facts -- kept distinct because they
    mean different things about whether the parser or the data is at fault.
    """
    from engine import fundamentals as F

    r = _get(s, NSE_RESULTS.format(symbol=_q(symbol)))
    time.sleep(REQUEST_DELAY)
    if r is None:
        return {"years": [], "status": "unreachable",
                "note": "filing index could not be read"}
    try:
        j = r.json()
        rows = j if isinstance(j, list) else (j.get("data") or [])
    except Exception as e:
        return {"years": [], "status": "unreachable",
                "note": f"filing index not JSON: {e}"}

    listed = len(rows)
    rows = [x for x in rows if _is_url(x.get("xbrl"))]
    placeholders = listed - len(rows)
    if not rows:
        return {"years": [], "status": "no_filings",
                "note": f"{listed} row(s) listed, none with a usable XBRL URL"
                        + (f" ({placeholders} placeholder)" if placeholders else "")}

    # Consolidated first, then standalone, newest first within each. A holding
    # company's standalone accounts make ROE and leverage meaningless.
    def key(x):
        return (0 if str(x.get("consolidated", "")).lower() == "consolidated" else 1,
                str(x.get("toDate") or x.get("financialYear") or ""))
    con = sorted([x for x in rows if key(x)[0] == 0],
                 key=lambda x: str(x.get("filingDate") or ""), reverse=True)
    std = sorted([x for x in rows if key(x)[0] == 1],
                 key=lambda x: str(x.get("filingDate") or ""), reverse=True)

    years, seen_fy = [], set()
    for row in con + std:
        if len(years) >= YEARS_WANTED:
            break
        fy = str(row.get("financialYear") or "")
        if fy in seen_fy:
            continue
        rx = _get(s, row["xbrl"])
        time.sleep(REQUEST_DELAY)
        if rx is None:
            continue
        facts, notes = F.parse_annual(rx.text, fy=fy, source=row["xbrl"])
        usable = any(getattr(facts, a) is not None for a in
                     ("equity", "pat", "cfo", "borrowings_current",
                      "borrowings_noncurrent"))
        if not usable:
            continue
        seen_fy.add(fy)
        d = {"fy": fy,
             "consolidated": str(row.get("consolidated", "")),
             "equity": facts.equity,
             "borrowings_current": facts.borrowings_current,
             "borrowings_noncurrent": facts.borrowings_noncurrent,
             "pat": facts.pat, "cfo": facts.cfo, "capex": facts.capex,
             "source": facts.source,
             "notes": [n for n in notes if "no duration" in n or "no instant" in n][:6]}
        years.append(d)

    if not years:
        return {"years": [], "status": "unparseable",
                "note": f"{len(rows)} filing(s) listed, none yielded usable facts"}
    return {"years": years, "status": "ok",
            "note": f"{len(years)} of {YEARS_WANTED} year(s) parsed"
                    + (f", {placeholders} placeholder row(s) skipped"
                       if placeholders else "")}


def universe() -> list:
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", ROOT / "engine/universe.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return list(m.ALL_SYMBOLS)


def run(symbols: list, refetch: bool = False, path: Path = STORE) -> dict:
    store = load_store(path)
    s = _session()
    todo = [x for x in symbols if refetch or x not in store["symbols"]]
    log.info(f"{len(symbols)} symbol(s) requested, {len(todo)} to fetch "
             f"({len(symbols) - len(todo)} already in the store)")

    empty_streak = 0
    for i, sym in enumerate(todo, 1):
        got = fetch_symbol(s, sym)
        store["symbols"][sym] = got
        if got["status"] == "ok":
            empty_streak = 0
        else:
            empty_streak += 1
            if empty_streak >= MAX_CONSECUTIVE_EMPTY:
                save_store(store, path)
                log.error(f"{empty_streak} symbols in a row returned nothing "
                          f"usable — stopping. That is not a property of the "
                          f"universe; it is almost certainly rate limiting or a "
                          f"changed endpoint. {len(store['symbols'])} symbol(s) "
                          f"are stored and a re-run resumes from there.")
                store["aborted"] = {"after": sym, "reason": "consecutive empties"}
                return store
        # Written every symbol: a failure at 300 must not cost the first 299.
        save_store(store, path)
        if i % 20 == 0 or i == len(todo):
            done = sum(1 for v in store["symbols"].values() if v["status"] == "ok")
            log.info(f"  {i}/{len(todo)}  last={sym} {got['status']}  "
                     f"ok so far {done}")
    return store


# ══════════════════════════════════════════════════════════════════
# DISTRIBUTIONS — the calibration question
# ══════════════════════════════════════════════════════════════════

def _facts(rows: list):
    from engine.fundamentals import AnnualFacts
    return [AnnualFacts(fy=r.get("fy", ""), equity=r.get("equity"),
                        borrowings_current=r.get("borrowings_current"),
                        borrowings_noncurrent=r.get("borrowings_noncurrent"),
                        pat=r.get("pat"), cfo=r.get("cfo"), capex=r.get("capex"),
                        source=r.get("source", "")) for r in rows]


def _pct(values: list, qs=(5, 10, 25, 50, 75, 90, 95)) -> dict:
    if not values:
        return {}
    v = sorted(values)
    out = {}
    for q in qs:
        i = min(len(v) - 1, max(0, int(round((q / 100) * (len(v) - 1)))))
        out[f"p{q}"] = v[i]
    return out


def distribution(path: Path = STORE) -> dict:
    from engine.fundamentals import (ROE_MIN_PCT, DE_MAX, FCF_POSITIVE_YEARS,
                                     FCF_WINDOW_YEARS, ROE_AVERAGE_YEARS)
    store = load_store(path)
    syms = store.get("symbols") or {}
    stats = {"total": len(syms), "status": {}, "roe": [], "de": [], "fcf_years": [],
             "years_available": {}, "missing": {}}

    for sym, rec in syms.items():
        st = rec.get("status", "?")
        stats["status"][st] = stats["status"].get(st, 0) + 1
        rows = rec.get("years") or []
        stats["years_available"][len(rows)] = stats["years_available"].get(len(rows), 0) + 1
        if st != "ok":
            continue
        fs = _facts(rows)
        # Averaged over the years AVAILABLE, not only when the full window is
        # present. The gate requires ROE_AVERAGE_YEARS; this report exists to
        # decide what that number should be, so requiring it here would make
        # the distribution empty whenever the data cannot satisfy the current
        # guess -- which is exactly the case being investigated.
        roes = [f.roe_pct for f in fs[:ROE_AVERAGE_YEARS] if f.roe_pct is not None]
        if roes:
            stats["roe"].append((sym, sum(roes) / len(roes)))
            stats.setdefault("roe_years", {})
            stats["roe_years"][len(roes)] = stats["roe_years"].get(len(roes), 0) + 1
        else:
            stats["missing"]["roe"] = stats["missing"].get("roe", 0) + 1
        if fs and fs[0].de is not None:
            stats["de"].append((sym, fs[0].de))
        else:
            stats["missing"]["de"] = stats["missing"].get("de", 0) + 1
        fcfs = [f.fcf for f in fs[:FCF_WINDOW_YEARS] if f.fcf is not None]
        if fcfs:
            stats["fcf_years"].append((sym, sum(1 for x in fcfs if x > 0),
                                       len(fcfs)))
        else:
            stats["missing"]["fcf"] = stats["missing"].get("fcf", 0) + 1
    return stats


def report(path: Path = STORE) -> int:
    from engine.fundamentals import (ROE_MIN_PCT, DE_MAX, FCF_POSITIVE_YEARS,
                                     FCF_WINDOW_YEARS)
    st = distribution(path)
    if not st["total"]:
        print(f"no store at {path} — run --all first")
        return 1

    print("=" * 74)
    print(f"FUNDAMENTALS STORE — {st['total']} symbol(s)")
    print("=" * 74)
    print("\nFETCH STATUS")
    for k, n in sorted(st["status"].items(), key=lambda kv: -kv[1]):
        print(f"  {k:<16}{n:>5}  {n / st['total'] * 100:>5.1f}%")
    print("\nANNUAL YEARS PARSED PER SYMBOL")
    for k in sorted(st["years_available"]):
        n = st["years_available"][k]
        print(f"  {k} year(s){'':<7}{n:>5}  {n / st['total'] * 100:>5.1f}%")
    if st["missing"]:
        print("\nMETRIC NOT COMPUTABLE (among symbols that fetched ok)")
        for k, n in sorted(st["missing"].items()):
            print(f"  {k:<16}{n:>5}")

    roe = [v for _, v in st["roe"]]
    de = [v for _, v in st["de"]]
    fcf = [(v, n) for _, v, n in st["fcf_years"]]

    if st.get("roe_years"):
        print("\nROE AVERAGED OVER HOW MANY YEARS")
        for k in sorted(st["roe_years"]):
            print(f"  {k} year(s){'':<7}{st['roe_years'][k]:>5}")
    print(f"\nAVERAGE ROE over available years  ({len(roe)} symbols)")
    if roe:
        q = _pct(roe)
        print("  " + "  ".join(f"{k}={v:.1f}%" for k, v in q.items()))
        for t in (8, 10, 12, 15, 18):
            n = sum(1 for v in roe if v > t)
            print(f"    > {t:>2}%   {n:>4} pass  ({n / len(roe) * 100:>5.1f}%)"
                  + ("   <- current floor" if t == ROE_MIN_PCT else ""))

    print(f"\nDEBT / EQUITY, latest year  ({len(de)} symbols)")
    if de:
        q = _pct(de)
        print("  " + "  ".join(f"{k}={v:.2f}" for k, v in q.items()))
        for t in (0.5, 0.75, 1.0, 1.5, 2.0):
            n = sum(1 for v in de if v < t)
            print(f"    < {t:<4}  {n:>4} pass  ({n / len(de) * 100:>5.1f}%)"
                  + ("   <- current ceiling" if t == DE_MAX else ""))

    print(f"\nFREE CASH FLOW: positive years out of those available"
          f"  ({len(fcf)} symbols)")
    if fcf:
        import collections as _c
        bywin = _c.Counter(n for _, n in fcf)
        print("  window actually available: "
              + ", ".join(f"{k}yr x{v}" for k, v in sorted(bywin.items())))
        for k in range(0, max(n for _, n in fcf) + 1):
            n = sum(1 for v, _ in fcf if v == k)
            if n:
                print(f"    {k} positive year(s)  {n:>4}  ({n / len(fcf) * 100:>5.1f}%)")
        n = sum(1 for v, _ in fcf if v >= FCF_POSITIVE_YEARS)
        print(f"    >= {FCF_POSITIVE_YEARS} positive      {n:>4}  "
              f"({n / len(fcf) * 100:>5.1f}%)   <- current floor")

    print("\nALL THREE TIER 2 TESTS TOGETHER, at the current thresholds")
    roe_d, de_d = dict(st["roe"]), dict(st["de"])
    fcf_d = {s_: v for s_, v, _ in st["fcf_years"]}
    both = [s for s in roe_d if s in de_d and s in fcf_d]
    if both:
        passing = [s for s in both
                   if roe_d[s] > ROE_MIN_PCT and de_d[s] < DE_MAX
                   and fcf_d[s] >= FCF_POSITIVE_YEARS]
        print(f"  {len(both)} symbol(s) have all three computable")
        print(f"  {len(passing)} pass all three  "
              f"({len(passing) / len(both) * 100:.1f}%)")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--symbol", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--distribution", action="store_true")
    a = ap.parse_args()

    if a.distribution:
        return report()

    syms = a.symbol or universe()
    if a.limit:
        syms = syms[:a.limit]
    if not syms:
        print("nothing to do — pass --all, --symbol or --limit")
        return 1
    run(syms, refetch=a.refetch)
    return report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
