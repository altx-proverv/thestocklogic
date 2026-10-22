"""
TSL — Bhavcopy Repair
=====================
Finds cached bhavcopy files that are not bhavcopies, re-fetches them, and
records the outcome as a repair or a gap.

THE FAULT
---------
download_all() skips any date whose file already exists:

    if fpath.exists():
        results[d] = True
        continue

That is what makes cached CSVs a trustworthy record of what NSE served. It is
also what makes a BAD file permanent. When NSE answers with an error page,
bhavcopy_save writes the HTML to disk under a .csv name, the download step
counts it as success, and every run afterwards skips it. The parse fails, 01b
logs one warning line among hundreds, and the session is silently absent from
every symbol's history for as long as the file sits there.

Found on this archive: 7 of 858 files are HTML. Five fall on NSE holidays, so
nothing ever asks for them. Two are real sessions -- 2026-05-12 and 2026-05-26
-- which were missing from every parquet in the universe.

WHY A SEPARATE TOOL
-------------------
It deletes files and re-downloads them. That is not something a nightly job
should do on its own initiative, and the failure it repairs is rare enough that
a deliberate run is the right shape. --fix is required; without it this only
reports.

A repair is NOT a revision. Nothing is overwritten -- a hole is filled -- so
the ledger records it under its own kind. If NSE cannot supply the date, that
is a gap, and the ledger records that instead: every signal computed on the
following session was computed without it, and nothing downstream can work
that out for itself.

USE
---
    python3 -m engine.repair_bhavcopy               # report only
    python3 -m engine.repair_bhavcopy --fix
    python3 -m engine.repair_bhavcopy --fix --date 2026-05-12
"""

import re
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

log = logging.getLogger("TSL-REPAIR")

RAW_DIR = ROOT / "data/raw/bhavcopy"
FNAME   = re.compile(r"cm(\d{2}[A-Za-z]{3}\d{4})bhav\.csv$")

# Enough bytes to see a doctype or an opening tag, and nothing more.
SNIFF = 400


