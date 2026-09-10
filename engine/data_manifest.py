"""
TSL — Stocks Manifest
=====================
A content fingerprint of data/processed/stocks, written after every 01b run
and checkable at any time.

WHY THIS EXISTS
---------------
01b rebuilt each symbol's parquet whole on every run, from whatever the cached
CSVs said at that moment, without ever reading the file it was overwriting.
Nothing compared the new bars against the old ones, so a settled bar could
change value and leave no trace -- the evidence of the previous value was
destroyed by the write that changed it. It surfaced weeks later as a backtest
that would not reproduce what the live pipeline had published, and by then the
question "what did this bar say in August" had no answer anywhere in the
system.

01b now refuses to rewrite a settled bar. This module is the independent
check on that: it fingerprints what is on disk, so drift is caught by a
comparison that takes two seconds rather than by a parity test three weeks
later.

WHAT IS HASHED, AND WHY NOT THE FILE
------------------------------------
The parquet BYTES are not stable. Compression level, row-group layout, the
pandas and pyarrow versions and the writer's own metadata all move without any
data changing, so a file hash produces false alarms and teaches everyone to
ignore it. What is hashed here is the CONTENT: dates as int64, then each value
column as float64, in a fixed column order, taken from the frame sorted by
date. Rewriting the same numbers with a different pyarrow gives the same
digest; changing one price by a paisa does not.

PER MONTH AS WELL AS PER SYMBOL
-------------------------------
A whole-symbol digest tells you RELIANCE changed. A per-month digest tells you
RELIANCE changed in 2026-08, which is usually enough to go straight to the
cause without storing a copy of the data. Months are cheap -- a few hundred
bytes per symbol -- and they are what makes the manifest worth reading rather
than just worth checking.

GROWTH IS NOT MUTATION, AND THE DIGEST HAS TO KNOW THE DIFFERENCE
-----------------------------------------------------------------
Appending today's bar changes the current month's digest, and so does a bar
truncated off the end. Neither is a settled bar moving, and a check that calls
them all "changed" is one nobody will read by the second week.

So each month stores its ROW COUNT next to its digest, and verification
re-digests the new month truncated to the old count. Legitimate growth only
ever happens at the tail, so a month that gained sessions still hashes
identically over its first n rows. Anything else -- a value edited, a bar
inserted mid-month, a date silently dropped -- breaks that prefix and is
reported as a mutation with the month named.

USE
---
    python3 -m engine.data_manifest --write     # after a data build
    python3 -m engine.data_manifest --verify    # anywhere, any time

--verify exits 1 on drift, so it can gate a backtest or run from cron.
"""

import sys
import json
import hashlib
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("TSL-MANIFEST")

ROOT        = Path(__file__).resolve().parent.parent
STOCKS_DIR  = ROOT / "data/processed/stocks"
MANIFEST    = ROOT / "data/processed/stocks_manifest.json"

# Fixed order. Adding a column changes every digest, which is correct -- the
# content genuinely changed -- but do it deliberately, not by reordering.
VALUE_COLS = ("open", "high", "low", "close", "prev_close",
              "volume", "delivery_qty", "delivery_pct", "trades")


def _digest(df: pd.DataFrame) -> str:
    """sha256 over dates + value columns. Stable across parquet rewrites."""
    h = hashlib.sha256()
    h.update(df["date"].to_numpy(dtype="datetime64[ns]").astype("int64").tobytes())
    for c in VALUE_COLS:
        h.update(c.encode())
        if c in df.columns:
            h.update(pd.to_numeric(df[c], errors="coerce")
                     .to_numpy(dtype="float64").tobytes())
        else:
            h.update(b"<absent>")
    return h.hexdigest()[:16]


def fingerprint_symbol(path: Path) -> dict:
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    months = {}
    for period, g in df.groupby(df["date"].dt.to_period("M")):
        g = g.reset_index(drop=True)
        # n travels with the digest so verification can re-hash the same
        # prefix and tell a month that grew from one that was edited.
        months[str(period)] = {"n": len(g), "d": _digest(g)}

    return {
        "rows":   len(df),
        "first":  df["date"].iloc[0].date().isoformat() if len(df) else None,
        "last":   df["date"].iloc[-1].date().isoformat() if len(df) else None,
        "digest": _digest(df),
        "months": months,
    }


