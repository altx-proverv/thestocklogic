#!/usr/bin/env python3
"""
THE SECTOR MAP -- built from NSE's own industry classification (2026-11-10).
===========================================================================

    python3 tools/build_sector_map.py --fetch      # fetch NSE's lists, rebuild
    python3 tools/build_sector_map.py              # rebuild from the stored lists

WHAT IT REPLACES. engine/universe.py carried a hand-edited literal dict whose
header claimed "Source: NSE India ... Auto-generated. Do not edit manually" --
no generator existed, the first version filed NTPC/ONGC/IOC/COALINDIA under
DEFENCE, and tools/nse_sectors.py folded NSE's Consumer Durables, Consumer
Services, Textiles and Media into FMCG and Chemicals into METAL (FMCG held 141
names, 40 of them FMCG by NSE). OPEN D-071.

THE RULE (operator, 2026-11-10, option (a)):
  * the label is NSE's "Industry" column VERBATIM, from NSE's index
    constituent lists (Nifty Total Market first -- 750 names -- then the
    narrower lists, which only fill gaps);
  * NSE's "Financial Services" is split: BANKING if the symbol is in an NSE bank
    index (Nifty Bank, Nifty PSU Bank, Nifty Private Bank), FINANCE otherwise --
    engine/fundamentals.py keys on those two strings;
  * a symbol NSE does not classify in any fetched list is UNKNOWN. Never a
    guess: a wrong sector sends a short signal to the wrong group of stocks.
  * two lists giving one symbol different industries is a CONFLICT, recorded;
    the Total Market list wins and the disagreement is kept in the file.

WHAT IT WRITES. engine/sector_map.json -- every universe symbol's label, the
source URLs with the fetch time and row count of each, the rule above, and
the conflicts -- and the exact NSE files it read, in engine/sector_sources/,
so tests/test_sector_map.py can rebuild the map from them and require it to be
identical. A map that claims to be generated must be reproducible by its
generator; the old one was not.

Not served by the site: engine/ is in .vercelignore.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SRC_DIR = ROOT / "engine" / "sector_sources"
OUT = ROOT / "engine" / "sector_map.json"
IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://nsearchives.nseindia.com/content/indices/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Industry sources, in precedence order. Total Market (Nifty 500 + Microcap 250)
# is the broad one; the others can only add symbols it lacks.
INDUSTRY_LISTS = (
    "ind_niftytotalmarket_list.csv",
    "ind_nifty500list.csv",
    "ind_niftymidcap150list.csv",
    "ind_niftysmallcap250list.csv",
    "ind_niftymicrocap250_list.csv",
    "ind_niftyfinancelist.csv",
    "ind_niftyconsumerdurableslist.csv",
    "ind_niftyindiamanufacturing_list.csv",
    "ind_niftyindiadefence_list.csv",
)
BANK_LISTS = (
    "ind_niftybanklist.csv",
    "ind_niftypsubanklist.csv",
    "ind_nifty_privatebanklist.csv",
)
FINANCIAL = "Financial Services"
UNKNOWN = "UNKNOWN"


def fetch(names=INDUSTRY_LISTS + BANK_LISTS) -> dict:
    """Download each list into engine/sector_sources/. -> {name: provenance}."""
    import requests
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    prov = {}
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/csv,*/*"})
    for n in names:
        url = BASE + n
        at = datetime.now(IST).isoformat(timespec="seconds")
        try:
            r = s.get(url, timeout=30)
            ok = r.status_code == 200 and r.text.lstrip().startswith("Company Name")
        except Exception as e:
            prov[n] = {"url": url, "fetched_at": at, "status": f"error: {e}"}
            continue
        prov[n] = {"url": url, "fetched_at": at, "status": r.status_code if ok else
                   f"{r.status_code} (not an index list)"}
        if ok:
            (SRC_DIR / n).write_text(r.text)
            prov[n]["rows"] = sum(1 for _ in csv.DictReader(io.StringIO(r.text)))
        time.sleep(0.5)
    (SRC_DIR / "provenance.json").write_text(json.dumps(prov, indent=1, sort_keys=True))
    return prov


def _read(name: str) -> list:
    p = SRC_DIR / name
    if not p.exists():
        return []
    rows = list(csv.DictReader(io.StringIO(p.read_text())))
    return [{k.strip(): (v or "").strip() for k, v in r.items()} for r in rows]


def build(universe: list) -> dict:
    """The map, from the stored lists only. Pure: same files in, same map out."""
    industry, seen_in, conflicts = {}, {}, []
    for n in INDUSTRY_LISTS:
        for r in _read(n):
            sym, ind = r.get("Symbol"), r.get("Industry")
            if not sym or not ind:
                continue
            if sym in industry and industry[sym] != ind:
                conflicts.append({"symbol": sym, "kept": industry[sym], "kept_from": seen_in[sym],
                                  "other": ind, "other_from": n})
                continue
            if sym not in industry:
                industry[sym], seen_in[sym] = ind, n
    banks = set()
    for n in BANK_LISTS:
        banks |= {r["Symbol"] for r in _read(n) if r.get("Symbol")}
    sectors = {}
    for sym in universe:
        ind = industry.get(sym)
        if ind is None:
            sectors[sym] = UNKNOWN
        elif ind == FINANCIAL:
            sectors[sym] = "BANKING" if sym in banks else "FINANCE"
        else:
            sectors[sym] = ind
    prov = {}
    pf = SRC_DIR / "provenance.json"
    if pf.exists():
        prov = json.loads(pf.read_text())
    return {
        "generator": "tools/build_sector_map.py",
        "rule": ("NSE index-list 'Industry' verbatim (Nifty Total Market first); "
                 "'Financial Services' -> BANKING if in an NSE bank index, else FINANCE; "
                 "unclassified -> UNKNOWN; conflicting lists recorded, Total Market wins"),
        "sources": prov,
        "fetched_at": max((v.get("fetched_at", "") for v in prov.values()), default=None),
        "counts": {k: sum(1 for v in sectors.values() if v == k)
                   for k in sorted(set(sectors.values()))},
        "conflicts": conflicts,
        "sectors": dict(sorted(sectors.items())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="fetch NSE's lists first")
    a = ap.parse_args()
    if a.fetch:
        prov = fetch()
        bad = {k: v["status"] for k, v in prov.items() if v["status"] != 200}
        for k, v in prov.items():
            print(f"  {k:<40} {v['status']}  {v.get('rows', '')}")
        if any(k in bad for k in INDUSTRY_LISTS[:1]):
            print("Nifty Total Market could not be fetched -- refusing to rebuild")
            return 1
    from engine.universe import UNIVERSE_SYMBOLS
    m = build(UNIVERSE_SYMBOLS)
    OUT.write_text(json.dumps(m, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(m['sectors'])} symbols, "
          f"{m['counts'].get(UNKNOWN, 0)} UNKNOWN, {len(m['conflicts'])} conflict(s)")
    for k, v in sorted(m["counts"].items(), key=lambda kv: -kv[1]):
        print(f"  {v:>4}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
