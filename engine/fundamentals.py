"""
TSL — Fundamentals Layer (Tier 1 gates; Tier 2 measures)
========================================================
TIER 1 IS THE ONLY GATE. It vetoes on DETERIORATION -- promoter pledge, auditor
departure, promoter exit, rising leverage. These are the risks a technical stop
cannot protect against: they do not show up as a gentle drift through a stop
level, they show up as a gap.

TIER 2 COMPUTES AND RECORDS. ROE, D/E and free cash flow are measured on every
signal and stored alongside the verdict, and they NEVER block. This is not a
half-finished gate and not a deprecation:

  Tier 2 asserts that quality predicts outcomes. On annual data lagging up to
  two years, against positions held for weeks. Nobody has tested that claim on
  this book. Switching it on would cut the universe to roughly a third on an
  unvalidated belief -- and at present it is measuring its own parser more than
  it is measuring the market.

  Keeping the computation is what makes the question answerable. Each verdict
  carries what Tier 2 measured and whether it WOULD have vetoed, so after a few
  hundred resolved trades the book can be asked directly: did the high-ROE names
  outperform here? Then the gate goes on with evidence, or comes off knowing
  why. Deleting the computation means guessing again in six months.

Consulted at signal time as a gate, from a cache, so it costs nothing in the
market-hours loop.

    python3 -m engine.fundamentals_fetch --all      # annual filings  -> store
    python3 -m engine.tier1_fetch --all            # shareholding + announcements
    python3 -m engine.fundamentals --refresh        # join both -> the gate cache
    python3 -m engine.fundamentals --report         # verdict distribution
    python3 -m engine.fundamentals --symbol RELIANCE   # one symbol, full detail

The two fetches hit the network; --refresh never does.

THREE VERDICTS, NOT TWO
-----------------------
    PASS          every rule that COULD be evaluated passed
    VETO          a rule fired, or a required filing is genuinely absent
    UNPARSEABLE   this symbol's data is missing where its peers have it

A PASS may carry NOT-EVALUATED CHECKS, and says so in its reason. That third
category is forced by the data: a rule needing three annual filings cannot be
evaluated for ANY symbol, because only two years are parseable. Treating that
like opacity would make every verdict UNPARSEABLE and the gate would block the
whole universe forever -- at which point the layer gets switched off, which is
worse than a pass that states what it did not check.

So the distinction is whose fault the absence is:

    structurally unavailable   no symbol has it (too few filing periods, or an
                               explicit sector exemption). Recorded on the
                               verdict, named in the reason, does not block.
    absent for this symbol     peers have the field and this one does not.
                               UNPARSEABLE, blocks.

UNPARSEABLE blocks trading exactly like VETO -- absence of confirmation is not
permission -- but it is recorded separately, and that distinction is the whole
point. A rising UNPARSEABLE count is a PARSER REGRESSION, not a deteriorating
market. Folded into VETO, an NSE taxonomy change would look like the universe
going bad and would empty it silently, which is what live_prices returning
200 + [] did for eleven weeks.

WHERE THE NUMBERS COME FROM, AND AT WHAT CADENCE
------------------------------------------------
Established by probing NSE directly, not assumed:

  QUARTERLY results XBRL     P&L ONLY. 80-103 elements: RevenueFromOperations,
  api/corporates-financial-  ProfitLossForPeriod, tax, OtherExpenses, share
  results?period=Quarterly   capital. Checked six filings at random: ZERO
                             contain borrowings, equity, cash flow or assets.
                             PaidUpValueOfEquityShareCapital is face value, not
                             net worth.

  ANNUAL results XBRL        THE FULL STATEMENT SET, ~240 elements. Equity,
  ...?period=Annual          BorrowingsCurrent/Noncurrent, the cash flow
                             statement. This is the only source for leverage,
                             ROE and free cash flow -- AND IT STOPS AT FY2024.
                             See below.

THE ANNUAL DATA IS TWO TO THREE YEARS STALE, AND THAT IS THE SOURCE, NOT US
--------------------------------------------------------------------------
Measured across the 417 symbols with filings, in September 2026:

    newest filing FY2024   197 symbols
    newest filing FY2023   217
    newest filing FY2022     3
    ------------------------------------
    nothing newer than FY2024:  417 of 417  (100%)

    3 annual years parseable:     9 of 417
    2 years:                    359
    1 year:                      49

RELIANCE's period=Annual listing has no FY2025 or FY2026 row AT ALL -- not a
placeholder, absent. The placeholder xbrl urls in that listing are FY2018 and
older. So the endpoint stopped carrying annual filings after FY2024, which lines
up with SEBI's Integrated Filing replacing the separate annual submission from
the quarter ending Dec-2024. Recovering current balance sheets means a new source
(the Integrated Filing- Financial route), not a parser change.

WHAT THAT DOES TO TIER 1, stated plainly because it is easy to miss:

    CURRENT (weeks old)     promoter pledge and promoter holding, from Jun-2026
                            shareholding; auditor resignation, from announcements
                            through Sep-2026. These are the gap risks, and they
                            are the checks doing the work.
    2-3 YEARS STALE         rising leverage, negative operating cash flow. And
                            rising leverage needs three filings, which only 9 of
                            417 symbols have, so it is almost always
                            not_evaluated regardless.

So the deterioration veto is in practice a SHAREHOLDING AND AUDITOR veto. That is
not a defect of the design -- those are precisely the signals a technical stop
cannot protect against -- but a reader must not imagine the balance-sheet half is
contributing much.

  SHAREHOLDING               promoter % inline as pr_and_prgrp, quarterly, no
  api/corporate-share-       XBRL parse needed. Pledge needs the linked SHP
  holdings-master            XBRL (~135 KB per company per quarter).

  ANNOUNCEMENTS              auditor resignation and qualified opinions are FREE
  api/corporate-             TEXT. Keyword classification, best effort, and a
  announcements              miss must never read as a pass.

HALF-YEARLY DOES NOT EXIST AS A CURRENT SOURCE. The brief asked for the two
leverage/cash-flow rules restated half-yearly rather than dropped. That cannot
be done: period=Half-Yearly returns 0 records by date range, and per symbol
returns filings from 2008-2010 -- pre-IndAS, 11 elements, none of the facts
needed. The available restatement is ANNUAL, so those two rules carry a
two-year lag rather than a one-year one, and CADENCE_ANNUAL is written into
their reason strings so the lag is visible wherever a verdict is read.

TWO PARSING TRAPS, BOTH FOUND IN REAL FILINGS
---------------------------------------------
1. DebtEquityRatio IS REPORTED AND IS USELESS. RELIANCE FY2024 reports it as
   0.00 while its actual leverage is 0.35. The ratio is computed here from
   Equity and Borrowings, never read from that element.

2. CONTEXT DATES LIE. In RELIANCE's FY2024 annual filing, contexts OneD and
   FourD both claim 2024-01-01..2024-03-31, but ProfitLossForPeriod is
   212,430,000,000 under OneD and 790,200,000,000 under FourD. The second is
   the full-year figure -- Rs79,020 crore, which is the published FY24 PAT.
   Selecting a duration context by its dates therefore picks the QUARTER and
   computes ROE at a quarter of its true value, which looks entirely plausible.

   So durations are resolved by the taxonomy's column convention and
   cross-checked, and an ambiguous resolution is UNPARSEABLE rather than a
   guess. Instant contexts (balance sheet items) are unaffected -- there is
   only one balance-sheet date.

THRESHOLDS ARE STARTING POINTS, STATED SO THEY CAN BE TESTED. The 8% entry
distance and the 82-score gate were both set by reasoning and both wrong.
"""

