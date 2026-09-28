#!/usr/bin/env python3
"""
NSE sector classification for symbols not yet in SYMBOL_SECTOR_MAP.
==================================================================

    python3 tools/nse_sectors.py --additions      # classify what is held back
    python3 tools/nse_sectors.py --symbol AGI --symbol EPACK

WHY THIS IS NEEDED AT ALL, AND WHY "OTHER" IS NOT AN OPTION
-----------------------------------------------------------
SYMBOL_SECTOR_MAP is what ALL_SYMBOLS is built from, so a symbol cannot enter the
universe without a sector. It is tempting to add one anyway and let 03b's
`.map(symbol_sector).fillna("OTHER")` absorb it. That would be the most expensive
shortcut available: 07_sector_momentum ranks sectors and 03b scores every stock
against the resulting sector_bias, so a single OTHER bucket of 300-odd names --
larger than any real sector -- does not merely mis-score the new symbols, it
corrupts the sector input for the 460 that were already working. Retagging 42
symbols was enough to change the top-3 long bias; 300 unclassified ones would
make the ranking meaningless.

So: no sector, no entry. A symbol we cannot classify is one we cannot score.

TWO SOURCES, IN PRIORITY ORDER, BOTH OFFICIAL
---------------------------------------------
  1. INDEX CONSTITUENT LISTS -- nsearchives.../content/indices/*.csv carry an
     `Industry` column holding NSE's 22-value macro-economic sector. ONE file,
     ind_niftytotalmarket_list.csv, covers 755 symbols; every sectoral list
     (Bank, IT, Pharma, Auto, Metal, FMCG, Realty, Energy, Infra) is a subset of
     it and together they add 3. This is the authoritative taxonomy and is used
     wherever it reaches.

  2. smIndustry FROM THE ANNOUNCEMENTS FEED -- a finer ~74-value industry string,
     available for any symbol that has filed an announcement. A single
     market-wide date-windowed request returns it for ~930 symbols. Used only
     where the index lists do not reach, and it is genuinely finer: it is what
     resolves a port out of the useless macro bucket "Services".

WHAT DOES NOT GET MAPPED, DELIBERATELY
--------------------------------------
"Services", "Diversified" and "Miscellaneous" are not sectors, they are the
absence of one. NSE files coworking space, logistics technology, cash handling,
staffing and ports under "Services" alike; picking a bucket for AWFIS or
BLACKBUCK from that string would be invention, not classification. They stay
unclassified and therefore out, and the count is reported rather than hidden.

THE MAPPING IS ANCHORED ON THE EXISTING 460, NOT INVENTED
---------------------------------------------------------
Every target below matches how comparable symbols are ALREADY tagged, so the
additions land in the same buckets as their peers rather than in a parallel
taxonomy: chemicals to METAL because AARTIIND, ATUL, SRF and PIDILITIND are
there; cement to INFRA because ACC, AMBUJACEM and ULTRACEMCO are; hospitals to
PHARMA because APOLLOHOSP and FORTIS are; consumer durables, textiles, hotels,
retail and media to FMCG because HAVELLS, PAGEIND, INDHOTEL, DMART and SUNTV are.
"""

from __future__ import annotations

import io
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("nse-sectors")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
IDX_URL = "https://nsearchives.nseindia.com/content/indices/{name}.csv"
ANN_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
           "&from_date={frm}&to_date={to}")
ANN_SYM_URL = ("https://www.nseindia.com/api/corporate-announcements?index=equities"
               "&symbol={symbol}&from_date={frm}&to_date={to}")

# ind_niftytotalmarket_list alone covers 755 symbols and every sectoral list is a
# subset of it. The rest are kept because they are cheap, they are what NSE
# publishes per sector, and a symbol newly added to one shows up here first.
INDEX_LISTS = (
    "ind_niftytotalmarket_list", "ind_nifty500list", "ind_niftysmallcap250list",
    "ind_niftymidcap150list", "ind_niftylargemidcap250list",
    "ind_niftymidsmallcap400list", "ind_niftysmallcap100list",
    "ind_niftybanklist", "ind_niftyitlist", "ind_niftypharmalist",
    "ind_niftyautolist", "ind_niftymetallist", "ind_niftyfmcglist",
    "ind_niftyrealtylist", "ind_niftyenergylist", "ind_niftyinfralist",
    "ind_niftyfinancelist", "ind_niftymedialist", "ind_niftypsubanklist",
    "ind_niftyhealthcarelist", "ind_niftyconsumerdurableslist",
    "ind_niftyoilgaslist",
)
BANK_LISTS = ("ind_niftybanklist", "ind_niftypsubanklist")

