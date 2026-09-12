#!/usr/bin/env python3
"""
THE FUNDAMENTALS GATE: MISSING IS NEVER A PASS
==============================================
Tier 1 vetoes deterioration, Tier 2 sets a quality floor, and a stock must pass
both. The property that matters most is the one that is easiest to get wrong:

    every path that cannot establish a rule must block, and must be
    distinguishable from a rule that fired

PASS / VETO / UNPARSEABLE exist for that. UNPARSEABLE blocks like VETO but is
counted apart, because a rising UNPARSEABLE count is a parser regression and a
rising VETO count is a deteriorating market. Folded together, an NSE taxonomy
change would look like the universe going bad and would empty it in silence --
which is what live_prices returning 200 and an empty array did for eleven weeks.

Offline: every fact is constructed, so this asserts the RULES, not NSE.

    python3 tests/test_fundamentals.py
"""

import sys
import json
import tempfile
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from engine import fundamentals as F          # noqa: E402
from engine.fundamentals import (             # noqa: E402
    AnnualFacts, Shareholding, VERDICT_PASS, VERDICT_VETO, VERDICT_UNPARSEABLE)

CR = 1e7


def year(fy, equity=1000 * CR, debt=300 * CR, pat=200 * CR,
         cfo=250 * CR, capex=50 * CR):
    """A healthy year: D/E 0.30, ROE 20%, FCF positive."""
    return AnnualFacts(fy=fy, equity=equity,
                       borrowings_current=debt / 2,
                       borrowings_noncurrent=debt / 2,
                       pat=pat, cfo=cfo, capex=capex)


def healthy(n=3):
    return [year(f"FY{2026 - i}") for i in range(n)]


def holds(*pairs):
    """pairs of (promoter_pct, pledged_pct), newest first."""
    return [Shareholding(as_of=f"2026-0{6 - i}-30", promoter_pct=p, pledged_pct=q)
            for i, (p, q) in enumerate(pairs)]


