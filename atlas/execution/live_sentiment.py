"""
THE ENTRY-SIDE SENTIMENT GATE, READ LIVE
========================================

WHAT THIS REPLACES. Gate 1 read data/processed/market.parquet: yesterday's
closing advance/decline counts and a 20-day moving average of the Nifty. A
daily aggregate deciding an intraday action -- and the file has also been
running a session behind (on 2026-10-13 it held 2026-10-09 while the session
traded), so the gate had never once evaluated on current data.

THE RULE, operator-approved 2026-10-14. At the moment a candidate is
evaluated, compare the Nifty's current price against three references:

    previous session's close
    today's open
    today's session VWAP

    bullish   above all three    -> longs permitted
    bearish   below all three    -> shorts permitted (when shorts are on)
    mixed     anything else      -> cash, no entry

Requiring all three to agree is deliberate: a single number crossing zero
would flip the gate all day.

────────────────────────────────────────────────────────────────────
VWAP: WHAT IS ACTUALLY AVAILABLE, AND WHAT THIS ONE MEASURES
────────────────────────────────────────────────────────────────────
An index has no traded VWAP, and this feed is explicit about it. Probed
2026-10-14 against the live Upstox v2 API:

  NSE_INDEX|Nifty 50  full market quote
      volume:        None
      average_price: None          <- no VWAP field at all
  NSE_INDEX|Nifty 50  1-minute candles, 2026-10-12
      375 candles, volume == 0 on every one, sum 0

So there is nothing to volume-weight. A "VWAP" computed from the index series
would be weighting by zero, and weighting index prices by the summed volume of
the 50 constituents would be a synthetic I invented -- the operator's brief
says do not approximate it with something else and call it VWAP.

  NSE_FO|48704  NIFTY FUT 27 OCT 26  full market quote
      volume:        3,932,695
      average_price: 22,680.66     <- a real, exchange-computed session VWAP
  NSE_FO|48704  1-minute candles, 2026-10-12
      385 candles, volume non-zero on every one, sum 2,643,420

THE DECISION: all three comparisons are computed on the NEAR-MONTH NIFTY
FUTURES CONTRACT, and the VWAP leg is the exchange's own `average_price` for
that contract.

WHAT IT IS REALLY MEASURING. The volume-weighted average price at which NIFTY
near-month futures actually traded this session -- not the index. The future is
not the index: on 2026-10-13 the future last traded at 22,860.0 against index
spot 22,721.95, a basis of +138.05 points (+0.61%) reflecting carry and
dividend expectations, decaying to zero at expiry.

WHY NOT INDEX SPOT AGAINST FUTURES VWAP. Because that basis would sit inside
one leg of a three-way comparison and nowhere else. Spot would be below a
futures VWAP almost permanently in a carry market, so the "above all three"
condition would almost never fire and the gate would read bearish-to-mixed
structurally rather than because of anything happening in the market. Keeping
all three legs on one instrument makes the basis cancel: it is in the price,
the open, the previous close and the VWAP alike.

WHAT THIS COSTS, STATED PLAINLY. The gate now answers "is the NIFTY near-month
future above its previous close, its open and its session VWAP" rather than
"is the Nifty 50 index above...". The two track each other closely but they are
not the same series, and a basis move with no index move can shift a leg. The
index spot and the basis are read and reported alongside every verdict so the
difference is visible rather than buried.

CONTRACT SELECTION. The nearest expiry strictly after today. On expiry day the
front month's VWAP is distorted by settlement and its volume collapses into the
next month, so the roll happens the day before rather than on the day.

PREVIOUS CLOSE IS DERIVED, NOT READ. `ohlc.close` in this feed is TODAY's close
once the session ends and the PREVIOUS close while it runs -- index_state.py
carries a note about exactly that trap. So previous close is computed as
`last_price - net_change`, which is anchored on the previous close. Verified
2026-10-14: index last_price 22,721.95 less net_change 358.60 gives 22,363.35,
which is the 2026-10-12 final 1-minute candle close to the paisa.

NO SILENT FALLBACK. There is no path from here back to market.parquet. A failed
or incomplete read returns ok=False with reason_code LIVE_SENTIMENT_UNAVAILABLE
and the entry is refused, loudly. The defect being fixed is a gate that failed
quietly; one that fails quietly in a new way is not an improvement.

THE NIGHTLY REGIME IS UNTOUCHED. market.parquet still feeds engine/03b_score's
regime filter, which remains the regime authority. This is the entry-side gate
only.
"""