# NSE macro-economic sector -> the 12 buckets in SYMBOL_SECTOR_MAP.
MACRO = {
    "Automobile and Auto Components":    "AUTO",
    "Capital Goods":                     "INFRA",
    "Chemicals":                         "METAL",
    "Construction":                      "INFRA",
    "Construction Materials":            "INFRA",
    "Consumer Durables":                 "FMCG",
    "Consumer Services":                 "FMCG",
    "Fast Moving Consumer Goods":        "FMCG",
    "Financial Services":                "FINANCE",   # BANK_LISTS override below
    "Forest Materials":                  "METAL",
    "Healthcare":                        "PHARMA",
    "Information Technology":            "IT",
    "Media Entertainment & Publication": "FMCG",
    "Metals & Mining":                   "METAL",
    "Oil Gas & Consumable Fuels":        "ENERGY",
    "Power":                             "ENERGY",
    "Realty":                            "REALTY",
    "Telecommunication":                 "TELECOM",
    "Textiles":                          "FMCG",
    "Utilities":                         "ENERGY",
    # NOT MAPPED, on purpose -- see the docstring. These are the absence of a
    # sector, and NSE files ports, coworking, staffing and cash handling under
    # the first one alike.
    "Services":                          None,
    "Diversified":                       None,
}

# The finer announcements taxonomy, used only where the index lists do not reach.
GRANULAR = {
    "Pharmaceuticals": "PHARMA", "Healthcare": "PHARMA", "Hospital": "PHARMA",
    "Construction": "INFRA", "Engineering": "INFRA", "Abrasives": "INFRA",
    "Fastners": "INFRA", "Diesel Engines": "INFRA", "Shipping": "INFRA",
    "Electrical Equipment": "INFRA", "Electronics - Industrial": "INFRA",
    "Cement And Cement Products": "INFRA", "Castings/Forgings": "INFRA",
    "Chemicals - Speciality": "METAL", "Chemicals - Organic": "METAL",
    "Chemicals - Inorganic": "METAL", "Dyes And Pigments": "METAL",
    "Pesticides And Agrochemicals": "METAL", "Fertilisers": "METAL",
    "Steel And Steel Products": "METAL", "Metals": "METAL",
    "Aluminium": "METAL", "Mining": "METAL", "Packaging": "METAL",
    "Plastic And Plastic Products": "METAL", "Paper And Paper Products": "METAL",
    "Brew/Distilleries": "FMCG", "Textile Products": "FMCG",
    "Textiles - Cotton": "FMCG", "Textiles - Synthetic": "FMCG",
    "Textiles": "FMCG", "Personal Care": "FMCG", "Cigarettes": "FMCG",
    "Food And Food Processing": "FMCG", "Consumer Durables": "FMCG",
    "Hotels": "FMCG", "Gems Jewellery And Watches": "FMCG", "Sugar": "FMCG",
    "Media & Entertainment": "FMCG", "Retail": "FMCG",
    "Oil Exploration/Production": "ENERGY", "Refineries": "ENERGY",
    "Power": "ENERGY", "Power Generation And Supply": "ENERGY",
    "Gas": "ENERGY", "Petrochemicals": "ENERGY",
    "Auto Ancillaries": "AUTO", "Automobiles - 4 Wheelers": "AUTO",
    "Automobiles - 2 And 3 Wheelers": "AUTO", "Tyres": "AUTO",
    "Computers - Software": "IT", "Computers - Hardware": "IT",
    "IT Enabled Services": "IT", "Telecommunication - Service Provider": "TELECOM",
    "Telecomm-Service": "TELECOM", "Telecommunication - Equipment": "TELECOM",
    "Finance": "FINANCE", "Finance - Housing": "FINANCE",
    "Financial Institution": "FINANCE", "Insurance": "FINANCE",
    "Banks": "BANKING",
    "Construction And Contracting - Real Estate": "REALTY",
    "Realty": "REALTY",
    # Not sectors:
    "Miscellaneous": None, "Diversified": None, "Trading": None,
}


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
    try:
        s.get("https://www.nseindia.com", timeout=40)
    except Exception as e:
        log.warning(f"could not warm the NSE session ({e}) — continuing")
    return s