def _classify(old: dict, path: Path) -> dict:
    """
    One symbol, against its manifest entry. Reads the parquet because a
    mutation test needs the rows, not just their fingerprint.

    Returns {'mutated': [months], 'shrunk': [months], 'gained': n_rows}.
    """
    df = pd.read_parquet(path)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    om = old.get("months", {})
    mutated, shrunk, gained = [], [], 0

    now = {str(p): g.reset_index(drop=True)
           for p, g in df.groupby(df["date"].dt.to_period("M"))}

    for ym, prev in sorted(om.items()):
        if ym not in now:
            shrunk.append(ym)                     # a whole month disappeared
            continue
        g, n = now[ym], prev["n"]
        if len(g) < n:
            shrunk.append(ym)
        elif len(g) == n:
            if _digest(g) != prev["d"]:
                mutated.append(ym)
        else:
            # Grew. Legitimate only if the rows it already had are untouched.
            if _digest(g.iloc[:n].reset_index(drop=True)) != prev["d"]:
                mutated.append(ym)
            else:
                gained += len(g) - n

    for ym in sorted(set(now) - set(om)):
        gained += len(now[ym])

    return {"mutated": mutated, "shrunk": shrunk, "gained": gained,
            "rows": len(df),
            "last": df["date"].iloc[-1].date().isoformat() if len(df) else None}


def build(stocks_dir: Path = STOCKS_DIR) -> dict:
    files = sorted(stocks_dir.glob("*.parquet"))
    out = {}
    for f in files:
        try:
            out[f.stem] = fingerprint_symbol(f)
        except Exception as e:
            log.warning(f"{f.stem}: unreadable ({e})")
            out[f.stem] = {"error": str(e)}
    return {"symbols": out, "n_symbols": len(out)}


def write(stocks_dir: Path = STOCKS_DIR, path: Path = MANIFEST) -> dict:
    m = build(stocks_dir)
    m["written_at"] = pd.Timestamp.now().isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(m, indent=1, sort_keys=True))
    log.info(f"manifest: {m['n_symbols']} symbols -> {path}")
    return m


def verify(stocks_dir: Path = STOCKS_DIR, path: Path = MANIFEST) -> dict:
    """
    Compare what is on disk against the manifest.

    Four outcomes, kept apart because they mean different things:
      changed  a settled bar moved -- the fault this module exists for
      grown    new sessions appended, nothing else touched. Expected daily.
      shrunk   history got shorter. A truncating rebuild, and always wrong.
      added / removed  symbols entering or leaving the universe.
    """
    if not path.exists():
        return {"ok": False, "reason": f"no manifest at {path} — run --write first"}

    old = json.loads(path.read_text())["symbols"]
    on_disk = {p.stem: p for p in sorted(stocks_dir.glob("*.parquet"))}

    changed, grown, shrunk = [], [], []
    for sym in sorted(set(old) & set(on_disk)):
        o = old[sym]
        if "error" in o:
            continue
        try:
            c = _classify(o, on_disk[sym])
        except Exception as e:
            log.warning(f"{sym}: unreadable ({e})")
            continue

        if c["mutated"]:
            changed.append((sym, c["mutated"], o["rows"], c["rows"]))
        elif c["shrunk"]:
            # The months are what separate a tail truncation from a bar
            # deleted out of 2023, which look identical in a row count.
            shrunk.append((sym, o["rows"], c["rows"], c["shrunk"]))
        elif c["gained"]:
            grown.append((sym, o["last"], c["last"], c["gained"]))

    return {
        "ok": not (changed or shrunk),
        "changed": changed,
        "grown":   grown,
        "shrunk":  shrunk,
        "added":   sorted(set(on_disk) - set(old)),
        "removed": sorted(set(old) - set(on_disk)),
    }


