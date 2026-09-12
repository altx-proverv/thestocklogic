#!/usr/bin/env python3
"""
TIER 1 SOURCES: A ZERO MUST BE STATED, NEVER INFERRED FROM SILENCE
==================================================================
Tier 1 is the only fundamentals gate, so its inputs decide who trades. Two of
them are shaped exactly like the traps this codebase keeps finding:

  1. A company with no pledge OMITS the encumbrance elements. Reading that as
     "pledge = 0" is the same error as reading absent capex as zero capex --
     except here it is worse, because the veto exists to catch the companies most
     likely to file badly. What makes zero legitimate is a SEPARATE BOOLEAN that
     states it. false + silence is zero; true + silence, or silence + silence,
     must block.

  2. EncumberedShareUnderPledgedAsPercentageOfTotalNumberOfShares appears many
     times per filing with a DIFFERENT DENOMINATOR each time and no hint in the
     name. GMRAIRPORT Jun-2026: 16.44% of promoter holding, 11.04% of total
     shares, same element. Picking the wrong context yields a plausible number.

And one judgment that must not drift: "Change in Auditors" is NOT a veto. 189 of
them in a six-week window against 28 resignations, the filers being HUDCO, HMT,
NTPCGREEN, BPCL, NFL, CONCOR, RAILTEL, ENGINERSIN -- PSUs reappointing CAG
auditors. Vetoing it empties the universe on a compliance formality.

Offline: every filing here is constructed. Real values are reproduced from
filings read by hand, and named where they are.

    python3 tests/test_tier1_sources.py
"""

import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from engine import tier1_fetch as T      # noqa: E402

PROM = "ShareholdingOfPromoterAndPromoterGroup_ContextI"
TOTAL = "ShareholdingPattern_ContextI"


def shp(prom_shares=None, prom_pct=None, pledge_num=None, pledge_pct=None,
        flag=None, enc_pct=None, extra=""):
    """A minimal SHP filing. Anything passed as None is simply absent."""
    p = []
    if prom_pct is not None:
        p.append(f'<in-shp:{T.EL_HOLDING_PCT} contextRef="{PROM}">{prom_pct}'
                 f'</in-shp:{T.EL_HOLDING_PCT}>')
    if prom_shares is not None:
        p.append(f'<in-shp:{T.EL_SHARES} contextRef="{PROM}">{prom_shares}'
                 f'</in-shp:{T.EL_SHARES}>')
    if pledge_num is not None:
        p.append(f'<in-shp:{T.EL_PLEDGE_NUM} contextRef="{PROM}">{pledge_num}'
                 f'</in-shp:{T.EL_PLEDGE_NUM}>')
    if pledge_pct is not None:
        p.append(f'<in-shp:{T.EL_PLEDGE_PCT} contextRef="{PROM}">{pledge_pct}'
                 f'</in-shp:{T.EL_PLEDGE_PCT}>')
    if enc_pct is not None:
        p.append(f'<in-shp:{T.EL_ENCUMBERED_PCT} contextRef="{PROM}">{enc_pct}'
                 f'</in-shp:{T.EL_ENCUMBERED_PCT}>')
    if flag is not None:
        p.append(f'<in-shp:{T.EL_PLEDGE_FLAG} contextRef="{PROM}">'
                 f'{"true" if flag else "false"}</in-shp:{T.EL_PLEDGE_FLAG}>')
    return "<xbrli:xbrl>" + "".join(p) + extra + "</xbrli:xbrl>"


def ann(*descs):
    return [{"desc": d, "sort_date": "2026-08-01 10:00:00"} for d in descs]


