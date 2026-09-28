#!/usr/bin/env python3
"""
A CACHED ERROR PAGE IS PERMANENT, SO IT MUST NEVER BE CACHED
============================================================
download_all() treats a file's existence as success:

    if fpath.exists():
        results[d] = True
        continue

That is what makes the archive a trustworthy record of what NSE served, and it is
also what made ONE bad fetch permanent. bhavcopy_save writes whatever NSE returns,
so an error page lands on disk under a .csv name, the download counts it as
success, every later run skips it, the parse fails as one warning among hundreds,
and that session is absent from every symbol's history for as long as the file
sits there. Seven of 858 files were HTML; two were real sessions missing from the
whole universe.

On 2026-09-28 a 09:30 IST run asked for THAT DAY's bhavcopy. The session had not
closed, NSE served an error page, and it was cached.

TWO GUARDS, FAILING AT DIFFERENT MOMENTS:
  the date       a session whose bhavcopy cannot exist yet is not requested
  the payload    a response that is not a bhavcopy is never written, and an
                 already-cached one is never counted as success

Deleting a pre-existing bad file stays with repair_bhavcopy --fix, deliberately:
that is not something a nightly job should do on its own initiative. What the
nightly job must not do is call it a success.

Offline: no network. bhavcopy_save is replaced.

    python3 tests/test_bhavcopy_guard.py
"""

import sys
import shutil
import logging
import tempfile
import importlib.util as il
from pathlib import Path
from datetime import date, datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
logging.basicConfig(level=logging.CRITICAL)

_sp = il.spec_from_file_location("d1b", ROOT / "engine/01b_download_bhavcopy.py")
D = il.module_from_spec(_sp)
_sp.loader.exec_module(D)

HTML = (b"<!DOCTYPE html>\n<html><head><title>Error</title></head>"
        b"<body>Resource not found</body></html>\n")
GOOD = (b"SYMBOL,SERIES,DATE1,PREV_CLOSE,OPEN_PRICE,HIGH_PRICE,LOW_PRICE,"
        b"LAST_PRICE,CLOSE_PRICE,AVG_PRICE,TTL_TRD_QNTY,TURNOVER_LACS,"
        b"NO_OF_TRADES,DELIV_QTY,DELIV_PER\n"
        b"RELIANCE,EQ,25-SEP-2026,1400,1405,1420,1395,1410,1412,1408,"
        b"1000000,14080,50000,600000,60\n")


def main() -> int:
    ok = True

    def check(label, got, want, extra=""):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"  {label:<52}{str(got):<14}"
              f"{'ok' if good else f'** want {want} **'}  {extra}")

    print("=" * 78)
    print("A SESSION WHOSE BHAVCOPY CANNOT EXIST YET IS NOT REQUESTED")
    print("-" * 78)
    # 2026-09-28 is a Monday; 09-25 the Friday before; 09-26/27 the weekend.
    for stamp, want, label in (
            ("2026-09-28 09:30", date(2026, 9, 25), "the real incident: mid-session run"),
            ("2026-09-28 17:59", date(2026, 9, 25), "one minute before the publish hour"),
            ("2026-09-28 18:40", date(2026, 9, 28), "the EOD chain, after publication"),
            ("2026-09-28 00:05", date(2026, 9, 25), "just past midnight"),
            ("2026-09-27 11:00", date(2026, 9, 25), "Sunday"),
            ("2026-09-26 20:00", date(2026, 9, 25), "Saturday evening")):
        dt = datetime.fromisoformat(stamp).replace(tzinfo=D.IST)
        check(label, D.last_requestable_session(dt), want, stamp)

    print()
    print("A RESPONSE THAT IS NOT A BHAVCOPY IS NEVER WRITTEN")
    print("-" * 78)
    tmp = Path(tempfile.mkdtemp())
    real_dir, real_save = D.RAW_DIR, D.bhavcopy_save
    d = date(2026, 9, 25)
    fname = D.csv_filename(d)
    try:
        D.RAW_DIR = tmp
        # NSE answers with an error page
        D.bhavcopy_save = lambda dd, out: (Path(out) / D.csv_filename(dd)
                                           ).write_bytes(HTML)
        res = D.download_all([d])
        check("an error page is not counted as downloaded", res[d], False)
        check("  and is not left on disk", (tmp / fname).exists(), False,
              "this is the whole fix: nothing later can mistake it for data")

        # NSE answers with a real bhavcopy
        D.bhavcopy_save = lambda dd, out: (Path(out) / D.csv_filename(dd)
                                           ).write_bytes(GOOD)
        res = D.download_all([d])
        check("a real bhavcopy is kept", res[d], True)
        check("  and stays on disk", (tmp / fname).exists(), True)

        # an empty response is not a bhavcopy either
        (tmp / fname).unlink()
        D.bhavcopy_save = lambda dd, out: (Path(out) / D.csv_filename(dd)
                                           ).write_bytes(b"")
        res = D.download_all([d])
        check("an empty response is rejected", res[d], False)
        check("  and not left on disk", (tmp / fname).exists(), False)

        print()
        print("AN ALREADY-CACHED BAD FILE IS NEVER COUNTED AS SUCCESS")
        print("-" * 78)
        # The poison already on the box. It must stop reading as done -- and it
        # must NOT be deleted here, because repair_bhavcopy owns that on purpose.
        (tmp / fname).write_bytes(HTML)
        D.bhavcopy_save = lambda dd, out: (_ for _ in ()).throw(
            AssertionError("must not re-download a file that exists"))
        res = D.download_all([d])
        check("cached HTML reports failure, not success", res[d], False)
        check("  and is LEFT for repair_bhavcopy --fix", (tmp / fname).exists(),
              True, "deleting history is a deliberate tool, not a nightly job")
        # and a good cached file is still skipped without a download
        (tmp / fname).write_bytes(GOOD)
        res = D.download_all([d])
        check("a good cached file is skipped, no request", res[d], True)
    finally:
        D.RAW_DIR, D.bhavcopy_save = real_dir, real_save
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("WHAT COUNTS AS 'NOT A BHAVCOPY'")
    print("-" * 78)
    from repair_bhavcopy import is_bad
    tmp2 = Path(tempfile.mkdtemp())
    try:
        for label, payload, bad_expected in (
                ("an HTML error page", HTML, True),
                ("an empty file", b"", True),
                ("a JSON error body", b'{"error":"not found"}\n', True),
                ("the legacy SYMBOL header", GOOD, False),
                ("the UDiFF TckrSymb header",
                 b"TradDt,BizDt,Sgmt,Src,FinInstrmTp,TckrSymb,SctySrs,ClsPric\n"
                 b"2026-09-25,2026-09-25,CM,NSE,STK,RELIANCE,EQ,1410\n", False)):
            f = tmp2 / "probe.csv"
            f.write_bytes(payload)
            why = is_bad(f)
            check(label, bool(why), bad_expected, why[:34])
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    print("-" * 78)
    print("BHAVCOPY GUARD:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
