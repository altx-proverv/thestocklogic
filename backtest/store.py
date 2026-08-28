"""
BACKTEST — the test/live seam
=============================
The one place backtest output is written, and the reason a backtest cannot
corrupt the live record.

Before this existed, a test run wrote into the same signals / signal_marks /
signal_outcomes / atlas_trades rows the dashboard reads. For a nightly batch
that is a nuisance; for an engine under active development it is a hazard.

THE SEAM IS STRUCTURAL, NOT A FLAG. write() takes no table name -- there are
two writers, one per backtest table, and both build their URL from a constant
that starts with LIVE_SAFE_PREFIX. A caller cannot name `signals` because
there is no parameter to name it in. An env var or a `--dry-run` flag would
have been a seam you can forget to set; this one has nothing to set.

READS OF LIVE TABLES ARE ALLOWED AND EXPLICIT. The baseline test has to
compare against the live record, so it must read it. That path is
read_live_readonly(), which is named to be conspicuous in a diff, issues GET
only, and is the sole function here that mentions a live table. Nothing in
this module can PATCH, POST or DELETE one.
"""

import os
import json
import logging
import subprocess
from pathlib import Path

import requests

log = logging.getLogger("BACKTEST-STORE")

SUPABASE_URL = os.environ.get("SUPABASE_URL",
                              "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

LIVE_SAFE_PREFIX = "backtest_"
RUNS_TABLE       = "backtest_runs"
SIGNALS_TABLE    = "backtest_signals"

# Named so that a future reader adding a writer here sees exactly what is out
# of bounds. Nothing consults this list at runtime -- the protection is that no
# writer accepts a table name -- but an assertion below uses it as a tripwire.
LIVE_TABLES = ("signals", "signal_marks", "signal_outcomes", "atlas_trades",
               "atlas_state", "live_prices", "broker_tokens")

assert RUNS_TABLE.startswith(LIVE_SAFE_PREFIX)
assert SIGNALS_TABLE.startswith(LIVE_SAFE_PREFIX)
assert not any(t.startswith(LIVE_SAFE_PREFIX) for t in LIVE_TABLES), \
    "a live table name collides with the backtest prefix -- the seam is unsound"


def _headers(prefer: str = "return=minimal"):
    if not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY not set -- refusing to run. "
                           "It lives in the crontab header, not an interactive "
                           "shell; export it or run under cron.")
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        prefer,
    }


def git_sha() -> str:
    """Code identity for the run record. A result is worthless without it."""
    try:
        root = Path(__file__).resolve().parent.parent
        sha = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=10)
        dirty = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=10)
        s = sha.stdout.strip() or "unknown"
        return s + ("-dirty" if dirty.stdout.strip() else "")
    except Exception as e:
        log.warning(f"could not read git sha: {e}")
        return "unknown"


# ══════════════════════════════════════════════════════════════════════
# WRITES — backtest tables only. No table-name parameter exists.
# ══════════════════════════════════════════════════════════════════════

def create_run(config: dict, config_hash: str, label: str = None,
               period_start=None, period_end=None, holdout_start=None,
               window_end=None) -> str:
    """Open a run row and return its run_id. Written BEFORE the work starts so
    a run that crashes still leaves a record of having been attempted."""
    row = {
        "git_sha":       git_sha(),
        "label":         label,
        "config":        config,
        "config_hash":   config_hash,
        "period_start":  period_start,
        "period_end":    period_end,
        "holdout_start": holdout_start,
        "window_end":    window_end,
        "status":        "RUNNING",
    }
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{RUNS_TABLE}",
                      headers=_headers("return=representation"),
                      json=row, timeout=30)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"could not open run row: HTTP {r.status_code} {r.text[:300]}")
    return r.json()[0]["run_id"]


def finish_run(run_id: str, status: str, n_candidates: int = None,
               n_resolved: int = None, runtime_sec: float = None,
               notes: str = None) -> bool:
    """Close the run. Called on success AND on failure -- every run persists,
    or a dozen configs get tried, one gets kept, and the other eleven are
    forgotten, which is selection on noise."""
    patch = {"status": status, "n_candidates": n_candidates,
             "n_resolved": n_resolved, "runtime_sec": runtime_sec,
             "notes": notes}
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{RUNS_TABLE}?run_id=eq.{run_id}",
                       headers=_headers(), json=patch, timeout=30)
    if r.status_code not in (200, 204):
        log.error(f"could not close run {run_id}: HTTP {r.status_code} {r.text[:200]}")
        return False
    return True


def write_signals(run_id: str, rows: list, batch: int = 500) -> int:
    """Persist every candidate of a run. Per-signal, never just aggregates."""
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), batch):
        chunk = [{**x, "run_id": run_id} for x in rows[i:i + batch]]
        r = requests.post(f"{SUPABASE_URL}/rest/v1/{SIGNALS_TABLE}",
                          headers=_headers("resolution=merge-duplicates"),
                          json=chunk, timeout=90)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"signal write failed at row {i}: "
                               f"HTTP {r.status_code} {r.text[:300]}")
        written += len(chunk)
    return written


# ══════════════════════════════════════════════════════════════════════
# READ — the only function here that names a live table. GET only.
# ══════════════════════════════════════════════════════════════════════

def read_live_readonly(table: str, select: str, order: str,
                       extra: str = "", page: int = 1000) -> list:
    """
    Read a LIVE table. Used by the baseline test, which has to compare against
    the live record to be worth anything.

    GET only, and deliberately verbose in name so that any diff adding a write
    path here is obvious. `order` is required and must be unique: PostgREST
    offset paging over a non-unique order silently skips and repeats rows,
    which is how a 6,313-row table first counted as 417 resolved and then 423.
    """
    if table not in LIVE_TABLES:
        raise ValueError(f"{table!r} is not a known live table")
    out, off = [], 0
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/{table}?select={select}&order={order}"
               f"{extra}&limit={page}&offset={off}")
        r = requests.get(url, headers=_headers("count=none"), timeout=120)
        if r.status_code != 200:
            raise RuntimeError(f"read {table} failed: HTTP {r.status_code} {r.text[:300]}")
        b = r.json()
        out += b
        if len(b) < page:
            return out
        off += page


def exact_count(table: str) -> int:
    """Authoritative row count, to verify a paged read got everything."""
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                     headers={**_headers(), "Prefer": "count=exact", "Range": "0-0"},
                     timeout=60)
    return int(r.headers.get("Content-Range", "*/0").split("/")[-1])
