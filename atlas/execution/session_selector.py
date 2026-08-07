"""
ATLAS Session Manager + Trade Selector (Phase: TRAINING)
=========================================================
Session rules (total 3 trades/day):
  MORNING  (09:30-11:30): max 2, direction follows market (LONG or SHORT)
  AFTERNOON(11:30-14:30): LONGS only, fills remaining slots
  CLOSING  (14:30-15:15): LONGS only (BTST), fills remaining slot
  SHORTS   : morning only. Never afternoon/closing.
  Unused slots carry forward.

Selection (when many candidates):
  1. winning-setups first (ranked by historical win rate)
  2. entry-range-clean (price within [entry_low, entry_high])
  3. sector-aligned (prefer strong sectors)
  4. no duplicate sectors in one day
"""
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ATLAS-SESSION")
IST = timezone(timedelta(hours=5, minutes=30))

MAX_TRADES_PER_DAY = 3
MORNING_MAX = 2

# Historical win rate by setup (from factor analysis 5 Jul 2026). Higher = better.
# Used to rank candidates: pick winning setups first.
SETUP_RANK = {
    "FVG + Demand OB Confluence":     45,
    "CHOCH -- Bearish Reversal":      50,   # tiny sample, but keep high
    "CHOCH Reversal -- Trend Change": 36,
    "Supply Order Block Rejection":   31,
    "Liquidity Sweep -- Short":       33,
    "ORB Breakout -- Long":           35,   # live breakout, provisional
    # cut/low setups score low so they never get picked if better exist:
    "Supply OB + BOS Breakdown":      17,
    "Institutional Accumulation":     12,
}
DEFAULT_SETUP_SCORE = 30


def now_ist():
    return datetime.now(IST)


def get_session() -> dict:
    """Return current session name + allowed directions."""
    t = now_ist()
    mins = t.hour * 60 + t.minute
    if 9*60+30 <= mins < 11*60+30:
        return {"session": "morning",   "allowed": ["LONG", "SHORT"], "morning": True}
    if 11*60+30 <= mins < 14*60+30:
        return {"session": "afternoon", "allowed": ["LONG"],          "morning": False}
    if 14*60+30 <= mins <= 15*60+15:
        return {"session": "closing",   "allowed": ["LONG"],          "morning": False, "btst": True}
    return {"session": "closed", "allowed": [], "morning": False}


def slots_available(trades_today: list) -> dict:
    """Compute remaining slots given trades already taken today."""
    total_taken   = len(trades_today)
    morning_taken = len([t for t in trades_today if t.get("session_taken") == "morning"])
    sess = get_session()

    total_left = max(0, MAX_TRADES_PER_DAY - total_taken)
    if sess["session"] == "morning":
        morning_left = max(0, MORNING_MAX - morning_taken)
        allow = min(total_left, morning_left)
    else:
        allow = total_left  # afternoon/closing fill remaining, longs only
    return {
        "session":    sess["session"],
        "allowed":    sess["allowed"],
        "slots":      allow,
        "total_left": total_left,
        "btst":       sess.get("btst", False),
    }


def _setup_score(name: str) -> int:
    for key, v in SETUP_RANK.items():
        if key.lower() in (name or "").lower():
            return v
    return DEFAULT_SETUP_SCORE


def rank_and_select(candidates: list, slots: int, allowed_dirs: list,
                    sector_strength: dict = None, taken_sectors: set = None) -> list:
    """
    Rank candidates and pick up to `slots`, applying:
      - direction must be in allowed_dirs (session rule)
      - winning-setups first
      - sector-aligned (bonus for strong sectors)
      - no duplicate sectors (this day)
    Each candidate: dict with symbol, direction, setup_name, sector, entry_range_ok(bool).
    """
    sector_strength = sector_strength or {}
    taken_sectors   = set(taken_sectors or set())

    # filter: direction allowed + entry-range clean
    pool = [c for c in candidates
            if c.get("direction", "").upper() in allowed_dirs
            and c.get("entry_range_ok", True)]

    # score each: setup win rate + sector strength bonus
    def score(c):
        s = _setup_score(c.get("setup_name", ""))
        sec = c.get("sector", "")
        s += sector_strength.get(sec, 0) * 0.5  # sector momentum bonus (small weight)
        return s

    pool.sort(key=score, reverse=True)

    picks, used_sectors = [], set(taken_sectors)
    for c in pool:
        if len(picks) >= slots:
            break
        sec = c.get("sector", "")
        if sec and sec in used_sectors:
            continue  # no duplicate sectors
        picks.append(c)
        if sec:
            used_sectors.add(sec)
    return picks


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Session now:", get_session())
    print("Slots (0 taken):", slots_available([]))
