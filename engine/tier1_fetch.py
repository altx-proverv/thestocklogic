#!/usr/bin/env python3
"""
TSL — TIER 1 SOURCES: shareholding (promoter %, pledge) and auditor events
==========================================================================
Tier 1 is the ONLY fundamentals gate (see engine/fundamentals.py). It needs two
things this module fetches, and without them every symbol is UNPARSEABLE and the
gate blocks the entire universe -- which is the state the layer shipped in.

    python3 -m engine.tier1_fetch --all
    python3 -m engine.tier1_fetch --symbol RELIANCE --symbol GMRAIRPORT -v
    python3 -m engine.tier1_fetch --report

WHERE THE NUMBERS COME FROM, ESTABLISHED BY PROBING
---------------------------------------------------
  api/corporate-share-holdings-master?symbol=X
      One request, ~22 quarterly rows. pr_and_prgrp is promoter+group percent,
      inline. `date` is the quarter end. `xbrl` links the full SHP filing.

  the SHP XBRL (~520 KB per company per quarter)
      The only place pledge lives.

PLEDGE: THE DENOMINATOR IS THE WHOLE PROBLEM
--------------------------------------------
There is no "pledge percent" field. EncumberedShareUnderPledgedAsPercentage-
OfTotalNumberOfShares appears MANY times under different contexts, with a
DIFFERENT DENOMINATOR each time, and the element name does not say which. In
GMRAIRPORT's Jun-2026 filing:

    ShareholdingOfPromoterAndPromoterGroup_ContextI   0.1644   <- what we want
    ShareholdingPattern_ContextI                      0.1104
    OtherIndianShareholders_ContextI                  0.3043
    Indian_ContextI                                   0.3035

1,166,000,000 pledged / 7,091,579,906 promoter shares = 0.1644. So the promoter
context expresses pledge as a fraction of PROMOTER HOLDING, which is what
"promoter pledge 25%" means everywhere it is quoted. ShareholdingPattern_ContextI
divides by total shares instead -- 11.04% for the same company, same element
name. Against a 25% threshold both of those pass here, but for a heavily pledged
promoter the choice decides the verdict. So the context is matched explicitly and
the value is CROSS-CHECKED against the raw share counts; a disagreement is
UNPARSEABLE, not a pick.

Values are FRACTIONS despite the name saying percentage (0.6716 for a company
whose pr_and_prgrp reads 50.48... no: 0.6716 <-> 67.16). Multiplied by 100 here.

PLEDGE ZERO IS AN EXPLICIT NO, NOT AN ABSENCE
---------------------------------------------
A company with no pledge OMITS the encumbrance value elements entirely -- and
that is exactly the shape that "missing is never a pass" exists to catch. What
rescues it is that the filing carries a separate BOOLEAN:

    WhetherAnySharesHeldByPromotersAreEncumberedUnderPledgedForPromoterAnd
    PromoterGroup = false          (RELIANCE, Jun-2026)

false + no value elements is a stated zero. true + no value elements is opacity
and yields None, which blocks. The boolean absent entirely also yields None.

AUDITOR EVENTS: THE desc IS THE ONLY REAL SIGNAL
------------------------------------------------
api/corporate-announcements carries `attchmntText`, and for auditor rows it is
BOILERPLATE -- it restates the category and nothing else:

    "Visagar Polytex Limited has informed the Exchange about Resignation of
     Statutory Auditor"

Checked across every auditor row in a six-week market-wide window: 0 of 28
resignations and 0 of 189 auditor changes carried any distinguishing text. So
keyword classification of the free text buys nothing; the substance is in the
PDF. What IS usable is that NSE's desc vocabulary is STRUCTURED, and separates
the cases:

    Resignation of Statutory Auditor      28  in six weeks   -> VETO
    Initiation of Forensic Audit           3                 -> VETO
    Change in Auditors                   189                 -> NOT a veto

CHANGE IN AUDITORS IS NOT A DETERIORATION SIGNAL, and treating it as one would
be the worst false positive in the layer. The eight most recent filers of it were
HUDCO, HMT, NTPCGREEN, BPCL, NFL, CONCOR, RAILTEL and ENGINERSIN -- PSUs whose
auditors are appointed by the CAG and reappointed annually. Add SEBI's mandatory
rotation and the category is overwhelmingly routine. At 189 per six weeks it
would veto most of the universe on a compliance formality. It is COUNTED and
stored so the number stays visible, and it does not veto.

QUALIFIED OPINION CANNOT BE READ FROM THIS SOURCE
-------------------------------------------------
The brief asks Tier 1 to veto on "auditor resignation OR qualified opinion".
Resignation is available and clean. A qualified or adverse opinion is NOT: there
is no desc for it, and the announcement text is the same boilerplate. The opinion
lives inside the audit report PDF attached to the results filing.

That makes it STRUCTURALLY UNAVAILABLE for every symbol -- the same category as
the three-year leverage test, not this symbol's opacity -- so it is recorded in
not_evaluated and named in the verdict, never silently dropped and never allowed
to block the whole universe. A PASS therefore does not assert a clean opinion,
and says so wherever it is read.
"""