from __future__ import annotations

import re
import sys
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("TSL-FUNDAMENTALS")

# The INDAS taxonomy's column ids. An annual filing carries exactly two duration
# contexts: the final QUARTER and the full YEAR, and FourD is the year. Verified
# across 7 filings -- RELIANCE, TCS, ITC, INFY, HINDUNILVR, MARUTI, WIPRO -- in
# every one of which FourD held the audited annual figure.
ANNUAL_DURATION_CONTEXT = "FourD"

# Bump when the parser or the rules change. A cached verdict carrying an older
# version is stale by definition -- otherwise a fixed parser leaves old wrong
# vetoes in place, and a changed threshold is applied to nothing.
PARSER_VERSION = 3

CACHE = ROOT / "data/processed/fundamentals.json"

VERDICT_PASS = "PASS"
VERDICT_VETO = "VETO"
VERDICT_UNPARSEABLE = "UNPARSEABLE"

# Written into every reason string that depends on a filing, so the lag is
# visible at the point a verdict is read rather than buried in this file.
CADENCE_QUARTERLY = "quarterly"
CADENCE_ANNUAL = "annual"

# ── TIER 1: deterioration veto. THE GATE. Strict, no exceptions ───
PLEDGE_MAX_PCT          = 25.0   # or rising materially, see the floors below
PROMOTER_DROP_MAX_PCT   = 5.0    # in a single quarter

# ── MATERIALITY FLOORS: A ROUNDING DIFFERENCE IS NOT DETERIORATION ─
# "Rising" and "fell" with no floor read noise as a signal. Measured on the
# Jun-2026 filings, the unfloored pledge rule vetoed SUNPHARMA for moving 1.4% ->
# 1.6%, PTCIL for 0.0% -> 0.1% and JSL for 0.5% -> 0.6%. Nine of fourteen rising
# vetoes were that shape. A veto has to cost something to be worth having.
#
# The floor is the LARGER of an absolute and a relative move, so it stays
# meaningful at both ends: 2 points matters when pledge is small, and 25% of the
# existing level matters when it is already large (a promoter at 40% adding 2
# points is a smaller change in kind than one at 2% adding 2 points).
PLEDGE_RISE_MIN_PTS     = 2.0
PLEDGE_RISE_MIN_REL     = 0.25

# The same shape for promoter exit, and for the same reason: of the six symbols
# the unfloored rule vetoed, four (BELRISE, BLUEJET, DOMS, PREMIERENE) were recent
# listings hitting lock-in expiry and one (NHPC) was a government offer for sale.
# Neither is an insider leaving. A promoter at 70% selling 7 points has parted
# with a tenth of their stake; the relative floor is what distinguishes that from
# a promoter genuinely stepping back.
PROMOTER_DROP_MIN_REL   = 0.25
DE_RISING_PERIODS       = 2      # consecutive ANNUAL filings, see above
CFO_NEGATIVE_PERIODS    = 2      # consecutive ANNUAL filings, see above
AUDITOR_LOOKBACK_QTRS   = 4

# The auditor rule is "resignation OR qualified opinion". Only the first half is
# readable: NSE's announcement vocabulary has "Resignation of Statutory Auditor"
# and "Initiation of Forensic Audit" as structured categories, but NOTHING for an
# audit opinion -- that lives inside the PDF attached to the results filing, and
# the announcement's own text is boilerplate restating the category.
#
# So it is structurally unavailable for EVERY symbol, like the three-year
# leverage test, and it goes into not_evaluated rather than being dropped. A PASS
# must not be read as asserting a clean audit opinion.
OPINION_NOT_READABLE = (
    "qualified/adverse audit opinion not evaluable: NSE announcements carry no "
    "such category and their free text is boilerplate — the opinion is inside "
    "the audit report PDF. Auditor RESIGNATION was checked; the opinion was not")

# ── TIER 2: quality floor. MEASURED, NOT ENFORCED ─────────────────
# These thresholds no longer decide anything. They are retained as the
# would_veto yardstick so the record says what the gate WOULD have rejected --
# which is the whole experiment. Changing one changes the recorded
# counterfactual, not who trades.
#
# Deliberately looser than Buffett's own:
# 15% ROE and D/E under 0.5 are calibrated for permanent ownership of a handful
# of businesses. This is a rotating book with a technical entry and a structural
# stop. D/E under 0.5 also excludes most Indian banks and NBFCs structurally --
# a sector exclusion disguised as a quality test.
# ROE floor 10%, not 12%, set against the measured spread rather than a guess.
# Over a 60-symbol spread the average-ROE distribution was p25 10.5%, p50 16.1%,
# p75 18.0%, and the coverage cliff is in the wrong place for 12%:
#     > 10%  79% pass
#     > 12%  62% pass      <- 17 points of coverage for 2 points of ROE
#     > 15%  56% pass      <-  6 points for 3 points of ROE
# 12% sat just above the lower quartile, so it was cutting the middle rather
# than the bottom.
ROE_MIN_PCT             = 10.0

# TWO years, not three. Only FY2023 and FY2024 are parseable from NSE's annual
# XBRL -- 73% of symbols yield 2 years, 12% yield 1, 15% none -- so a 3-year
# window is unsatisfiable for everyone and waiting for FY2025 costs months. Two
# years still says something, and the window is named in every reason string so
# nobody reads a 2-year average as a 3-year one.
ROE_AVERAGE_YEARS       = 2
DE_MAX                  = 1.0
FCF_WINDOW_YEARS        = 2
# BOTH years, stated that way. "2 of 3" over a two-year window reads as looser
# than it is -- it is in fact every year available.
FCF_POSITIVE_YEARS      = 2

