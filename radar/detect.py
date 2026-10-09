"""
Detection. Mode A event triggers, Mode B extremes scanner.

docs/TSL_FLASH.md §03 and §04. Everything here is a pure function of the
bhavcopy frames plus config -- no network, no database, no clock. The nightly
runner supplies the session to score and the store writes the result, which is
what makes a replay over historical sessions produce the same answers the live
run produced on the night.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from radar import bars, levels

log = logging.getLogger("RADAR-DETECT")

TRIGGER_SHOCK_1D = "shock_1d"
TRIGGER_SHOCK_5D = "shock_5d"
TRIGGER_CIRCUIT = "circuit"

# Tolerance when matching a locked session's move against a published band
# width. NSE bands are applied to a tick-rounded price, so a 10% band lands at
# 9.97% or 10.02% rather than exactly 10.
BAND_TOLERANCE_PCT = 0.25


def universe(cfg) -> tuple:
    """(symbols, sector_map). The Nifty 500 list from engine/universe.py.

    READ-ONLY, and the only thing Radar imports from the signals side. That
    file is auto-generated from ind_nifty500list.csv and currently resolves
    wider than its name -- the count is logged and recorded per run so a
    universe change shows up in the record rather than being inferred.
    """
    name = str(cfg.universe).strip().lower()
    if name not in ("nifty500", "nifty_500", "500"):
        log.warning(f"universe={cfg.universe!r} is not a name Radar knows — "
                    f"falling back to the Nifty 500 list")
    from engine.universe import ALL_SYMBOLS, SYMBOL_SECTOR_MAP
    return list(ALL_SYMBOLS), dict(SYMBOL_SECTOR_MAP)


# ── MODE A — the event triggers ───────────────────────────────────

def _move_pct(cur: float, ref: float) -> float | None:
    try:
        cur, ref = float(cur), float(ref)
    except (TypeError, ValueError):
        return None
    if not ref or pd.isna(ref) or pd.isna(cur):
        return None
    return (cur / ref - 1.0) * 100.0


def check_shock_1d(df: pd.DataFrame, cfg) -> dict | None:
    """|1-day move| >= shock_1d_pct AND volume >= shock_1d_vol_mult x 20-day avg."""
    if df is None or len(df) < cfg.vol_avg_window + 1:
        return None
    row = df.iloc[-1]
    move = _move_pct(row["close"], row["prev_close"])
    if move is None:
        return None
    avg = bars.vol_average(df, cfg.vol_avg_window, through_prior=True)
    if not avg:
        return None
    try:
        mult = float(row["volume"]) / avg
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if abs(move) < cfg.shock_1d_pct or mult < cfg.shock_1d_vol_mult:
        return None
    return {"trigger_type": TRIGGER_SHOCK_1D,
            "move_pct": round(move, 2),
            "vol_multiple": round(mult, 2),
            "direction": "up" if move > 0 else "down",
            "move_threshold": cfg.shock_1d_pct,
            "vol_threshold": cfg.shock_1d_vol_mult}


def check_shock_5d(df: pd.DataFrame, cfg) -> dict | None:
    """|5-day move| >= shock_5d_pct AND 5-day volume >= mult x 5 x 20-day avg.

    The volume side is stated against the SUM over five sessions compared to
    five times the daily average, so the multiple is directly comparable to the
    1-day one: both read as "x times a normal session".
    """
    if df is None or len(df) < cfg.vol_avg_window + 6:
        return None
    row = df.iloc[-1]
    ref = df["close"].iloc[-6]
    move = _move_pct(row["close"], ref)
    if move is None:
        return None
    avg = bars.vol_average(df, cfg.vol_avg_window, through_prior=True)
    if not avg:
        return None
    try:
        vol5 = float(df["volume"].astype(float).iloc[-5:].sum())
    except (TypeError, ValueError):
        return None
    mult = vol5 / (avg * 5.0)
    if abs(move) < cfg.shock_5d_pct or mult < cfg.shock_5d_vol_mult:
        return None
    return {"trigger_type": TRIGGER_SHOCK_5D,
            "move_pct": round(move, 2),
            "vol_multiple": round(mult, 2),
            "direction": "up" if move > 0 else "down",
            "move_threshold": cfg.shock_5d_pct,
            "vol_threshold": cfg.shock_5d_vol_mult}


def check_circuit(df: pd.DataFrame, cfg) -> dict | None:
    """A session LOCKED at a price band. Narrower than it sounds -- see below.

    NEITHER BHAVCOPY LAYOUT PUBLISHES THE BAND, and every number must trace to
    bhavcopy, so the band cannot be looked up. What is derivable is the one
    unambiguous circuit state: high == low, meaning the price never moved all
    session, together with a move matching a published band width.

    WHAT THIS MISSES, stated here as well as in the doc because it is the kind
    of gap that gets forgotten: a stock that touched its band intraday and came
    off it. That session has high != low and is, at EOD, indistinguishable from
    any other volatile day. Radar reports nothing for it rather than labelling
    a different trigger as a circuit.

    No volume condition. A locked stock often trades almost nothing precisely
    because there is no one on the other side, so requiring a volume multiple
    here would filter out the cleanest instances of the thing being detected.
    """
    if df is None or len(df) < 2:
        return None
    row = df.iloc[-1]
    try:
        hi, lo = float(row["high"]), float(row["low"])
    except (TypeError, ValueError):
        return None
    if pd.isna(hi) or pd.isna(lo) or hi != lo:
        return None
    move = _move_pct(row["close"], row["prev_close"])
    if move is None or move == 0:
        return None
    for band in cfg.circuit_bands:
        if abs(abs(move) - band) <= BAND_TOLERANCE_PCT:
            avg = bars.vol_average(df, cfg.vol_avg_window, through_prior=True)
            mult = None
            try:
                mult = round(float(row["volume"]) / avg, 2) if avg else None
            except (TypeError, ValueError, ZeroDivisionError):
                mult = None
            return {"trigger_type": TRIGGER_CIRCUIT,
                    "move_pct": round(move, 2),
                    "vol_multiple": mult,
                    "direction": "up" if move > 0 else "down",
                    "band_pct": band,
                    "move_threshold": band,
                    "vol_threshold": 1.0}
    return None


# ORDER MATTERS. A session can satisfy more than one trigger -- POLICYBZR on
# 2026-09-24 met shock_1d, and the following four sessions met shock_5d off the
# same crash. The first match wins and the most specific comes first, so a card
# is labelled by the sharpest thing that happened rather than by whichever
# check ran last.
TRIGGER_ORDER = (check_circuit, check_shock_1d, check_shock_5d)


def detect_symbol(symbol: str, on: date, cfg) -> dict | None:
    """Score one symbol on one session. -> an event dict, or None."""
    df = bars.frame_through(symbol, on)
    if df is None or not len(df) or df["date"].iloc[-1] != on:
        # The symbol did not trade that session, or its bhavcopy is missing.
        # Either way there is nothing to measure; silence, not a zero.
        return None
    for check in TRIGGER_ORDER:
        hit = check(df, cfg)
        if hit:
            hit.update({"symbol": symbol, "event_date": on,
                        "close": round(float(df["close"].iloc[-1]), 2)})
            return hit
    return None


def detect_events(on: date, cfg, symbols=None, sector_map=None) -> list:
    """Every Mode A event on one session, across the universe."""
    if symbols is None:
        symbols, sector_map = universe(cfg)
    sector_map = sector_map or {}
    out = []
    for sym in symbols:
        try:
            hit = detect_symbol(sym, on, cfg)
        except Exception as e:
            # ONE BAD SYMBOL MUST NOT END THE SWEEP. A malformed parquet would
            # otherwise silently truncate the universe at wherever it sorted,
            # and the run would report a short list rather than a failure.
            log.warning(f"{sym}: detection raised ({type(e).__name__}: {e})")
            continue
        if hit:
            hit["sector"] = sector_map.get(sym)
            out.append(hit)
    log.info(f"{on}: {len(out)} event(s) from {len(symbols)} symbols")
    return out


# ── MODE B — the extremes scanner ─────────────────────────────────

def scan_symbol(symbol: str, on: date, cfg) -> list:
    """Scanner rows for one symbol on one session. Zero, one, or two sides.

    A session can print both a new 52-week high and a new 52-week low -- a huge
    outside bar on a stock near a year-long extreme does exactly that -- so
    this returns a list rather than one row.
    """
    df = bars.frame_through(symbol, on)
    if df is None or not len(df) or df["date"].iloc[-1] != on:
        return []
    avg = bars.vol_average(df, cfg.vol_avg_window, through_prior=True)
    if not avg:
        return []
    row = df.iloc[-1]
    try:
        vol = float(row["volume"])
        close = float(row["close"])
    except (TypeError, ValueError):
        return []
    mult = vol / avg
    if mult < cfg.scanner_vol_mult:
        return []

    rows = []
    for side in ("high", "low"):
        if not levels.is_new_52w_extreme(df, side):
            continue
        extreme = levels.w52_high(df) if side == "high" else levels.w52_low(df)
        if extreme is None:
            continue
        dp = row.get("delivery_pct")
        try:
            dp = None if dp is None or pd.isna(dp) else round(float(dp), 2)
        except (TypeError, ValueError):
            dp = None
        rows.append({
            "symbol": symbol, "trade_date": on, "side": side,
            "close": round(close, 2),
            "extreme_price": extreme,
            # SIGNED, from the extreme to the close. A stock that printed the
            # high and closed under it reads negative, which is the thing a
            # reader wants to see at a glance.
            "pct_from_extreme": levels.dist_pct(close, extreme),
            "vol_multiple": round(mult, 2),
            "volume": int(vol),
            "delivery_pct": dp,
        })
    return rows


def scan_extremes(on: date, cfg, symbols=None, sector_map=None) -> dict:
    """The full sweep, ranked. -> {"high": [...], "low": [...]}

    Ranked by volume multiple, descending, top scanner_top_n_each_side each
    side. Ties broken by symbol so the ordering is stable across runs -- an
    unstable sort would make two identical replays disagree about rank_in_side
    and the independent-recomputation check would fail on nothing.
    """
    if symbols is None:
        symbols, sector_map = universe(cfg)
    sector_map = sector_map or {}
    buckets = {"high": [], "low": []}
    for sym in symbols:
        try:
            for r in scan_symbol(sym, on, cfg):
                r["sector"] = sector_map.get(sym)
                buckets[r["side"]].append(r)
        except Exception as e:
            log.warning(f"{sym}: scan raised ({type(e).__name__}: {e})")
            continue

    for side in ("high", "low"):
        buckets[side].sort(key=lambda r: (-r["vol_multiple"], r["symbol"]))
        buckets[side] = buckets[side][:cfg.scanner_top_n_each_side]
        for i, r in enumerate(buckets[side], start=1):
            r["rank_in_side"] = i
    log.info(f"{on}: scanner {len(buckets['high'])} high(s), "
             f"{len(buckets['low'])} low(s)")
    return buckets
