"""
WHEN THE PUBLISHED STOP DOES NOT FIT THE BAND, DERIVE ONE FROM STRUCTURE
========================================================================

WHY. On 2026-10-15, 103 long candidates carried a valid zone and NOT ONE had a
stop inside [MIN_STOP_PCT, MAX_STOP_PCT] = [0.5%, 5.0%] -- 86 too wide, 17 too
tight. publication_kind was blamed for locking them out; the stop band was
doing it. HEXT's published stop was 16.56% from entry and TATAELXSI's 17.72%,
both roughly three times the cap.

A published stop that does not fit is not a reason to skip the trade. The zone
is real -- that is what made the row a candidate rather than a reject -- so
ATLAS derives its own stop from the structure it already computed, and only
clamps when no structural level fits.

────────────────────────────────────────────────────────────────────
STRUCTURE FIRST, IN THIS ORDER OF PREFERENCE
────────────────────────────────────────────────────────────────────
Every level below is already computed per symbol by engine/02b_smc_signals.py
and sits in data/processed/smc/<SYMBOL>.parquet. NOTHING NEW IS INVENTED.

  LONG, looking below entry:
    1  active_demand_ob_low    the demand order block's low. First because an
                               OB is the level the move originated from: price
                               below it says the block failed, which is the
                               cleanest structural invalidation available.
    2  active_bull_fvg_low     the bullish fair value gap's low. Second
                               because a filled-through FVG is weaker evidence
                               than a broken OB but is still a level the
                               engine identified rather than a round number.
    3  last_swing_low          the most recent 5-bar fractal swing low. Third
                               because it is the loosest of the three -- but
                               it is the same level the overnight trail
                               already moves stops to (exits.swing_low_since),
                               so using it here is consistent with how a stop
                               is managed later.

  SHORT, looking above entry (kept for symmetry; shorts are disabled):
    1  active_supply_ob_high
    2  active_bear_fvg_high
    3  last_swing_high

THE NEAREST QUALIFYING LEVEL WINS. Among levels that sit on the correct side of
entry AND produce a stop inside the band, the one closest to entry is chosen:
the tightest structural stop that is still outside noise. Choosing the furthest
would maximise risk for no extra information.

CLAMPING IS THE LAST RESORT, and it is named as such. If no structural level
fits, the stop is clamped to the band edge the published stop violated -- a
too-wide stop clamps to MAX_STOP_PCT, a too-tight one to MIN_STOP_PCT. A clamp
is an arithmetic level with no structure behind it, so it is recorded as
"clamp:max" or "clamp:min" rather than dressed up as a derived level.

THE TARGET IS RECOMPUTED, NOT REUSED. The published target was set against the
published stop; once the stop moves, the published target is not the same R
multiple and means nothing. exits.target_price() recomputes it at
TARGET_R_MULTIPLE from the fill and the stop actually used.

NOTHING HERE PLACES AN ORDER or reads a broker. It is arithmetic over levels
the nightly chain already wrote.
"""

import logging
from pathlib import Path

log = logging.getLogger("ATLAS-DERIVED-STOP")

# order of preference, per side. (column, label)
LONG_LEVELS = (("active_demand_ob_low", "demand_ob_low"),
               ("active_bull_fvg_low", "bull_fvg_low"),
               ("last_swing_low", "last_swing_low"))
SHORT_LEVELS = (("active_supply_ob_high", "supply_ob_high"),
                ("active_bear_fvg_high", "bear_fvg_high"),
                ("last_swing_high", "last_swing_high"))

SMC_DIR = Path("data/processed/smc")


def smc_levels(symbol: str, row: dict = None) -> dict:
    """The structural levels for one symbol. `row` short-circuits the read.

    Returns {} rather than raising: a missing SMC file means no structure, and
    the caller clamps.
    """
    if row is not None:
        return dict(row)
    p = SMC_DIR / f"{symbol}.parquet"
    if not p.exists():
        return {}
    try:
        import pandas as pd
        cols = [c for c, _ in LONG_LEVELS + SHORT_LEVELS]
        d = pd.read_parquet(p)
        have = [c for c in cols if c in d.columns]
        if not have or not len(d):
            return {}
        last = d.sort_values("date").iloc[-1] if "date" in d.columns else d.iloc[-1]
        return {c: last.get(c) for c in have}
    except Exception as e:
        log.warning(f"{symbol}: could not read SMC levels ({e})")
        return {}