from __future__ import annotations

import re
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from urllib.parse import quote
from datetime import date, datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("TSL-TIER1-FETCH")

STORE = ROOT / "data/processed/tier1_sources.json"
SCHEMA = 1

SHP_MASTER = ("https://www.nseindia.com/api/corporate-share-holdings-master"
              "?index=equities&symbol={symbol}")
ANNOUNCEMENTS = ("https://www.nseindia.com/api/corporate-announcements"
                 "?index=equities&symbol={symbol}"
                 "&from_date={frm}&to_date={to}")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Tier 1 compares holdings[0] against holdings[1] -- pledge rising and promoter
# holding falling are both quarter-on-quarter. Two is what the rules consume; a
# third would add 460 x 520 KB of XBRL for nothing.
QUARTERS_WANTED = 2

# AUDITOR_LOOKBACK_QTRS in fundamentals.py is 4, i.e. one year of announcements.
LOOKBACK_DAYS = 370

REQUEST_DELAY = 0.35
TIMEOUT = 60
RETRIES = 2

# Same failure mode as the annual fetch: NSE degrades its payload under rapid
# requests rather than refusing, so a rate-limited run does not crash -- it marks
# every symbol unparseable and the resulting "Tier 1 admits nobody" looks like a
# finding about the market.
MAX_CONSECUTIVE_EMPTY = 10

# ── the SHP taxonomy, by context ──────────────────────────────────
PROMOTER_CONTEXT_KEY = "ShareholdingOfPromoterAndPromoterGroup"
EL_PLEDGE_PCT   = "EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares"
# TOTAL encumbrance, of which pledge is one kind. RECORDED, NOT GATED: VEDL's
# Jun-2026 filing reports pledge 0% and total encumbrance 100% of promoter
# holding, all of it under non-disposal undertakings and "other encumbrances".
# Economically that is close to a pledge and Tier 1's rule names pledge, so the
# gap is stored rather than resolved here -- widening the rule is a decision, not
# a parser detail, and it cannot be made from a number nobody has looked at.
EL_ENCUMBERED_PCT = "EncumberedSharesHeldAsPercentageOfTotalNumberOfShares"
EL_PLEDGE_NUM   = "NumberOfSharesEncumberedUnderPledged"
EL_SHARES       = "NumberOfFullyPaidUpEquityShares"
EL_HOLDING_PCT  = "ShareholdingAsAPercentageOfTotalNumberOfShares"
EL_PLEDGE_FLAG  = ("WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"
                   "ForPromoterAndPromoterGroup")
EL_PLEDGE_FLAG_ALT = "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"

# Percentage points of disagreement tolerated between the reported pledge
# fraction and the one computed from the share counts. Rounding in the filing is
# real (4 decimal places on a fraction is 0.01 of a point); anything larger means
# the context match is wrong and the number must not be used.
PLEDGE_CROSSCHECK_TOL = 0.05

# ── auditor classification, from the structured desc only ─────────
AUDITOR_VETO_DESCS = (
    "resignation of statutory auditor",
    "initiation of forensic audit",
)
# Counted, never a veto. See the module docstring.
AUDITOR_NOTED_DESCS = (
    "change in auditors",
)

