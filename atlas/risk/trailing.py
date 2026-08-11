"""
ATLAS Risk — Trailing Stop Recommendations
==========================================
RECOMMENDATION ONLY. ATLAS places no stop-loss orders in this phase
(ALLOW_AUTOMATED_STOP_LOSS = False, ENABLE_EXIT_MANAGEMENT = False) and nothing
here submits anything to the broker. This computes a number for the daily
report; the operator decides whether to move a stop.

METHOD
------
For a LONG, the trail is the most recent SWING LOW that sits above the current
stop -- structure that price has already defended, not a percentage of the
entry. For a SHORT, the most recent SWING HIGH below the current stop.

Swing points come from data/processed/smc/<SYMBOL>.parquet, written by
02b_smc_signals.detect_swing_points(): a swing low is the lowest low within
SWING_LOOKBACK candles either side. Only swings at or after the entry date are
considered -- a swing from before the position existed is not a trail, it is
just an old price.

The buffer matches engine/zone_entry.STOP_BUFFER, so a trail is computed the
same way the original structural stop was: pushed just outside the level rather
than sitting exactly on it, where a retest would take it out.

NEVER LOOSEN
------------
A trail may only tighten. For a LONG the recommendation must be strictly ABOVE
the current stop; for a SHORT, strictly BELOW. This is enforced twice -- once
when filtering candidate swings and again on the buffered result, because the
buffer moves the number in the loosening direction and could otherwise push a
marginal candidate back below the current stop. If nothing qualifies the answer
is "no trail", never a weaker stop.
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

log = logging.getLogger("ATLAS-TRAIL")

SMC_DIR = Path(__file__).parent.parent.parent / "data" / "processed" / "smc"

# Same buffer the entry stop uses (engine/zone_entry.STOP_BUFFER), so the trail
# is derived exactly as the original stop was.
STOP_BUFFER = 0.002


def _short_date(v) -> str:
    """'2026-08-08' -> '08 Aug', for a one-line report row."""
    try:
        import pandas as pd
        return pd.Timestamp(v).strftime("%d %b")
    except Exception:
        return str(v)[:10]


def _load_swings(symbol: str):
    """(dates, lows, highs, swing_low_flags, swing_high_flags) or None."""
    path = SMC_DIR / f"{symbol}.parquet"
    if not path.exists():
        return None
    try:
        import pandas as pd
        cols = ["date", "low", "high", "swing_low", "swing_high"]
        df = pd.read_parquet(path, columns=cols).sort_values("date")
        return df
    except Exception as e:
        log.warning(f"{symbol}: could not read swing data: {e}")
        return None


def recommend_trail(symbol: str, direction: str, current_stop: float,
                    entry_date=None) -> dict:
    """Suggested trailing stop, or a reason there isn't one.

    Returns {"ok": bool, "price": float|None, "swing_date": str,
             "tighten_pct": float, "reason": str}
    """
    out = {"ok": False, "price": None, "swing_date": "", "tighten_pct": 0.0,
           "reason": ""}

    d = (direction or "LONG").upper()
    try:
        stop = float(current_stop)
    except (TypeError, ValueError):
        out["reason"] = "no current stop recorded"
        return out
    if stop <= 0:
        out["reason"] = "no current stop recorded"
        return out

    df = _load_swings(symbol)
    if df is None or df.empty:
        out["reason"] = "no swing data"
        return out

    import pandas as pd
    if entry_date:
        try:
            df = df[pd.to_datetime(df["date"]) >= pd.Timestamp(entry_date)]
        except Exception:
            pass                       # unparseable entry date -> consider all
    if df.empty:
        out["reason"] = "no swings since entry"
        return out

    if d == "SHORT":
        cand = df[df["swing_high"].fillna(False).astype(bool)]
        # must TIGHTEN: a short's stop moves DOWN
        cand = cand[cand["high"] < stop]
        price_col, better = "high", lambda p: p * (1 + STOP_BUFFER)
    else:
        cand = df[df["swing_low"].fillna(False).astype(bool)]
        # must TIGHTEN: a long's stop moves UP
        cand = cand[cand["low"] > stop]
        price_col, better = "low", lambda p: p * (1 - STOP_BUFFER)

    if cand.empty:
        out["reason"] = ("no swing high below the current stop" if d == "SHORT"
                         else "no swing low above the current stop")
        return out

    row = cand.iloc[-1]                       # most recent qualifying swing
    raw = float(row[price_col])
    trail = round(better(raw), 2)

    # Re-check AFTER the buffer. It moves the number toward the current stop and
    # can push a marginal candidate to the wrong side of it.
    loosens = (trail >= stop) if d == "SHORT" else (trail <= stop)
    if loosens:
        out["reason"] = (f"nearest qualifying swing {raw:.2f} is too close to the "
                         f"current stop {stop:.2f} once buffered")
        return out

    out.update({
        "ok": True,
        "price": trail,
        "swing_date": str(row["date"])[:10],
        "tighten_pct": round(abs(trail - stop) / stop * 100, 2),
        "reason": f"swing {'high' if d == 'SHORT' else 'low'} {raw:.2f} · {_short_date(row['date'])}",
    })
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    if len(sys.argv) < 4:
        print("usage: python3 atlas/risk/trailing.py SYMBOL LONG|SHORT CURRENT_STOP [ENTRY_DATE]")
        sys.exit(1)
    print(json.dumps(recommend_trail(sys.argv[1], sys.argv[2], float(sys.argv[3]),
                                     sys.argv[4] if len(sys.argv) > 4 else None),
                     indent=2))
