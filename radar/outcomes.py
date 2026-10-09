"""
Forward outcomes. Every detected event, on the list or off it.

docs/TSL_FLASH.md §07. THIS IS THE MODULE THAT MAKES DISPLAY AND MEASUREMENT
SEPARATE THINGS, and the separation is the point: the Hot 10 is ten cards, the
base-rate library needs every event ever detected, and a displacement model
that stopped measuring would starve it while looking like it worked.

Ten survivors a day is a biased sample of exactly the wrong kind -- biased
toward the events that stayed interesting, which is the question the base rate
is supposed to answer.

MEASURED FROM THE EVENT-SESSION CLOSE, not from the pre-event close. The
pre-event close is a price no reader could have acted on once the event was
visible, and measuring from it would put the shock itself inside the "recovery"
number -- a stock that fell 36% and then did nothing would show -36% at 5 days
and read as still falling.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd

from radar import bars

log = logging.getLogger("RADAR-OUTCOMES")

HORIZONS = (5, 20, 60)


def measure(symbol: str, event_date: date, cfg,
            as_of: date | None = None) -> dict | None:
    """Forward returns and max drawdown from the event close. None if unknown.

    -> {"base_close", "ret_5d", "ret_20d", "ret_60d", "max_drawdown_pct",
        "sessions_elapsed", "measured_through", "window_complete"}

    PARTIAL IS NORMAL AND IS NOT A FAILURE. An event three sessions old has no
    5-day return yet; that key is simply absent rather than null-or-zero, so a
    consumer averaging ret_5d cannot silently include zeros for events that
    have not got there. The window keeps filling in on later runs.
    """
    df = bars.load_symbol(symbol)
    if df is None or not len(df):
        return None

    base_rows = df[df["date"] == event_date]
    if not len(base_rows):
        # The event session is not in the series. Nothing to measure from, and
        # inventing a base from the nearest row would silently shift the whole
        # window.
        log.warning(f"{symbol}: no bar for event_date {event_date}")
        return None
    try:
        base = float(base_rows.iloc[0]["close"])
    except (TypeError, ValueError):
        return None
    if not base or pd.isna(base):
        return None

    fwd = df[df["date"] > event_date]
    if as_of is not None:
        fwd = fwd[fwd["date"] <= as_of]
    fwd = fwd.reset_index(drop=True)

    out = {"base_close": round(base, 2),
           "sessions_elapsed": int(len(fwd)),
           "measured_through": (fwd["date"].iloc[-1].isoformat()
                                if len(fwd) else None),
           "window_complete": len(fwd) >= int(cfg.outcome_window_trading_days)}

    for h in HORIZONS:
        if len(fwd) >= h:
            try:
                px = float(fwd["close"].iloc[h - 1])
                out[f"ret_{h}d"] = round((px / base - 1.0) * 100.0, 2)
            except (TypeError, ValueError):
                pass

    # MAX DRAWDOWN OVER WHAT HAS ELAPSED, capped at the window. The worst CLOSE
    # relative to the event close -- closes, not intraday lows, because a
    # reader holding through it experiences marks, and an intraday wick that
    # recovered by the bell is not a drawdown they had to sit with.
    window = fwd.iloc[:int(cfg.outcome_window_trading_days)]
    if len(window):
        try:
            worst = float(window["close"].astype(float).min())
            out["max_drawdown_pct"] = round((worst / base - 1.0) * 100.0, 2)
            best = float(window["close"].astype(float).max())
            out["max_runup_pct"] = round((best / base - 1.0) * 100.0, 2)
        except (TypeError, ValueError):
            pass
    return out


def sweep(events: list, cfg, as_of: date | None = None) -> list:
    """Re-measure a batch. -> [{"id", "outcome", "window_complete"}]

    TAKES EVERY EVENT IT IS GIVEN AND FILTERS NOTHING BY STATUS. The caller
    passes live and displaced alike; this module does not know what a Hot 10 is
    and must not learn, because the moment it can tell the difference someone
    will optimise it to skip the ones nobody is looking at.
    """
    out = []
    for e in events:
        sym = e.get("symbol")
        ed = e.get("event_date")
        if isinstance(ed, str):
            ed = date.fromisoformat(ed)
        if not sym or not ed:
            continue
        try:
            m = measure(sym, ed, cfg, as_of=as_of)
        except Exception as ex:
            log.warning(f"{sym}: outcome measurement raised "
                        f"({type(ex).__name__}: {ex})")
            continue
        if m is None:
            continue
        out.append({"id": e.get("id"), "symbol": sym, "event_date": ed,
                    "outcome": m, "window_complete": m["window_complete"]})
    n_done = sum(1 for r in out if r["window_complete"])
    log.info(f"outcomes: measured {len(out)} event(s), "
             f"{n_done} window(s) complete")
    return out
