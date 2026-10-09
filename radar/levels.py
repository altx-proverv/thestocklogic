"""
Levels — pure functions over bhavcopy. No model touches these, now or later.

docs/TSL_FLASH.md §05. Each function takes a date-sorted frame cut at the
session being scored and returns a price or None. None means the input was
missing or too short, and the caller DROPS that line: a level computed from 40
sessions and labelled `dma_200` would be a fabricated number with a plausible
shape, which is the exact failure mode the hard rules exist to prevent.

CORPORATE ACTIONS DROP A LEVEL, THEY DO NOT SCALE IT. The parquets are not
split-adjusted, so any window spanning a split or bonus is comparing two
different price scales. INDIAGLYCO split on 2026-09-17 and this module would
have reported "52-week high, +218.53% from here" off a pre-split 1219.00
against a post-split close of 382.70. Each multi-session level is now gated on
bars.action_in_last() for its OWN lookback, so a stock can lose its 200-DMA and
keep its 50-DMA -- which is the honest answer when only the longer window
straddles the action. Adjusting the series here would mean inferring a ratio
from a price gap, which is a guess with a number's face on it.

NO "ENTRY" OR "EXIT" IN ANY NAME OR LABEL HERE. The level types are
descriptive -- where price has been, not what to do about it -- and
tests/test_radar.py asserts that no banned word appears in this module or in
anything it produces. The naming is what keeps a reference price from reading
as an instruction.
"""

from __future__ import annotations

import logging

import pandas as pd

log = logging.getLogger("RADAR-LEVELS")

# 52 weeks of sessions. NSE runs ~250 a year; 252 is the convention and the
# figure the exchange's own 52-week columns are built on.
SESSIONS_52W = 252