def _date_of(path: Path):
    m = FNAME.search(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d%b%Y").date()
    except ValueError:
        return None


# ── FILES WHOSE INTERNAL DATE IS KNOWN NOT TO MATCH THEIR NAME ────
#
# Measured 2026-10-22 across 962 files. Each holds an EARLIER session than its
# filename claims, so the session it is named for is ABSENT from every parquet
# and the one it actually contains is DUPLICATED under two dates.
#
# GRANDFATHERED, DELIBERATELY AND TEMPORARILY. parse refuses a mis-dated file,
# which is the right behaviour -- but applying that to the whole backlog at once
# would silently drop ~20 sessions from every symbol's history in one rebuild,
# and Hemal asked for the list before anything is removed. So these are allowed
# through with a loud warning per run, and `repair_bhavcopy --fix` is what
# clears them once the list has been read.
#
# 2026-10-20 IS DELIBERATELY NOT IN THIS LIST, and that absence is the fix.
# NSE publishes no bhavcopy for that date on either endpoint -- the modern UDiFF
# path returns 404 and the legacy path serves 19-Oct content under the 20-Oct
# name, which is why that one file was legacy-format when every other 2026 file
# is UDiFF. It is not a session NSE lost: 2026-10-20 is DUSSEHRA, confirmed
# against NSE's own holiday-master API, and our NSE_HOLIDAYS does not list it.
#
# So there was nothing to recover. The 744 fabricated bars were removed, the
# file was quarantined off the archive rather than left in it, and a
# re-download now hits the write-time guard above and is unlinked before it can
# be cached again. The session is ABSENT, which is the honest state.
KNOWN_MISDATED = {
    "cm01May2024bhav.csv", "cm01May2026bhav.csv", "cm03Apr2026bhav.csv",
    "cm03Mar2026bhav.csv", "cm05Nov2025bhav.csv", "cm08Mar2024bhav.csv",
    "cm11Apr2024bhav.csv", "cm14Apr2026bhav.csv", "cm14Nov2023bhav.csv",
    "cm14Sep2026bhav.csv", "cm15Jan2026bhav.csv", "cm17Apr2024bhav.csv",
    "cm17Jul2024bhav.csv", "cm17Jun2024bhav.csv", "cm19Sep2023bhav.csv",
    "cm20May2024bhav.csv", "cm20Nov2024bhav.csv", "cm22Oct2025bhav.csv",
    "cm26Jan2026bhav.csv", "cm26Jun2026bhav.csv", "cm26Mar2026bhav.csv",
    "cm28May2026bhav.csv", "cm29Jun2023bhav.csv", "cm31Mar2026bhav.csv",
}

_DATE_COLS = (("TradDt", "%Y-%m-%d"), ("DATE1", "%d-%b-%Y"))


def internal_date(path: Path):
    """The trading date the FILE ITSELF claims. (date|None, why_not).

    THE FILENAME IS NOT EVIDENCE. 01b stamps every row with the date it asked
    for, so when NSE serves a different session the rows land under the wrong
    date and the series gains a duplicate bar indistinguishable from a real one.
    cm20Oct2026bhav.csv held 19-Oct data and RELIANCE, TCS and INDIAGLYCO all
    carried byte-identical bars -- volume included -- for the two dates.
    """
    import csv as _csv
    try:
        with open(path, newline="", errors="replace") as fh:
            rdr = _csv.reader(fh)
            header = [c.strip() for c in next(rdr)]
            row = next(rdr)
    except Exception as e:
        return None, f"unreadable ({type(e).__name__})"
    for col, fmt in _DATE_COLS:
        if col in header:
            raw = row[header.index(col)].strip()
            try:
                return datetime.strptime(raw, fmt).date(), ""
            except ValueError:
                return None, f"{col} unparseable: {raw!r}"
    return None, f"no date column (header starts {header[:3]})"


def is_bad(path: Path) -> str:
    """Reason this file is not a usable bhavcopy, or '' if it looks fine."""
    try:
        head = path.read_bytes()[:SNIFF]
    except Exception as e:
        return f"unreadable ({e})"
    low = head.lower()
    if b"<!doctype" in low or b"<html" in low:
        return f"HTML error page ({path.stat().st_size} bytes)"
    if not head.strip():
        return "empty file"
    # A bhavcopy's first line is a header in one of the two known formats.
    first = head.split(b"\n", 1)[0].decode("utf-8", "replace")
    if "SYMBOL" not in first.upper() and "TCKRSYMB" not in first.upper():
        return f"unrecognised header: {first[:60]!r}"
    # ── THE CONTENT MUST BE THE SESSION THE NAME CLAIMS ───────────
    # This is what the first three checks could not catch: a perfectly
    # well-formed bhavcopy for the WRONG DAY. 25 of 962 files on 2026-10-22,
    # and 20 of those named for genuine trading sessions.
    named = _date_of(path)
    if named is not None:
        got, why = internal_date(path)
        if got is None:
            return f"date column unusable: {why}"
        if got != named:
            return (f"contains {got} but is named for {named} "
                    f"({(got - named).days:+d} day(s))")
    return ""


def scan(raw_dir: Path = RAW_DIR) -> list:
    """[(path, date, reason, is_trading_day)] for every bad file."""
    try:
        from engine.trading_calendar import is_trading_day
    except ModuleNotFoundError:
        from trading_calendar import is_trading_day

    out = []
    for f in sorted(raw_dir.glob("*.csv")):
        reason = is_bad(f)
        if not reason:
            continue
        d = _date_of(f)
        out.append((f, d, reason, bool(d and is_trading_day(d))))
    return out


def repair(only: str = None, raw_dir: Path = RAW_DIR, fix: bool = False) -> dict:
    bad = scan(raw_dir)
    if only:
        want = datetime.strptime(only, "%Y-%m-%d").date()
        bad = [b for b in bad if b[1] == want]

    print("=" * 74)
    print("BHAVCOPY REPAIR — cached files that are not bhavcopies")
    print("=" * 74)
    if not bad:
        print("  nothing to repair")
        return {"repaired": [], "gaps": [], "inert": []}

    trading = [b for b in bad if b[3]]
    inert   = [b for b in bad if not b[3]]

    for f, d, reason, _ in inert:
        print(f"  inert    {f.name:<24} {d}  not a trading day — never requested")
    for f, d, reason, _ in trading:
        print(f"  BROKEN   {f.name:<24} {d}  {reason}")

    if not fix:
        print(f"\n  {len(trading)} real session(s) affected. Re-run with --fix "
              f"to re-fetch them.")
        return {"repaired": [], "gaps": [], "inert": inert}

    try:
        from jugaad_data.nse import bhavcopy_save
    except Exception as e:
        print(f"\n  cannot import jugaad_data ({e}) — no re-fetch possible")
        return {"repaired": [], "gaps": [], "inert": inert}

    try:
        from engine.data_manifest import record_repair, record_gap
    except ModuleNotFoundError:
        from data_manifest import record_repair, record_gap

    # A date whose bhavcopy cannot exist yet must not be re-fetched. Without
    # this, --fix run during market hours on today's file unlinks it, asks NSE
    # again, gets the same error page, and RECORDS A GAP -- which asserts that
    # NSE had no data for that session and that every signal on the next one was
    # computed without it. Neither is true: it simply has not been published.
    # A false gap in the ledger is worse than the bad file it replaced.
    try:
        import importlib.util as _il
        _sp = _il.spec_from_file_location("b01d", ROOT / "engine/01b_download_bhavcopy.py")
        _b01 = _il.module_from_spec(_sp)
        _sp.loader.exec_module(_b01)
        newest = _b01.last_requestable_session()
    except Exception as e:
        log.warning(f"cannot determine the newest requestable session ({e}) — "
                    f"proceeding, a premature re-fetch will look like a gap")
        newest = None

    if newest is not None:
        early = [b for b in trading if b[1] > newest]
        if early:
            trading = [b for b in trading if b[1] <= newest]
            for f, d, reason, _ in early:
                print(f"  HELD     {f.name:<24} {d}  its bhavcopy is not "
                      f"published yet (newest available: {newest})")
            print(f"\n  {len(early)} file(s) held back rather than re-fetched. "
                  f"Re-run after {_b01.BHAVCOPY_READY_IST_HOUR}:00 IST; recording "
                  f"a gap now would claim NSE has no data for a session that has "
                  f"simply not been published.")

    repaired, gaps = [], []
    for f, d, reason, _ in trading:
        print(f"\n  re-fetching {d} ...")
        f.unlink()                      # download_all skips anything present
        try:
            bhavcopy_save(d, str(raw_dir))
        except Exception as e:
            print(f"    fetch failed: {type(e).__name__}: {str(e)[:120]}")

        # A GAP IS A CLAIM, so it is not made about a session NSE may still be
        # publishing. BHAVCOPY_READY_IST_HOUR is when the file is normally up, not
        # a guarantee; on a late evening the newest session can still be absent at
        # 18:05 without anything being wrong. Recording a gap then would assert
        # that NSE has no data for today and that tomorrow's signals were computed
        # without it. For the newest session, a failed re-fetch is reported and
        # left for the next run; for anything older it is a real gap.
        newest_session = (d == newest) if newest is not None else False

        if not f.exists():
            if newest_session:
                print(f"    still absent, and this is the newest session — NOT "
                      f"recording a gap. NSE may not have published it yet; "
                      f"re-run later.")
                continue
            print(f"    still absent — recording a GAP")
            record_gap(d.isoformat(), "NSE served no bhavcopy on re-fetch")
            gaps.append(d)
            continue

        still = is_bad(f)
        if still:
            f.unlink()                  # do not re-cache the junk
            if newest_session:
                print(f"    still not a bhavcopy ({still}), and this is the "
                      f"newest session — NOT recording a gap. Discarded; re-run "
                      f"later.")
                continue
            print(f"    still not a bhavcopy ({still}) — recording a GAP")
            record_gap(d.isoformat(), f"re-fetch returned {still}")
            gaps.append(d)
            continue

        # Parse it before believing it.
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "b01", ROOT / "engine/01b_download_bhavcopy.py")
            b01 = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(b01)
            df = b01.parse_bhavcopy_csv(f, d)
        except Exception as e:
            print(f"    fetched but unparseable ({e}) — recording a GAP")
            record_gap(d.isoformat(), f"re-fetch unparseable: {e}")
            gaps.append(d)
            continue

        print(f"    OK — {len(df)} EQ rows, {df['symbol'].nunique()} symbols")
        record_repair(d.isoformat(),
                      "cached file was an error page; re-fetched successfully",
                      n_symbols=int(df["symbol"].nunique()))
        repaired.append(d)
        time.sleep(0.3)

    print("\n" + "-" * 74)
    print(f"  repaired {len(repaired)}, gaps {len(gaps)}")
    if repaired:
        print( "  Re-run 01b to fold these sessions into the parquets. They are")
        print( "  NEW dates, so the freeze appends them rather than rejecting.")
        print( "  Expect data_manifest --verify to report the affected months as")
        print( "  changed: a mid-month insertion breaks the digest prefix, which")
        print( "  is the check working.")
    return {"repaired": repaired, "gaps": gaps, "inert": inert}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    a = sys.argv
    only = a[a.index("--date") + 1] if "--date" in a and len(a) > a.index("--date") + 1 else None
    r = repair(only=only, fix="--fix" in a)
    sys.exit(1 if r["gaps"] else 0)