# Sectors exempt from the TIER 2 leverage test.
#
# Banks and NBFCs do not report BorrowingsCurrent/Noncurrent: deposits are a
# liability but they are not borrowings, and leverage for a lender is not
# comparable to leverage for a manufacturer. Measured over a 60-symbol spread,
# D/E was uncomputable for 12 of 51 symbols and EVERY ONE was banking or
# finance, while the overall parse rate was identical at 85% for financials and
# non-financials -- so banks file perfectly well, the field simply does not
# exist for them.
#
# Left in place, the leverage test would veto a quarter of the universe on
# missing data, which is the "sector exclusion disguised as a quality test" the
# looser threshold was chosen to avoid. The exemption is RECORDED on the verdict
# so it can never be mistaken for having passed the test.
#
# TIER 1's rising-leverage test is NOT exempt. A change in leverage is
# meaningful even where the level is not comparable, so it runs wherever the
# data exists.
# ── WHO IS EXEMPT FROM A LEVERAGE TEST, AND WHY ───────────────────
# THE PRINCIPLE IS: ENTITIES WHOSE LIABILITIES ARE NOT DEBT. Not deposit-takers
# specifically -- that was too literal a reading of it, and it is the principle
# that decides membership.
#
# For a bank or an NBFC the liability funding the book is deposits and borrowings
# raised to on-lend, not debt in the sense D/E measures. For an insurer it is
# POLICYHOLDER FLOAT -- reserves against future claims, which are a liability but
# categorically not borrowing. In both cases NSE reports no
# BorrowingsCurrent/Noncurrent at all, and vetoing LICI or HDFCLIFE for "missing
# borrowings" would be precisely the false signal that narrowing this exemption
# was meant to prevent: reading a category error as opacity.
#
# It used to be keyed on the BANKING/FINANCE sector tag, which covered 103 of 460
# symbols and swept in businesses the argument says nothing about: ADANIPORTS,
# CONCOR, INDIGO, DELHIVERY, BLUEDART, GESHIP, SCI, 3MINDIA, GODREJIND, REDINGTON,
# MMTC, JSWINFRA -- plus exchanges and registrars (BSE, MCX, CDSL, IEX, CRISIL,
# KFINTECH, CAMS) which are financial but hold no deposits and whose leverage is
# perfectly meaningful. A sector tag was standing in for a balance-sheet fact.
#
# So the exemption is now an EXPLICIT LIST of lending businesses. A list is the
# honest shape: membership is a claim about what the company does, it cannot be
# inferred from the missing data it is meant to excuse without circularity, and a
# name has to be argued for rather than inherited from a label.
#
# Deliberately NOT exempt, though all were under the sector tag: asset managers
# (HDFCAMC, UTIAMC, NAM-INDIA, ABSLAMC), exchanges and depositories (BSE, MCX,
# CDSL, IEX), registrars and rating agencies (KFINTECH, CAMS, CRISIL), brokers
# and wealth managers (ANGELONE, MOTILALOFS, NUVAMA, 360ONE, POLICYBZR) and
# investment holding companies (BAJAJHLDNG, TATAINVEST). These are fee or
# commission businesses. Their liabilities ARE debt and their leverage means
# exactly what it says.
BANKS = frozenset({
    "AUBANK", "AXISBANK", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "CANBK",
    "CUB", "FEDERALBNK", "HDFCBANK", "ICICIBANK", "IDBI", "IDFCFIRSTB",
    "INDIANB", "INDUSINDBK", "J&KBANK", "KARURVYSYA", "KOTAKBANK", "MAHABANK",
    "PNB", "RBLBANK", "SBIN", "UNIONBANK",
})
# NBFCs, housing finance, gold loan, microfinance, vehicle and consumer lending.
# Holding companies are included only where the consolidated book IS the lender
# (CHOLAHLDNG over CHOLAFIN, BAJAJFINSV over BAJFINANCE); BAJAJHLDNG and
# TATAINVEST are pure investment holdings and are not here.
NBFCS = frozenset({
    "AADHARHFC", "AAVAS", "ABCAPITAL", "APTUS", "BAJAJFINSV", "BAJAJHFL",
    "BAJFINANCE", "CANFINHOME", "CGCL", "CHOLAFIN", "CHOLAHLDNG", "CREDITACC",
    "FIVESTAR", "HDBFS", "HOMEFIRST", "IFCI", "IIFL", "JIOFIN", "LICHSGFIN",
    "LTF", "M&MFIN", "MANAPPURAM", "MUTHOOTFIN", "PNBHOUSING", "POONAWALLA",
    "SAMMAANCAP", "SBFC", "SBICARD", "SHRIRAMFIN", "SUNDARMFIN",
    # PSU lending institutions -- RBI-registered NBFC-IFCs, and HUDCO an HFC.
    # These were missed on the first pass because the list was built from the
    # BANKING/FINANCE-tagged names and SYMBOL_SECTOR_MAP tags all five DEFENCE,
    # which is plainly wrong and is worth distrusting elsewhere too. Their
    # liabilities are bonds raised to on-lend, so the principle covers them
    # exactly: PFC's -74,699cr of "negative operating cash flow" is a loan book
    # growing, not a company running out of money.
    "HUDCO", "IREDA", "IRFC", "PFC", "RECLTD",
})
# Life, general and health insurers, plus holding companies whose consolidated
# book IS the insurer (MFSL over Max Life), on the same basis as CHOLAHLDNG.
# POLICYBZR is deliberately absent: an insurance BROKER earns commission and
# carries no float, so its leverage is ordinary and measurable.
INSURERS = frozenset({
    "GICRE", "GODIGIT", "HDFCLIFE", "ICICIGI", "ICICIPRULI", "LICI", "MFSL",
    "NIACL", "NIVABUPA", "SBILIFE", "STARHEALTH",
})
LENDERS = BANKS | NBFCS
# The exemption set. Everything here funds itself with liabilities that are not
# debt; nothing else is exempt.
LEVERAGE_EXEMPT = BANKS | NBFCS | INSURERS

# Kept only for the sector label in reason strings. NOT used to exempt anything.
FINANCIAL_SECTORS = ("BANKING", "FINANCE")


# ══════════════════════════════════════════════════════════════════
# FACTS
# ══════════════════════════════════════════════════════════════════

