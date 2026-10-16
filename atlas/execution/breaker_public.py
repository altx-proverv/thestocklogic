"""
THE BREAKER, READ-ONLY. The only thing the public page may see.
===============================================================

WHY THIS FILE IMPORTS NOTHING FROM ATLAS.

The obvious implementation is `from atlas.execution import range_breaker` and
return its `observed()` / `blocked_symbols()`. That would be wrong, and the
wrongness is not stylistic: importing range_breaker puts `block_symbol`,
`_range_save`, `_blocked_save` and `SD.stand_down` into the import closure of
everything downstream. A page path could then reach them in one attribute
lookup, and the only thing standing between a public web page and standing
ATLAS down would be nobody having typed it.

So this module reads the breaker's STATE FILES directly, with stdlib only, and
returns plain dicts. Its import closure contains no ATLAS module at all, which
is what tests/test_page_cannot_write_breaker.py asserts by walking the graph
rather than grepping for strings.

THREE TOOLS HAVE ALREADY WRITTEN PRODUCTION TRADING STATE THIS MONTH. A public
web page must not be the fourth, and "must not" has to be a property of the
code rather than a note in a review.

EVERY FUNCTION HERE OPENS FILES 'r'. There is no write path, no mkdir, no
tempfile, and no requests import. Nothing here can halt, block, stand down, or
persist anything.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# The same defaults atlas/config.py uses, read from the environment the same
# way -- restated rather than imported so this module's closure stays empty.
_STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", "/var/lib/atlas"))
RANGE_FILE = Path(os.environ.get("ATLAS_BREAKER_RANGE_FILE")
                  or str(_STATE_DIR / "breaker_range.json"))
BLOCKED_FILE = Path(os.environ.get("ATLAS_BREAKER_BLOCKED_FILE")
                    or str(_STATE_DIR / "breaker_blocked.json"))


def _read(path: Path) -> dict:
    """Parse a state file, or {}. Never raises, never writes."""
    try:
        if path.exists():
            return json.loads(path.read_text()) or {}
    except Exception as e:
        log.warning(f"breaker_public: could not read {path}: {e}")
    return {}


def _today() -> str:
    return datetime.now(IST).date().isoformat()


def session_date() -> str:
    """The session the state files are stamped with, or today."""
    return str(_read(RANGE_FILE).get("session_date") or _today())


def opening_ranges() -> dict:
    """{symbol: {"low", "high"}} for the session's fixed 09:15-10:00 ranges."""
    d = _read(RANGE_FILE)
    out = {}
    for sym, r in (d.get("ranges") or {}).items():
        if not isinstance(r, dict):
            continue
        lo, hi = r.get("low"), r.get("high")
        if lo is None or hi is None:
            continue
        out[sym] = {"low": float(lo), "high": float(hi)}
    return out


def breaks(include_observed: bool = True) -> list:
    """Opening-range breaks for the session, newest first.

    -> [{"symbol", "side", "scope", "at", "range_low", "range_high",
         "break_close", "break_minute", "detail"}]

    Parses the breaker's own detail string, which is the one place the range
    and the breaking close are recorded together:

      "GRINDWELL LONG; a one-minute CLOSE broke the floor of the 09:15-10:00
       opening range. range 1879.00-1906.90 (45 candles 09:15-09:59), closed
       at 1877.30 at 10:04 IST"
    """
    import re
    d = _read(RANGE_FILE)
    rows, seen = [], set()
    for o in reversed(d.get("observed") or []):
        scope = str(o.get("scope") or "")
        detail = str(o.get("detail") or "")
        sym = scope.split(":")[-1] if scope != "market" else "MARKET"
        if sym in seen:
            # One row per symbol: the breaker re-records the same symbol each
            # cycle with a newer close, and the tape wants the break, not a
            # tick stream. reversed() above means this keeps the FIRST break.
            continue
        side = "LONG" if " LONG" in detail else ("SHORT" if " SHORT" in detail
                                                 else "")
        edge = ("floor" if "floor" in detail
                else "ceiling" if "ceiling" in detail else "")
        m = re.search(r"range ([\d.]+)-([\d.]+)", detail)
        c = re.search(r"closed at ([\d.]+) at (\d\d:\d\d)", detail)
        if not include_observed and not o.get("fired"):
            pass
        rows.append({
            "symbol": sym,
            "side": side,
            "edge": edge,
            "scope": "market" if scope == "market" else "symbol",
            "at": str(o.get("at") or ""),
            "range_low": float(m.group(1)) if m else None,
            "range_high": float(m.group(2)) if m else None,
            "break_close": float(c.group(1)) if c else None,
            "break_minute": c.group(2) if c else None,
            "detail": detail,
        })
        seen.add(sym)
    return rows


def blocked() -> dict:
    """{symbol: detail} blocked by a stock-level break this session."""
    d = _read(BLOCKED_FILE)
    if str(d.get("session_date") or "") != _today():
        return {}
    return {k: str(v) for k, v in (d.get("symbols") or {}).items()}


def state() -> dict:
    """Everything the page is allowed to know, in one read."""
    b = breaks()
    return {"session_date": session_date(),
            "ranges_held": len(opening_ranges()),
            "breaks": len(b),
            "symbols_broken": sorted({r["symbol"] for r in b
                                      if r["scope"] == "symbol"}),
            "market_break": any(r["scope"] == "market" for r in b),
            "blocked": sorted(blocked())}
