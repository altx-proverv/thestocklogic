"""
BACKTEST — run configuration
============================
A config is the set of knobs a run varies. Everything not listed here is
inherited from atlas/config.py, which since the dedup is the single source for
the risk budget, notional cap, quantity multiple and stop band -- so a backtest
override has exactly one place to apply.

The config is hashed and stored with every run. Two runs with the same hash and
the same git_sha ran the same experiment; if they disagree, something outside
both is not deterministic and that is worth knowing.
"""

import sys
import json
import hashlib
from dataclasses import dataclass, asdict, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas.config import (
    MAX_RISK_PER_TRADE, MAX_NOTIONAL_PER_TRADE, QUANTITY_MULTIPLE,
    MIN_STOP_PCT, MAX_STOP_PCT,
)

# Resolution horizon. Not in atlas/config.py because it belongs to the
# measurement, not to the trading rules -- mark_signals owns it and the
# backtest must not diverge from it or results stop being comparable.
from engine.mark_signals import WINDOW_DAYS as MARKS_WINDOW_DAYS

# ── The pinned baseline window ────────────────────────────────────────
# The dashboard's window is the last 20 DISTINCT mark_date values, so its
# figures move every day as signals age out: resolved ran 302, 298, 294, 291,
# 291, 289, 289, 292, 286, 290, 291, 278 across consecutive sessions. A
# baseline stated as an equality therefore has to name the window it belongs
# to, or the same correct engine passes today and fails tomorrow.
#
# 2026-08-21 is the window end at which the live record shows 289 resolved on
# the all-publications basis. Corroborated independently: tsl-dashboard.html
# cites shorts at -Rs31,836, and at the Rs1L-per-signal notional that view uses
# it is exactly the -31.836% short sum for this window.
BASELINE_WINDOW_END = "2026-08-21"

# What the live record shows at that window. The baseline run must reproduce
# every one of these. Both bases are recorded rather than one being chosen:
# all-publications answers "of everything we published, how did it do";
# first-signal-only answers "what would the book have made", since Gate 3b
# skips a symbol already held. They are different questions.
BASELINE_EXPECTED = {
    "all_publications": {
        "n_signals": 424,
        "resolved": 289, "unresolved": 135,
        "STOP": 85, "TARGET": 127, "SAME_DAY": 77, "EXPIRED": 0,
        "long_n": 212, "short_n": 77,
        "long_sum_pct": 436.375, "short_sum_pct": -31.836,
        "total_sum_pct": 404.540,
    },
    "first_signal_only": {
        "n_signals": 243,
        "resolved": 177, "unresolved": 66,
        "STOP": 54, "TARGET": 71, "SAME_DAY": 52, "EXPIRED": 0,
        "long_n": 125, "short_n": 52,
        "long_sum_pct": 218.125, "short_sum_pct": -27.525,
        "total_sum_pct": 190.600,
    },
}


@dataclass(frozen=True)
class Config:
    """One experiment. Defaults ARE the baseline (current live settings)."""

    # --- what the first test after baseline varies -------------------
    # single_ob      one order block = one candle's range, 1-2% wide (current)
    # merged_ob      overlapping blocks merged into one zone
    # consolidation  the consolidation range around a cluster of blocks
    zone_definition: str = "single_ob"

    # 'current' = whatever universe.py resolves today. Universe expansion is
    # explicitly NOT tested yet: it needs bhavcopy history for ~1,500 more
    # symbols plus liquidity, circuit-band and spread filters.
    universe: str = "current"

    # --- gates, defaulted from the live config -----------------------
    max_entry_dist_pct: float = 0.30
    min_stop_pct: float = MIN_STOP_PCT
    max_stop_pct: float = MAX_STOP_PCT
    risk_per_trade: float = MAX_RISK_PER_TRADE
    max_notional: float = MAX_NOTIONAL_PER_TRADE
    qty_multiple: int = QUANTITY_MULTIPLE
    resolution_window_days: int = MARKS_WINDOW_DAYS

    # --- period ------------------------------------------------------
    # Regime is usable from 2025-04-04 (nifty_200dma needs 200 bars); before
    # that market_regime is 'unknown', which means no trade.
    period_start: str = "2025-04-04"
    period_end: str = "2026-08-27"
    holdout_start: str = "2026-08-01"       # tune <= 07-31, verify on August

    # Rolling-window end this run reports against.
    window_end: str = BASELINE_WINDOW_END

    label: str = "baseline"

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        """Stable across runs and orderings; excludes the human label."""
        d = {k: v for k, v in sorted(self.to_dict().items()) if k != "label"}
        return hashlib.sha256(json.dumps(d, sort_keys=True,
                                         default=str).encode()).hexdigest()[:16]

    def is_baseline(self) -> bool:
        return self.hash() == Config().hash()


BASELINE = Config()
