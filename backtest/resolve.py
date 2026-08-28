"""
BACKTEST — resolution
=====================
Deliberately thin. Every function here forwards to engine/mark_signals.py.

The point of the backtester is that its results are comparable with the live
record, and that only holds if the two resolve identically. Reimplementing
"walk forward, stop wins the bar, entry at entry_ref for longs, day-one
open-to-close for shorts" would produce a second definition that drifts -- the
exact failure this codebase has had four times with duplicated constants.

resolve_signal() is already a pure function of (signal, closes, all_dates) with
no I/O, so reuse is real rather than a fork. If a resolution rule changes, it
changes in mark_signals and both sides move together.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from engine.mark_signals import (          # noqa: F401  (re-exported on purpose)
    resolve_signal,
    resolve_all,
    load_closes,
    trading_dates,
    WINDOW_DAYS,
)

__all__ = ["resolve_signal", "resolve_all", "load_closes", "trading_dates",
           "WINDOW_DAYS", "resolve_candidates"]


def resolve_candidates(signals_df, closes=None, all_dates=None) -> dict:
    """
    Resolve a set of candidates. `signals_df` needs the columns resolve_signal
    reads: signal_date, symbol, direction, entry_ref, sl, target_1.

    Returns {(signal_date, symbol, direction): {resolution, resolved_pnl_pct,
    resolved_on}} -- empty dict entries are omitted, exactly as resolve_all
    does, because unresolved is a real state and not a zero.
    """
    if closes is None:
        closes = load_closes()
    if all_dates is None:
        all_dates = trading_dates(closes)
    return resolve_all(signals_df, closes, all_dates)