def report(r: dict) -> None:
    print("=" * 72)
    print("STOCKS MANIFEST — settled bars must not move")
    print("=" * 72)
    if "reason" in r:
        print(f"  {r['reason']}")
        return

    if r["changed"]:
        print(f"\n  HISTORY CHANGED — {len(r['changed'])} symbol(s):")
        for sym, months, orow, nrow in r["changed"][:25]:
            print(f"    {sym:<14} months {', '.join(months[:6])}"
                  f"   rows {orow} -> {nrow}")
    if r["shrunk"]:
        print(f"\n  BARS LOST — {len(r['shrunk'])} symbol(s):")
        for sym, orow, nrow, months in r["shrunk"][:25]:
            print(f"    {sym:<14} {orow} -> {nrow} rows"
                  f"   months {', '.join(months[:6])}")
    if r["grown"]:
        print(f"\n  appended, nothing else moved — {len(r['grown'])} symbol(s)")
        for sym, ol, nl, n in r["grown"][:5]:
            print(f"    {sym:<14} {ol} -> {nl}  (+{n})")
        if len(r["grown"]) > 5:
            print(f"    ... and {len(r['grown'])-5} more")
    if r["added"]:
        print(f"\n  new symbols: {len(r['added'])} — {r['added'][:8]}")
    if r["removed"]:
        print(f"\n  symbols gone: {len(r['removed'])} — {r['removed'][:8]}")

    print("\n" + "-" * 72)
    print("MANIFEST:", "OK — no settled bar moved" if r["ok"]
          else "*** DRIFT — history changed under the backtester ***")


# ══════════════════════════════════════════════════════════════════
# THE REVISIONS LEDGER
# ══════════════════════════════════════════════════════════════════
# Bars are frozen once written, so an upstream revision is now REJECTED rather
# than absorbed. That is the right call for the record -- the signals were
# generated from what was there at the time, and rewriting the inputs after
# the fact makes the published history unreproducible for a second time, in
# the opposite direction.
#
# But rejecting a revision silently is its own kind of forgetting. The bar NSE
# now disagrees with is still the bar every backtest reads, and anyone
# measuring against those dates should be able to see that upstream has since
# said something different.
#
# So every rejected revision is appended here, permanently, with both values.
# Append-only and one JSON object per line: the file is the record, and a run
# that finds nothing writes nothing.

REVISIONS = ROOT / "data/processed/revisions.jsonl"


# THREE KINDS OF EVENT, KEPT APART
# --------------------------------
#   revision  upstream now disagrees with a bar we already hold. Rejected by
#             default: the signals were generated from what was there at the
#             time, and rewriting the inputs afterwards makes the published
#             history unreproducible a second time, in the other direction.
#   repair    a date we never had, now obtained. Not a revision -- nothing is
#             being overwritten, a hole is being filled -- but it still changes
#             history under anything already measured against that month, so it
#             is recorded just as loudly.
#   gap       a session that is genuinely unavailable. The most important of
#             the three, because nothing downstream can infer it: every signal
#             computed on the following session was computed with a hole, and
#             that has to be discoverable years later.
KIND_REVISION = "revision"
KIND_REPAIR   = "repair"
KIND_GAP      = "gap"


def _append(rows: list, path: Path = REVISIONS) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return len(rows)


def record_revisions(conflicts: dict, accepted: bool,
                     path: Path = REVISIONS) -> int:
    """
    Append this run's rejected (or accepted) revisions.

    `conflicts` is {symbol: [(date, column, old, new), ...]} as build_parquets
    collects it. Returns the number of rows written.
    """
    if not conflicts:
        return 0
    seen_at = pd.Timestamp.now().isoformat(timespec="seconds")
    rows = []
    for sym, items in sorted(conflicts.items()):
        for d, col, ov, nv in items:
            rows.append({
                "kind":     KIND_REVISION,
                "seen_at":  seen_at,
                "symbol":   sym,
                "date":     d,
                "column":   col,
                "kept":     None if pd.isna(ov) else float(ov),
                "upstream": None if pd.isna(nv) else float(nv),
                "applied":  bool(accepted),
            })
    n = _append(rows, path)
    log.info(f"revisions ledger: +{n} revision(s) -> {path}")
    return n


