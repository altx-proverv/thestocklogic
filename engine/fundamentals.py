"""
TSL — Fundamentals Layer (Tier 1 veto, Tier 2 quality floor)
============================================================
A stock must pass BOTH tiers to be tradeable. Consulted at signal time as a
gate, from a cache, so it costs nothing in the market-hours loop.

    python3 -m engine.fundamentals --symbol RELIANCE     # one symbol, verbose
    python3 -m engine.fundamentals --refresh             # rebuild the cache
    python3 -m engine.fundamentals --report              # verdict distribution

THREE VERDICTS, NOT TWO
-----------------------
    PASS          every required fact was found and every rule passed
    VETO          a rule fired, or a required filing is genuinely absent
    UNPARSEABLE   the filing exists and we could not read it

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
                             ROE and free cash flow.

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

# Bump when the parser or the rules change. A cached verdict carrying an older
# version is stale by definition -- otherwise a fixed parser leaves old wrong
# vetoes in place, and a changed threshold is applied to nothing.
PARSER_VERSION = 1

CACHE = ROOT / "data/processed/fundamentals.json"

VERDICT_PASS = "PASS"
VERDICT_VETO = "VETO"
VERDICT_UNPARSEABLE = "UNPARSEABLE"

# Written into every reason string that depends on a filing, so the lag is
# visible at the point a verdict is read rather than buried in this file.
CADENCE_QUARTERLY = "quarterly"
CADENCE_ANNUAL = "annual"

# ── TIER 1: deterioration veto. Strict, no exceptions ─────────────
PLEDGE_MAX_PCT          = 25.0   # or rising quarter on quarter
PROMOTER_DROP_MAX_PCT   = 5.0    # in a single quarter
DE_RISING_PERIODS       = 2      # consecutive ANNUAL filings, see above
CFO_NEGATIVE_PERIODS    = 2      # consecutive ANNUAL filings, see above
AUDITOR_LOOKBACK_QTRS   = 4

# ── TIER 2: quality floor. Deliberately looser than Buffett's own ─
# 15% ROE and D/E under 0.5 are calibrated for permanent ownership of a handful
# of businesses. This is a rotating book with a technical entry and a structural
# stop. D/E under 0.5 also excludes most Indian banks and NBFCs structurally --
# a sector exclusion disguised as a quality test.
ROE_MIN_PCT             = 12.0
ROE_AVERAGE_YEARS       = 3
DE_MAX                  = 1.0
FCF_POSITIVE_YEARS      = 2
FCF_WINDOW_YEARS        = 3


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

    Context dates cannot be trusted -- see the module docstring: OneD and FourD
    carried identical dates and different values, the larger being the annual
    one. So the choice is made by MAGNITUDE among duration contexts, which is
    sound for a year-to-date column of a cumulative measure (a full year cannot
    be smaller in absolute terms than a quarter within it) and is recorded in
    the note so a reader knows how it was chosen.

    Returns (None, "", why) when there is nothing to choose from, and the caller
    treats that as UNPARSEABLE rather than zero.
    """
    hits = [(c, v) for c, v in facts_for(xml, element)
            if contexts.get(c, ("",))[0] == "duration"]
    if not hits:
        return None, "", f"{element}: no duration context"
    if len(hits) == 1:
        return hits[0][1], hits[0][0], f"{element}: single duration context"
    best = max(hits, key=lambda cv: abs(cv[1]))
    return (best[1], best[0],
            f"{element}: {len(hits)} duration contexts, took the largest "
            f"magnitude as the year-to-date column")


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

def tier1(symbol: str, annuals: list, holdings: list,
          auditor_flags: list) -> Verdict:
    """
    Strict. Any rule firing is a VETO; any rule that cannot be evaluated is
    UNPARSEABLE. `annuals` newest first, `holdings` newest first.
    """
    missing = []

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
                return Verdict(symbol, VERDICT_VETO,
                               f"promoter pledge rising {prev.pledged_pct:.1f}% -> "
                               f"{cur.pledged_pct:.1f}% ({CADENCE_QUARTERLY}, "
                               f"{prev.as_of} -> {cur.as_of})", cur.as_of, "TIER1")

    # 2. promoter holding down more than 5 points in a quarter
    if len(holdings) >= 2:
        cur, prev = holdings[0], holdings[1]
        if cur.promoter_pct is None or prev.promoter_pct is None:
            missing.append("promoter % missing in a shareholding filing")
        else:
            drop = prev.promoter_pct - cur.promoter_pct
            if drop > PROMOTER_DROP_MAX_PCT:
                return Verdict(symbol, VERDICT_VETO,
                               f"promoter holding fell {drop:.1f} points "
                               f"({prev.promoter_pct:.1f}% -> {cur.promoter_pct:.1f}%) "
                               f"in one quarter ({CADENCE_QUARTERLY}, "
                               f"{prev.as_of} -> {cur.as_of})", cur.as_of, "TIER1")
    elif holdings:
        missing.append("only one shareholding filing, cannot compare quarters")

    # 3. D/E rising two periods running -- ANNUAL, not quarterly.
    #    Stated in the reason because a two-year lag on a deterioration veto is
    #    materially weaker than a two-quarter one, and a reader must know.
    des = [(a.fy, a.de) for a in annuals[:DE_RISING_PERIODS + 1]]
    if any(d is None for _, d in des) or len(des) < DE_RISING_PERIODS + 1:
        missing.append(f"need {DE_RISING_PERIODS + 1} annual filings with "
                       f"equity and borrowings to test rising leverage")
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
    cfos = [(a.fy, a.cfo) for a in annuals[:CFO_NEGATIVE_PERIODS]]
    if len(cfos) < CFO_NEGATIVE_PERIODS or any(c is None for _, c in cfos):
        missing.append(f"need {CFO_NEGATIVE_PERIODS} annual filings with "
                       f"operating cash flow")
    elif all(c < 0 for _, c in cfos):
        chain = ", ".join(f"{fy}:{c/1e7:,.0f}cr" for fy, c in cfos)
        return Verdict(symbol, VERDICT_VETO,
                       f"operating cash flow negative {CFO_NEGATIVE_PERIODS} "
                       f"filings running ({chain}) [{CADENCE_ANNUAL} cadence -- "
                       f"cash flow is only filed yearly]", annuals[0].fy, "TIER1")

    # 5. auditor resigned or qualified
    if auditor_flags is None:
        missing.append("announcements not checked")
    elif auditor_flags:
        first = auditor_flags[0]
        return Verdict(symbol, VERDICT_VETO,
                       f"auditor event in the last {AUDITOR_LOOKBACK_QTRS} "
                       f"quarters: {str(first)[:140]} [keyword match on free-text "
                       f"announcements, best effort]", "", "TIER1")

    if missing:
        return Verdict(symbol, VERDICT_UNPARSEABLE,
                       "tier 1 not established: " + "; ".join(missing[:4]),
                       annuals[0].fy if annuals else "", "TIER1",
                       details={"missing": missing})
    return Verdict(symbol, VERDICT_PASS, "tier 1: no deterioration signal",
                   annuals[0].fy if annuals else "", "TIER1")