def main() -> int:
    ok = True

    def case(label, verdict_obj, want_verdict, want_in_reason=None):
        nonlocal ok
        good = verdict_obj.verdict == want_verdict
        if good and want_in_reason:
            good = want_in_reason.lower() in verdict_obj.reason.lower()
        ok &= good
        mark = "ok" if good else f"** want {want_verdict}"
        if want_in_reason and verdict_obj.verdict == want_verdict and not good:
            mark = f"** reason missing {want_in_reason!r}"
        print(f"  {label:<46}{verdict_obj.verdict:<13}{mark}")
        if not good:
            print(f"      reason: {verdict_obj.reason[:110]}")

    HOLD_OK = holds((60.0, 0.0), (60.0, 0.0), (60.0, 0.0))

    print("A CLEAN STOCK PASSES BOTH TIERS")
    print("-" * 78)
    case("healthy 3 years, no pledge", F.evaluate("X", healthy(), HOLD_OK, []),
         VERDICT_PASS)

    print()
    print("TIER 1 — EACH VETO FIRES")
    print("-" * 78)
    case("pledge above 25%",
         F.evaluate("X", healthy(), holds((60.0, 30.0), (60.0, 30.0)), []),
         VERDICT_VETO, "pledge")
    case("pledge rising quarter on quarter",
         F.evaluate("X", healthy(), holds((60.0, 10.0), (60.0, 5.0)), []),
         VERDICT_VETO, "rising")
    case("promoter holding down more than 5 pts",
         F.evaluate("X", healthy(), holds((50.0, 0.0), (60.0, 0.0)), []),
         VERDICT_VETO, "promoter holding fell")
    case("promoter down exactly 5 pts is not a veto",
         F.evaluate("X", healthy(), holds((55.0, 0.0), (60.0, 0.0), (60.0, 0.0)), []),
         VERDICT_PASS)
    rising = [year("FY2026", debt=600 * CR), year("FY2025", debt=400 * CR),
              year("FY2024", debt=200 * CR)]
    case("D/E rising two filings running",
         F.evaluate("X", rising, HOLD_OK, []), VERDICT_VETO, "debt-to-equity rose")
    neg = [year("FY2026", cfo=-10 * CR), year("FY2025", cfo=-20 * CR), year("FY2024")]
    case("operating cash flow negative twice",
         F.evaluate("X", neg, HOLD_OK, []), VERDICT_VETO, "operating cash flow negative")
    case("auditor event",
         F.evaluate("X", healthy(), HOLD_OK, ["Resignation of Statutory Auditor"]),
         VERDICT_VETO, "auditor")

    print()
    print("THE CADENCE IS IN THE REASON, SO THE LAG IS VISIBLE")
    print("-" * 78)
    v = F.evaluate("X", rising, HOLD_OK, [])
    for token in ("annual", "lags"):
        good = token in v.reason.lower()
        ok &= good
        print(f"  leverage reason mentions {token!r:<22}{'ok' if good else '** MISSING **'}")
    v = F.evaluate("X", neg, HOLD_OK, [])
    good = "annual" in v.reason.lower()
    ok &= good
    print(f"  {'cash-flow reason mentions cadence':<46}{'ok' if good else '** MISSING **'}")

    print()
    print("TIER 2 — EACH VETO FIRES")
    print("-" * 78)
    low_roe = [year(f"FY{2026-i}", pat=80 * CR) for i in range(3)]     # 8% ROE
    case("3-year average ROE below 12%",
         F.evaluate("X", low_roe, HOLD_OK, []), VERDICT_VETO, "average roe")
    levered = [year(f"FY{2026-i}", debt=1500 * CR) for i in range(3)]  # D/E 1.5
    case("D/E at or above 1.0",
         F.evaluate("X", levered, HOLD_OK, []), VERDICT_VETO, "debt-to-equity 1.50")
    burn = [year("FY2026", cfo=10 * CR, capex=900 * CR),
            year("FY2025", cfo=10 * CR, capex=900 * CR),
            year("FY2024")]
    case("free cash flow positive in only 1 of 3",
         F.evaluate("X", burn, HOLD_OK, []), VERDICT_VETO, "free cash flow positive in only")

    print()
    print("MISSING IS UNPARSEABLE, NEVER PASS")
    print("-" * 78)
    case("no shareholding data at all",
         F.evaluate("X", healthy(), [], []), VERDICT_UNPARSEABLE, "shareholding")
    case("pledge % absent from the filing",
         F.evaluate("X", healthy(), holds((60.0, None), (60.0, None)), []),
         VERDICT_UNPARSEABLE, "pledge")
    case("only one shareholding filing",
         F.evaluate("X", healthy(), holds((60.0, 0.0)), []),
         VERDICT_UNPARSEABLE, "one shareholding")
    case("only 2 annual filings, need 3 for leverage",
         F.evaluate("X", healthy(2), HOLD_OK, []),
         VERDICT_UNPARSEABLE, "annual filings")
    case("announcements not checked (None)",
         F.evaluate("X", healthy(), HOLD_OK, None),
         VERDICT_UNPARSEABLE, "announcements")
    nocapex = [year(f"FY{2026-i}", capex=None) for i in range(3)]
    case("capex absent -> FCF unknown, not zero",
         F.evaluate("X", nocapex, HOLD_OK, []),
         VERDICT_UNPARSEABLE, "free cash flow")
    # the specific trap: capex absent must NOT read as capex zero, which would
    # make a capital-hungry business look cash-generative.
    good = nocapex[0].fcf is None
    ok &= good
    print(f"  {'AnnualFacts.fcf is None when capex is None':<46}"
          f"{'ok' if good else '** TREATED AS ZERO **'}")

    print()
    print("A VETO OUTRANKS A CLEAN TIER 2")
    print("-" * 78)
    # healthy fundamentals but a pledge problem: must be TIER1 VETO, not PASS
    v = F.evaluate("X", healthy(), holds((60.0, 40.0), (60.0, 40.0)), [])
    good = v.verdict == VERDICT_VETO and v.tier == "TIER1"
    ok &= good
    print(f"  {'tier 1 veto wins over a passing tier 2':<46}"
          f"{v.verdict}/{v.tier}  {'ok' if good else '** WRONG **'}")

    print()
    print("THE GATE FAILS CLOSED IN EVERY DIRECTION")
    print("-" * 78)
    tmp = Path(tempfile.mkdtemp()) / "f.json"
    checks = [
        ("no cache file", {}, "X"),
        ("symbol absent from the cache",
         {"parser_version": F.PARSER_VERSION, "verdicts": {}}, "X"),
        ("cache written by an older parser",
         {"parser_version": F.PARSER_VERSION - 1,
          "verdicts": {"X": {"verdict": VERDICT_PASS, "reason": "fine"}}}, "X"),
        ("verdict is UNPARSEABLE",
         {"parser_version": F.PARSER_VERSION,
          "verdicts": {"X": {"verdict": VERDICT_UNPARSEABLE, "reason": "no data"}}}, "X"),
        ("verdict is VETO",
         {"parser_version": F.PARSER_VERSION,
          "verdicts": {"X": {"verdict": VERDICT_VETO, "reason": "pledge"}}}, "X"),
    ]
    for label, cache, sym in checks:
        allowed, why = F.gate(sym, cache=cache)
        ok &= not allowed
        print(f"  {label:<46}{'BLOCKED' if not allowed else '** ALLOWED **':<14}"
              f"{why[:34]}")
    allowed, why = F.gate("X", cache={
        "parser_version": F.PARSER_VERSION,
        "verdicts": {"X": {"verdict": VERDICT_PASS, "reason": "both tiers"}}})
    ok &= allowed
    print(f"  {'a PASS verdict is allowed':<46}"
          f"{'ALLOWED' if allowed else '** BLOCKED **'}")

    print()
    print("THE CONTEXT TRAP: A YEAR IS NOT A QUARTER")
    print("-" * 78)
    # Both duration contexts claim the same dates; only the magnitude separates
    # the year-to-date column from the quarter. Selecting by date gives ROE at a
    # quarter of its true value, which looks plausible. Observed in RELIANCE's
    # FY2024 filing.
    xml = """<xbrli:xbrl>
      <xbrli:context id="OneD"><xbrli:period>
        <xbrli:startDate>2024-01-01</xbrli:startDate>
        <xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="FourD"><xbrli:period>
        <xbrli:startDate>2024-01-01</xbrli:startDate>
        <xbrli:endDate>2024-03-31</xbrli:endDate></xbrli:period></xbrli:context>
      <xbrli:context id="OneI"><xbrli:period>
        <xbrli:instant>2024-03-31</xbrli:instant></xbrli:period></xbrli:context>
      <in-bse-fin:ProfitLossForPeriod contextRef="OneD">212430000000.00</in-bse-fin:ProfitLossForPeriod>
      <in-bse-fin:ProfitLossForPeriod contextRef="FourD">790200000000.00</in-bse-fin:ProfitLossForPeriod>
      <in-bse-fin:Equity contextRef="OneI">9257880000000.00</in-bse-fin:Equity>
    </xbrli:xbrl>"""
    facts, notes = F.parse_annual(xml, fy="FY2024")
    good = facts.pat == 790200000000.0
    ok &= good
    print(f"  PAT resolved to {facts.pat/1e7:,.0f} cr "
          f"({'the year' if good else 'THE QUARTER'})   "
          f"{'ok' if good else '** PICKED THE QUARTER **'}")
    good = facts.equity == 9257880000000.0
    ok &= good
    print(f"  equity from the instant context               {'ok' if good else '** WRONG **'}")
    good = abs(facts.roe_pct - 8.53) < 0.1
    ok &= good
    print(f"  ROE {facts.roe_pct:.1f}% (2.3% would mean the quarter was used)"
          f"   {'ok' if good else '** WRONG **'}")

    print("-" * 78)
    print("FUNDAMENTALS:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
