"""
ATLAS LONG-EQUITY SIZING — how much, and where the three tranches go.
=====================================================================

Decided 2026-11-10. Wired into the entry path for the LONG book on 2026-11-10
via size_pilot() (atlas_entry Gate 5). Nothing here places, modifies or cancels
an order; it only computes. It answers one question -- given a symbol, an
entry price and the book, what is the position and where are its tranches --
and refuses, with a reason, whenever an input it needs is missing or suspect.

THE CEILING is a share of the LONG BOOK, which is 60% of total capital -- not of
total capital. By market cap, computed at use time as the share count from the
company's newest shareholding-pattern filing times the entry price (no market
cap is ever stored; it would be stale by the next session):

    >= 1,00,000 cr   8%        2,000 - 10,000 cr    1.5%
    35,000 - 1,00,000  5%      500 - 2,000 cr       0.75%
    10,000 - 35,000    3%      < 500 cr             0.4%

Each band includes its lower edge.

THE LIQUIDITY CAP overrides whenever tighter: the position is at most 2% of the
20-session MEDIAN daily traded value, NSE's own figure from the bhavcopy. The
median, so one block deal cannot lift the cap.

TRANCHES are 40 / 30 / 30 of the position (the tighter of the two bounds),
placed PER TRANCHE:
    T1 = entry
    T2 = nearest support at least 1.0 ATR below entry;  if none, entry - 1.5 ATR
    T3 = nearest support at least 1.0 ATR below T2;     if none, T2 - 1.5 ATR
Supports are engine.active_zones.supports_below(): demand zones under the
current zone rules (60-session age limit, kept by decision). The 1.0 ATR
minimum is what stops two tranches landing at one price -- JSWENERGY's two
nearest supports on 2026-11-09 were 513.60 and 512.85.

ATR IS THE `atr` COLUMN 02b WRITES: Wilder, 14 sessions, ta's
AverageTrueRange. Read, not recomputed -- a second ATR definition in one
codebase is the relative-volume defect over again.

THE BOOK: at most 70% of the long book deployed by new names; the other 30% is
a permanent reserve that funds averaging into EXISTING positions (T2, T3) and
never a new name, and it may not fall below 10% of the long book.

REFUSALS, all of which mean "do not size", never "size with a stand-in":
    no share count, or one from a quarter older than the newest whose filing
    deadline (21 days, SEBI LODR Reg 31) plus our weekly fetch (7 days) has
    passed; a corporate action since that quarter (the price no longer
    matches the count); a discontinuity in the trailing 252 sessions -- the
    same history_discontinuity flag 03b excludes on, which also covers the
    ATR and the zones; no ATR; fewer than 20 sessions of traded value; T1
    quantity of zero.
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SMC_DIR = ROOT / "data/processed/smc"
TIER1_STORE = ROOT / "data/processed/tier1_sources.json"

LONG_BOOK_FRACTION = 0.60
# (lower edge in Rs crore, ceiling as a fraction of the long book), highest first
BANDS = ((100_000, 0.08), (35_000, 0.05), (10_000, 0.03),
         (2_000, 0.015), (500, 0.0075), (0, 0.004))
LIQ_FRACTION = 0.02
LIQ_SESSIONS = 20
TRANCHE_SPLIT = (0.40, 0.30, 0.30)
LADDER_MIN_ATR = 1.0          # a support counts only this far below the tranche above
LADDER_STEP_ATR = 1.5         # the volatility step when no support qualifies
MAX_DEPLOYED = 0.70           # new names stop here
RESERVE_FLOOR = 0.10          # averaging stops here
SHP_DEADLINE_DAYS = 21        # SEBI LODR Reg 31(1)(b)
FETCH_LAG_DAYS = 7            # tier1_fetch --refetch runs weekly (Sat)
DISCONTINUITY_SESSIONS = 252  # 02b's history_discontinuity window
CRORE = 1e7


# ── pure rules ────────────────────────────────────────────────────

def band(mcap_cr: float) -> tuple:
    """-> (lower edge in crore, ceiling fraction of the long book)."""
    for edge, frac in BANDS:
        if mcap_cr >= edge:
            return edge, frac
    return BANDS[-1]


def ladder(entry: float, atr: float, supports: list) -> list:
    """[(price, source)] for T1, T2, T3. `supports` are zone tops (any order)."""
    def step(above: float):
        ok = [s for s in supports if s <= above - LADDER_MIN_ATR * atr]
        if ok:
            return max(ok), "support"
        return above - LADDER_STEP_ATR * atr, "atr"
    t2 = step(entry)
    t3 = step(t2[0])
    return [(entry, "entry"), t2, t3]


def expected_quarter(today: date) -> date:
    """The newest quarter end whose filing should be in the store by `today`."""
    lag = timedelta(days=SHP_DEADLINE_DAYS + FETCH_LAG_DAYS)
    qes = [date(y, m, d) for y in (today.year - 1, today.year)
           for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(q for q in qes if q + lag <= today)


def headroom(total_capital: float, deployed: float, kind: str) -> float:
    """Rupees still allowed. kind 'new' (a new name's T1) or 'average' (T2/T3)."""
    book = total_capital * LONG_BOOK_FRACTION
    limit = MAX_DEPLOYED if kind == "new" else 1.0 - RESERVE_FLOOR
    return max(0.0, book * limit - deployed)


def size(symbol: str, entry: float, shares: float | None, atr: float | None,
         supports: list, traded_values: list, total_capital: float | None,
         book_capital: float | None = None) -> dict:
    """The rule on its inputs. Refuses rather than substituting.

    book_capital, when given, IS the long book and the ceilings apply to it
    directly -- the pilot's ring-fenced LONG_PILOT_CAPITAL. Otherwise the long
    book is LONG_BOOK_FRACTION of total_capital.
    """
    out = {"symbol": symbol, "entry": entry, "refused": None}
    if not shares or shares <= 0:
        out["refused"] = "no share count"
        return out
    if atr is None or not atr > 0 or math.isnan(atr):
        out["refused"] = "no ATR"
        return out
    tv = [v for v in traded_values if v is not None]
    if len(traded_values) < LIQ_SESSIONS or len(tv) < LIQ_SESSIONS:
        out["refused"] = (f"traded value known for {len(tv)} of the last "
                          f"{LIQ_SESSIONS} sessions")
        return out
    mcap_cr = shares * entry / CRORE
    edge, frac = band(mcap_cr)
    book = (float(book_capital) if book_capital is not None
            else float(total_capital) * LONG_BOOK_FRACTION)
    ceiling = book * frac
    median_tv = statistics.median(tv[-LIQ_SESSIONS:])
    liq_cap = LIQ_FRACTION * median_tv
    position = min(ceiling, liq_cap)
    tranches = []
    for (price, src), w in zip(ladder(entry, atr, supports), TRANCHE_SPLIT):
        amt = position * w
        tranches.append({"price": round(price, 2), "source": src, "amount": amt,
                         "qty": int(amt // price) if price > 0 else 0,
                         "atr_below_entry": round((entry - price) / atr, 2),
                         "pct_below_entry": round((price / entry - 1) * 100, 2)})
    out.update({"mcap_cr": mcap_cr, "band_edge_cr": edge, "ceiling_pct_of_book": frac,
                "long_book": book, "ceiling": ceiling, "median_traded_value": median_tv,
                "liquidity_cap": liq_cap,
                "bound": "liquidity" if liq_cap < ceiling else "ceiling",
                "position": position, "atr": atr, "atr_pct": atr / entry * 100,
                "tranches": tranches})
    # T1 ROUNDS UP TO ONE SHARE IF ONE SHARE FITS INSIDE THE POSITION BOUND
    # (operator, 2026-11-10). The bound is the tighter of ceiling and liquidity
    # cap -- "fits inside the ceiling" read against the bound that actually
    # applies. If one share does not fit, the symbol is refused and says why.
    if tranches[0]["qty"] < 1:
        if 0 < entry <= position:
            tranches[0]["qty"] = 1
            tranches[0]["rounded_up"] = True
        else:
            out["refused"] = (f"one share at Rs {entry:,.2f} exceeds the position "
                              f"bound Rs {position:,.0f} ({out['bound']}); T1 of "
                              f"Rs {tranches[0]['amount']:,.0f} buys no share")
            return out
    # THREE REAL TRANCHES OR NO POSITION (operator, 2026-11-10, OPEN D-073).
    # T2 and T3 are NOT rounded up and NOT skipped: the ladder is the strategy,
    # and a position that can never average is a different strategy whose
    # results would pollute the evidence. A stock that cannot carry three real
    # tranches at this capital does not belong in the book.
    for name, t in (("T2", tranches[1]), ("T3", tranches[2])):
        if t["qty"] < 1:
            out["refused"] = (f"{name} of Rs {t['amount']:,.0f} buys no share at "
                              f"Rs {t['price']:,.2f}: the ladder cannot be three real "
                              f"tranches at a Rs {book:,.0f} book")
            return out
    return out


def size_pilot(symbol: str, entry: float | None = None, today: date | None = None,
               store: dict | None = None, frame=None) -> dict:
    """The long pilot: the ceilings apply directly to LONG_PILOT_CAPITAL."""
    from atlas.config import LONG_PILOT_CAPITAL
    return size_symbol(symbol, None, entry=entry, today=today, store=store,
                       frame=frame, book_capital=LONG_PILOT_CAPITAL)


# ── inputs, read from where they already live ─────────────────────

def _qe(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except Exception:
        return None


def share_count(symbol: str, today: date, store: dict | None = None) -> tuple:
    """-> (shares or None, record or None, refusal or None)."""
    if store is None:
        try:
            store = json.loads(TIER1_STORE.read_text())
        except Exception as e:
            return None, None, f"share store unreadable: {e}"
    rec = ((store.get("symbols") or {}).get(symbol) or {}).get("shares")
    if not rec or rec.get("total") is None:
        why = "; ".join((rec or {}).get("notes") or []) or "never fetched"
        return None, rec, f"no share count ({why})"
    qe = _qe(rec.get("quarter_end") or "")
    if qe is None:
        return None, rec, f"share count has no readable quarter ({rec.get('quarter_end')!r})"
    want = expected_quarter(today)
    if qe < want:
        return None, rec, f"share count is from {qe}, the {want} filing is due and not on file"
    return float(rec["total"]), rec, None


def size_symbol(symbol: str, total_capital: float | None, entry: float | None = None,
                today: date | None = None, store: dict | None = None,
                frame=None, book_capital: float | None = None) -> dict:
    """Read every input for `symbol` as of its last stored session, then size."""
    import pandas as pd
    from engine.active_zones import supports_below
    from engine.price_basis import discontinuities
    from radar.bars import session_traded_value

    df = frame if frame is not None else pd.read_parquet(SMC_DIR / f"{symbol}.parquet")
    df = df.sort_values("date").reset_index(drop=True)
    last = df.iloc[-1]
    asof = pd.Timestamp(last["date"]).date()
    today = today or asof
    entry = float(entry if entry is not None else last["close"])
    base = {"symbol": symbol, "entry": entry, "asof": str(asof)}

    shares, rec, why = share_count(symbol, today, store)
    if rec:
        base.update({"shares_quarter": rec.get("quarter_end"), "shares_filed": rec.get("filed")})
    if why:
        return {**base, "refused": why}
    qe = _qe(rec["quarter_end"])
    acts = [pd.Timestamp(d).date() for d in discontinuities(df)]
    after = [d for d in acts if d > qe]
    if after:
        return {**base, "refused": f"price discontinuity on {after[0]}, after the "
                                   f"{qe} share count: price and count are on different bases"}
    if bool(last.get("history_discontinuity", False)):
        return {**base, "refused": f"discontinuity within {DISCONTINUITY_SESSIONS} sessions "
                                   f"(history_discontinuity, as 03b excludes on): ATR and zones unusable"}
    atr = last.get("atr")
    atr = float(atr) if atr is not None and pd.notna(atr) else None
    sess = [pd.Timestamp(d).date() for d in df["date"].iloc[-LIQ_SESSIONS:]]
    tvs = [session_traded_value(symbol, d) for d in sess]
    sup = [z["top"] for z in supports_below(df)]
    r = size(symbol, entry, shares, atr, sup, tvs, total_capital, book_capital=book_capital)
    r.update({k: v for k, v in base.items() if k not in r})
    r["supports_below"] = sup
    return r
