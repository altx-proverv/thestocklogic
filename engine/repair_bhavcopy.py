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

    repaired, gaps = [], []
    for f, d, reason, _ in trading:
        print(f"\n  re-fetching {d} ...")
        f.unlink()                      # download_all skips anything present
        try:
            bhavcopy_save(d, str(raw_dir))
        except Exception as e:
            print(f"    fetch failed: {type(e).__name__}: {str(e)[:120]}")

        if not f.exists():
            print(f"    still absent — recording a GAP")
            record_gap(d.isoformat(), "NSE served no bhavcopy on re-fetch")
            gaps.append(d)
            continue

        still = is_bad(f)
        if still:
            print(f"    still not a bhavcopy ({still}) — recording a GAP")
            f.unlink()                  # do not re-cache the junk
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