@dataclass
class AnnualFacts:
    """One annual filing, normalised. None means the element was not present."""
    fy: str = ""
    equity: float | None = None
    borrowings_current: float | None = None
    borrowings_noncurrent: float | None = None
    pat: float | None = None
    cfo: float | None = None
    capex: float | None = None
    source: str = ""

    @property
    def debt(self) -> float | None:
        parts = [x for x in (self.borrowings_current, self.borrowings_noncurrent)
                 if x is not None]
        return sum(parts) if parts else None

    @property
    def de(self) -> float | None:
        d, e = self.debt, self.equity
        if d is None or not e or e <= 0:
            return None
        return d / e

    @property
    def roe_pct(self) -> float | None:
        if self.pat is None or not self.equity or self.equity <= 0:
            return None
        return self.pat / self.equity * 100.0

    @property
    def fcf(self) -> float | None:
        """CFO less capex. Capex absent is NOT zero -- that would read a
        capital-hungry business as cash-generative."""
        if self.cfo is None or self.capex is None:
            return None
        return self.cfo - abs(self.capex)


@dataclass
class Shareholding:
    as_of: str = ""
    promoter_pct: float | None = None
    pledged_pct: float | None = None
    source: str = ""


@dataclass
class Verdict:
    symbol: str
    verdict: str
    reason: str
    as_of: str = ""
    tier: str = ""
    parser_version: int = PARSER_VERSION
    # Checks that could not run for a reason that is not this symbol's fault --
    # too few filing periods for anyone, or an explicit sector exemption. Named
    # on the verdict and in the reason so a PASS never implies they passed.
    not_evaluated: list = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def tradeable(self) -> bool:
        return self.verdict == VERDICT_PASS


# ══════════════════════════════════════════════════════════════════
# XBRL
# ══════════════════════════════════════════════════════════════════

# Element -> the fact we want. Long IFRS names, as they actually appear: the
# short form PurchaseOfPropertyPlantAndEquipment is ABSENT from these filings
# and only the ...ClassifiedAsInvestingActivities form is present, which is the
# kind of near-miss that silently yields None.
INSTANT_FACTS = {
    "Equity": "equity",
    "BorrowingsCurrent": "borrowings_current",
    "BorrowingsNoncurrent": "borrowings_noncurrent",
}
DURATION_FACTS = {
    "ProfitLossForPeriod": "pat",
    "CashFlowsFromUsedInOperatingActivities": "cfo",
}
CAPEX_ELEMENTS = (
    "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
    "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
)


def parse_contexts(xml: str) -> dict:
    """{context id: ('instant'|'duration', start, end)}."""
    out = {}
    for m in re.finditer(r'<xbrli:context id="([^"]+)">(.*?)</xbrli:context>',
                         xml, re.S):
        cid, body = m.group(1), m.group(2)
        inst = re.search(r"<xbrli:instant>([^<]+)", body)
        if inst:
            out[cid] = ("instant", inst.group(1), inst.group(1))
            continue
        sd = re.search(r"<xbrli:startDate>([^<]+)", body)
        ed = re.search(r"<xbrli:endDate>([^<]+)", body)
        if sd and ed:
            out[cid] = ("duration", sd.group(1), ed.group(1))
    return out


def facts_for(xml: str, element: str) -> list:
    """[(contextRef, float value)] for every occurrence of an element."""
    out = []
    for m in re.finditer(
            rf'<[\w.-]+:{re.escape(element)}\b[^>]*contextRef="([^"]+)"[^>]*>'
            rf'\s*([-\d.eE+]+)\s*<', xml):
        try:
            out.append((m.group(1), float(m.group(2))))
        except ValueError:
            continue
    return out


def pick_annual_duration(xml: str, element: str, contexts: dict) -> tuple:
    """
    (value, context, note) for the FULL-YEAR figure of a duration element.

    Chosen by CONTEXT ID, not by dates and not by magnitude.

    Dates cannot be trusted: OneD and FourD carry identical dates and different
    values. But magnitude is also wrong, and wrong in a knowable way. It was
    justified as "a full year cannot be smaller in absolute terms than a quarter
    within it" -- which is FALSE whenever quarters have opposite signs. A company
    with a large Q4 profit and a small full-year profit after three loss-making
    quarters has |Q4| > |year|, and magnitude then silently reports the QUARTER
    as the annual figure. Same for operating cash flow, where a strong Q4
    collection against a weak year is ordinary.

    ANNUAL_DURATION_CONTEXT is the taxonomy's year column and is used directly.
    Magnitude survives only as a fallback for filings that do not carry it, and
    the note records which path was taken so a reader is never guessing.
    """
    hits = [(c, v) for c, v in facts_for(xml, element)
            if contexts.get(c, ("",))[0] == "duration"]
    if not hits:
        return None, "", f"{element}: no duration context"
    for c, v in hits:
        if c == ANNUAL_DURATION_CONTEXT:
            return v, c, f"{element}: {ANNUAL_DURATION_CONTEXT} (annual column)"
    if len(hits) == 1:
        return hits[0][1], hits[0][0], f"{element}: single duration context"
    best = max(hits, key=lambda cv: abs(cv[1]))
    return (best[1], best[0],
            f"{element}: no {ANNUAL_DURATION_CONTEXT}; fell back to the largest "
            f"of {len(hits)} duration contexts, which is unreliable when "
            f"quarters have opposite signs")


def pick_instant(xml: str, element: str, contexts: dict) -> tuple:
    """(value, context, note) for a balance-sheet item: the LATEST instant."""
    hits = [(c, v) for c, v in facts_for(xml, element)
            if contexts.get(c, ("",))[0] == "instant"]
    if not hits:
        return None, "", f"{element}: no instant context"
    best = max(hits, key=lambda cv: contexts[cv[0]][1])   # latest date
    return best[1], best[0], f"{element}: instant {contexts[best[0]][1]}"


def parse_annual(xml: str, fy: str = "", source: str = "") -> tuple:
    """(AnnualFacts, [notes]). Never raises on content -- missing is None."""
    notes = []
    ctx = parse_contexts(xml)
    if not ctx:
        return AnnualFacts(fy=fy, source=source), ["no xbrli:context elements"]

    f = AnnualFacts(fy=fy, source=source)
    for element, attr in INSTANT_FACTS.items():
        v, _, note = pick_instant(xml, element, ctx)
        setattr(f, attr, v)
        notes.append(note)
    for element, attr in DURATION_FACTS.items():
        v, _, note = pick_annual_duration(xml, element, ctx)
        setattr(f, attr, v)
        notes.append(note)

    capex = None
    for element in CAPEX_ELEMENTS:
        v, _, note = pick_annual_duration(xml, element, ctx)
        notes.append(note)
        if v is not None:
            capex = (capex or 0.0) + abs(v)
    f.capex = capex
    return f, notes


