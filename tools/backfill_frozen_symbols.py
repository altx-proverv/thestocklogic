"""
BACKFILL THE SESSIONS A FROZEN PARQUET MISSED
=============================================

WHY THIS EXISTS. engine/universe.EXCLUDED used to be enforced by
01b_download_bhavcopy refusing to WRITE excluded symbols, so adding a symbol to
EXCLUDED stopped its parquet being updated. Commit fe169df (2026-09-13) added
40 exclusions and 37 of those parquets froze at their 2026-09-11 bar. 01b now
ingests the whole universe, which fixes it going forward; this replays what was
missed in between.

NO DOWNLOADS. Every session's bhavcopy is already in data/raw/bhavcopy. This
reads those files with 01b's OWN parser -- importing it rather than
reimplementing it, because a second parser is a second set of column-mapping
bugs, and the old/new NSE layouts are exactly where those live.

SETTLED BARS ARE NEVER REWRITTEN. Only dates strictly after a symbol's current
last bar are appended. A symbol whose history disagrees with the source for a
date it already has is reported, not corrected -- that is a different job with
a different blast radius (see engine/repair_bhavcopy.py).

WRITES NOTHING WITHOUT --apply. The default is a dry run that prints what it
would do, because the only cheap moment to notice a bad plan is before it runs.
"""

import sys
import argparse
import importlib.util
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill")

RAW_DIR = ROOT / "data" / "raw" / "bhavcopy"
STOCKS_DIR = ROOT / "data" / "processed" / "stocks"