# ── MECHANICAL DILUTION: WHY A PROMOTER PERCENTAGE FELL ───────────
# A promoter stake can fall because insiders are leaving, or because of a
# corporate action that moves the number without anyone losing conviction. Tier 1
# should veto the first and not the second, so it needs the CAUSE.
#
# ONLY THESE TWO DESCS QUALIFY, and the two rejected candidates matter more than
# the two accepted ones:
#
#   "Disclosure under SEBI Takeover Regulations" is filed by ALL SIX symbols whose
#   promoter holding fell -- SHRIRAMFIN, DOMS, NHPC, BELRISE, BLUEJET and
#   PREMIERENE. It is filed BECAUSE the holding changed, so it is a consequence of
#   the drop, not a cause of it, and using it would excuse every case including
#   the genuine ones. It discriminates nothing.
#
#   "Allotment of Securities" appears for SHRIRAMFIN (3) and PREMIERENE. It is
#   routine ESOP and bond allotment. Counting it would excuse SHRIRAMFIN -- the
#   one case this mechanism exists to catch.
#
# Of the six, only NHPC filed an actual Offer for sale, which is exactly the
# government divestment that should not read as insiders leaving.
DILUTION_DESCS = (
    "offer for sale",
    "qualified institutional placement",
)



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
    s.headers.update({"User-Agent": UA,
                      "Accept": "application/json,text/plain,*/*"})
    try:
        s.get("https://www.nseindia.com", timeout=TIMEOUT)
    except Exception as e:
        log.warning(f"could not warm the NSE session ({e}) — continuing")
    return s


def _get(s, url: str, tries: int = RETRIES, quiet: bool = False):
    """None on failure. A 4xx will not become a 200, so it is not retried."""
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
        log.warning(f"gave up on {url[:90]}: {last}")
    return None


def _is_url(v) -> bool:
    """The same placeholder trap as the annual fetch: .../xbrl/- is truthy."""
    return (isinstance(v, str) and v.startswith("http")
            and v.rstrip("/").lower().endswith(".xml"))