def derive(symbol: str, entry: float, published_stop: float, direction: str,
           min_pct: float = None, max_pct: float = None,
           row: dict = None) -> dict:
    """-> {stop, source, stop_pct, published_stop, published_pct, why, clamped}

    `source` is one of the labels above, "clamp:max", "clamp:min", or
    "published" when the published stop already fits.
    """
    from atlas.config import MIN_STOP_PCT, MAX_STOP_PCT
    lo = float(min_pct if min_pct is not None else MIN_STOP_PCT)
    hi = float(max_pct if max_pct is not None else MAX_STOP_PCT)
    d = str(direction).upper()
    e = float(entry)
    out = {"stop": None, "source": None, "stop_pct": None,
           "published_stop": published_stop, "published_pct": None,
           "why": "", "clamped": False, "candidates": []}
    if not e > 0:
        out["why"] = "invalid entry price"
        return out

    def pct(level):
        return abs(e - float(level)) / e * 100.0

    def correct_side(level):
        return (float(level) < e) if d == "LONG" else (float(level) > e)

    if published_stop and float(published_stop) > 0:
        out["published_pct"] = pct(published_stop)
        if correct_side(published_stop) and lo <= out["published_pct"] <= hi:
            out.update({"stop": float(published_stop), "source": "published",
                        "stop_pct": out["published_pct"],
                        "why": (f"the published stop is "
                                f"{out['published_pct']:.2f}% from entry, "
                                f"inside [{lo}, {hi}] — used as published")})
            return out

    levels = LONG_LEVELS if d == "LONG" else SHORT_LEVELS
    vals = smc_levels(symbol, row)
    ok = []
    for col, label in levels:
        v = vals.get(col)
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v <= 0 or v != v:                      # NaN-safe
            continue
        p = pct(v)
        rec = {"label": label, "level": v, "pct": round(p, 2),
               "correct_side": correct_side(v), "in_band": lo <= p <= hi}
        out["candidates"].append(rec)
        if rec["correct_side"] and rec["in_band"]:
            ok.append((p, label, v))

    if ok:
        # THE NEAREST QUALIFYING LEVEL: tightest structural stop still outside
        # noise. Ties break on the preference order, which `levels` encodes.
        ok.sort(key=lambda t: (t[0], [l for _, l in levels].index(t[1])))
        p, label, v = ok[0]
        out.update({"stop": v, "source": label, "stop_pct": p,
                    "why": (f"published stop "
                            f"{(out['published_pct'] or 0):.2f}% is outside "
                            f"[{lo}, {hi}]; derived from {label} at {v:.2f} "
                            f"= {p:.2f}%")})
        return out

    # ── CLAMP, named as a clamp ───────────────────────────────────
    too_tight = (out["published_pct"] is not None
                 and out["published_pct"] < lo)
    edge = lo if too_tight else hi
    # JUST INSIDE THE EDGE, NOT ON IT. validate_stop() compares strictly
    # (< MIN, > MAX), and e * (1 - 5.0/100) recomputes as 5.000000000000004%
    # for most entry prices -- so clamping exactly to the edge produced a stop
    # the very next gate rejected. Found by replaying 2026-10-15: all 25
    # clamp:max rows failed revalidation. The inset is 0.1% of the edge, which
    # on the 5.0 cap is 0.005 percentage points.
    edge_in = edge * (1.0 + 1e-3) if too_tight else edge * (1.0 - 1e-3)
    v = (e * (1 - edge_in / 100.0) if d == "LONG"
         else e * (1 + edge_in / 100.0))
    out.update({"stop": v, "source": f"clamp:{'min' if too_tight else 'max'}",
                "stop_pct": edge_in, "clamped": True,
                "why": (f"no structural level sits on the correct side of "
                        f"entry inside [{lo}, {hi}] "
                        f"({len(out['candidates'])} level(s) considered); "
                        f"clamped to the {'MIN' if too_tight else 'MAX'} band "
                        f"edge at {edge}% = {v:.2f}. This is arithmetic, not "
                        f"structure.")})
    log.warning(f"{symbol}: {out['why']}")
    return out
