#!/usr/bin/env python3
"""
A BHAVCOPY'S FILENAME IS NOT EVIDENCE OF WHAT IS INSIDE IT
===========================================================
01b_download_bhavcopy stamps every row with the date from the FILENAME:

    df["date"] = pd.Timestamp(d)       # d comes from the file it asked for

The file also carries its own trading date -- TradDt in the 2026+ layout, DATE1
in the legacy one -- and nothing compared the two. When NSE serves a different
session than the one requested, the rows land under the wrong date and the
series gains a duplicate bar that is indistinguishable from a real one.

TWO LIVE EXAMPLES, both found on 2026-10-22:

  cm14Sep2026bhav.csv   contains 11-Sep-2026 data, so 2026-09-14 is a copy of
                        2026-09-11 -- identical OHLC *and* identical volume,
                        which is not something two sessions do
  cm20Oct2026bhav.csv   contains 2026-10-19 data, so the whole 20 Oct session
                        in every parquet is a duplicate of 19 Oct. RELIANCE,
                        TCS and INDIAGLYCO all carry byte-identical bars for
                        the two dates, volume included

A duplicate bar is not a cosmetic problem. It puts a prev_close from one
session against a close from another, which is exactly the signal
engine/price_basis.feed_discontinuities exists to catch -- and the signals
batch of 2026-10-20 was built on the duplicate.

WHY THIS IS A RATCHET AND NOT A ZERO-TOLERANCE ASSERTION. 25 files disagreed on
2026-10-22 and deleting the backlog is explicitly not this change's job --
repair_bhavcopy --fix owns that, and a nightly job must not delete history on
its own initiative. So the known set is recorded in
engine/repair_bhavcopy.KNOWN_MISDATED, production's own list, and a file outside
it fails. That keeps the backlog visible instead of silent and makes the 26th
loud.

2026-10-20 IS NOT IN THAT SET, which is what makes it fixed rather than
recorded: NSE serves no bhavcopy for that date on either endpoint, so there is
nothing to recover and parse_bhavcopy_csv now refuses the file instead of
stamping 19-Oct data with it. The session is ABSENT, which is the honest state.

The archive is gitignored, so its contents differ between the box (962 files)
and a development machine (953). A recorded name that is absent locally is not
a failure; a mismatch that is not recorded is.
"""
import csv
import re
import sys
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
BHAV = ROOT / "data" / "raw" / "bhavcopy"

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<58} {str(got)[:26]:<28} {'ok' if ok else 'FAIL':<6} {note}")


# ── THE RECORDED SET LIVES IN PRODUCTION, NOT HERE ────────────────
# It was duplicated in this file, which is the same single-source-of-truth
# mistake the price-basis detector was fixed for. engine/repair_bhavcopy owns
# the list because it owns the repair; this file asserts against it.
from engine.repair_bhavcopy import (KNOWN_MISDATED as KNOWN_MISMATCHED,  # noqa: E402
                                    is_bad, internal_date as _internal_date)

# Saved HTML error pages, not bhavcopies. is_bad() rejects these at write time;
# they are listed so the date check can report them separately instead of
# counting them as date mismatches.
KNOWN_NOT_A_BHAVCOPY = {
    "cm02Oct2025bhav.csv", "cm10Apr2025bhav.csv", "cm15Aug2025bhav.csv",
    "cm25Dec2025bhav.csv", "cm27Aug2025bhav.csv",
}

def internal_date(path):
    """Production's reader, so the test cannot drift from what ships."""
    return _internal_date(Path(path))


def filename_date(name):
    m = re.match(r"cm(\d{2})([A-Za-z]{3})(\d{4})bhav\.csv$", name)
    if not m:
        return None
    try:
        return datetime.strptime("".join(m.groups()), "%d%b%Y").date()
    except ValueError:
        return None


print()
print("THE CHECK ITSELF, ON SYNTHETIC FILES — so this bites with no archive")
print("-" * 104)
import tempfile                                                   # noqa: E402