def record_repair(session: str, note: str, n_symbols: int = None,
                  path: Path = REVISIONS) -> int:
    """A session that was missing and has now been obtained."""
    n = _append([{
        "kind":       KIND_REPAIR,
        "seen_at":    pd.Timestamp.now().isoformat(timespec="seconds"),
        "date":       session,
        "n_symbols":  n_symbols,
        "note":       note,
    }], path)
    log.info(f"revisions ledger: recorded REPAIR of {session} -> {path}")
    return n


def record_gap(session: str, note: str, path: Path = REVISIONS) -> int:
    """
    A trading session with no data, and no way to get it.

    Recorded so that anyone measuring the sessions either side of it can find
    out. Indicators are recursive and structure is confirmed by later bars, so
    a hole does not stay local to its own date.
    """
    n = _append([{
        "kind":    KIND_GAP,
        "seen_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "date":    session,
        "note":    note,
    }], path)
    log.warning(f"revisions ledger: recorded GAP at {session} — {note}")
    return n


def revisions_report(path: Path = REVISIONS) -> None:
    """Which dates carry a revision, and what upstream now says."""
    if not path.exists():
        print(f"no revisions recorded ({path} does not exist)")
        return

    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if not rows:
        print("no revisions recorded")
        return

    df = pd.DataFrame(rows)
    if "kind" not in df.columns:
        df["kind"] = KIND_REVISION          # ledgers written before kinds existed

    print("=" * 74)
    print("DATA EVENTS — what has moved under the backtester, and what is absent")
    print("=" * 74)

    gaps = df[df["kind"] == KIND_GAP]
    if len(gaps):
        print(f"\n  GAPS — {gaps['date'].nunique()} session(s) with NO DATA.")
        print( "  Signals on the following session were computed with a hole,")
        print( "  and indicators are recursive, so the effect is not local to")
        print( "  the missing date:")
        for _, r in gaps.sort_values("date").iterrows():
            print(f"    {r['date']}   {r.get('note','')}")

    reps = df[df["kind"] == KIND_REPAIR]
    if len(reps):
        print(f"\n  REPAIRS — {len(reps)} session(s) recovered after being absent.")
        print( "  Nothing was overwritten, but any measurement made BEFORE the")
        print( "  repair saw a different history than one made after:")
        for _, r in reps.sort_values("date").iterrows():
            n = r.get("n_symbols")
            print(f"    {r['date']}   {'' if pd.isna(n) else f'{int(n)} symbols  '}"
                  f"{r.get('note','')}")

    revs = df[df["kind"] == KIND_REVISION]
    if len(revs):
        kept = revs[~revs["applied"].fillna(False)]
        print(f"\n  REVISIONS — {len(revs)} field(s), {len(kept)} rejected, "
              f"{len(revs) - len(kept)} applied")
        print(f"  {revs['symbol'].nunique()} symbol(s) over "
              f"{revs['date'].nunique()} session(s)")
        print(f"\n  BY SESSION — the dates to distrust if a measurement ever")
        print(f"  disagrees with a source outside this repo:")
        for d, g in sorted(kept.groupby("date")):
            syms = sorted(g["symbol"].unique())
            print(f"    {d}   {len(g):>3} field(s), {len(syms):>3} symbol(s)"
                  f"   {', '.join(syms[:6])}{' ...' if len(syms) > 6 else ''}")
        print(f"\n  DETAIL (most recent 30):")
        for _, r in revs.tail(30).iterrows():
            mark = "applied " if r.get("applied") else "kept    "
            print(f"    {mark} {r['symbol']:<14} {r['date']}  {r['column']:<13}"
                  f" {r['kept']} | upstream {r['upstream']}")

    if not len(gaps) and not len(reps) and not len(revs):
        print("  ledger exists but holds no recognised events")
    print()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--write" in sys.argv:
        write()
    elif "--verify" in sys.argv:
        r = verify()
        report(r)
        sys.exit(0 if r.get("ok") else 1)
    elif "--revisions" in sys.argv:
        revisions_report()
    else:
        print(__doc__)