# ══════════════════════════════════════════════════════════════════
# TIER 1 — deterioration veto
# ══════════════════════════════════════════════════════════════════

def sector_of(symbol: str) -> str:
    try:
        sys.path.insert(0, str(ROOT / "engine"))
        from universe import get_symbol_sector
        return (get_symbol_sector(symbol) or "").upper()
    except Exception:
        return ""


def is_leverage_exempt(symbol: str) -> bool:
    """
    True when the symbol's liabilities are not debt, so D/E is a category error
    rather than a measurement. See the LEVERAGE_EXEMPT comment for why this is an
    explicit list and not a sector test.
    """
    return symbol in LEVERAGE_EXEMPT


def exempt_kind(symbol: str) -> str:
    if symbol in BANKS:
        return "bank"
    if symbol in NBFCS:
        return "lender (NBFC)"
    if symbol in INSURERS:
        return "insurer"
    return ""


def exempt_phrase(symbol: str) -> str:
    kind = exempt_kind(symbol)
    return f"{'an' if kind[:1].lower() in 'aeiou' else 'a'} {kind}"


def is_lender_cfo_exempt(symbol: str) -> bool:
    """
    Banks and NBFCs only -- see the comment at the cash-flow check. Insurers are
    exempt from the LEVERAGE test but not this one: their liabilities are not debt,
    yet their operating cash flow still measures something real.
    """
    return symbol in LENDERS


def exempt_because(symbol: str) -> str:
    """The liability that is not debt, named so the exemption is never generic."""
    if symbol in INSURERS:
        return "policyholder float is a claims reserve, not borrowing"
    return "deposits and lending liabilities are not debt"


