"""
BACKTEST — pipeline replay
==========================
Regenerates the candidates the pipeline would have published on a given
session, using only bars up to and including that session.

WHY IT RECOMPUTES PER DATE INSTEAD OF CACHING
---------------------------------------------
The obvious design is to run 02b once per symbol over full history and slice
the result per date. Measured against a genuine as-of computation on RELIANCE,
that disagrees on 17 columns -- ob_high, ob_low, last_swing_high/low,
is_demand_ob, is_supply_ob, equilibrium, the FVG block. Swing points are
confirmed by the bars after them and order blocks are identified
retroactively, so a cached full-history frame carries tomorrow's confirmation
in today's row. It is fast and it is wrong, and backtest/asof.py exists to
keep it that way on purpose.

The costs measured on the box, per symbol per date:

    full history (905 bars)   1875 ms  -> 346 sessions x 539 symbols = 97 h
    bounded window (N bars)   see LOOKBACK_BARS below

Only 02b + active_zones + zone_entry are per-symbol. 03b's score_vectorized is
row-wise -- no rolling, shift or groupby -- so scoring runs once over the
date's rows rather than per symbol, and is not the cost.
"""

import sys
import logging
import importlib.util
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from backtest.asof import slice_asof

log = logging.getLogger("BACKTEST-REPLAY")

# NONE. Measured, not assumed.
#
# A bounded window would have cut the cost proportionally, so it was tested
# first: compute the row at D from the last N bars <= D, compare against the
# same row computed from ALL bars <= D, rtol 1e-9. Across 6 symbols x 4 dates:
#
#     N=120  0/24 exact   ema20, ema50, ema200, rsi differ
#     N=180  0/24 exact   ema50, ema200, rsi, macd_hist differ
#     N=250  0/24 exact   ema50, ema200, macd_hist, rsi differ
#     N=300  0/24 exact   ema50, ema200, price_in_bear_fvg, macd_hist differ
#     N=400  0/24 exact   ema200, ema50, price_in_bear_fvg differ
#
# The survivors are all RECURSIVE: an EMA never fully forgets its seed. After
# 400 bars an EMA200 still carries roughly (1-2/201)^400 ~ 1.8% of its initial
# condition, which is a material difference in an indicator a gate reads, not
# floating-point noise. No window in the tested range is sound, and a larger
# one saves nothing worth having.
#
# So: full history up to D, every date. 1875 ms/symbol/date measured on the
# box -- 11 sessions x 539 symbols ~ 3.1 h, 346 sessions ~ 97 h. That cost is
# reported rather than optimised away, because every way of reducing it that
# was tested changes the numbers.
LOOKBACK_BARS = None

MIN_BARS = 120          # below this 02b cannot produce usable structure


def _load_script(name: str, rel: str):
    """02b and 03b start with a digit, so plain import is a syntax error."""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_smc = None
_score = None


def _stages():
    global _smc, _score
    if _smc is None:
        _smc = _load_script("smc02b", "engine/02b_smc_signals.py")
        _score = _load_script("score03b", "engine/03b_score.py")
    return _smc, _score


def load_frames(symbols=None, stocks_dir: Path = None) -> dict:
    """{symbol: full-history OHLCV}. Sliced per date, never used whole."""
    stocks_dir = stocks_dir or ROOT / "data/processed/stocks"
    out = {}
    for f in sorted(stocks_dir.glob("*.parquet")):
        if symbols and f.stem not in symbols:
            continue
        df = pd.read_parquet(f)
        df["date"] = pd.to_datetime(df["date"])
        out[f.stem] = df.sort_values("date").reset_index(drop=True)
    return out


def load_market() -> pd.DataFrame:
    m = pd.read_parquet(ROOT / "data/processed/market.parquet")
    m["date"] = pd.to_datetime(m["date"])
    return m


_sector_cache = {}


