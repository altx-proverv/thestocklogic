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

WHY THIS IS A RATCHET AND NOT A ZERO-TOLERANCE ASSERTION. There are 25 such
files in the archive today and deleting them is explicitly not this change's
job -- repair_bhavcopy --fix owns that, and a nightly job must not delete
history on its own initiative. So the known set is RECORDED below and a file
outside it fails. That keeps 25 known defects visible instead of silent, and
makes the 26th loud.

The archive is gitignored, so its contents differ between the box (962 files)
and a development machine (953). A recorded name that is absent locally is not
a failure; a mismatch that is not recorded is.
"""
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BHAV = ROOT / "data" / "raw" / "bhavcopy"

failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<58} {str(got)[:26]:<28} {'ok' if ok else 'FAIL':<6} {note}")


# ── THE RECORDED SET, measured on the box 2026-10-22 ──────────────
# Every one of these holds an EARLIER session than its name claims. Five of the
# 25 are named for non-trading days (NSE served the previous session for a date
# that never traded); the other 20 are named for genuine sessions, which is the
# worse half -- a real session is missing from the archive AND another is
# duplicated under its name.
KNOWN_MISMATCHED = {
    "cm01May2024bhav.csv", "cm01May2026bhav.csv", "cm03Apr2026bhav.csv",
    "cm03Mar2026bhav.csv", "cm05Nov2025bhav.csv", "cm08Mar2024bhav.csv",
    "cm11Apr2024bhav.csv", "cm14Apr2026bhav.csv", "cm14Nov2023bhav.csv",
    "cm14Sep2026bhav.csv", "cm15Jan2026bhav.csv", "cm17Apr2024bhav.csv",
    "cm17Jul2024bhav.csv", "cm17Jun2024bhav.csv", "cm19Sep2023bhav.csv",
    "cm20May2024bhav.csv", "cm20Nov2024bhav.csv", "cm20Oct2026bhav.csv",
    "cm22Oct2025bhav.csv", "cm26Jan2026bhav.csv", "cm26Jun2026bhav.csv",
    "cm26Mar2026bhav.csv", "cm28May2026bhav.csv", "cm29Jun2023bhav.csv",
    "cm31Mar2026bhav.csv",
}

# Saved HTML error pages, not bhavcopies. test_bhavcopy_guard.py owns the guard
# that stops new ones being written; they are listed here so the date check can
# report them separately instead of counting them as date mismatches.
KNOWN_NOT_A_BHAVCOPY = {
    "cm02Oct2025bhav.csv", "cm10Apr2025bhav.csv", "cm15Aug2025bhav.csv",
    "cm25Dec2025bhav.csv", "cm27Aug2025bhav.csv",
}

DATE_COLS = (("TradDt", "%Y-%m-%d"), ("DATE1", "%d-%b-%Y"))


def internal_date(path):
    """The trading date the FILE claims. (date|None, why)."""
    try:
        with open(path, newline="", errors="replace") as fh:
            rdr = csv.reader(fh)
            header = [c.strip() for c in next(rdr)]
            row = next(rdr)
    except Exception as e:
        return None, f"unreadable: {type(e).__name__}"
    for col, fmt in DATE_COLS:
        if col in header:
            raw = row[header.index(col)].strip()
            try:
                return datetime.strptime(raw, fmt).date(), ""
            except ValueError:
                return None, f"{col} unparseable: {raw!r}"
    return None, f"no date column (header starts {header[:3]})"


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
print("=" * 104)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — bhavcopy filename dates agree with their contents, "
      "or are recorded as not agreeing")