def fetch_index_industry(s=None) -> tuple:
    """({symbol: macro sector}, {bank symbols}). Raises if the primary list fails."""
    import pandas as pd
    s = s or _session()
    ind, banks = {}, set()
    primary_ok = False
    for name in INDEX_LISTS:
        try:
            r = s.get(IDX_URL.format(name=name), timeout=40)
            if r.status_code != 200:
                log.warning(f"{name}: HTTP {r.status_code}")
                continue
            d = pd.read_csv(io.StringIO(r.text))
            d.columns = [c.strip() for c in d.columns]
            if "Industry" not in d.columns or "Symbol" not in d.columns:
                log.warning(f"{name}: no Industry column")
                continue
        except Exception as e:
            log.warning(f"{name}: {e}")
            continue
        if name == INDEX_LISTS[0]:
            primary_ok = True
        for sym, macro in zip(d["Symbol"].astype(str).str.strip(),
                              d["Industry"].astype(str).str.strip()):
            if sym and macro and macro.lower() != "nan":
                ind.setdefault(sym, macro)
                if name in BANK_LISTS:
                    banks.add(sym)
        time.sleep(0.4)
    if not primary_ok:
        # Falling back to the finer source alone would silently reclassify the
        # whole universe on a different taxonomy. Better to stop.
        raise RuntimeError(f"{INDEX_LISTS[0]} could not be read — refusing to "
                           f"classify from the secondary source alone")
    return ind, banks


def fetch_announcement_industry(s=None, days: int = 90) -> dict:
    """{symbol: granular industry} from one market-wide announcements window."""
    from datetime import date, timedelta
    s = s or _session()
    today = date.today()
    frm = (today - timedelta(days=days)).strftime("%d-%m-%Y")
    r = s.get(ANN_URL.format(frm=frm, to=today.strftime("%d-%m-%Y")), timeout=180)
    if r.status_code != 200:
        log.warning(f"announcements feed HTTP {r.status_code} — secondary source "
                    f"unavailable")
        return {}
    rows = r.json()
    rows = rows if isinstance(rows, list) else (rows.get("data") or [])
    out = {}
    for x in rows:
        sym = (x.get("symbol") or "").strip()
        gi = (x.get("smIndustry") or "").strip()
        if sym and gi and gi != "-":
            out.setdefault(sym, gi)
    log.info(f"announcements feed: {len(rows):,} rows -> {len(out)} symbols")
    return out


def fetch_announcement_industry_per_symbol(symbols: list, s=None,
                                           days: int = 370) -> dict:
    """
    {symbol: granular industry}, one request each.

    THE MARKET-WIDE FEED TRUNCATES. A 90-day window returned 52,681 rows but only
    933 DISTINCT symbols, which left 79 of the additions looking as though they
    had filed nothing in three months -- implausible for a listed company, and it
    would have dropped them for a gap in the harvest rather than a gap in NSE.
    Per-symbol is ~1.4s each and definitive, so it is worth the requests for the
    residue. The window is a year, because industry does not change and the only
    thing needed is one filing of any kind.
    """
    from datetime import date, timedelta
    s = s or _session()
    today = date.today()
    frm = (today - timedelta(days=days)).strftime("%d-%m-%Y")
    out = {}
    from urllib.parse import quote
    for i, sym in enumerate(symbols, 1):
        try:
            r = s.get(ANN_SYM_URL.format(symbol=quote(sym, safe=""), frm=frm,
                                         to=today.strftime("%d-%m-%Y")),
                      timeout=60)
            if r.status_code == 200:
                rows = r.json()
                rows = rows if isinstance(rows, list) else (rows.get("data") or [])
                for x in rows:
                    gi = (x.get("smIndustry") or "").strip()
                    if gi and gi != "-":
                        out[sym] = gi
                        break
        except Exception as e:
            log.warning(f"{sym}: {e}")
        if i % 25 == 0:
            log.info(f"  per-symbol industry {i}/{len(symbols)} ({len(out)} found)")
        time.sleep(0.35)
    return out