def tier1(symbol: str, annuals: list, holdings: list,
          auditor_flags: list) -> Verdict:
    """
    Strict. Any rule firing is a VETO; any rule that cannot be evaluated is
    UNPARSEABLE. `annuals` newest first, `holdings` newest first.
    """
    missing, skipped = [], []
    # Sub-threshold moves: the check ran and passed, but the movement is recorded
    # so "no deterioration signal" never means "nothing moved".
    immaterial = {}

    # 1. promoter pledge
    if not holdings:
        missing.append("no shareholding data")
    else:
        cur = holdings[0]
        if cur.pledged_pct is None:
            missing.append("pledge % not in the shareholding filing")
        else:
            if cur.pledged_pct > PLEDGE_MAX_PCT:
                return Verdict(symbol, VERDICT_VETO,
                               f"promoter pledge {cur.pledged_pct:.1f}% exceeds "
                               f"{PLEDGE_MAX_PCT:.0f}% ({CADENCE_QUARTERLY}, "
                               f"as of {cur.as_of})", cur.as_of, "TIER1")
            prev = next((h for h in holdings[1:] if h.pledged_pct is not None), None)
            if prev and cur.pledged_pct > prev.pledged_pct:
                move = cur.pledged_pct - prev.pledged_pct
                need = max(PLEDGE_RISE_MIN_PTS,
                           prev.pledged_pct * PLEDGE_RISE_MIN_REL)
                if move >= need:
                    return Verdict(symbol, VERDICT_VETO,
                                   f"promoter pledge rising {prev.pledged_pct:.1f}% "
                                   f"-> {cur.pledged_pct:.1f}% (+{move:.1f} pts, "
                                   f"floor {need:.1f}) ({CADENCE_QUARTERLY}, "
                                   f"{prev.as_of} -> {cur.as_of})",
                                   cur.as_of, "TIER1")
                # The check RAN and passed. Recorded so an immaterial drift is
                # visible rather than invisible -- a series of sub-threshold rises
                # is something a reader may want to see.
                immaterial["pledge_rise"] = (
                    f"pledge rose {prev.pledged_pct:.2f}% -> {cur.pledged_pct:.2f}% "
                    f"(+{move:.2f} pts), below the {need:.2f} pt floor")

    # 2. promoter holding down more than 5 points in a quarter
    if len(holdings) >= 2:
        cur, prev = holdings[0], holdings[1]
        if cur.promoter_pct is None or prev.promoter_pct is None:
            missing.append("promoter % missing in a shareholding filing")
        else:
            drop = prev.promoter_pct - cur.promoter_pct
            need = max(PROMOTER_DROP_MAX_PCT,
                       prev.promoter_pct * PROMOTER_DROP_MIN_REL)
            if drop > need:
                return Verdict(symbol, VERDICT_VETO,
                               f"promoter holding fell {drop:.1f} points "
                               f"({prev.promoter_pct:.1f}% -> {cur.promoter_pct:.1f}%, "
                               f"{drop / prev.promoter_pct * 100:.0f}% of the stake; "
                               f"floor {need:.1f} pts) in one quarter "
                               f"({CADENCE_QUARTERLY}, {prev.as_of} -> "
                               f"{cur.as_of})", cur.as_of, "TIER1")
            if drop > 0:
                immaterial["promoter_drop"] = (
                    f"promoter holding fell {drop:.2f} pts "
                    f"({prev.promoter_pct:.2f}% -> {cur.promoter_pct:.2f}%, "
                    f"{drop / prev.promoter_pct * 100:.0f}% of the stake), below "
                    f"the {need:.2f} pt floor")
    elif holdings:
        missing.append("only one shareholding filing, cannot compare quarters")

    # 3. D/E rising two periods running -- ANNUAL, not quarterly.
    #    Stated in the reason because a two-year lag on a deterioration veto is
    #    materially weaker than a two-quarter one, and a reader must know.
    des = [(a.fy, a.de) for a in annuals[:DE_RISING_PERIODS + 1]]
    if len(des) < DE_RISING_PERIODS + 1:
        # STRUCTURAL: only two annual years are parseable from this source, for
        # every symbol, so nobody can show two consecutive rises. Not this
        # symbol's opacity, so it does not block -- but it is named.
        # This is the NORMAL case, not an edge one: only 9 of the 417 symbols
        # with filings have three parseable annual years, because NSE's
        # period=Annual endpoint stops at FY2024. So rising leverage is kept --
        # it still fires where the filings exist -- but it is RARELY EVALUABLE,
        # and a reader who sees this on almost every verdict should know it is
        # the source and not a per-symbol defect.
        skipped.append(f"rising leverage needs {DE_RISING_PERIODS + 1} annual "
                       f"filings, only {len(des)} parseable ({CADENCE_ANNUAL}) — "
                       f"rarely evaluable: the annual endpoint stops at FY2024, "
                       f"so only ~9 of 417 symbols carry three years")
    elif any(d is None for _, d in des):
        if is_leverage_exempt(symbol):
            # The same category error as the tier 2 exemption, reached from the
            # other side: a lender reports no borrowings at all, so there is no
            # level to compare between years. Not opacity, so it does not block.
            #
            # NARROW ON PURPOSE. This is the one exemption that changes who
            # TRADES, since the alternative branch is UNPARSEABLE and blocks. It
            # is granted on the explicit LEVERAGE_EXEMPT list, never on a sector
            # tag: an airline or a port with no readable borrowings is opacity and
            # must block, and under the old sector test ADANIPORTS and INDIGO were
            # being waved through here.
            skipped.append(f"rising leverage not applicable: {symbol} is "
                           f"{exempt_phrase(symbol)} and reports no borrowings — "
                           f"{exempt_because(symbol)} ({CADENCE_ANNUAL})")
        else:
            missing.append("an annual filing lacks equity or borrowings, so "
                           "rising leverage cannot be tested")
    else:
        rising = all(des[i][1] > des[i + 1][1] for i in range(DE_RISING_PERIODS))
        if rising:
            chain = " -> ".join(f"{fy}:{d:.2f}" for fy, d in reversed(des))
            return Verdict(symbol, VERDICT_VETO,
                           f"debt-to-equity rose {DE_RISING_PERIODS} filings "
                           f"running ({chain}) [{CADENCE_ANNUAL} cadence -- "
                           f"balance sheets are only filed yearly, so this lags "
                           f"by up to two years]", annuals[0].fy, "TIER1")

    # 4. negative operating cash flow two periods running -- ANNUAL likewise
    # NOT APPLICABLE TO LENDERS. Under Ind AS a loan disbursed is an OPERATING
    # outflow, so a lender growing its book MUST report negative operating cash
    # flow -- it is the healthy state, not a warning. This is the deposits
    # category error one layer over, and unfloored it was the single largest veto
    # class: 25 of 38 cash-flow vetoes were lenders, BAJFINANCE at -42,140cr and
    # PFC at -74,699cr among them. Both are loan books growing.
    #
    # Scoped to LENDERS rather than all of LEVERAGE_EXEMPT: the Ind AS mechanic is
    # about lending, and an insurer is different in kind -- premiums in and claims
    # out, so a life insurer with negative operating cash flow IS saying
    # something. No insurer is currently caught by this test either way, so the
    # narrower scope costs nothing and keeps a real check alive.
    cfos = [(a.fy, a.cfo) for a in annuals[:CFO_NEGATIVE_PERIODS]]
    if is_lender_cfo_exempt(symbol):
        skipped.append(f"negative cash flow not applicable: {symbol} is "
                       f"{exempt_phrase(symbol)} — loans disbursed are an "
                       f"operating outflow under Ind AS, so a growing book "
                       f"reports negative CFO by construction ({CADENCE_ANNUAL})")
    elif len(cfos) < CFO_NEGATIVE_PERIODS:
        skipped.append(f"negative cash flow needs {CFO_NEGATIVE_PERIODS} annual "
                       f"filings, only {len(cfos)} parseable ({CADENCE_ANNUAL})")
    elif any(c is None for _, c in cfos):
        missing.append("an annual filing lacks operating cash flow")
    elif all(c < 0 for _, c in cfos):
        chain = ", ".join(f"{fy}:{c/1e7:,.0f}cr" for fy, c in cfos)
        return Verdict(symbol, VERDICT_VETO,
                       f"operating cash flow negative {CFO_NEGATIVE_PERIODS} "
                       f"filings running ({chain}) [{CADENCE_ANNUAL} cadence -- "
                       f"cash flow is only filed yearly]", annuals[0].fy, "TIER1")

    # 5. auditor resigned or qualified -- only the resignation half is readable
    if auditor_flags is None:
        missing.append("announcements not checked")
    else:
        skipped.append(OPINION_NOT_READABLE)
    if auditor_flags:
        first = auditor_flags[0]
        return Verdict(symbol, VERDICT_VETO,
                       f"auditor event in the last {AUDITOR_LOOKBACK_QTRS} "
                       f"quarters: {str(first)[:140]} [keyword match on free-text "
                       f"announcements, best effort]", "", "TIER1")

    if missing:
        return Verdict(symbol, VERDICT_UNPARSEABLE,
                       "tier 1 not established: " + "; ".join(missing[:4]),
                       annuals[0].fy if annuals else "", "TIER1",
                       not_evaluated=skipped, details={"missing": missing})
    note = ("tier 1: no deterioration signal"
            + (f" ({len(skipped)} check(s) not evaluable: "
               f"{'; '.join(skipped)})" if skipped else ""))
    return Verdict(symbol, VERDICT_PASS, note,
                   annuals[0].fy if annuals else "", "TIER1",
                   not_evaluated=skipped,
                   details={"immaterial": immaterial} if immaterial else {})


# ══════════════════════════════════════════════════════════════════
# TIER 2 — quality floor
# ══════════════════════════════════════════════════════════════════

