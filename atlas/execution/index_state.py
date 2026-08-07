"""
ATLAS Market Direction Gate — Nifty / Bank Nifty Opening Range
================================================================
Determines the day's tradeable direction from the INDEX, not just regime.
Rules (user-defined):
  LONG_CONFIRMED  : Nifty > prev-day HIGH  AND  breaks first-15min HIGH
  SHORT_CONFIRMED : Nifty < prev-day LOW   AND  breaks first-15min LOW
  WAIT            : Nifty within prev-day range -> no clear direction -> cash
                    (until a clear break; avoid reversals)

ATLAS takes NO trade until this gate returns LONG or SHORT.
Reads live index quotes via the existing upstox_ws helpers.
Writes state to atlas_market_state so the entry gate can read it.
"""
import sys, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY
import requests

log = logging.getLogger("ATLAS-INDEX")
IST = timezone(timedelta(hours=5, minutes=30))

# Upstox index instrument keys
INDEX_KEYS = {
    "NIFTY50":   "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}
ORB_END = (9, 30)   # first 15-min window ends 9:30 IST
BUFFER  = 0.0005    # 0.05% break buffer


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def _now_ist():
    return datetime.now(IST)


def get_index_quote(index_key: str) -> dict:
    """Live quote + OHLC for one index via upstox_ws helpers."""
    try:
        from engine.upstox_ws import get_market_quotes, get_ohlc_quotes
        q  = get_market_quotes([index_key]) or {}
        oh = get_ohlc_quotes([index_key], interval="1d") or {}
        lk = index_key.replace("|", ":")
        quote = q.get(lk, {})
        ohlc  = oh.get(lk, {}).get("ohlc", {}) or quote.get("ohlc", {})
        return {
            "ltp":      quote.get("last_price", 0),
            "day_high": ohlc.get("high", 0),
            "day_low":  ohlc.get("low", 0),
            "day_open": ohlc.get("open", 0),
            "prev_close": ohlc.get("close", 0),  # note: 1d ohlc close = today's; prev needs history
        }
    except Exception as e:
        log.warning(f"index quote failed {index_key}: {e}")
        return {}


def get_prev_day_levels(index_key: str) -> dict:
    """Previous trading day's HIGH/LOW for the index (historical)."""
    try:
        from engine.upstox_ws import BASE_URL, get_headers
        # Upstox historical: /historical-candle/{key}/day/{to}/{from}
        to_d = (_now_ist().date() - timedelta(days=1)).isoformat()
        fr_d = (_now_ist().date() - timedelta(days=7)).isoformat()
        url = f"{BASE_URL}/historical-candle/{index_key}/day/{to_d}/{fr_d}"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            if candles:
                # candles: [ts, open, high, low, close, volume] — newest first
                last = candles[0]
                return {"prev_high": last[2], "prev_low": last[3], "prev_close": last[4]}
    except Exception as e:
        log.warning(f"prev-day levels failed {index_key}: {e}")
    return {}


def get_first15_range(index_key: str) -> dict:
    """First 15-min (9:15-9:30) HIGH/LOW for the index via 1-min historical."""
    try:
        from engine.upstox_ws import BASE_URL, get_headers
        today = _now_ist().date().isoformat()
        url = f"{BASE_URL}/historical-candle/intraday/{index_key}/1minute"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            # keep only 9:15-9:30 candles
            hi, lo = None, None
            for cndl in candles:
                ts = cndl[0]  # ISO timestamp
                try:
                    t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(IST)
                except Exception:
                    continue
                mins = t.hour * 60 + t.minute
                if 9*60+15 <= mins < 9*60+30:
                    h, l = cndl[2], cndl[3]
                    hi = h if hi is None else max(hi, h)
                    lo = l if lo is None else min(lo, l)
            if hi is not None:
                return {"or_high": hi, "or_low": lo}
    except Exception as e:
        log.warning(f"first-15 range failed {index_key}: {e}")
    return {}


def classify_index(index_key: str) -> dict:
    """Apply the user's opening-range rules to one index."""
    q  = get_index_quote(index_key)
    pd = get_prev_day_levels(index_key)
    orb = get_first15_range(index_key)
    ltp = q.get("ltp", 0)

    result = {"ltp": ltp, **pd, **orb, "direction": "WAIT", "reason": ""}
    if not ltp or not pd or not orb:
        result["reason"] = "insufficient data (ltp/prev-day/first-15 missing)"
        return result

    prev_high, prev_low = pd["prev_high"], pd["prev_low"]
    or_high, or_low     = orb["or_high"], orb["or_low"]

    # only decide AFTER 9:30 (opening range complete)
    now_mins = _now_ist().hour * 60 + _now_ist().minute
    if now_mins < ORB_END[0]*60 + ORB_END[1]:
        result["reason"] = "before 9:30 — opening range not complete"
        return result

    # LONG: above prev-day high AND breaks first-15 high
    if ltp > prev_high and ltp > or_high * (1 + BUFFER):
        result["direction"] = "LONG_CONFIRMED"
        result["reason"] = f"LTP {ltp:.0f} > prevHigh {prev_high:.0f} & > ORhigh {or_high:.0f}"
    # SHORT: below prev-day low AND breaks first-15 low
    elif ltp < prev_low and ltp < or_low * (1 - BUFFER):
        result["direction"] = "SHORT_CONFIRMED"
        result["reason"] = f"LTP {ltp:.0f} < prevLow {prev_low:.0f} & < ORlow {or_low:.0f}"
    else:
        result["direction"] = "WAIT"
        result["reason"] = f"LTP {ltp:.0f} within range (prev {prev_low:.0f}-{prev_high:.0f}) — no clear break"
    return result


def get_market_direction() -> dict:
    """
    Master gate: classify NIFTY 50 (primary) + BANK NIFTY (confirm).
    Returns dict with overall 'direction' in {LONG, SHORT, WAIT} + detail.
    """
    nifty = classify_index(INDEX_KEYS["NIFTY50"])
    bank  = classify_index(INDEX_KEYS["BANKNIFTY"])

    nd = nifty["direction"]
    bd = bank["direction"]

    # Primary = Nifty 50. Bank Nifty confirms/aligns.
    if nd == "LONG_CONFIRMED":
        overall = "LONG"
    elif nd == "SHORT_CONFIRMED":
        overall = "SHORT"
    else:
        overall = "WAIT"

    state = {
        "direction": overall,
        "nifty":  nifty,
        "banknifty": bank,
        "ts": _now_ist().isoformat(),
        "aligned": (nd == bd),
    }
    log.info(f"MARKET DIRECTION: {overall} | Nifty {nd} | Bank {bd} | aligned={state['aligned']}")
    _store_state(state)
    return state


def _store_state(state: dict):
    """Persist to atlas_market_state so the entry gate can read it."""
    rec = {
        "state_date": _now_ist().date().isoformat(),
        "direction":  state["direction"],
        "nifty_ltp":  state["nifty"].get("ltp", 0),
        "nifty_dir":  state["nifty"].get("direction", ""),
        "bank_ltp":   state["banknifty"].get("ltp", 0),
        "bank_dir":   state["banknifty"].get("direction", ""),
        "aligned":    state["aligned"],
        "reason":     state["nifty"].get("reason", ""),
        "updated_at": state["ts"],
    }
    try:
        # upsert on state_date
        requests.post(f"{SUPABASE_URL}/rest/v1/atlas_market_state?on_conflict=state_date",
                      headers={**_headers(), "Prefer": "resolution=merge-duplicates"},
                      json=rec, timeout=10)
    except Exception as e:
        log.warning(f"market state store failed: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json
    print(json.dumps(get_market_direction(), indent=2, default=str))