def sector_bias_asof(d) -> dict:
    """
    Sector classification as it stood on d.

    sector_momentum.parquet holds ONE date and is overwritten nightly, so the
    persisted file is today's snapshot and using it for a June session would
    leak. compute_sector_momentum already takes a target_date and filters
    df[date <= target_date], so the as-of value is recomputed rather than read.

    Cached per date. The call re-globs and re-reads every stock parquet, and
    the answer for a given date never changes -- without this it is paid once
    per date per run for no gain.

    RETURN SHAPE IS INCONSISTENT UPSTREAM. compute_sector_momentum is annotated
    `-> pd.DataFrame` but returns (sector_df, stocks_df) on the success path and
    a bare pd.DataFrame() on its empty path at line 94, so `a, b = ...` raises
    on empty input. Both shapes are handled here rather than relying on either.
    """
    key = pd.Timestamp(d)
    if key in _sector_cache:
        return _sector_cache[key]

    mod = _load_script("sector07", "engine/07_sector_momentum.py")
    out = mod.compute_sector_momentum(key)
    sec = out[0] if isinstance(out, tuple) else out

    if sec is None or not hasattr(sec, "empty") or sec.empty \
            or "trade_bias" not in sec.columns:
        bias = {}
    else:
        bias = dict(zip(sec["sector"], sec["trade_bias"]))

    _sector_cache[key] = bias
    return bias


def structure_for_date(d, frames: dict, market: pd.DataFrame,
                       lookback: int = LOOKBACK_BARS) -> pd.DataFrame:
    """
    One row per symbol: its SMC/zone state at d, computed from bars <= d only.

    This is the expensive half and the half that must be as-of correct.
    """
    smc, _ = _stages()
    m = slice_asof(market, d)
    rows = []

    for sym, df in frames.items():
        h = slice_asof(df, d)
        if len(h) < MIN_BARS:
            continue
        if h["date"].iloc[-1] != pd.Timestamp(d):
            continue                      # symbol did not trade on d
        if lookback:
            h = h.tail(lookback)
        try:
            from engine.active_zones import add_active_zones
            from engine.zone_entry import compute_zone_entries
            out = compute_zone_entries(add_active_zones(smc.compute_smc_signals(h.copy(), m)))
        except Exception as e:
            log.debug(f"{sym} @ {d}: {type(e).__name__}: {e}")
            continue
        last = out.iloc[[-1]].copy()
        last["symbol"] = sym
        rows.append(last)

    if not rows:
        return pd.DataFrame()
    return pd.concat(rows, ignore_index=True)


def generate_for_date(d, frames: dict, market: pd.DataFrame,
                      lookback: int = LOOKBACK_BARS) -> pd.DataFrame:
    """
    Candidates the pipeline would publish on d. Signature matches what
    backtest.asof.assert_asof drives, so the guard tests the real path.
    """
    _, score = _stages()
    combined = structure_for_date(d, frames, market, lookback)
    if combined.empty:
        return pd.DataFrame()

    symbol_sector = score.load_symbol_sector()
    bias = sector_bias_asof(d)

    scored = []
    for direction in ("long", "short"):
        try:
            s = score.process_direction(combined.copy(), direction, bias, symbol_sector)
        except Exception as e:
            log.error(f"scoring {direction} @ {d}: {type(e).__name__}: {e}")
            continue
        scored.append(s[s["qualifies"]])

    if not scored:
        return pd.DataFrame()
    q = pd.concat(scored, ignore_index=True)
    if q.empty:
        return q

    # Same publication cut as build_playbooks: nearest to entry first.
    plays = []
    for direction, n in (("long", score.TOP_N_LONG), ("short", score.TOP_N_SHORT)):
        g = q[q["direction"] == direction]
        if len(g):
            plays.append(g.nsmallest(n, "entry_dist_pct"))
    if not plays:
        return pd.DataFrame()

    out = pd.concat(plays, ignore_index=True)
    out["signal_date"] = pd.Timestamp(d)
    keep = [c for c in ("signal_date", "symbol", "direction", "setup_name",
                        "entry_ref", "entry_low", "entry_high", "sl",
                        "target_1", "target_2", "qty", "notional", "risk_inr",
                        "entry_dist_pct", "stop_pct", "total_score")
            if c in out.columns]
    return out[keep].sort_values(["symbol", "direction"]).reset_index(drop=True)