with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    agree = td / "cm15Jun2026bhav.csv"
    agree.write_text("TradDt,TckrSymb,ClsPric\n2026-06-15,X,10\n")
    disagree = td / "cm16Jun2026bhav.csv"
    disagree.write_text("TradDt,TckrSymb,ClsPric\n2026-06-15,X,10\n")
    legacy = td / "cm17Jun2026bhav.csv"
    legacy.write_text("SYMBOL,SERIES,DATE1,CLOSE_PRICE\nX,EQ,17-Jun-2026,10\n")
    legacy_bad = td / "cm18Jun2026bhav.csv"
    legacy_bad.write_text("SYMBOL,SERIES,DATE1,CLOSE_PRICE\nX,EQ,17-Jun-2026,10\n")
    html = td / "cm19Jun2026bhav.csv"
    html.write_text("<!DOCTYPE html>\n<html><body>nope</body></html>\n")

    check("a matching new-format file passes",
          internal_date(agree)[0] == filename_date(agree.name), True)
    check("a mis-dated new-format file is caught",
          internal_date(disagree)[0] == filename_date(disagree.name), False,
          "file says 2026-06-15, name says 06-16")
    check("a matching legacy file passes",
          internal_date(legacy)[0] == filename_date(legacy.name), True)
    check("a mis-dated legacy file is caught",
          internal_date(legacy_bad)[0] == filename_date(legacy_bad.name), False)
    got, why = internal_date(html)
    check("an HTML error page has no date to compare",
          (got is None and "no date column" in why), True, why[:34])

print()
print("THE LIVE ARCHIVE")
print("-" * 104)
if not BHAV.exists():
    check("the archive directory exists", False, True,
          f"{BHAV} absent — the synthetic checks above still ran")
else:
    files = sorted(BHAV.glob("cm*bhav.csv"))
    agree, mismatched, notbhav, unparsed = [], [], [], []
    for f in files:
        fd = filename_date(f.name)
        if fd is None:
            unparsed.append((f.name, "unparseable filename"))
            continue
        idt, why = internal_date(f)
        if idt is None:
            (notbhav if "no date column" in why else unparsed).append((f.name, why))
            continue
        (agree if idt == fd else mismatched).append(f.name)

    print(f"  files scanned                : {len(files)}")
    print(f"  filename date == file's date : {len(agree)}")
    print(f"  DISAGREE                     : {len(mismatched)}")
    print(f"  not a bhavcopy (HTML etc.)   : {len(notbhav)}")
    print(f"  otherwise unparsed           : {len(unparsed)}")
    print()

    # THE RATCHET. A recorded name that is absent on this machine is fine; a
    # mismatch nobody recorded is a new defect and fails.
    unrecorded = sorted(set(mismatched) - KNOWN_MISMATCHED)
    check("every mis-dated bhavcopy is a recorded one", unrecorded, [],
          f"{len(KNOWN_MISMATCHED)} recorded")
    stale = sorted(KNOWN_MISMATCHED - set(mismatched) - {f.name for f in files})
    print(f"  recorded but not present on this machine: {len(stale)}"
          + (f" ({', '.join(stale[:4])}...)" if stale else ""))
    fixed = sorted((KNOWN_MISMATCHED & {f.name for f in files}) - set(mismatched))
    print(f"  recorded, present, and now AGREEING: {len(fixed)}"
          + (f" -> drop from KNOWN_MISMATCHED: {fixed}" if fixed else ""))

    new_html = sorted(set(n for n, _ in notbhav) - KNOWN_NOT_A_BHAVCOPY)
    check("no NEW saved error page", new_html, [],
          f"{len(KNOWN_NOT_A_BHAVCOPY)} recorded")
    check("nothing is unparsed for an unrecorded reason",
          [n for n, _ in unparsed], [])

    # The live-relevant subset, reported rather than enforced: a mis-dated file
    # inside the 252-session level window is still corrupting a published level
    # today, and that is a decision for the operator, not for this test.
    recent = sorted(n for n in mismatched
                    if (filename_date(n) or datetime(1900, 1, 1).date()).year >= 2026)
    print(f"  mis-dated files dated 2026 (still inside every live window): "
          f"{len(recent)}")
    for n in recent:
        fd, idt = filename_date(n), internal_date(BHAV / n)[0]
        print(f"      {n:<24} name {fd}  file {idt}  "
              f"({(idt - fd).days:+d} day(s))")