@dataclass
class Tier2Metrics:
    """
    Tier 2 MEASURES. It does not gate. See the module docstring for why.

    would_veto is the crux of the record: True when the old Tier 2 rules would
    have thrown this signal away, None when they could not be established. Kept
    so that once a few hundred trades have resolved, the question "did the names
    Tier 2 would have rejected actually do worse on this book?" can be answered
    from the trade log instead of guessed at. Nothing reads it to decide
    anything today.
    """
    roe_pct: float | None = None
    roe_years: list = field(default_factory=list)
    de: float | None = None
    de_exempt: bool = False
    fcf: list = field(default_factory=list)
    fcf_positive_years: int | None = None
    would_veto: bool | None = None
    would_veto_reason: str = ""
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def tier2(symbol: str, annuals: list) -> Tier2Metrics:
    """
    Compute ROE, D/E and FCF and record them. NEVER returns a verdict, and the
    caller has nothing to branch on -- that is deliberate and structural, not an
    oversight to be tidied up later. If a future reader wants Tier 2 to gate
    again, the change belongs in evaluate() with evidence attached, not here.
    """
    m = Tier2Metrics()
    win = f"{ROE_AVERAGE_YEARS}-year window, {CADENCE_ANNUAL}"
    fails, unestablished = [], []

    roes = [a.roe_pct for a in annuals[:ROE_AVERAGE_YEARS] if a.roe_pct is not None]
    m.roe_years = [round(r, 2) for r in roes]
    if len(roes) < ROE_AVERAGE_YEARS:
        unestablished.append(f"ROE needs {ROE_AVERAGE_YEARS} years, have {len(roes)}")
    else:
        m.roe_pct = round(sum(roes) / len(roes), 2)
        if m.roe_pct <= ROE_MIN_PCT:
            fails.append(f"average ROE {m.roe_pct:.1f}% not above "
                         f"{ROE_MIN_PCT:.0f}% [{win}]")

    # LEVERAGE: exempt for lenders only. Deposits are not borrowings, so an
    # absent D/E for a bank is a category error rather than opacity. Recorded,
    # never silent.
    if is_leverage_exempt(symbol):
        m.de_exempt = True
        m.notes.append(f"leverage exempt, not measured: {symbol} is "
                       f"{exempt_phrase(symbol)} ({sector_of(symbol)}) — "
                       f"{exempt_because(symbol)}, so D/E is not comparable and "
                       f"NSE reports no BorrowingsCurrent/Noncurrent for it")
    elif not annuals or annuals[0].de is None:
        unestablished.append("no annual filing with equity and borrowings for D/E")
    else:
        m.de = round(annuals[0].de, 3)
        if m.de >= DE_MAX:
            fails.append(f"debt-to-equity {m.de:.2f} not below {DE_MAX:.1f} "
                         f"({annuals[0].fy}) [{CADENCE_ANNUAL}]")

    fcfs = [(a.fy, a.fcf) for a in annuals[:FCF_WINDOW_YEARS] if a.fcf is not None]
    m.fcf = [{"fy": fy, "value": v} for fy, v in fcfs]
    if len(fcfs) < FCF_WINDOW_YEARS:
        unestablished.append(f"free cash flow needs {FCF_WINDOW_YEARS} years of "
                             f"CFO and capex, have {len(fcfs)}")
    else:
        m.fcf_positive_years = sum(1 for _, v in fcfs if v > 0)
        if m.fcf_positive_years < FCF_POSITIVE_YEARS:
            chain = ", ".join(f"{fy}:{v/1e7:,.0f}cr" for fy, v in fcfs)
            fails.append(f"free cash flow not positive in both years "
                         f"({m.fcf_positive_years} of {len(fcfs)}: {chain}) [{win}]")

    # A fail is decisive even when another check is unestablished: the old rules
    # would have vetoed on the one that failed. The reverse is not true, so an
    # unestablished check with no fails leaves would_veto as None rather than
    # False -- "we could not tell" is not "it passed".
    if fails:
        m.would_veto = True
        m.would_veto_reason = "; ".join(fails)
    elif unestablished:
        m.would_veto = None
        m.would_veto_reason = "not established: " + "; ".join(unestablished[:4])
    else:
        m.would_veto = False
        m.would_veto_reason = f"quality floor met [{win}]"
    return m


def evaluate(symbol: str, annuals: list, holdings: list,
             auditor_flags: list) -> Verdict:
    """
    TIER 1 IS THE GATE. Tier 2 is measured and attached, and cannot change the
    verdict -- there is no branch on it below, by design.

    Tier 2 asserts that quality predicts outcomes, on annual data lagging up to
    two years, against positions held for weeks. That claim is untested on this
    book, and gating on it would cut the universe to roughly a third on a belief
    about the market that is currently indistinguishable from a belief about
    this parser. So it measures and waits for the evidence.
    """
    v = tier1(symbol, annuals, holdings, auditor_flags)
    m = tier2(symbol, annuals)
    v.details["tier2"] = m.as_dict()
    # Tier 2's own unmeasured checks land under tier2, not on not_evaluated:
    # not_evaluated qualifies the GATE's verdict, and Tier 2 is not the gate.
    return v


# ══════════════════════════════════════════════════════════════════
# CACHE — the gate reads this, never the network
# ══════════════════════════════════════════════════════════════════

def load_cache(path: Path = CACHE) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.error(f"fundamentals cache unreadable ({e}) — treating as empty, "
                  f"which blocks every symbol")
        return {}


def save_cache(verdicts: dict, path: Path = CACHE) -> None:
    payload = {
        "parser_version": PARSER_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "verdicts": {s: asdict(v) if isinstance(v, Verdict) else v
                     for s, v in verdicts.items()},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True))
    log.info(f"fundamentals cache: {len(verdicts)} verdict(s) -> {path}")


def gate(symbol: str, cache: dict = None) -> tuple:
    """
    (tradeable, reason). THE function the signal path calls.

    Fails CLOSED in every direction: no cache, no entry for the symbol, or an
    entry written by an older parser all block. A fundamentals layer that
    defaults to permitting is decoration.
    """
    cache = cache if cache is not None else load_cache()
    if not cache:
        return False, "fundamentals cache missing or unreadable"
    if cache.get("parser_version") != PARSER_VERSION:
        return False, (f"fundamentals cache written by parser "
                       f"v{cache.get('parser_version')}, this is v{PARSER_VERSION} "
                       f"— refresh it")
    row = (cache.get("verdicts") or {}).get(symbol)
    if not row:
        return False, f"{symbol} has no fundamentals verdict"
    if row.get("verdict") != VERDICT_PASS:
        return False, f"{row.get('verdict')}: {row.get('reason', '')}"
    return True, row.get("reason", "fundamentals pass")


# ══════════════════════════════════════════════════════════════════
# BUILDING THE CACHE — joins the two source stores, no network
# ══════════════════════════════════════════════════════════════════

ANNUAL_STORE = ROOT / "data/processed/fundamentals_annual.json"
TIER1_STORE = ROOT / "data/processed/tier1_sources.json"


def _load_store(path: Path) -> dict:
    if not path.exists():
        log.error(f"{path.name} is missing — every symbol will be UNPARSEABLE")
        return {}
    try:
        return (json.loads(path.read_text()) or {}).get("symbols") or {}
    except Exception as e:
        log.error(f"{path.name} unreadable ({e}) — every symbol UNPARSEABLE")
        return {}


def _annuals_for(rec: dict) -> list:
    """Store rows -> AnnualFacts, newest first (the store already orders them)."""
    out = []
    for y in (rec or {}).get("years", []):
        out.append(AnnualFacts(
            fy=y.get("fy", ""),
            equity=y.get("equity"),
            borrowings_current=y.get("borrowings_current"),
            borrowings_noncurrent=y.get("borrowings_noncurrent"),
            pat=y.get("pat"), cfo=y.get("cfo"), capex=y.get("capex"),
            source=y.get("source", "")))
    return out