def main() -> int:
    ok = True

    def check(label, got, want, detail=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<52}{str(got):<12}"
              f"{'ok' if good else f'** want {want} **'}")
        if detail and not good:
            print(f"      {detail}")

    print("=" * 78)
    print("PLEDGE ZERO IS A STATED ZERO")
    print("-" * 78)
    # RELIANCE Jun-2026: no encumbrance elements at all, boolean explicitly false.
    _, pledged, _, notes = T.parse_shp(shp(prom_pct=0.5048, prom_shares=6832000000,
                                           flag=False))
    check("flag=false, no elements -> stated zero", pledged, 0.0)
    good = any("explicit zero" in n for n in notes)
    ok &= good
    print(f"  {'and the record says WHY it is zero':<52}"
          f"{'ok' if good else '** the note is missing **'}")
    # the two branches that must NOT become zero
    _, pledged, _, notes = T.parse_shp(shp(prom_pct=0.60, prom_shares=1000,
                                           flag=True))
    check("flag=TRUE, no elements -> blocks", pledged, None,
          "a company saying 'yes we pledged' with no number is the "
          "single most important case not to read as zero")
    good = any("NOT zero" in n for n in notes)
    ok &= good
    print(f"  {'and it is named as unparseable, not zero':<52}"
          f"{'ok' if good else '** MISSING **'}")
    _, pledged, _, _ = T.parse_shp(shp(prom_pct=0.60, prom_shares=1000))
    check("no flag, no elements -> blocks", pledged, None)

    print()
    print("THE DENOMINATOR: THE PROMOTER CONTEXT, CROSS-CHECKED")
    print("-" * 78)
    # GMRAIRPORT Jun-2026, read by hand: 1,166,000,000 pledged of 7,091,579,906
    # promoter shares = 16.442%. The filing reports 0.1644 under the promoter
    # context and 0.1104 under ShareholdingPattern_ContextI -- total shares.
    real = shp(prom_pct=0.6716, prom_shares=7091579906,
               pledge_num=1166000000, pledge_pct=0.1644, enc_pct=0.1644,
               extra=f'<in-shp:{T.EL_PLEDGE_PCT} contextRef="{TOTAL}">0.1104'
                     f'</in-shp:{T.EL_PLEDGE_PCT}>')
    prom_pct, pledged, enc, notes = T.parse_shp(real)
    check("GMRAIRPORT pledge (16.44 of promoter, not 11.04)", pledged, 16.44)
    check("  and promoter % comes back as 67.16", prom_pct, 67.16)
    good = any("cross-check" in n for n in notes)
    ok &= good
    print(f"  {'the value is cross-checked against counts':<52}"
          f"{'ok' if good else '** NOT CHECKED **'}")
    # a reported percentage that does not match the counts means the context
    # match is wrong. Refusing is the point: a wrong denominator is plausible.
    bad = shp(prom_pct=0.6716, prom_shares=7091579906,
              pledge_num=1166000000, pledge_pct=0.1104)
    _, pledged, _, notes = T.parse_shp(bad)
    check("reported % disagreeing with counts -> blocks", pledged, None)
    good = any("cross-check FAILED" in n for n in notes)
    ok &= good
    print(f"  {'and the disagreement is stated':<52}"
          f"{'ok' if good else '** SILENT **'}")
    # Two promoter contexts for the REPORTED percentage: picking one would be a
    # guess, so it is discarded -- but the share counts are still unambiguous, so
    # the number is computed from them rather than thrown away. The reason is
    # recorded either way; what must never happen is a silent pick of 0.90.
    two = shp(prom_pct=0.60, prom_shares=1000, pledge_num=100, pledge_pct=0.10) \
        .replace("</xbrli:xbrl>",
                 f'<in-shp:{T.EL_PLEDGE_PCT} contextRef="{PROM}_Extra">0.90'
                 f'</in-shp:{T.EL_PLEDGE_PCT}></xbrli:xbrl>')
    _, pledged, _, notes = T.parse_shp(two)
    check("ambiguous reported % -> computed from counts", pledged, 10.0)
    good = any("ambiguous" in n for n in notes)
    ok &= good
    print(f"  {'and the ambiguity is on the record':<52}"
          f"{'ok' if good else '** SILENT PICK **'}")
    # but if the COUNTS are ambiguous too, there is nothing left to trust
    two_n = two.replace("</xbrli:xbrl>",
                        f'<in-shp:{T.EL_SHARES} contextRef="{PROM}_Extra">9'
                        f'</in-shp:{T.EL_SHARES}></xbrli:xbrl>')
    _, pledged, _, _ = T.parse_shp(two_n)
    check("ambiguous counts as well -> blocks", pledged, None)
    # only counts, no reported percentage: compute it
    _, pledged, _, _ = T.parse_shp(shp(prom_pct=0.60, prom_shares=4000,
                                       pledge_num=1000))
    check("counts only -> computed", pledged, 25.0)

    print()
    print("PLEDGE IS NOT THE WHOLE OF ENCUMBRANCE")
    print("-" * 78)
    # VEDL Jun-2026: pledge 0%, total promoter encumbrance 99.99%, all of it
    # non-disposal undertakings and "other". Tier 1's rule names pledge, so this
    # does not gate -- but a 100% encumbered promoter must not vanish.
    _, pledged, enc, notes = T.parse_shp(shp(prom_pct=0.5472, prom_shares=2139700000,
                                             flag=False, enc_pct=0.9999))
    check("VEDL pledge 0 with encumbrance 99.99 recorded", pledged, 0.0)
    check("  encumbered_pct is kept", enc, 99.99)
    good = any("Recorded, not gated" in n for n in notes)
    ok &= good
    print(f"  {'and the gap is stated, not swallowed':<52}"
          f"{'ok' if good else '** SWALLOWED **'}")

    print()
    print("AUDITOR EVENTS COME FROM THE STRUCTURED desc")
    print("-" * 78)
    veto, noted = T.classify_announcements(ann("Resignation of Statutory Auditor"))
    check("resignation of statutory auditor -> veto", len(veto), 1)
    veto, noted = T.classify_announcements(ann("Initiation of Forensic Audit"))
    check("initiation of forensic audit -> veto", len(veto), 1)
    # THE ONE THAT MATTERS. 189 per six weeks, mostly PSUs reappointing CAG
    # auditors. If this ever returns a veto, the universe empties.
    veto, noted = T.classify_announcements(ann("Change in Auditors"))
    check("change in auditors -> NOT a veto", len(veto), 0,
          "189 in six weeks vs 28 resignations; the filers were PSUs")
    check("  but it IS counted", len(noted), 1)
    veto, noted = T.classify_announcements(
        ann("Copy of Newspaper Publication", "Outcome of Board Meeting",
            "Resignation of Director/KMP/SMP", "Change in Director(s)"))
    check("routine categories -> nothing", len(veto) + len(noted), 0,
          "a director resigning is not an auditor resigning")
    veto, _ = T.classify_announcements([])
    check("no announcements in the window -> no veto", len(veto), 0)

    print()
    print("A BOILERPLATE TEXT MENTIONING AN AUDITOR IS NOT A SIGNAL")
    print("-" * 78)
    # The free text for auditor rows restates the category, so classifying on it
    # adds false positives and no recall. This asserts the text is NOT consulted.
    rows = [{"desc": "Analysts/Institutional Investor Meet/Con. Call Updates",
             "attchmntText": "The auditor resigned last year, as previously "
                             "disclosed, and a qualified opinion was discussed.",
             "sort_date": "2026-08-01 10:00:00"}]
    veto, noted = T.classify_announcements(rows)
    check("auditor words in an unrelated row -> nothing", len(veto), 0)

    print()
    print("THE PLACEHOLDER URL IS STILL NOT A URL")
    print("-" * 78)
    for v, want in ((None, False), ("", False), ("-", False),
                    ("https://nsearchives.nseindia.com/corporate/xbrl/-", False),
                    ("https://nsearchives.nseindia.com/corporate/xbrl/SHP_1.xml", True)):
        got = T._is_url(v)
        ok &= got == want
        print(f"  {str(v)[-46:]:<52}{str(got):<12}"
              f"{'ok' if got == want else '** WRONG **'}")

    print()
    print("A SYMBOL WITH AN AMPERSAND MUST SURVIVE THE QUERY STRING")
    print("-" * 78)
    # ?symbol=M&M asks NSE for symbol "M" and a stray parameter "M". NSE answers
    # 200 with an empty payload rather than an error, so the symbol reports "no
    # filings" and reads as a company that does not file. It blocked M&M, M&MFIN,
    # J&KBANK, ARE&M and GVT&D -- M&M being a top-20 large cap -- and nothing in
    # the pipeline looked wrong. Encoding is the whole fix; this pins it.
    for sym, want in (("M&M", "M%26M"), ("J&KBANK", "J%26KBANK"),
                      ("ARE&M", "ARE%26M"), ("GVT&D", "GVT%26D"),
                      ("RELIANCE", "RELIANCE")):
        got = T._q(sym)
        ok &= got == want
        print(f"  {sym:<16}-> {got:<34}{'ok' if got == want else f'** want {want} **'}")
    url = T.SHP_MASTER.format(symbol=T._q("M&M"))
    good = url.count("&") == 1 and "symbol=M%26M" in url
    ok &= good
    print(f"  {'the built url has one & (the separator)':<52}"
          f"{'ok' if good else '** SPLIT: ' + url[-40:]}")

    print("-" * 78)
    print("TIER 1 SOURCES:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