print()
print("THE DOWNLOADER'S TWO GUARDS, FED THE TWO REAL PAYLOADS")
print("-" * 104)
# THE EXACT TWO THINGS THAT GOT THROUGH. An HTML error page (five of those are
# on disk, each one a session absent from every parquet) and a well-formed
# bhavcopy for the wrong day (cm20Oct2026, which produced a duplicate 20 Oct
# across the whole universe). Both are fed to the guards that ship.
import importlib.util as _ilu                                     # noqa: E402

_spec = _ilu.spec_from_file_location("dl", ROOT / "engine/01b_download_bhavcopy.py")
_dl = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_dl)

_real20 = BHAV / "cm20Oct2026bhav.csv"
with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    # (1) THE HTML ERROR PAGE.
    _html = td / "cm21Jul2026bhav.csv"
    _html.write_text("<!DOCTYPE html>\n<html><head><title>Error</title></head>"
                     "<body>Access Denied</body></html>\n")
    check("an HTML error page is rejected at write time",
          bool(is_bad(_html)), True, is_bad(_html)[:40])

    # (2) THE REAL cm20Oct2026 CONTENT: legacy layout, DATE1 = 19-Oct-2026,
    # under a 20-Oct name. Copied verbatim when the archive has it; otherwise
    # reconstructed with the same defining shape, so this bites on any machine.
    _wrong = td / "cm20Oct2026bhav.csv"
    if _real20.exists():
        _wrong.write_bytes(_real20.read_bytes())
        _src = f"the real file ({_real20.stat().st_size:,} bytes)"
    else:
        _wrong.write_text(
            "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, "
            "LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY\n"
            "RELIANCE, EQ, 19-Oct-2026, 1211.20, 1212.00, 1228.20, 1201.00, "
            "1202.00, 1202.00, 1210.00, 15572554\n")
        _src = "a replica (archive lacks the file on this machine)"
    _why = is_bad(_wrong)
    check("a bhavcopy for the WRONG DAY is rejected", bool(_why), True, _src)
    check("  and the reason names both dates",
          ("2026-10-19" in _why and "2026-10-20" in _why), True, _why[:46])

    # (3) THE PARSE REFUSES IT rather than stamping from the filename, which is
    # the behaviour that produced the duplicate.
    _dl._REFUSED.clear()
    _got = _dl.parse_bhavcopy_csv(_wrong, date(2026, 10, 20))
    check("parse_bhavcopy_csv returns NO rows for it", len(_got), 0,
          "an empty frame is the refusal; build_parquets skips the session")
    check("  and records the refusal for the alert", len(_dl._REFUSED), 1)

    # (4) THE CONTROL. A correctly dated file passes both and parses.
    _ok = td / "cm19Oct2026bhav.csv"
    _ok.write_text(
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
        "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, NO_OF_TRADES, "
        "DELIV_QTY, DELIV_PER\n"
        "RELIANCE, EQ, 19-Oct-2026, 1211.20, 1212.00, 1228.20, 1201.00, "
        "1202.00, 1202.00, 1210.00, 15572554, 100, 50, 40\n")
    check("a correctly dated file passes the guard", is_bad(_ok), "")
    _dl._REFUSED.clear()
    _rows = _dl.parse_bhavcopy_csv(_ok, date(2026, 10, 19))
    check("  and parses to rows", len(_rows) > 0, True, f"{len(_rows)} row(s)")
    check("  with nothing refused", len(_dl._REFUSED), 0)

    # (5) THE BACKLOG IS NOT SILENTLY DROPPED. A grandfathered name is still
    # ingested, loudly, because Hemal asked for the list before anything goes.
    _grand = td / "cm14Sep2026bhav.csv"
    _grand.write_text(
        "SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, "
        "LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, NO_OF_TRADES, "
        "DELIV_QTY, DELIV_PER\n"
        "INDIAGLYCO, BE, 11-Sep-2026, 310.40, 310.00, 310.00, 294.90, 294.90, "
        "294.90, 297.66, 164145, 3256, 50, 40\n")
    check("a RECORDED mis-dated file is still ingested",
          len(_dl.parse_bhavcopy_csv(_grand, date(2026, 9, 14))) > 0, True,
          "grandfathered; repair_bhavcopy --fix clears it")
    check("  2026-10-20 is NOT grandfathered — that is the fix",
          "cm20Oct2026bhav.csv" in KNOWN_MISMATCHED, False)

print()
print("=" * 104)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — bhavcopy filename dates agree with their contents, "
      "or are recorded as not agreeing")