def _f(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(x) else x


def _spans_action(df: pd.DataFrame, sessions: int) -> bool:
    from radar.bars import action_in_last
    return action_in_last(df, sessions)


def w52_high(df: pd.DataFrame) -> float | None:
    """Highest HIGH in 252 sessions. Intraday high, not close.

    The 52-week high a reader sees quoted anywhere is the intraday extreme, so
    using the close here would produce a number lower than every other source
    and look like an error in ours.
    """
    if df is None or len(df) < 2:
        return None
    if _spans_action(df, SESSIONS_52W):
        return None
    w = df["high"].astype(float).iloc[-SESSIONS_52W:]
    return round(float(w.max()), 2) if len(w) else None


def w52_low(df: pd.DataFrame) -> float | None:
    if df is None or len(df) < 2:
        return None
    if _spans_action(df, SESSIONS_52W):
        return None
    w = df["low"].astype(float).iloc[-SESSIONS_52W:]
    return round(float(w.min()), 2) if len(w) else None


def is_new_52w_extreme(df: pd.DataFrame, side: str) -> bool:
    """Did the LAST session set a new 252-session extreme?

    THE PRIOR WINDOW EXCLUDES TODAY. Comparing today's high against a window
    that contains today is `x >= max(…, x)`, which is true for every row -- the
    scanner would return the whole universe. The comparison is against the 252
    sessions BEFORE this one.
    """
    if df is None or len(df) < 21:
        return False
    # A "new 52-week low" measured across a split is just the split. The
    # scanner must not list it, and dropping it here keeps that out of both the
    # scanner and the card.
    if _spans_action(df, SESSIONS_52W):
        return False
    cur = df.iloc[-1]
    prior = df.iloc[:-1]
    if not len(prior):
        return False
    window = prior.iloc[-SESSIONS_52W:]
    if side == "high":
        hi = _f(cur["high"])
        return hi is not None and hi >= float(window["high"].astype(float).max())
    lo = _f(cur["low"])
    return lo is not None and lo <= float(window["low"].astype(float).min())


def ipo_price(df: pd.DataFrame, max_age_years: int, download_floor,
              floor_slack_days: int) -> float | None:
    """First close in the series -- ONLY when the series begins at listing.

    USUALLY NONE, AND THAT IS CORRECT. The parquets start at the downloader's
    START_DATE, so the first row of a series is the first session that was
    DOWNLOADED. For a stock listed in 2015 that row is an ordinary Tuesday in
    January 2023, and reporting it as an IPO price would invent a number with
    exactly the right shape to be believed.

    So the series must begin strictly after the download floor (plus slack for
    the floor landing on a non-session), and the listing must be inside
    max_age_years of the last session. POLICYBZR -- listed 2021, data from
    2023 -- correctly gets None.
    """
    if df is None or len(df) < 2:
        return None
    first = df["date"].iloc[0]
    last = df["date"].iloc[-1]
    from datetime import timedelta
    if first <= download_floor + timedelta(days=floor_slack_days):
        return None                      # the floor, not a listing
    if (last - first).days > max_age_years * 365:
        return None                      # listed, but too long ago to be useful
    from radar.bars import corporate_actions
    if corporate_actions(df):
        # The listing price is on a pre-action scale. There is no honest way to
        # show it next to today's price.
        return None
    return round(_f(df["close"].iloc[0]) or 0, 2) or None


def event_high(df: pd.DataFrame) -> float | None:
    return round(_f(df["high"].iloc[-1]) or 0, 2) or None if df is not None and len(df) else None


def event_low(df: pd.DataFrame) -> float | None:
    return round(_f(df["low"].iloc[-1]) or 0, 2) or None if df is not None and len(df) else None


def pre_event_close(df: pd.DataFrame) -> float | None:
    """The close BEFORE the event session.

    Taken from prev_close on the event row rather than from the previous row's
    close, because the two differ across a corporate action: bhavcopy's
    prev_close is adjusted and the prior row's close is not. prev_close is what
    the exchange measured the day's move against, so it is what the card must
    show.
    """
    if df is None or not len(df):
        return None
    v = _f(df["prev_close"].iloc[-1])
    if v:
        return round(v, 2)
    if len(df) >= 2:
        v = _f(df["close"].iloc[-2])
        return round(v, 2) if v else None
    return None


def dma(df: pd.DataFrame, period: int) -> float | None:
    """Simple moving average of close. None when the history is short.

    The None is load-bearing: a 200-DMA from 150 sessions is not a 200-DMA, and
    a newly listed stock must show no 200-DMA line rather than a mislabelled
    150-session mean.
    """
    if df is None or len(df) < period:
        return None
    # PER-PERIOD, so a 50-DMA can survive an action that kills the 200.
    if _spans_action(df, period):
        return None
    return round(float(df["close"].astype(float).iloc[-period:].mean()), 2)


def rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder's RSI on closes.

    WILDER, NOT A SIMPLE MEAN OF GAINS. The two diverge by several points on a
    series with one enormous bar in it -- which is every series Radar looks at
    -- and "RSI 14" without qualification means Wilder's everywhere a reader
    would check it.
    """
    if df is None or len(df) < period + 1:
        return None
    if _spans_action(df, period + 1):
        return None
    close = df["close"].astype(float)
    delta = close.diff().dropna()
    if len(delta) < period:
        return None
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    # Seed with the simple mean of the first `period`, then smooth -- the
    # standard Wilder recursion.
    avg_g = float(gain.iloc[:period].mean())
    avg_l = float(loss.iloc[:period].mean())
    for i in range(period, len(delta)):
        avg_g = (avg_g * (period - 1) + float(gain.iloc[i])) / period
        avg_l = (avg_l * (period - 1) + float(loss.iloc[i])) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


def swing_pivots(df: pd.DataFrame, bars: int = 5, want: int = 3) -> dict:
    """Most recent `want` fractal swing highs and lows. -> {"highs":[], "lows":[]}

    A 5-BAR FRACTAL means the centre bar's high is the highest of the five --
    two either side -- so a pivot is only confirmed `bars//2` sessions after it
    forms. That lag is real and not a defect: a "swing high" declared on the
    day it printed is just today's high, and would be relabelled tomorrow.

    Returned newest-first, so index 0 is the most recent confirmed pivot.
    """
    out = {"highs": [], "lows": []}
    if df is None or len(df) < bars:
        return out
    half = bars // 2
    if half < 1:
        return out
    # PIVOTS BEFORE THE MOST RECENT ACTION ARE ON THE OLD PRICE SCALE. Dropped
    # individually rather than dropping the whole set: a stock that split three
    # months ago still has real swing pivots since, and they are the ones a
    # reader cares about.
    from radar.bars import corporate_actions
    acts = corporate_actions(df)
    floor_date = max(acts) if acts else None
    highs = df["high"].astype(float).to_numpy()
    lows = df["low"].astype(float).to_numpy()
    dates = list(df["date"])

    hi_piv, lo_piv = [], []
    for i in range(half, len(df) - half):
        if floor_date is not None and dates[i] < floor_date:
            continue
        window_h = highs[i - half:i + half + 1]
        window_l = lows[i - half:i + half + 1]
        # STRICTLY the max of its window, with ties resolved to the EARLIER bar
        # (argmax returns the first). A plateau of equal highs would otherwise
        # register as several pivots at the same price.
        if window_h.argmax() == half:
            hi_piv.append((dates[i], round(float(highs[i]), 2)))
        if window_l.argmin() == half:
            lo_piv.append((dates[i], round(float(lows[i]), 2)))

    out["highs"] = [{"date": d, "price": p} for d, p in hi_piv[::-1][:want]]
    out["lows"] = [{"date": d, "price": p} for d, p in lo_piv[::-1][:want]]
    return out


def dist_pct(level: float | None, current: float | None) -> float | None:
    """Signed distance from current price to a level, as a %.

    SIGNED, and the sign means "where the level is relative to here": positive
    is above, negative is below. An unsigned distance would make a level 8%
    overhead and one 8% underneath render identically, which on a card about a
    crash is the difference that matters.
    """
    c = _f(current)
    l = _f(level)
    if not c or l is None:
        return None
    return round((l - c) / c * 100.0, 2)


def compute_all(df: pd.DataFrame, cfg, vwap: float | None = None) -> list:
    """Every level for one event, as rows ready for radar_levels.

    -> [{"level_type", "price", "dist_pct", "ref_date"}]

    DROPS what it cannot compute. A short history yields fewer rows, never a
    row with a None price or a substituted proxy, so the card renders the
    levels that exist and says nothing about the ones that do not.
    """
    if df is None or not len(df):
        return []
    cur = _f(df["close"].iloc[-1])
    ref = df["date"].iloc[-1]
    from radar.bars import DOWNLOAD_FLOOR, FLOOR_SLACK_DAYS

    simple = {
        "w52_high":        w52_high(df),
        "w52_low":         w52_low(df),
        "event_high":      event_high(df),
        "event_low":       event_low(df),
        "event_vwap":      vwap,
        "pre_event_close": pre_event_close(df),
        "ipo_price":       ipo_price(df, cfg.ipo_max_age_years,
                                     DOWNLOAD_FLOOR, FLOOR_SLACK_DAYS),
    }
    for period in cfg.dma_list:
        simple[f"dma_{period}"] = dma(df, period)

    rows = []
    for ltype, price in simple.items():
        if price is None:
            continue
        rows.append({"level_type": ltype, "price": price,
                     "dist_pct": dist_pct(price, cur), "ref_date": ref})

    piv = swing_pivots(df, cfg.swing_fractal_bars, want=3)
    for i, p in enumerate(piv["highs"], start=1):
        rows.append({"level_type": f"swing_high_{i}", "price": p["price"],
                     "dist_pct": dist_pct(p["price"], cur), "ref_date": p["date"]})
    for i, p in enumerate(piv["lows"], start=1):
        rows.append({"level_type": f"swing_low_{i}", "price": p["price"],
                     "dist_pct": dist_pct(p["price"], cur), "ref_date": p["date"]})

    # RSI IS A READING, NOT A PRICE, so it carries no dist_pct. Stored as a
    # level row because it is one more computed fact about the same session and
    # a separate table for one number would be worse.
    r = rsi(df, cfg.rsi_period)
    if r is not None:
        rows.append({"level_type": f"rsi_{cfg.rsi_period}", "price": r,
                     "dist_pct": None, "ref_date": ref})
    return rows