def load_01b():
    """01b cannot be imported by name -- the module starts with a digit."""
    spec = importlib.util.spec_from_file_location(
        "dl_01b", ROOT / "engine" / "01b_download_bhavcopy.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def file_internal_date(p: Path):
    """The trade date the FILE claims, or None if it does not say.

    Not used to decide anything -- used to REPORT when a file's own date
    disagrees with its filename. 24 cached bhavcopies have that disagreement
    (they carry the previous session's rows under a date that is not theirs),
    and a replay that crosses one of them should say so rather than silently
    writing a duplicate bar.
    """
    import datetime as dt
    try:
        head = pd.read_csv(p, nrows=2)
    except Exception:
        return None
    head.columns = [c.strip() for c in head.columns]
    if "TradDt" in head.columns:
        try:
            return dt.date.fromisoformat(str(head["TradDt"].iloc[0]).strip()[:10])
        except ValueError:
            return None
    if "DATE1" in head.columns:
        try:
            return dt.datetime.strptime(
                str(head["DATE1"].iloc[0]).strip(), "%d-%b-%Y").date()
        except ValueError:
            return None
    return None


def sessions_available(dl, after=None):
    """[(date, path)] for every bhavcopy on disk, sorted, optionally after a date."""
    import datetime as dt, re
    out = []
    for p in RAW_DIR.glob("cm*bhav.csv"):
        m = re.match(r"cm(\d{2})([A-Za-z]{3})(\d{4})bhav", p.stem)
        if not m:
            continue
        try:
            d = dt.datetime.strptime("".join(m.groups()), "%d%b%Y").date()
        except ValueError:
            continue
        if after is None or d > after:
            out.append((d, p))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="write the parquets; omit for a dry run")
    ap.add_argument("--symbols", default="",
                    help="comma-separated subset; default is every stale "
                         "universe symbol")
    args = ap.parse_args()

    dl = load_01b()
    from engine.universe import SYMBOL_SECTOR_MAP, EXCLUDED

    # ── WHAT IS STALE, AND AGAINST WHAT ───────────────────────────
    last_bar = {}
    for sym in SYMBOL_SECTOR_MAP:
        p = STOCKS_DIR / f"{sym}.parquet"
        if not p.exists():
            continue
        d = pd.read_parquet(p, columns=["date"])
        if len(d):
            last_bar[sym] = pd.to_datetime(d["date"]).max().date()

    session = pd.Series(list(last_bar.values())).mode().iloc[0]
    stale = sorted(s for s, d in last_bar.items() if d < session)
    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        stale = [s for s in stale if s in want]

    log.info(f"session (modal last bar) : {session}")
    log.info(f"universe symbols         : {len(SYMBOL_SECTOR_MAP)}")
    log.info(f"stale symbols to replay  : {len(stale)}")
    log.info(f"mode                     : {'APPLY' if args.apply else 'DRY RUN'}")
    log.info("")

    # ── PARSE ONCE, NOT ONCE PER SYMBOL ───────────────────────────
    oldest = min(last_bar[s] for s in stale) if stale else session
    files = sessions_available(dl, after=oldest)
    log.info(f"replaying {len(files)} session file(s) after {oldest}")

    misdated = []
    frames = []
    for d, p in files:
        internal = file_internal_date(p)
        if internal is not None and internal != d:
            misdated.append((d, internal, p.name))
        try:
            df = dl.parse_bhavcopy_csv(p, d)
        except Exception as e:
            log.warning(f"  {p.name}: parse failed ({e}) — skipped")
            continue
        if len(df):
            frames.append(df)

    if not frames:
        log.error("no source rows parsed — nothing to do")
        return 1
    src = pd.concat(frames, ignore_index=True)
    src["date"] = pd.to_datetime(src["date"])
    log.info(f"parsed {len(src):,} EQ row(s) across "
             f"{src['date'].nunique()} session(s)")

    if misdated:
        log.warning("")
        log.warning("=" * 70)
        log.warning(f"{len(misdated)} FILE(S) IN THIS RANGE CARRY A DATE THAT IS "
                    f"NOT THEIR OWN:")
        for d, internal, name in misdated:
            log.warning(f"  {name}: filename says {d}, the file says {internal}")
        log.warning("The parser stamps the REQUESTED date, so replaying these "
                    "writes a bar that duplicates the prior session -- which is "
                    "exactly what every other universe symbol already carries "
                    "for those dates. Replayed for PARITY, not because it is "
                    "right. Fixing the 24 affected files is a separate job.")
        log.warning("=" * 70)
    log.info("")

    # ── PER SYMBOL ────────────────────────────────────────────────
    if args.apply:
        try:
            from engine.stocks_snapshot import take as take_snapshot
            take_snapshot(label="pre-backfill_frozen_symbols")
            log.info("snapshot taken before writing")
        except Exception as e:
            log.error(f"SNAPSHOT FAILED ({e}) — refusing to write without one")
            return 1

    log.info(f"{'SYMBOL':<14}{'WAS':<13}{'NOW':<13}{'+BARS':>6}  NOTE")
    log.info("-" * 74)
    added_total, written, nosource = 0, [], []
    for sym in stale:
        out = STOCKS_DIR / f"{sym}.parquet"
        cur = pd.read_parquet(out)
        cur["date"] = pd.to_datetime(cur["date"])
        have = set(cur["date"])
        last = cur["date"].max()

        new = src[(src["symbol"] == sym) & (src["date"] > last)].copy()
        new = new[~new["date"].isin(have)]
        if new.empty:
            nosource.append(sym)
            log.info(f"{sym:<14}{str(last.date()):<13}{str(last.date()):<13}"
                     f"{0:>6}  NO EQ SOURCE ROWS — left stale, needs a decision")
            continue

        merged = (pd.concat([cur, new], ignore_index=True)
                    .sort_values("date")
                    .drop_duplicates("date", keep="first")
                    .reset_index(drop=True))
        # keep=first means an existing settled bar wins over a replayed one.
        assert len(merged) == len(cur) + len(new), \
            f"{sym}: row arithmetic does not close"

        note = ""
        if args.apply:
            merged.to_parquet(out, index=False)
            written.append(sym)
        else:
            note = "(dry run)"
        added_total += len(new)
        log.info(f"{sym:<14}{str(last.date()):<13}"
                 f"{str(merged['date'].max().date()):<13}{len(new):>6}  {note}")

    log.info("-" * 74)
    log.info(f"{len(stale)} stale symbol(s): {len(stale) - len(nosource)} "
             f"replayable, {len(nosource)} with no EQ source rows")
    log.info(f"bars {'written' if args.apply else 'that would be written'}: "
             f"{added_total}")
    if nosource:
        log.info(f"left stale on purpose: {', '.join(nosource)}")
    if not args.apply:
        log.info("")
        log.info("DRY RUN — nothing was written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
