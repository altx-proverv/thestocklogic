#!/usr/bin/env python3
"""
IS THE REAL TICK MAP PRESENT AND SANE? READ-ONLY. Exit 0 sane, 1 not, 2 cannot tell.
====================================================================================

ONE QUESTION, ON THE BOX. tests/test_exits.py and tests/test_exit_path_rebuild.py
used to read /var/lib/atlas/tick_map.json, so they answered "is this box's file
there?" while claiming to answer "are the exit paths right?" -- and would have
passed on a wrong file. Those suites now run on a committed fixture. This is the
other question, asked of the file the exits actually use.

  1  the file exists and parses as {"days": {...}, "last_known": {...}}
  2  it holds the session it should: the newest NSE trading day on or before
     today, once the 08:30 IST refresh (atlas/execution/zerodha_morning.py)
     has had time to run; before that, the previous trading day
  3  every universe symbol (engine.universe.ALL_SYMBOLS) has a tick, today's
     or last-known -- a symbol with none cannot have an exit priced
  4  every tick is one of the six the instrument master uses (tick_map.py's own
     measurement: 0.01, 0.05, 0.10, 0.50, 1.00, 5.00); anything else is wrong

    python tools/check_tick_map.py            # the configured file (TICK_MAP_FILE)
    python tools/check_tick_map.py --path F
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
KNOWN_TICKS = {0.01, 0.05, 0.10, 0.50, 1.00, 5.00}
REFRESH_DONE_IST = (8, 45)          # zerodha_morning runs 03:00 UTC = 08:30 IST


def check(path: Path, now=None, symbols=None) -> dict:
    now = now or datetime.now(IST)
    out = {"path": str(path), "ok": False, "problems": [], "summary": ""}
    if not path.exists():
        out["problems"].append("file does not exist")
        return out
    try:
        d = json.loads(path.read_text())
        days, last = d["days"], d["last_known"]
        assert isinstance(days, dict) and isinstance(last, dict)
    except Exception as e:
        out["problems"].append(f"unreadable or wrong shape ({type(e).__name__}: {e})")
        return out

    from engine.trading_calendar import is_trading_day, prev_trading_day
    today = now.date()
    refreshed_today = (is_trading_day(today)
                       and (now.hour, now.minute) >= REFRESH_DONE_IST)
    want = today if refreshed_today else prev_trading_day(today)
    newest = max(days) if days else None
    if want.isoformat() not in days:
        out["problems"].append(f"no map for {want} (newest cached day: {newest})")

    if symbols is None:
        from engine.universe import ALL_SYMBOLS
        symbols = ALL_SYMBOLS
    day_map = days.get(want.isoformat()) or {}
    missing = [s for s in symbols if s not in day_map and s not in last]
    if missing:
        out["problems"].append(f"{len(missing)} universe symbol(s) have no tick: "
                               f"{', '.join(missing[:12])}")

    odd = sorted({(s, v) for m in [day_map, last] for s, v in m.items()
                  if not isinstance(v, (int, float))
                  or round(float(v), 2) not in KNOWN_TICKS}, key=str)[:12]
    if odd:
        out["problems"].append(f"tick values outside {sorted(KNOWN_TICKS)}: {odd}")

    out["ok"] = not out["problems"]
    out["summary"] = (f"tick map {path}: newest day {newest}, expected {want}, "
                      f"{len(day_map)} symbol(s) that day, {len(last)} last-known, "
                      f"{len(symbols) - len(missing)}/{len(symbols)} universe "
                      f"symbols covered" + ("" if out["ok"] else
                                            " — " + "; ".join(out["problems"])))
    return out


def line() -> str:
    """One line for a daily message. Never raises."""
    try:
        from atlas.config import TICK_MAP_FILE
        r = check(Path(TICK_MAP_FILE))
        return ("Tick map: OK — " if r["ok"] else "Tick map: NOT SANE — ") + (
            r["summary"] or "; ".join(r["problems"]))
    except Exception as e:
        return f"Tick map: could not be checked ({type(e).__name__}: {e})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path")
    a = ap.parse_args(argv)
    try:
        if a.path:
            p = Path(a.path)
        else:
            from atlas.config import TICK_MAP_FILE
            p = Path(TICK_MAP_FILE)
        r = check(p)
    except Exception as e:
        print(f"CANNOT TELL: {type(e).__name__}: {e}")
        return 2
    print(("SANE  " if r["ok"] else "NOT SANE  ") + (r["summary"] or "; ".join(r["problems"])))
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