def classify(symbols: list, idx: dict, banks: set, gran: dict,
             probed: set = None) -> tuple:
    """
    ({symbol: sector}, [(symbol, why-unresolved)]).

    Index macro first, granular second. A symbol whose only classification is a
    non-sector ("Services", "Miscellaneous", "Diversified") is UNRESOLVED, not
    forced into a bucket.
    """
    resolved, unresolved = {}, []
    for sym in symbols:
        macro = idx.get(sym)
        gi = gran.get(sym)
        sec = None
        if macro in MACRO and MACRO[macro]:
            sec = MACRO[macro]
            if sec == "FINANCE" and sym in banks:
                sec = "BANKING"
        if sec is None and gi:
            sec = GRANULAR.get(gi)
        if sec is None and gi == "Banks":
            sec = "BANKING"
        if sec:
            resolved[sym] = sec
        else:
            if macro and gi:
                why = f"macro {macro!r} is not a sector; industry {gi!r} unmapped"
            elif macro:
                why = f"macro {macro!r} is not a sector, and no finer industry"
            elif gi:
                why = f"industry {gi!r} unmapped and not in any index list"
            elif probed and sym in probed:
                # Checked individually: NSE answers with its filings and leaves
                # smIndustry empty. AGI returns 122 announcements, all with no
                # industry string, where RELIANCE returns "Refineries". The gap is
                # NSE's, not the harvest's -- confirmed per symbol before writing
                # any of these off.
                why = ("in no index list, and NSE publishes no industry for it "
                       "even on its own filings")
            else:
                why = "in no index list and has filed no announcement"
            unresolved.append((sym, why))
    return resolved, unresolved


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--additions", action="store_true",
                    help="classify the symbols universe_filter holds back")
    ap.add_argument("--symbol", action="append", default=[])
    ap.add_argument("--out", default="", help="write {symbol: sector} JSON here")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if a.additions:
        import importlib.util as il
        sp = il.spec_from_file_location("uf", ROOT / "tools/universe_filter.py")
        uf = il.module_from_spec(sp)
        sp.loader.exec_module(uf)
        f = uf.funnel(verbose=False)
        syms = sorted(uf.compare_to_current(f["survivors"])["add"])
    else:
        syms = a.symbol
    if not syms:
        ap.error("give --additions or --symbol")

    s = _session()
    idx, banks = fetch_index_industry(s)
    gran = fetch_announcement_industry(s)
    resolved, unresolved = classify(syms, idx, banks, gran)

    # Anything the two bulk sources missed gets one request of its own before it
    # is written off. Dropping a symbol for a hole in the harvest is not the same
    # as dropping one NSE cannot classify.
    residue = [sym for sym, why in unresolved if "no index list" in why
               or "filed no announcement" in why]
    if residue:
        log.info(f"{len(residue)} symbol(s) unreached by the bulk sources — "
                 f"fetching each")
        extra = fetch_announcement_industry_per_symbol(residue, s)
        gran = {**gran, **extra}
        resolved, unresolved = classify(syms, idx, banks, gran, probed=set(residue))
        log.info(f"per-symbol pass resolved {len(extra)} of {len(residue)}")

    print("=" * 78)
    print(f"SECTOR CLASSIFICATION — {len(syms)} symbol(s)")
    print("=" * 78)
    print(f"  index lists reached      {sum(1 for x in syms if x in idx):>5}")
    print(f"  announcements reached    {sum(1 for x in syms if x in gran):>5}")
    print(f"  RESOLVED                 {len(resolved):>5}   "
          f"{len(resolved)/max(len(syms),1)*100:>5.1f}%")
    print(f"  UNRESOLVED -> stay out   {len(unresolved):>5}   "
          f"{len(unresolved)/max(len(syms),1)*100:>5.1f}%")
    print(f"\nresolved by sector:")
    for sec, n in Counter(resolved.values()).most_common():
        print(f"  {sec:<10}{n:>5}")
    print(f"\nUNRESOLVED ({len(unresolved)}) — a symbol we cannot classify is one "
          f"we cannot score:")
    for why, group in sorted(
            {w: [s_ for s_, w2 in unresolved if w2 == w] for _, w in unresolved
             }.items(), key=lambda kv: -len(kv[1])):
        print(f"  {len(group):>4}  {why}")
        for i in range(0, min(len(group), 24), 8):
            print(f"        {' '.join(group[i:i+8])}")
        if len(group) > 24:
            print(f"        … and {len(group)-24} more")
    if a.out:
        Path(a.out).write_text(json.dumps(resolved, indent=1, sort_keys=True))
        print(f"\nwrote {len(resolved)} mapping(s) -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
