"""
THE STOCK LOGIC — Trading Calendar
===================================
NSE holiday-aware date utilities.
Used by: trade_review.py, signal generator, cron jobs.
"""

from datetime import date, timedelta
from typing import List, Optional

# NSE holidays 2025-2026 (Saturday/Sunday auto-excluded separately)
NSE_HOLIDAYS = {
    # 2025
    "2025-01-26", "2025-02-19", "2025-03-14", "2025-03-31",
    "2025-04-10", "2025-04-14", "2025-04-17", "2025-04-18",
    "2025-05-01", "2025-06-07", "2025-07-28", "2025-08-15",
    "2025-08-27", "2025-10-02", "2025-10-21", "2025-10-22",
    "2025-10-23", "2025-11-05", "2025-11-20", "2025-12-25",
    # 2026
    "2026-01-26", "2026-03-18", "2026-04-02", "2026-04-06",
    "2026-04-14", "2026-04-17", "2026-05-01", "2026-08-15",
    "2026-10-02", "2026-10-21", "2026-10-22", "2026-11-05",
    "2026-11-20", "2026-12-25",
}


def is_trading_day(d: date) -> bool:
    """Return True if d is a valid NSE trading day."""
    if d.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    return d.isoformat() not in NSE_HOLIDAYS


def next_trading_day(from_date: Optional[date] = None) -> date:
    """
    Return the next trading day after from_date.
    If from_date is None, uses today.
    """
    d = (from_date or date.today()) + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
        if (d - (from_date or date.today())).days > 14:
            raise ValueError(f"Could not find next trading day within 14 days of {from_date}")
    return d


def prev_trading_day(from_date: Optional[date] = None) -> date:
    """Return the most recent trading day before from_date."""
    d = (from_date or date.today()) - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
        if ((from_date or date.today()) - d).days > 14:
            raise ValueError(f"Could not find prev trading day within 14 days of {from_date}")
    return d


def next_n_trading_days(from_date: date, n: int = 5) -> List[date]:
    """Return the next n trading days after from_date."""
    days = []
    d = from_date + timedelta(days=1)
    while len(days) < n:
        if is_trading_day(d):
            days.append(d)
        d += timedelta(days=1)
        if (d - from_date).days > 60:
            break
    return days


def trading_days_between(start: date, end: date) -> int:
    """
    Count trading days between start and end (exclusive of start, inclusive of end).
    """
    if end <= start:
        return 0
    count = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            count += 1
        d += timedelta(days=1)
    return count


def is_friday_signal(signal_date: date) -> bool:
    """
    Return True if signal_date is a Friday.
    Friday signals carry gap risk over the weekend.
    BTST trades from Friday close require extra caution.
    """
    return signal_date.weekday() == 4


def friday_gap_risk_flag(signal_date: date) -> dict:
    """
    Returns a risk flag dict for Friday signals.
    Gap risk: 2 calendar days of news before Monday open.
    """
    if not is_friday_signal(signal_date):
        return {"has_gap_risk": False, "reason": None}

    return {
        "has_gap_risk":   True,
        "reason":         "Friday signal — weekend gap risk. 2 days of news before Monday open.",
        "recommendation": "Reduce position size by 50% or skip. Set wider SL for gap protection.",
        "next_open":      next_trading_day(signal_date).isoformat(),
    }


def days_until_next_trading_day(from_date: Optional[date] = None) -> int:
    """Return calendar days until the next trading day."""
    d = from_date or date.today()
    nxt = next_trading_day(d)
    return (nxt - d).days


if __name__ == "__main__":
    today = date.today()
    print(f"Today:               {today} ({'trading day' if is_trading_day(today) else 'NOT a trading day'})")
    print(f"Next trading day:    {next_trading_day(today)}")
    print(f"Prev trading day:    {prev_trading_day(today)}")
    print(f"Next 5 trading days: {next_n_trading_days(today, 5)}")
    print(f"Trading days this week so far: {trading_days_between(today - timedelta(days=today.weekday()), today)}")
    print(f"Friday gap risk:     {friday_gap_risk_flag(today)}")
    print(f"Days to next open:   {days_until_next_trading_day(today)}")
