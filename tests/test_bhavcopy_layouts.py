#!/usr/bin/env python3
"""
BOTH BHAVCOPY LAYOUTS, AND THE UNITS BETWEEN THEM
=================================================
NSE switched the daily file to the UDiFF layout between 2026-08-18 and
2026-09-10. Both are on disk under the same filename pattern:

    old      SYMBOL, SERIES, CLOSE_PRICE, TURNOVER_LACS   -- lakhs
    UDiFF    TckrSymb, SctySrs, ClsPric, TtlTrfVal        -- RUPEES

universe_filter knew only the old names and skipped everything else in silence,
so its session count was computed over files it could parse rather than sessions
that exist. On the box that reported RELIANCE and TCS as having fewer than 250
trading days and proposed dropping all 495 symbols.

The units are the more dangerous half. TtlTrfVal is in rupees and
TURNOVER_LACS in lakhs -- a factor of 1e5. Read interchangeably, every symbol
looks 100,000x more liquid and the Rs5 crore filter stops filtering. That
failure produces a plausible-looking output, which is why it gets a test rather
than a comment.

Fixtures are built here rather than fetched, so this runs offline and does not
depend on NSE being reachable or on which layout it is serving today.
"""

import sys
import shutil
import logging
import tempfile
import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

spec = importlib.util.spec_from_file_location("uf", ROOT / "tools/universe_filter.py")
uf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uf)

# RELIANCE-shaped: Rs1,200 crore of turnover on a Rs1,274 close.
CRORE = 1200.0
OLD_HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, "
              "LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, "
              "TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")
UDIFF_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,"
                "SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,FinInstrmNm,"
                "OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
                "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,"
                "TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def write_old(path: Path, symbol="RELIANCE", close=1274.0, crore=CRORE):
    lacs = crore * 100                      # crore -> lakhs
    path.write_text(
        OLD_HEADER + "\n"
        f"{symbol}, EQ, 18-Aug-2026, 1300, 1310, 1330, 1260, 1275, {close}, "
        f"1280, 9000000, {lacs}, 179169, 4000000, 45\n")


def write_udiff(path: Path, symbol="RELIANCE", close=1274.0, crore=CRORE):
    rupees = crore * 1e7                    # crore -> rupees
    path.write_text(
        UDIFF_HEADER + "\n"
        f"2026-09-10,2026-09-10,CM,NSE,STK,1,INE002A01018,{symbol},EQ,,,,,"
        f"RELIANCE INDUSTRIES,1310,1330,1260,{close},1275,1300,,,,,"
        f"9000000,{rupees},179169,,,,,,,\n")


def main() -> int:
    ok = True
    print("EACH LAYOUT NORMALISES, AND THE UNITS RECONCILE")
    print("-" * 78)
    tmp = Path(tempfile.mkdtemp())
    seen = {}
    for name, writer in (("old", write_old), ("UDiFF", write_udiff)):
        p = tmp / f"{name}.csv"
        writer(p)
        n = uf._normalise(pd.read_csv(p))
        row = n[(n["series"] == "EQ") & (n["symbol"] == "RELIANCE")].iloc[0]
        got = row["turnover_lacs"] / 100     # lakhs -> crore
        seen[name] = got
        good = abs(got - CRORE) < 0.01
        ok &= good
        print(f"  {name:<8} turnover Rs{got:>9,.1f} crore   "
              f"{'ok' if good else f'** expected Rs{CRORE:,.0f} crore **'}")
    same = abs(seen['old'] - seen['UDiFF']) < 0.01
    ok &= same
    print(f"  {'the two agree:':<30}{'ok' if same else '** 1e5 UNIT BUG **'}")

    print()
    print("AN UNRECOGNISED LAYOUT IS FATAL, NOT SKIPPED")
    print("-" * 78)
    try:
        uf._normalise(pd.DataFrame({"foo": [1], "bar": [2]}))
        print("  ** returned instead of raising — a third layout would be skipped **")
        ok = False
    except ValueError as e:
        print(f"  ok — ValueError: {str(e)[:56]}")

    print()
    print("EVERY SESSION IS COUNTED, WHATEVER ITS LAYOUT")
    print("-" * 78)
    # This is the actual bug: sessions counted over files the tool could parse.
    raw = Path(tempfile.mkdtemp())
    write_old(raw / "cm18Aug2026bhav.csv")
    write_udiff(raw / "cm10Sep2026bhav.csv")
    write_old(raw / "cm19Aug2026bhav.csv")
    uf.RAW_DIR = raw
    g, first, last = uf.bhavcopy_stats(lookback=300)
    n = int(g.loc["RELIANCE", "sessions"])
    good = n == 3
    ok &= good
    print(f"  3 files (2 old + 1 UDiFF) -> sessions={n}   "
          f"{'ok' if good else '** UDiFF file was skipped **'}")

    print()
    print("AN UNPARSEABLE FILE STOPS THE FUNNEL")
    print("-" * 78)
    (raw / "cm20Aug2026bhav.csv").write_text("<!DOCTYPE html>\n<html>error</html>\n")
    try:
        uf.bhavcopy_stats(lookback=300)
        print("  ** computed a funnel from a partial read **")
        ok = False
    except RuntimeError as e:
        print(f"  ok — RuntimeError: {str(e)[:58]}")

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(raw, ignore_errors=True)
    print("-" * 78)
    print("BHAVCOPY LAYOUTS:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