import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("ATLAS-LIVE-SENTIMENT")
IST = timezone(timedelta(hours=5, minutes=30))

# THE REASON CODE. Distinct from SENTIMENT (a readable market that permits no
# side) because the two call for different actions: mixed means wait, this means
# find out why the feed is not answering.
REASON_UNAVAILABLE = "LIVE_SENTIMENT_UNAVAILABLE"

NIFTY_INDEX_KEY = "NSE_INDEX|Nifty 50"
_FUT_CACHE = {"day": None, "key": None, "symbol": None, "expiry": None}


def _now_ist():
    return datetime.now(IST)


def near_month_future(force: bool = False) -> dict:
    """The NIFTY futures contract to read. Cached per day.

    Resolved from the Upstox instrument master rather than hardcoded: a
    hardcoded key is wrong on the first roll and silently reads a dead contract
    with no volume, which would fail the VWAP leg every cycle.
    """
    today = _now_ist().date()
    if not force and _FUT_CACHE["day"] == today and _FUT_CACHE["key"]:
        return dict(_FUT_CACHE)
    import gzip, json, requests
    r = requests.get(
        "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
        timeout=60)
    r.raise_for_status()
    rows = json.loads(gzip.decompress(r.content).decode())
    cands = []
    for x in rows:
        if x.get("segment") != "NSE_FO":
            continue
        if str(x.get("instrument_type", "")).upper() not in ("FUT", "FUTIDX"):
            continue
        if str(x.get("asset_symbol", "")).upper() != "NIFTY":
            continue
        exp = x.get("expiry")
        try:
            expd = datetime.fromtimestamp(exp / 1000, IST).date()
        except Exception:
            continue
        # STRICTLY AFTER TODAY: roll the day before expiry, not on it.
        if expd > today:
            cands.append((expd, x))
    if not cands:
        raise RuntimeError("no NIFTY future with an expiry after today")
    cands.sort(key=lambda t: t[0])
    expd, x = cands[0]
    _FUT_CACHE.update({"day": today, "key": x.get("instrument_key"),
                       "symbol": x.get("trading_symbol"),
                       "expiry": expd.isoformat()})
    return dict(_FUT_CACHE)


def _quote(keys: list) -> dict:
    from engine.upstox_ws import get_market_quotes
    return get_market_quotes(keys) or {}


