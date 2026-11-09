#!/usr/bin/env python3
"""SHARE COUNT FROM THE SHP FILING: exactly one grand-total fact or nothing.

engine/tier1_fetch.parse_shares reads NumberOfShares under the filing's grand
total context. A missing or duplicated fact must come back None with a note --
sizing refuses a symbol with no count, so a guess here would become a position.
Planted XBRL fragments only; nothing is fetched.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
from engine import tier1_fetch as T                             # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


def fact(el, ctx, v):
    return f'<in-bse-shp:{el} contextRef="{ctx}" unitRef="shares" decimals="0">{v}</in-bse-shp:{el}>'


TOT = T.SHARES_TOTAL_CONTEXT
PRO = "ShareholdingOfPromoterAndPromoterGroup_ContextI"
GOOD = "".join([
    fact("NumberOfShares", TOT, "13532538722"),
    fact("NumberOfFullyPaidUpEquityShares", TOT, "13312748854"),
    fact("NumberOfShares", "Indian_ContextI", "999"),           # another context
    fact("NumberOfShares", PRO, "6825842850"),
    fact("ShareholdingAsAPercentageOfTotalNumberOfShares", PRO, "0.5044"),
    '<in-bse-shp:DateOfReport contextRef="MainI">2026-09-30</in-bse-shp:DateOfReport>',
])

r = T.parse_shares(GOOD)
check("the grand total (column VII) is read", r["total"], 13532538722.0)
check("  not another context's NumberOfShares", r["total"] != 999.0, True)
check("fully paid (column IV) recorded", r["fully_paid"], 13312748854.0)
check("promoter shares recorded for the cross-check", r["promoter_shares"], 6825842850.0)
check("promoter fraction recorded", r["promoter_fraction"], 0.5044)
check("report date recorded", r["report_date"], "2026-09-30")

r = T.parse_shares(GOOD.replace(f'contextRef="{TOT}" unitRef="shares" decimals="0">13532538722', 'contextRef="Other_ContextI" unitRef="shares" decimals="0">13532538722'))
check("no grand-total fact -> None", r["total"], None)
check("  and a note says why", "need exactly one" in " ".join(r["notes"]), True)

r = T.parse_shares(GOOD + fact("NumberOfShares", TOT, "1"))
check("two grand-total facts -> None, not a pick", r["total"], None)

r = T.parse_shares("")
check("an empty document -> None, never raises", r["total"], None)

rec = T._shares_record({"date": "30-SEP-2026", "broadcastDate": "21-OCT-2026 16:25:15",
                        "xbrl": "https://x/y.xml"}, GOOD)
check("record carries the quarter it came from", rec["quarter_end"], "30-SEP-2026")
check("  and the filing date", rec["filed"], "21-OCT-2026 16:25:15")
check("  and no market cap field", any("cap" in k for k in rec), False)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the share count is one grand-total fact or None")
