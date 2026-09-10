"""
TSL — Stocks Snapshots
======================
An archive of data/processed/stocks taken before anything rewrites it, so the
state a measurement was made against is recoverable rather than remembered.

WHY
---
01b used to rebuild every symbol's parquet whole on each run, overwriting the
previous file without reading it. When a bar turned out to have moved, the
value it moved FROM was already gone -- the write that changed it was also the
only thing that could have recorded it. That is now guarded, but a guard only
protects the files it runs against: a rebuild that is deliberately accepted
(TSL_ACCEPT_REVISIONS=1), a manual delete, or a bug in something else still
lands on the only copy there is.

So: snapshot first, then build. The cost is a compressed copy of a directory
that is tens of megabytes, and it buys the ability to answer "what did this
say yesterday" with a file rather than an argument.

WHAT IS KEPT
------------
A gzipped tar of the parquets plus the manifest digest of what went in, named
by timestamp. KEEP most recent are retained and older ones pruned, because an
unbounded archive of a directory that changes daily eventually becomes the
disk-space problem instead.

RESTORE IS ALSO A CHANGE
------------------------
restore() snapshots the CURRENT state before overwriting it. Putting yesterday
back is itself a rewrite of history, and the same argument that justifies
snapshotting before a build justifies it here -- more so, because a restore is
usually done in a hurry.

USE
---
    python3 -m engine.stocks_snapshot --take "before 01b"
    python3 -m engine.stocks_snapshot --list
    python3 -m engine.stocks_snapshot --restore stocks_20260910_2241.tar.gz
"""

import sys
import json
import shutil
import tarfile
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("TSL-SNAPSHOT")

ROOT       = Path(__file__).resolve().parent.parent
STOCKS_DIR = ROOT / "data/processed/stocks"
SNAP_DIR   = ROOT / "data/snapshots"
KEEP       = 5


def _size_mb(p: Path) -> float:
    return round(p.stat().st_size / 1024 / 1024, 1)


def take(label: str = "", stocks_dir: Path = STOCKS_DIR,
         snap_dir: Path = SNAP_DIR, keep: int = KEEP) -> Path:
    """Archive the stocks directory. Returns the archive path, or None."""
    files = sorted(stocks_dir.glob("*.parquet"))
    if not files:
        log.warning(f"nothing to snapshot — {stocks_dir} holds no parquets")
        return None

    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    out = snap_dir / f"stocks_{stamp}.tar.gz"
    # Two snapshots in the same second must not become one. restore() takes a
    # safety copy immediately before unpacking, which is precisely when a
    # collision happens, and the copy silently lost would be the current state
    # -- the one thing a restore must not destroy.
    n = 1
    while out.exists():
        out = snap_dir / f"stocks_{stamp}_{n}.tar.gz"
        n += 1

    with tarfile.open(out, "w:gz") as tar:
        for f in files:
            tar.add(f, arcname=f"stocks/{f.name}")

    # A digest of what went in, so a snapshot can be identified without
    # unpacking it and compared against the live directory later.
    try:
        from engine.data_manifest import build as build_manifest
    except ModuleNotFoundError:
        from data_manifest import build as build_manifest
    meta = {
        "taken_at": pd.Timestamp.now().isoformat(timespec="seconds"),
        "label":    label,
        "n_files":  len(files),
        "size_mb":  _size_mb(out),
        "manifest": build_manifest(stocks_dir),
    }
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1, sort_keys=True))

    log.info(f"snapshot: {len(files)} parquets -> {out.name} ({meta['size_mb']} MB)"
             + (f"  [{label}]" if label else ""))
    _prune(snap_dir, keep)
    return out


def _prune(snap_dir: Path, keep: int) -> None:
    # BY MTIME, NOT BY NAME. The collision suffix breaks name ordering: once
    # the unsuffixed name is pruned it gets reused, and "stocks_T.tar.gz"
    # sorts BEFORE "stocks_T_1.tar.gz" because '.' < '_'. Sorting by name
    # therefore treated the newest snapshot as the oldest and deleted it
    # moments after writing it.
    snaps = sorted(snap_dir.glob("stocks_*.tar.gz"),
                   key=lambda p: p.stat().st_mtime)
    for old in snaps[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
        old.with_suffix(".json").unlink(missing_ok=True)
        log.info(f"pruned old snapshot {old.name}")


def listing(snap_dir: Path = SNAP_DIR) -> list:
    out = []
    for t in sorted(snap_dir.glob("stocks_*.tar.gz"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        meta = {}
        j = t.with_suffix(".json")
        if j.exists():
            try:
                meta = json.loads(j.read_text())
            except Exception:
                pass
        out.append({"name": t.name, "size_mb": _size_mb(t),
                    "taken_at": meta.get("taken_at", "?"),
                    "label": meta.get("label", ""),
                    "n_files": meta.get("n_files", "?")})
    return out


def restore(name: str, stocks_dir: Path = STOCKS_DIR,
            snap_dir: Path = SNAP_DIR) -> bool:
    """
    Unpack a snapshot over the stocks directory.

    The current state is snapshotted first. A restore is a rewrite like any
    other, and the state being replaced is usually the one nobody thought to
    keep.
    """
    arc = snap_dir / name
    if not arc.exists():
        log.error(f"no such snapshot: {arc}")
        return False

    safety = take(label=f"pre-restore of {name}", stocks_dir=stocks_dir,
                  snap_dir=snap_dir)
    if safety:
        log.info(f"current state saved as {safety.name} before restoring")

    staging = snap_dir / "_restore_tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with tarfile.open(arc, "r:gz") as tar:
        tar.extractall(staging)

    src = staging / "stocks"
    files = sorted(src.glob("*.parquet"))
    if not files:
        log.error(f"{name} contains no parquets — nothing restored")
        shutil.rmtree(staging)
        return False

    stocks_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.copy2(f, stocks_dir / f.name)
    shutil.rmtree(staging)

    log.info(f"restored {len(files)} parquets from {name}")
    log.info("re-run `python3 -m engine.data_manifest --write` to rebaseline")
    return True


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    a = sys.argv
    if "--take" in a:
        i = a.index("--take")
        take(label=a[i + 1] if len(a) > i + 1 and not a[i + 1].startswith("-") else "")
    elif "--list" in a:
        rows = listing()
        if not rows:
            print("no snapshots")
        for r in rows:
            print(f"  {r['name']:<32} {r['taken_at']:<20} "
                  f"{str(r['n_files']):>4} files  {r['size_mb']:>6} MB  {r['label']}")
    elif "--restore" in a:
        i = a.index("--restore")
        if len(a) <= i + 1:
            sys.exit("--restore needs a snapshot name (see --list)")
        sys.exit(0 if restore(a[i + 1]) else 1)
    else:
        print(__doc__)