def _num(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════
# SHP XBRL
# ══════════════════════════════════════════════════════════════════

def _facts(xml: str, element: str) -> list:
    """[(contextRef, raw text)] for every occurrence, attribute order agnostic."""
    out = []
    for m in re.finditer(rf"<(?:[\w.-]+:)?{element}\b([^>]*)>([^<]*)<", xml):
        attrs, val = m.group(1), m.group(2)
        cm = re.search(r'contextRef\s*=\s*"([^"]+)"', attrs)
        out.append((cm.group(1) if cm else "", val.strip()))
    return out


def _promoter_context(xml: str, element: str) -> tuple:
    """
    (value, context, why) for `element` under the promoter-group context.

    Requires EXACTLY ONE matching context. Zero means the taxonomy moved; more
    than one means the key is no longer specific and picking would be a guess.
    Both return None, which blocks -- never a silent choice.
    """
    hits = [(c, v) for c, v in _facts(xml, element)
            if PROMOTER_CONTEXT_KEY.lower() in c.lower()]
    if not hits:
        return None, "", f"{element}: no {PROMOTER_CONTEXT_KEY} context"
    if len(hits) > 1:
        ctxs = ", ".join(c for c, _ in hits[:4])
        return None, "", (f"{element}: {len(hits)} promoter contexts ({ctxs}) — "
                          f"ambiguous, refusing to pick")
    return _num(hits[0][1]), hits[0][0], ""


def _flag(xml: str, element: str):
    """True / False / None. Only an explicit false is a stated zero."""
    for _, v in _facts(xml, element):
        t = v.strip().lower()
        if t in ("true", "1", "yes"):
            return True
        if t in ("false", "0", "no"):
            return False
    return None


def parse_shp(xml: str) -> tuple:
    """
    (promoter_pct, pledged_pct, encumbered_pct, notes). Either of the first two
    may be None, which blocks. encumbered_pct is recorded only.

    pledged_pct is a percentage of PROMOTER HOLDING, cross-checked against the
    share counts. promoter_pct is read here too so it can be checked against the
    master JSON -- two independent readings of the same number.
    """
    notes = []

    holding, _, why = _promoter_context(xml, EL_HOLDING_PCT)
    if holding is None:
        notes.append(why or "promoter holding % not readable")
        promoter_pct = None
    else:
        promoter_pct = round(holding * 100.0, 4)

    shares, _, _ = _promoter_context(xml, EL_SHARES)
    pledged_num, _, _ = _promoter_context(xml, EL_PLEDGE_NUM)
    reported, _, why_pct = _promoter_context(xml, EL_PLEDGE_PCT)

    flag = _flag(xml, EL_PLEDGE_FLAG)
    if flag is None:
        flag = _flag(xml, EL_PLEDGE_FLAG_ALT)

    pledged_pct = None
    if reported is None and pledged_num is None:
        # No value elements at all. The boolean decides whether that is a stated
        # zero or opacity -- this is the branch "missing is never a pass" is
        # aimed at, and the only thing that makes a zero legitimate.
        if flag is False:
            pledged_pct = 0.0
            notes.append("no encumbrance elements, and the filing states "
                         "WhetherAnySharesHeldByPromotersAreEncumberedUnderPledged"
                         "=false — an explicit zero, not an absence")
        elif flag is True:
            notes.append("filing states shares ARE pledged but carries no "
                         "encumbrance value under the promoter context — "
                         "unparseable, NOT zero")
        else:
            notes.append("no encumbrance elements and no pledge boolean — "
                         "unparseable, NOT zero")
    else:
        computed = None
        if pledged_num is not None and shares:
            computed = pledged_num / shares * 100.0
        if reported is not None and computed is not None:
            gap = abs(reported * 100.0 - computed)
            if gap > PLEDGE_CROSSCHECK_TOL:
                notes.append(f"pledge cross-check FAILED: reported "
                             f"{reported * 100:.4f}% vs {computed:.4f}% computed "
                             f"from {pledged_num:,.0f}/{shares:,.0f} shares "
                             f"(gap {gap:.4f} pts > {PLEDGE_CROSSCHECK_TOL}) — "
                             f"the context match is wrong, refusing the value")
            else:
                pledged_pct = round(reported * 100.0, 4)
                notes.append(f"pledge {pledged_pct:.2f}% of promoter holding, "
                             f"cross-checked against {pledged_num:,.0f}/"
                             f"{shares:,.0f} shares (gap {gap:.4f} pts)")
        elif reported is not None:
            pledged_pct = round(reported * 100.0, 4)
            notes.append(f"pledge {pledged_pct:.2f}% of promoter holding, "
                         f"reported only — share counts absent, not cross-checked")
        elif computed is not None:
            pledged_pct = round(computed, 4)
            notes.append(f"pledge {pledged_pct:.2f}% computed from "
                         f"{pledged_num:,.0f}/{shares:,.0f} shares — "
                         f"{why_pct or 'reported percentage absent'}")

        if pledged_pct is not None and flag is False and pledged_pct > 0:
            notes.append(f"NOTE: boolean says not pledged but the filing "
                         f"reports {pledged_pct:.2f}% — taking the number")

    enc, _, _ = _promoter_context(xml, EL_ENCUMBERED_PCT)
    encumbered_pct = round(enc * 100.0, 4) if enc is not None else None
    if (encumbered_pct is not None and pledged_pct is not None
            and encumbered_pct - pledged_pct > 1.0):
        notes.append(f"total promoter encumbrance {encumbered_pct:.2f}% exceeds "
                     f"pledge {pledged_pct:.2f}% — the difference is non-disposal "
                     f"undertakings and other encumbrances, which Tier 1's rule "
                     f"does not cover. Recorded, not gated")
    return promoter_pct, pledged_pct, encumbered_pct, notes


# ══════════════════════════════════════════════════════════════════
# ANNOUNCEMENTS
# ══════════════════════════════════════════════════════════════════

def classify_announcements(rows: list) -> tuple:
    """
    (veto_flags, noted, dilution) from the STRUCTURED desc only.

    Free text is not consulted: for auditor rows it restates the category
    verbatim, so matching on it adds false positives and no recall.
    """
    veto, noted, dilution = [], [], []
    for r in rows or []:
        d = (r.get("desc") or "").strip()
        dl = d.lower()
        stamp = (r.get("sort_date") or r.get("an_dt") or "")[:10]
        if any(k in dl for k in AUDITOR_VETO_DESCS):
            veto.append(f"{d} ({stamp})")
        elif any(k in dl for k in AUDITOR_NOTED_DESCS):
            noted.append(f"{d} ({stamp})")
        if any(k in dl for k in DILUTION_DESCS):
            dilution.append(f"{d} ({stamp})")
    return veto, noted, dilution


# ══════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════

def load_store(path: Path = STORE) -> dict:
    if not path.exists():
        return {"schema": SCHEMA, "symbols": {}}
    try:
        d = json.loads(path.read_text())
    except Exception as e:
        log.error(f"tier 1 store unreadable ({e}) — starting empty")
        return {"schema": SCHEMA, "symbols": {}}
    if d.get("schema") != SCHEMA:
        log.warning(f"tier 1 store is schema {d.get('schema')}, want {SCHEMA} — "
                    f"starting empty")
        return {"schema": SCHEMA, "symbols": {}}
    d.setdefault("symbols", {})
    return d


def save_store(store: dict, path: Path = STORE) -> None:
    store["schema"] = SCHEMA
    store["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, indent=1, sort_keys=True))
    tmp.replace(path)


# ══════════════════════════════════════════════════════════════════
# FETCH
# ══════════════════════════════════════════════════════════════════

def fetch_symbol(s, symbol: str, verbose: bool = False) -> dict:
    """
    One symbol's Tier 1 sources. Never raises; every failure is recorded in a
    way that makes fundamentals.tier1() block rather than pass.
    """
    out = {"holdings": [], "auditor": None, "dilution": None,
           "notes": [], "status": "ok"}

    # ── shareholding ──────────────────────────────────────────────
    r = _get(s, SHP_MASTER.format(symbol=_q(symbol)))
    if r is None:
        out["status"] = "no_shareholding"
        out["notes"].append("shareholding master request failed")
    else:
        try:
            rows = r.json()
        except Exception as e:
            rows = []
            out["notes"].append(f"shareholding master not JSON: {e}")
        rows = rows if isinstance(rows, list) else (rows.get("data") or [])
        usable = [x for x in rows if _is_url(x.get("xbrl"))][:QUARTERS_WANTED]
        if not usable:
            out["status"] = "no_shareholding"
            out["notes"].append(f"{len(rows)} shareholding row(s), none with a "
                                f"real SHP xbrl url")
        for row in usable:
            time.sleep(REQUEST_DELAY)
            as_of = (row.get("date") or "").strip()
            master_pct = _num(row.get("pr_and_prgrp"))
            h = {"as_of": as_of, "promoter_pct": master_pct,
                 "pledged_pct": None, "encumbered_pct": None,
                 "source": row.get("xbrl"), "notes": []}
            if row.get("revisedStatus") not in (None, "", "-"):
                h["notes"].append(f"filing marked revised: {row.get('revisedStatus')}")
            rx = _get(s, row["xbrl"])
            if rx is None:
                h["notes"].append("SHP xbrl request failed — pledge unknown")
            else:
                p_xbrl, pledged, encumbered, notes = parse_shp(rx.text)
                h["pledged_pct"] = pledged
                h["encumbered_pct"] = encumbered
                h["notes"].extend(notes)
                # two independent readings of promoter %; disagreement is
                # recorded and the master value kept, since tier 1's drop test
                # compares master to master and must stay on one source.
                if p_xbrl is not None and master_pct is not None:
                    if abs(p_xbrl - master_pct) > 0.05:
                        h["notes"].append(f"promoter % disagrees: master "
                                          f"{master_pct}% vs xbrl {p_xbrl}%")
                elif master_pct is None and p_xbrl is not None:
                    h["promoter_pct"] = p_xbrl
                    h["notes"].append("promoter % taken from the xbrl; absent "
                                      "from the master row")
            out["holdings"].append(h)
            if verbose:
                log.info(f"  {symbol} {as_of}: promoter={h['promoter_pct']} "
                         f"pledge={h['pledged_pct']} "
                         f"encumbered={h['encumbered_pct']}")
                for n in h["notes"]:
                    log.info(f"      {n}")

    # ── announcements ─────────────────────────────────────────────
    time.sleep(REQUEST_DELAY)
    today = date.today()
    frm = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%d-%m-%Y")
    ra = _get(s, ANNOUNCEMENTS.format(symbol=_q(symbol), frm=frm,
                                      to=today.strftime("%d-%m-%Y")))
    if ra is None:
        # auditor stays None, which fundamentals.tier1() reads as
        # "announcements not checked" and blocks. Deliberate.
        out["notes"].append("announcements request failed — auditor not checked")
        if out["status"] == "ok":
            out["status"] = "no_announcements"
    else:
        try:
            arows = ra.json()
        except Exception as e:
            arows = None
            out["notes"].append(f"announcements not JSON: {e}")
        if arows is not None:
            arows = arows if isinstance(arows, list) else (arows.get("data") or [])
            veto, noted, dilution = classify_announcements(arows)
            out["auditor"] = {"flags": veto, "noted": noted,
                              "rows_scanned": len(arows),
                              "window_days": LOOKBACK_DAYS,
                              "from": frm}
            out["dilution"] = dilution
            if verbose:
                log.info(f"  {symbol} announcements: {len(arows)} rows, "
                         f"{len(veto)} veto, {len(noted)} noted")
    return out


def run(symbols: list, refetch: bool = False, verbose: bool = False,
        path: Path = STORE) -> dict:
    store = load_store(path)
    have = store["symbols"]
    todo = symbols if refetch else [x for x in symbols if x not in have]
    log.info(f"{len(symbols)} symbol(s) requested, {len(todo)} to fetch "
             f"({len(symbols) - len(todo)} already in the store)")
    s = _session()
    empties = 0
    for i, sym in enumerate(todo, 1):
        rec = fetch_symbol(s, sym, verbose=verbose)
        have[sym] = rec
        save_store(store, path)          # resumable: written after every symbol
        usable = bool(rec["holdings"]) and rec["auditor"] is not None
        empties = 0 if usable else empties + 1
        if i % 10 == 0 or i == len(todo) or verbose:
            ok_n = sum(1 for v in have.values()
                       if v.get("holdings") and v.get("auditor") is not None)
            log.info(f"{i:>4}/{len(todo)}  last={sym} {rec['status']}  "
                     f"usable so far {ok_n}")
        if empties >= MAX_CONSECUTIVE_EMPTY:
            log.error(f"{empties} symbols in a row returned nothing usable — "
                      f"almost certainly rate limiting, not {empties} bad "
                      f"symbols. Stopping so the store is not poisoned; rerun "
                      f"to resume from {sym}.")
            break
        time.sleep(REQUEST_DELAY)
    return store


def report(path: Path = STORE) -> int:
    store = load_store(path)
    syms = store["symbols"]
    if not syms:
        print("tier 1 store is empty")
        return 1
    n = len(syms)
    two_q = sum(1 for v in syms.values() if len(v.get("holdings", [])) >= 2)
    pledge_known = sum(1 for v in syms.values()
                       if v.get("holdings")
                       and v["holdings"][0].get("pledged_pct") is not None)
    ann = sum(1 for v in syms.values() if v.get("auditor") is not None)
    dil = [k for k, v in syms.items() if v.get("dilution")]
    flagged = [k for k, v in syms.items() if (v.get("auditor") or {}).get("flags")]
    noted = [k for k, v in syms.items() if (v.get("auditor") or {}).get("noted")]
    print("=" * 78)
    print(f"TIER 1 SOURCES — {n} symbol(s), written {store.get('written_at','?')}")
    print("=" * 78)
    print(f"  {two_q:>4}/{n}  have {QUARTERS_WANTED} quarters of shareholding")
    print(f"  {pledge_known:>4}/{n}  pledge % readable for the latest quarter")
    print(f"  {ann:>4}/{n}  announcements checked")
    print(f"  {len(flagged):>4}      auditor VETO flag "
          f"({', '.join(flagged[:10])}{'...' if len(flagged) > 10 else ''})")
    print(f"  {len(noted):>4}      'Change in Auditors' noted, NOT a veto "
          f"({', '.join(noted[:8])}{'...' if len(noted) > 8 else ''})")
    print(f"  {len(dil):>4}      OFS/QIP in the window (explains a promoter drop) "
          f"({', '.join(dil[:8])}{'...' if len(dil) > 8 else ''})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--symbol", action="append", default=[])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refetch", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if a.report:
        return report()
    if a.all:
        from engine.universe import ALL_SYMBOLS
        syms = list(ALL_SYMBOLS)
    else:
        syms = a.symbol
    if not syms:
        ap.error("give --all or --symbol")
    if a.limit:
        syms = syms[:a.limit]
    run(syms, refetch=a.refetch, verbose=a.verbose)
    return report()


if __name__ == "__main__":
    sys.exit(main())