def read(max_age_seconds: float = None) -> dict:
    """The live sentiment read. NEVER RAISES.

    -> dict with ok / verdict / reason_code / the three legs / age / detail.
    """
    from atlas.config import LIVE_SENTIMENT_MAX_AGE_SECONDS
    max_age = (LIVE_SENTIMENT_MAX_AGE_SECONDS if max_age_seconds is None
               else max_age_seconds)
    out = {
        "ok": False, "verdict": None, "reason_code": REASON_UNAVAILABLE,
        "detail": "", "instrument": None, "trading_symbol": None,
        "expiry": None, "last": None, "prev_close": None, "open": None,
        "vwap": None, "above_prev_close": None, "above_open": None,
        "above_vwap": None, "as_of": None, "age_seconds": None,
        "index_spot": None, "basis": None, "source": "upstox live quote",
    }
    try:
        fut = near_month_future()
        out["instrument"] = fut["key"]
        out["trading_symbol"] = fut["symbol"]
        out["expiry"] = fut["expiry"]
    except Exception as e:
        out["detail"] = f"could not resolve the NIFTY futures contract: {e}"
        return out

    try:
        q = _quote([fut["key"], NIFTY_INDEX_KEY])
    except Exception as e:
        out["detail"] = f"quote request failed: {type(e).__name__}: {e}"
        return out

    fk = fut["key"].replace("|", ":")
    row = q.get(fk) or {}
    if not row:
        out["detail"] = (f"the feed returned no row for {fut['symbol']} "
                         f"({fut['key']})")
        return out

    idx = q.get(NIFTY_INDEX_KEY.replace("|", ":")) or {}
    out["index_spot"] = idx.get("last_price")

    last = row.get("last_price")
    net  = row.get("net_change")
    opn  = (row.get("ohlc") or {}).get("open")
    vwap = row.get("average_price")
    ts   = row.get("timestamp")

    # EVERY LEG MUST BE PRESENT AND NON-ZERO. A zero here is not a price, it is
    # a field the feed did not fill, and treating it as one would put the gate
    # on the wrong side of every comparison.
    legs = {"last_price": last, "net_change": net, "ohlc.open": opn,
            "average_price": vwap}
    bad = [n for n, v in legs.items()
           if v is None or (n != "net_change" and not float(v or 0) > 0)]
    if bad:
        out["detail"] = (f"{fut['symbol']} quote is incomplete -- "
                         f"{', '.join(bad)} missing or zero")
        return out

    prev_close = float(last) - float(net)
    if not prev_close > 0:
        out["detail"] = (f"derived previous close {prev_close:.2f} is not a "
                         f"price (last {last}, net_change {net})")
        return out

    out.update({"last": float(last), "prev_close": prev_close,
                "open": float(opn), "vwap": float(vwap)})
    if out["index_spot"]:
        out["basis"] = round(float(last) - float(out["index_spot"]), 2)

    # AGE. The quote carries its own timestamp; a feed that has stopped
    # updating looks identical to a quiet market from the price alone.
    if ts:
        try:
            tq = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            out["as_of"] = tq.astimezone(IST).isoformat(timespec="seconds")
            out["age_seconds"] = round((_now_ist() - tq.astimezone(IST))
                                       .total_seconds(), 1)
        except Exception:
            out["as_of"] = str(ts)
    if out["age_seconds"] is None:
        out["detail"] = f"{fut['symbol']} quote carries no usable timestamp"
        return out
    if out["age_seconds"] > max_age:
        out["detail"] = (f"{fut['symbol']} quote is {out['age_seconds']:.0f}s "
                         f"old, past the {max_age:.0f}s limit -- the feed is "
                         f"not current")
        return out

    out.update({"above_prev_close": out["last"] > out["prev_close"],
                "above_open":       out["last"] > out["open"],
                "above_vwap":       out["last"] > out["vwap"]})
    # ONE IMPLEMENTATION OF THE RULE. This had its own copy of the comparison,
    # which is how the live path and verdict_from could disagree at the edges --
    # and did, at exact equality.
    out["verdict"] = verdict_from(out["last"], out["prev_close"],
                                  out["open"], out["vwap"])
    out["ok"] = True
    out["reason_code"] = None
    out["detail"] = legs_line(out)
    return out


def legs_line(s: dict) -> str:
    """One line naming every input and which comparisons passed."""
    def mark(v):
        return "above" if v else "below"
    return (f"{s.get('trading_symbol')} {s.get('last'):.2f} "
            f"{mark(s.get('above_prev_close'))} prev close "
            f"{s.get('prev_close'):.2f}, "
            f"{mark(s.get('above_open'))} open {s.get('open'):.2f}, "
            f"{mark(s.get('above_vwap'))} session VWAP {s.get('vwap'):.2f} "
            f"[quote {s.get('age_seconds')}s old"
            + (f", index {s['index_spot']:.2f}, basis {s['basis']:+.2f}"
               if s.get("index_spot") and s.get("basis") is not None else "")
            + "]")


def verdict_from(last, prev_close, open_, vwap) -> str:
    """The rule itself, on four numbers. Shared with the replay harness so the
    replay cannot drift from the live path.

    ABOVE AND BELOW ARE BOTH STRICT, AND "NOT ABOVE" IS NOT "BELOW". This first
    read `not a and not b and not c` for bearish, which makes a price resting
    EXACTLY on a reference count as below it -- so a last price equal to all
    three references returned bearish, a direction, when the honest answer is
    that it is on the line and the rule names no side. The approved rule is
    "above all three" or "below all three"; equality is neither, and at a
    reference is precisely where a gate should not be committing.
    """
    above = (last > prev_close, last > open_, last > vwap)
    below = (last < prev_close, last < open_, last < vwap)
    if all(above):
        return "bullish"
    if all(below):
        return "bearish"
    return "mixed"
