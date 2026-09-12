#!/usr/bin/env python3
"""
THE FUNDAMENTALS GATE: MISSING IS NEVER A PASS
==============================================
TIER 1 IS THE GATE -- it vetoes deterioration. TIER 2 MEASURES AND NEVER BLOCKS;
it records ROE, D/E, FCF and whether the old rules WOULD have vetoed, so the
claim "quality predicts outcomes here" becomes answerable from the trade log
instead of assumed. The asymmetry is asserted below, deliberately and in detail:
a name failing every Tier 2 rule must still PASS.

The property that matters most is the one that is easiest to get wrong:

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

    print("A CLEAN STOCK PASSES")
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
    # 60 -> 40 is a third of the stake: past both floors.
    case("promoter holding down a third of the stake",
         F.evaluate("X", healthy(), holds((40.0, 0.0), (60.0, 0.0)), []),
         VERDICT_VETO, "promoter holding fell")
    case("promoter down exactly 5 pts is not a veto",
         F.evaluate("X", healthy(), holds((55.0, 0.0), (60.0, 0.0), (60.0, 0.0)), []),
         VERDICT_PASS)
    print()
    print("MATERIALITY FLOORS: A ROUNDING DIFFERENCE IS NOT DETERIORATION")
    print("-" * 78)

    def floor(label, hs, want, want_in_reason=None):
        nonlocal ok
        v = F.evaluate("X", healthy(), hs, [])
        good = v.verdict == want
        if good and want_in_reason:
            good = want_in_reason.lower() in v.reason.lower()
        ok &= good
        imm = (v.details.get("immaterial") or {})
        print(f"  {label:<52}{v.verdict:<8}"
              f"{'ok' if good else '** want ' + want + ' **'}")
        return v, imm

    # every one of these is a real Jun-2026 filing pair the unfloored rule vetoed
    v, imm = floor("SUNPHARMA shape: pledge 1.4 -> 1.6",
                   holds((60.0, 1.6), (60.0, 1.4)), VERDICT_PASS)
    good = "pledge_rise" in imm
    ok &= good
    print(f"  {'and the immaterial move is still recorded':<52}"
          f"{'ok' if good else '** INVISIBLE **'}")
    floor("JSL shape: pledge 0.5 -> 0.6",
          holds((60.0, 0.6), (60.0, 0.5)), VERDICT_PASS)
    floor("GMRAIRPORT shape: pledge 15.5 -> 16.4 (rel floor)",
          holds((60.0, 16.4), (60.0, 15.5)), VERDICT_PASS)
    # and the ones that must still fire
    floor("AJANTPHARM shape: pledge 17.9 -> 22.5",
          holds((60.0, 22.5), (60.0, 17.9)), VERDICT_VETO, "rising")
    floor("FLUOROCHEM shape: pledge 3.1 -> 6.0 (abs floor)",
          holds((60.0, 6.0), (60.0, 3.1)), VERDICT_VETO, "rising")
    floor("NCC shape: pledge 0.0 -> 3.8 from nothing",
          holds((60.0, 3.8), (60.0, 0.0)), VERDICT_VETO, "rising")
    # promoter exit: lock-in expiry and a government OFS are not insiders leaving
    v, imm = floor("DOMS shape: promoter 70.4 -> 63.4 (IPO lock-in)",
                   holds((63.4, 0.0), (70.4, 0.0)), VERDICT_PASS)
    good = "promoter_drop" in imm
    ok &= good
    print(f"  {'and that drop is recorded, not discarded':<52}"
          f"{'ok' if good else '** INVISIBLE **'}")
    floor("NHPC shape: promoter 67.4 -> 61.4 (government OFS)",
          holds((61.4, 0.0), (67.4, 0.0)), VERDICT_PASS)
    # DELIBERATE LOSS, recorded here so it is not rediscovered as a surprise:
    # SHRIRAMFIN 25.4 -> 20.3 is a fifth of the promoter's stake and was the one
    # of six unfloored vetoes that looked like a genuine reduction. The relative
    # floor lets it through. Distinguishing it needs the CAUSE of the drop (an
    # OFS or QIP announcement in the same quarter), not a bigger number.
    floor("SHRIRAMFIN 25.4 -> 20.3 passes (known trade-off)",
          holds((20.3, 0.0), (25.4, 0.0)), VERDICT_PASS)

    print()
    print("A LENDER'S NEGATIVE CASH FLOW IS ITS BUSINESS MODEL")
    print("-" * 78)
    # Under Ind AS a loan disbursed is an operating outflow, so a growing lender
    # reports negative CFO by construction. 25 of 38 cash-flow vetoes were
    # lenders. An insurer is NOT exempt here -- premiums in, claims out.
    burn2 = [year(f"FY{2026-i}", cfo=-500 * CR) for i in range(3)]
    for sym, want in (("BAJFINANCE", VERDICT_PASS), ("PFC", VERDICT_PASS),
                      ("HDFCBANK", VERDICT_PASS),
                      ("LICI", VERDICT_VETO),        # insurer: check stays live
                      ("RELIANCE", VERDICT_VETO)):
        v = F.evaluate(sym, burn2, HOLD_OK, [])
        good = v.verdict == want
        ok &= good
        print(f"  {sym + ' with CFO negative 2 years':<52}{v.verdict:<8}"
              f"{'ok' if good else '** want ' + want + ' **'}")
    v = F.evaluate("BAJFINANCE", burn2, HOLD_OK, [])
    good = any("operating outflow" in x for x in v.not_evaluated)
    ok &= good
    print(f"  {'and the exemption names the Ind AS mechanic':<52}"
          f"{'ok' if good else '** GENERIC **'}")

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
    print("TIER 2 MEASURES AND NEVER BLOCKS")
    print("-" * 78)
    # THE CENTRAL PROPERTY of the new design, and the one a future refactor is
    # most likely to break by "tidying up" the unused return value: a name that
    # fails every Tier 2 rule still PASSES the gate, and the record says the old
    # rules would have thrown it away. If these flip to VETO, the gate has been
    # switched back on silently.
    def t2(label, annuals, sym="X", want_veto=True, want_in_reason=None):
        nonlocal ok
        v = F.evaluate(sym, annuals, HOLD_OK, [])
        m = v.details.get("tier2", {})
        good = (v.verdict == VERDICT_PASS
                and m.get("would_veto") is want_veto)
        if good and want_in_reason:
            good = want_in_reason.lower() in m.get("would_veto_reason", "").lower()
        ok &= good
        wv = {True: "would_veto", False: "would_pass", None: "unestablished"}[
            m.get("would_veto")]
        print(f"  {label:<46}{v.verdict:<8}{wv:<14}{'ok' if good else '** WRONG **'}")
        return m

    low_roe = [year(f"FY{2026-i}", pat=80 * CR) for i in range(3)]     # 8% ROE
    m = t2("ROE 8% is recorded, not vetoed", low_roe,
           want_in_reason="average roe")
    good = m.get("roe_pct") == 8.0
    ok &= good
    print(f"      roe_pct recorded as {m.get('roe_pct')}"
          f"{'' if good else '  ** want 8.0 **'}")
    levered = [year(f"FY{2026-i}", debt=1500 * CR) for i in range(3)]  # D/E 1.5
    m = t2("D/E 1.50 is recorded, not vetoed", levered,
           want_in_reason="debt-to-equity 1.50")
    good = m.get("de") == 1.5
    ok &= good
    print(f"      de recorded as {m.get('de')}"
          f"{'' if good else '  ** want 1.5 **'}")
    burn = [year("FY2026", cfo=10 * CR, capex=900 * CR),
            year("FY2025", cfo=10 * CR, capex=900 * CR),
            year("FY2024")]
    m = t2("cash burn is recorded, not vetoed", burn,
           want_in_reason="not positive in both years")
    good = m.get("fcf_positive_years") == 0 and len(m.get("fcf") or []) == 2
    ok &= good
    print(f"      fcf_positive_years {m.get('fcf_positive_years')} of "
          f"{len(m.get('fcf') or [])}{'' if good else '  ** WRONG **'}")
    # and the clean case is recorded as clean, so would_veto is a real signal
    # rather than a constant
    t2("a healthy name records would_pass", healthy(), want_veto=False,
       want_in_reason="quality floor met")
    # "could not tell" is not "passed": an unmeasurable check with no failures
    # must leave would_veto None, never False.
    t2("capex absent -> unestablished, not a pass",
       [year(f"FY{2026-i}", capex=None) for i in range(3)],
       want_veto=None, want_in_reason="free cash flow needs")

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
    # CHANGED DELIBERATELY. Only two annual years are parseable from NSE, for
    # every symbol, so a rule needing three can never run. Treating that like
    # opacity made every verdict UNPARSEABLE and blocked the whole universe --
    # at which point the layer gets switched off, which is worse than a pass
    # that states what it did not check. It is a PASS that NAMES the gap.
    v = F.evaluate("X", healthy(2), HOLD_OK, [])
    good = (v.verdict == VERDICT_PASS and v.not_evaluated
            and any("rising leverage" in x for x in v.not_evaluated))
    ok &= good
    print(f"  {'2 filings: structural gap named, not blocking':<46}"
          f"{v.verdict:<13}{'ok' if good else '** WRONG **'}")
    if good:
        print(f"      not_evaluated: {v.not_evaluated[0][:66]}")
    case("announcements not checked (None)",
         F.evaluate("X", healthy(), HOLD_OK, None),
         VERDICT_UNPARSEABLE, "announcements")
    # HALF the auditor rule is unreadable and a PASS must say so. NSE has no
    # category for an audit opinion and the announcement text is boilerplate, so
    # "no auditor event found" cannot be read as "the opinion was clean".
    v = F.evaluate("X", healthy(), HOLD_OK, [])
    good = (v.verdict == VERDICT_PASS
            and any("opinion not evaluable" in x for x in v.not_evaluated)
            and "opinion" in v.reason)
    ok &= good
    print(f"  {'a clean auditor check names the opinion gap':<46}"
          f"{v.verdict:<13}{'ok' if good else '** UNSTATED **'}")
    nocapex = [year(f"FY{2026-i}", capex=None) for i in range(3)]
    # NOT a gate case any more -- Tier 2 does not block, so absent capex cannot
    # stop a trade. What must still hold is that it is not read as capex ZERO,
    # which would make a capital-hungry business look cash-generative.
    # the specific trap: capex absent must NOT read as capex zero, which would
    # make a capital-hungry business look cash-generative.
    good = nocapex[0].fcf is None
    ok &= good
    print(f"  {'AnnualFacts.fcf is None when capex is None':<46}"
          f"{'ok' if good else '** TREATED AS ZERO **'}")

    print()
    print("THE WINDOW AND THE EXEMPTION ARE NAMED, NEVER SILENT")
    print("-" * 78)
    v = F.evaluate("X", healthy(), HOLD_OK, [])
    m = v.details["tier2"]
    good = f"{F.ROE_AVERAGE_YEARS}-year window" in m["would_veto_reason"]
    ok &= good
    print(f"  {'a clean tier 2 record states its window':<46}"
          f"{'ok' if good else '** MISSING **'}")
    low = [year(f"FY{2026-i}", pat=50 * CR) for i in range(3)]     # 5% ROE
    m = F.evaluate("X", low, HOLD_OK, []).details["tier2"]
    good = "2-year window" in m["would_veto_reason"] and "5.0%" in m["would_veto_reason"]
    ok &= good
    print(f"  {'an ROE shortfall states its window':<46}"
          f"{'ok' if good else '** MISSING **'}")

    # LEVERAGE_EXEMPT is exempt from a leverage test; nobody else is. The
    # principle is entities whose LIABILITIES ARE NOT DEBT -- deposits and lending
    # liabilities for banks and NBFCs, policyholder float for insurers -- and the
    # exemption is granted by name, never by sector tag. It matters in both
    # directions: the alternative branch BLOCKS, so a wrongly granted exemption
    # lets a symbol trade that should not, and a wrongly withheld one vetoes LICI
    # for the crime of not borrowing money.
    import importlib.util as _il
    _u = _il.spec_from_file_location("u_fin", ROOT / "engine/universe.py")
    _m = _il.module_from_spec(_u); _u.loader.exec_module(_m)
    bank = "HDFCBANK"
    nonbank = "RELIANCE"
    for sym, want in (("HDFCBANK", True), ("BAJFINANCE", True),
                      ("MUTHOOTFIN", True),
                      # float is a claims reserve, not borrowing
                      ("LICI", True), ("HDFCLIFE", True), ("ICICIGI", True),
                      ("RELIANCE", False),
                      # all four carried the exemption under the old
                      # BANKING/FINANCE sector test and must not any more
                      ("ADANIPORTS", False), ("INDIGO", False),
                      ("CONCOR", False), ("BSE", False),
                      # fee and commission businesses: leverage means what it says
                      ("HDFCAMC", False), ("CRISIL", False),
                      # an insurance BROKER carries no float
                      ("POLICYBZR", False)):
        got = F.is_leverage_exempt(sym)
        ok &= got == want
        tag = _m.SYMBOL_SECTOR_MAP.get(sym, "?")
        print(f"  {sym + ' (' + tag + ')':<46}"
              f"{'exempt' if got else 'not exempt':<13}"
              f"{'ok' if got == want else '** WRONG **'}")
    # and the list must not drift out of the universe
    dead = sorted(F.LEVERAGE_EXEMPT - set(_m.ALL_SYMBOLS))
    ok &= not dead
    print(f"  {'every exempt name is a real universe symbol':<46}"
          f"{'ok' if not dead else '** dead: ' + ', '.join(dead[:6])}")
    # no borrowings reported at all, which is the real shape for a lender
    nodebt = [AnnualFacts(fy=f"FY{2026-i}", equity=1000 * CR, pat=200 * CR,
                          cfo=250 * CR, capex=50 * CR) for i in range(3)]
    vb = F.evaluate(bank, nodebt, HOLD_OK, [])
    mb = vb.details["tier2"]
    good = (vb.verdict == VERDICT_PASS and mb["de_exempt"]
            and any("exempt" in x and "bank" in x for x in mb["notes"]))
    # and an insurer's exemption must name FLOAT, not deposits -- a generic
    # reason would hide which category error is being excused
    mi = F.evaluate("LICI", nodebt, HOLD_OK, []).details["tier2"]
    good2 = mi["de_exempt"] and any("float" in x for x in mi["notes"])
    ok &= good2
    ok &= good
    print(f"  {'a lender records leverage EXEMPT':<46}{vb.verdict:<13}"
          f"{'ok' if good else '** WRONG **'}")
    if good:
        print(f"      {[x for x in mb['notes'] if 'exempt' in x][0][:70]}")
    print(f"  {'an insurer exemption names policyholder float':<46}"
          f"{'PASS':<13}{'ok' if good2 else '** GENERIC REASON **'}")
    if good2:
        print(f"      {[x for x in mi['notes'] if 'exempt' in x][0][:70]}")
    # A non-lender with no borrowings reported is opacity, not a category error,
    # and it still BLOCKS -- but via TIER 1, whose rising-leverage test reads the
    # same borrowings. Tier 2 is not what stops this trade, and the two must not
    # be conflated: Tier 2 records the same gap as merely unestablished.
    vn = F.evaluate(nonbank, nodebt, HOLD_OK, [])
    mn = vn.details["tier2"]
    good = (vn.verdict == VERDICT_UNPARSEABLE and vn.tier == "TIER1"
            and not mn["de_exempt"] and mn["would_veto"] is None
            and "D/E" in mn["would_veto_reason"])
    ok &= good
    print(f"  {'a non-lender with no D/E: TIER 1 blocks':<46}{vn.verdict:<13}"
          f"{'ok' if good else '** WRONG **'}")

    print()
    print("THE ROE FLOOR MOVES THE RECORD, NOT THE VERDICT")
    print("-" * 78)
    # The yardstick is still "above 10%", so 10.0% itself does not clear it --
    # but every one of these trades. The floor now decides what the
    # counterfactual says, which is the only thing it is allowed to decide.
    for pat_cr, want_veto in ((110, False), (150, False),
                              (100, True), (90, True), (80, True)):
        ys = [year(f"FY{2026-i}", pat=pat_cr * CR) for i in range(3)]
        v = F.evaluate("X", ys, HOLD_OK, [])
        good = (v.verdict == VERDICT_PASS
                and v.details["tier2"]["would_veto"] is want_veto)
        ok &= good
        print(f"  ROE {pat_cr / 1000 * 100:>5.1f}%  ->  {v.verdict:<8}"
              f"{'would_veto' if want_veto else 'would_pass':<14}"
              f"{'ok' if good else '** WRONG **'}")

    print()
    print("TIER 1 STILL VETOES, WITH TIER 2 ATTACHED")
    print("-" * 78)
    # healthy fundamentals but a pledge problem: must be TIER1 VETO, not PASS
    v = F.evaluate("X", healthy(), holds((60.0, 40.0), (60.0, 40.0)), [])
    good = (v.verdict == VERDICT_VETO and v.tier == "TIER1"
            and v.details.get("tier2", {}).get("would_veto") is False)
    ok &= good
    print(f"  {'tier 1 vetoes a name tier 2 would have kept':<46}"
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