def _holdings_for(rec: dict) -> list:
    return [Shareholding(as_of=h.get("as_of", ""),
                         promoter_pct=h.get("promoter_pct"),
                         pledged_pct=h.get("pledged_pct"),
                         source=h.get("source", ""))
            for h in (rec or {}).get("holdings", [])]


def build_cache(symbols: list = None, path: Path = CACHE) -> dict:
    """
    Evaluate every symbol from the two stores and write the cache the gate reads.

    A symbol absent from a store is NOT skipped -- it is evaluated with empty
    inputs, which yields UNPARSEABLE and blocks. Skipping would leave the gate
    with no row, which also blocks, but silently and without a reason a reader
    can act on.
    """
    annual = _load_store(ANNUAL_STORE)
    t1 = _load_store(TIER1_STORE)
    if symbols is None:
        from engine.universe import ALL_SYMBOLS
        symbols = list(ALL_SYMBOLS)
    verdicts = {}
    for sym in symbols:
        arec, trec = annual.get(sym) or {}, t1.get(sym) or {}
        auditor = trec.get("auditor")
        # auditor None means the announcements were never successfully read, and
        # tier1() must see None -- not [] -- or an unchecked symbol reads as clean.
        flags = auditor.get("flags") if isinstance(auditor, dict) else None
        v = evaluate(sym, _annuals_for(arec), _holdings_for(trec), flags)
        row = asdict(v)
        if isinstance(auditor, dict) and auditor.get("noted"):
            row["details"]["auditor_noted"] = auditor["noted"]
        verdicts[sym] = row
    save_cache(verdicts, path)
    log.info(f"cache written: {len(verdicts)} symbol(s), parser v{PARSER_VERSION}")
    return verdicts


def report_cache(path: Path = CACHE) -> int:
    """
    The distribution, and WHY each blocked symbol blocked. Tier 2 appears as a
    counterfactual column only -- it does not gate, and the report must not imply
    it does.
    """
    import collections
    c = load_cache(path)
    vs = c.get("verdicts") or {}
    if not vs:
        print("fundamentals cache is empty")
        return 1
    n = len(vs)
    counts = collections.Counter(v.get("verdict") for v in vs.values())
    passed = counts.get(VERDICT_PASS, 0)
    print("=" * 78)
    print(f"FUNDAMENTALS — {n} symbol(s), parser v{c.get('parser_version')}, "
          f"written {c.get('written_at')}")
    print("TIER 1 IS THE GATE. Tier 2 is recorded and does not block.")
    print("=" * 78)
    for k in (VERDICT_PASS, VERDICT_VETO, VERDICT_UNPARSEABLE):
        m = counts.get(k, 0)
        print(f"  {k:<14}{m:>5}   {m / n * 100:>5.1f}%")
    print(f"\n  TIER 1 ADMITS {passed}/{n} = {passed / n * 100:.1f}% "
          f"of the universe")

    def bucket(reason: str) -> str:
        r = (reason or "").lower()
        for key, label in (("pledge rising", "pledge rising q/q"),
                           ("promoter pledge", "pledge above the cap"),
                           ("promoter holding fell", "promoter holding fell >5pts"),
                           ("auditor event", "auditor resignation / forensic audit"),
                           ("debt-to-equity rose", "leverage rose 2 filings"),
                           ("cash flow negative", "operating cash flow negative"),
                           ("no shareholding", "no shareholding data"),
                           ("pledge %", "pledge % not readable"),
                           ("only one shareholding", "one shareholding filing only"),
                           ("promoter % missing", "promoter % missing"),
                           ("announcements not checked", "announcements unread"),
                           ("lacks equity or borrowings", "leverage fields absent"),
                           ("lacks operating cash flow", "cash flow absent")):
            if key in r:
                return label
        return "other"

    for verdict, title in ((VERDICT_VETO, "WHY VETOED"),
                           (VERDICT_UNPARSEABLE, "WHY UNPARSEABLE")):
        rows = [v for v in vs.values() if v.get("verdict") == verdict]
        if not rows:
            continue
        print(f"\n{title} ({len(rows)}):")
        for label, m in collections.Counter(
                bucket(v.get("reason")) for v in rows).most_common():
            print(f"  {m:>5}  {label}")

    ne = collections.Counter()
    for v in vs.values():
        for x in v.get("not_evaluated") or []:
            if "rising leverage" in x:
                ne["rising leverage not evaluable (needs 3 annual filings)"] += 1
            elif "opinion not evaluable" in x:
                ne["audit opinion not readable from announcements"] += 1
            elif "not applicable" in x:
                ne["leverage n/a: lender reports no borrowings"] += 1
    print("\nCHECKS NOT EVALUATED (named on the verdict, do not block):")
    for label, m in ne.most_common():
        print(f"  {m:>5}  {label}   {m / n * 100:>5.1f}%")
    exempt = sum(1 for v in vs.values()
                 if (v.get("details", {}).get("tier2") or {}).get("de_exempt"))
    print(f"  {exempt:>5}  tier 2 leverage exempt ({len(BANKS)} banks + "
          f"{len(NBFCS)} NBFCs + {len(INSURERS)} insurers = "
          f"{len(LEVERAGE_EXEMPT)} named)")

    t2 = [v.get("details", {}).get("tier2") or {} for v in vs.values()]
    wv = collections.Counter(
        {True: "would have been vetoed by tier 2",
         False: "would have passed tier 2",
         None: "tier 2 not establishable"}[m.get("would_veto")] for m in t2)
    print("\nTIER 2, AS A COUNTERFACTUAL ONLY (nobody is blocked by this):")
    for label, m in wv.most_common():
        print(f"  {m:>5}  {label}   {m / n * 100:>5.1f}%")
    both = sum(1 for v in vs.values()
               if v.get("verdict") == VERDICT_PASS
               and (v.get("details", {}).get("tier2") or {}).get("would_veto") is False)
    print(f"\n  had tier 2 also gated, {both}/{n} = {both / n * 100:.1f}% "
          f"would trade instead of {passed / n * 100:.1f}%")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--refresh" in sys.argv:
        build_cache()
        sys.exit(report_cache())
    elif "--report" in sys.argv:
        sys.exit(report_cache())
    elif "--symbol" in sys.argv:
        sym = sys.argv[sys.argv.index("--symbol") + 1]
        build_cache([sym], path=CACHE.with_suffix(".one.json"))
        c = load_cache(CACHE.with_suffix(".one.json"))
        print(json.dumps(c["verdicts"][sym], indent=2))
    else:
        print(__doc__)
