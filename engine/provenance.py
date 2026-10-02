"""
THE STOCK LOGIC — engine provenance.

Which commit wrote a row. Added 2026-10-02 after an audit in which every era
boundary in the signal record had to be inferred from WHICH COLUMNS ARE NULL:

    before 2026-06-09   no rsi / rvol / atr_pct / delivery_pct
    before 2026-08-10   no zone_source / entry_dist_pct / stop_pct
    before 2026-10-01   publication_kind meaningless

That inference worked only because the changes happened to land weeks apart. Two
deployments in one week and it stops working entirely, and the record offers no
other way to tell which rules produced a row.

The deploy is `git reset --hard origin/main` every five minutes, so .git is
present on the box and the SHA is exactly the code that ran.

NEVER RAISES. Provenance is a label on a row, and a chain that fails because it
could not read its own version number has failed for the worst possible reason.
Resolution order, each falling through:

    ATLAS_SHA in the environment   -- lets a container or a test pin it
    git rev-parse --short HEAD     -- the normal path on the box
    "unknown"                      -- honest, and still distinguishable from NULL,
                                      which means "written before provenance
                                      existed"
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_cache = None


def engine_sha() -> str:
    """Short SHA of the code currently running. Cached per process.

    Cached because the chain calls this once per row literal and a subprocess per
    row would be absurd -- and because the SHA cannot change mid-process: the
    deploy rewrites files, but a running Python process keeps the bytecode it
    imported. A row's SHA should be the code that is executing, not whatever
    landed on disk since it started.
    """
    global _cache
    if _cache is not None:
        return _cache
    env = os.environ.get("ATLAS_SHA", "").strip()
    if env:
        _cache = env[:12]
        return _cache
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=str(ROOT), capture_output=True, text=True,
                             timeout=5)
        sha = (out.stdout or "").strip()
        if out.returncode == 0 and sha:
            _cache = sha[:12]
            return _cache
    except Exception:
        pass
    _cache = "unknown"
    return _cache


def reset_cache() -> None:
    """For tests only."""
    global _cache
    _cache = None