# ══════════════════════════════════════════════════════════════════
# TIER 2 — quality floor
# ══════════════════════════════════════════════════════════════════

def tier2(symbol: str, annuals: list) -> Verdict:
    missing = []
    window = annuals[:ROE_AVERAGE_YEARS]

    roes = [a.roe_pct for a in window if a.roe_pct is not None]
    if len(roes) < ROE_AVERAGE_YEARS:
        missing.append(f"need {ROE_AVERAGE_YEARS} years of ROE, have {len(roes)}")
    else:
        avg = sum(roes) / len(roes)
        if avg <= ROE_MIN_PCT:
            return Verdict(symbol, VERDICT_VETO,
                           f"{ROE_AVERAGE_YEARS}-year average ROE {avg:.1f}% is "
                           f"not above {ROE_MIN_PCT:.0f}% "
                           f"({', '.join(f'{r:.1f}%' for r in roes)}) "
                           f"[{CADENCE_ANNUAL}]", annuals[0].fy, "TIER2")

    if not annuals or annuals[0].de is None:
        missing.append("no annual filing with equity and borrowings for D/E")
    elif annuals[0].de >= DE_MAX:
        return Verdict(symbol, VERDICT_VETO,
                       f"debt-to-equity {annuals[0].de:.2f} is not below "
                       f"{DE_MAX:.1f} ({annuals[0].fy}) [{CADENCE_ANNUAL}]",
                       annuals[0].fy, "TIER2")

    fcf_window = annuals[:FCF_WINDOW_YEARS]
    fcfs = [(a.fy, a.fcf) for a in fcf_window if a.fcf is not None]
    if len(fcfs) < FCF_WINDOW_YEARS:
        missing.append(f"need {FCF_WINDOW_YEARS} years of free cash flow "
                       f"(CFO and capex), have {len(fcfs)}")
    else:
        positive = sum(1 for _, v in fcfs if v > 0)
        if positive < FCF_POSITIVE_YEARS:
            chain = ", ".join(f"{fy}:{v/1e7:,.0f}cr" for fy, v in fcfs)
            return Verdict(symbol, VERDICT_VETO,
                           f"free cash flow positive in only {positive} of "
                           f"{FCF_WINDOW_YEARS} years ({chain}) "
                           f"[{CADENCE_ANNUAL}]", annuals[0].fy, "TIER2")

    if missing:
        return Verdict(symbol, VERDICT_UNPARSEABLE,
                       "tier 2 not established: " + "; ".join(missing[:4]),
                       annuals[0].fy if annuals else "", "TIER2",
                       details={"missing": missing})
    return Verdict(symbol, VERDICT_PASS, "tier 2: quality floor met",
                   annuals[0].fy if annuals else "", "TIER2")


def evaluate(symbol: str, annuals: list, holdings: list,
             auditor_flags: list) -> Verdict:
    """
    BOTH tiers. Tier 1 first: a deterioration veto outranks a quality pass, and
    an unestablished Tier 1 is not rescued by a clean Tier 2.
    """
    v1 = tier1(symbol, annuals, holdings, auditor_flags)
    if v1.verdict != VERDICT_PASS:
        return v1
    return tier2(symbol, annuals)


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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--report" in sys.argv:
        c = load_cache()
        vs = (c.get("verdicts") or {})
        import collections
        counts = collections.Counter(v.get("verdict") for v in vs.values())
        print(f"cache: {len(vs)} symbol(s), parser v{c.get('parser_version')}, "
              f"written {c.get('written_at')}")
        for k, n in counts.most_common():
            print(f"  {k:<14}{n:>5}")
    else:
        print(__doc__)
