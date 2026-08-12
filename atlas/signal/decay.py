"""
ATLAS Signal Engine — Decay and Invalidation
Signals decay, get invalidated, and are removed automatically.

ZERO MEANS DISABLED, NOT "IMMEDIATELY"
--------------------------------------
atlas/config.py marks SIGNAL_DECAY_MINUTES, MAX_LIVE_SIGNALS and MIN_RVOL as
"DEPRECATED. Retained as names so imports do not break. Not used to gate." and
sets all three to 0. This module read those zeros as thresholds rather than as
off-switches, which inverted every rule it implements:

    age_minutes > 0          -> every signal with a signal_time expired
                                on the first pass
    len(signals) <= 0        -> false, so sorted_sigs[0:] displaced
                                EVERY remaining signal
    rvol < MIN_RVOL * 0.5    -> never fires (the only inert branch, and the
                                only one that did something useful)

Between the first two branches, every row in live_signals for the day was
marked within one 10-minute cron tick. Confirmed against production: 786 rows
sampled across 10 trading dates, 100% of them on every date carried a
[EXPIRED] setup_name.

That mattering more than it looks: remove_signal() PATCHes `setup_name`
itself. live_signals has no status column (columns are: created_at, direction,
entry, id, ltp, orb_range_pct, prev_day_high, prev_day_low, rvol, session,
setup_name, signal_date, signal_time, sl, symbol, target_1, target_2), so the
only field available to record state was one holding real data. The original
setup names are not recoverable.

Each rule now checks whether it is enabled before doing anything, and
remove_signal preserves the original setup_name alongside the marker so a
future enablement is non-destructive. Adding a real `status` column is the
actual fix for the schema gap; noted, not done here.
"""

import os, sys, requests, logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY,
    SIGNAL_DECAY_MINUTES, MIN_RVOL, MAX_LIVE_SIGNALS
)

log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# A threshold of 0 disables its rule. It is never a live threshold meaning
# "expire everything instantly" / "keep zero signals".
DECAY_ENABLED = SIGNAL_DECAY_MINUTES > 0
CAP_ENABLED   = MAX_LIVE_SIGNALS   > 0
RVOL_ENABLED  = MIN_RVOL           > 0
ANY_RULE_ENABLED = DECAY_ENABLED or CAP_ENABLED or RVOL_ENABLED
HTTP_TIMEOUT = 10

def _headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }

def get_live_signals():
    today = datetime.now(IST).date().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_signals?signal_date=eq.{today}&order=signal_time.asc",
        headers=_headers(), timeout=HTTP_TIMEOUT
    )
    return r.json() if r.status_code == 200 else []

def get_live_prices(symbols):
    if not symbols: return {}
    # Quote + percent-encode: a bare `in.(M&M,...)` truncates at the ampersand
    # and PostgREST rejects the whole filter with 400 PGRST100.
    from urllib.parse import quote
    vals = ",".join('"' + s.replace('"', '""') + '"' for s in symbols)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/live_prices?symbol={quote(f'in.({vals})', safe='')}&select=symbol,ltp",
        headers=_headers(), timeout=HTTP_TIMEOUT
    )
    return {row["symbol"]: row for row in r.json()} if r.status_code == 200 else {}

def is_signal_expired(signal):
    if not DECAY_ENABLED:
        return False, None
    signal_time_str = signal.get("signal_time", "")
    if not signal_time_str: return False, None
    try:
        now = datetime.now(IST)
        today = now.date()
        h, m = map(int, signal_time_str.split(":"))
        signal_dt = datetime(today.year, today.month, today.day, h, m, tzinfo=IST)
        age_minutes = (now - signal_dt).total_seconds() / 60
        if age_minutes > SIGNAL_DECAY_MINUTES:
            return True, f"Time decay — {age_minutes:.0f}min old (max {SIGNAL_DECAY_MINUTES}min)"
    except Exception:
        pass
    return False, None

def is_signal_invalidated(signal, current_price):
    if not current_price: return False, None
    direction = signal.get("direction", "")
    entry     = float(signal.get("entry", 0))
    sl        = float(signal.get("sl", 0))
    rvol      = float(signal.get("rvol", 0))
    if not entry or not sl: return False, None
    if direction == "LONG" and current_price <= sl:
        return True, f"SL breached — LTP {current_price:.1f} <= SL {sl:.1f}"
    if direction == "SHORT" and current_price >= sl:
        return True, f"SL breached — LTP {current_price:.1f} >= SL {sl:.1f}"
    if direction == "LONG" and current_price > entry * 1.005:
        return True, f"Entry missed — LTP {current_price:.1f} too far above entry {entry:.1f}"
    if direction == "SHORT" and current_price < entry * 0.995:
        return True, f"Entry missed — LTP {current_price:.1f} too far below entry {entry:.1f}"
    if RVOL_ENABLED and rvol > 0 and rvol < MIN_RVOL * 0.5:
        return True, f"RVOL collapsed — {rvol:.1f}x"
    return False, None

def remove_signal(signal_id, reason, original_setup=""):
    """Mark a signal expired WITHOUT destroying its setup name.

    live_signals has no status column, so the marker has to share the
    setup_name field. Previously this replaced it outright, so the original
    setup name was lost on the first cron tick and could not be recovered.
    Preserve it after the marker instead.
    """
    original = (original_setup or "").strip()
    if original.startswith("[EXPIRED]"):
        return True                       # already marked; do not re-clobber
    marker = f"[EXPIRED] {reason}"
    payload = f"{marker} | {original}" if original else marker
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/live_signals?id=eq.{signal_id}",
        headers=_headers(),
        json={"setup_name": payload},
        timeout=HTTP_TIMEOUT,
    )
    return r.status_code in (200, 204)

def enforce_max_signals(signals):
    if not CAP_ENABLED: return []
    if len(signals) <= MAX_LIVE_SIGNALS: return []
    sorted_sigs = sorted(signals, key=lambda s: float(s.get("rvol", 0)), reverse=True)
    to_remove = sorted_sigs[MAX_LIVE_SIGNALS:]
    removed = []
    for sig in to_remove:
        reason = f"Displaced by higher conviction signal"
        if remove_signal(sig["id"], reason, sig.get("setup_name", "")):
            removed.append(sig)
            log.info(f"Displaced: {sig['symbol']} — {reason}")
    return removed

def run_decay_check():
    log.info("Running signal decay check...")
    if not ANY_RULE_ENABLED:
        # All three thresholds are 0 in atlas/config.py, i.e. every rule is
        # off. Return before touching the table -- this job used to mark every
        # row in it on exactly this configuration.
        log.info("All decay rules disabled (SIGNAL_DECAY_MINUTES=%s, "
                 "MAX_LIVE_SIGNALS=%s, MIN_RVOL=%s) -- nothing to do.",
                 SIGNAL_DECAY_MINUTES, MAX_LIVE_SIGNALS, MIN_RVOL)
        return {"checked": 0, "expired": [], "invalidated": [], "displaced": [],
                "disabled": True}
    signals = get_live_signals()
    if not signals:
        log.info("No live signals to check")
        return {"checked": 0, "expired": [], "invalidated": [], "displaced": []}

    active  = [s for s in signals if not s.get("setup_name", "").startswith("[EXPIRED]")]
    symbols = list(set(s["symbol"] for s in active))
    prices  = get_live_prices(symbols)

    expired = []
    invalidated = []

    for sig in active:
        symbol = sig["symbol"]
        ltp    = float(prices.get(symbol, {}).get("ltp", 0))
        is_exp, exp_reason = is_signal_expired(sig)
        if is_exp:
            if remove_signal(sig["id"], exp_reason, sig.get("setup_name", "")):
                expired.append({"symbol": symbol, "reason": exp_reason})
                log.info(f"Expired: {symbol} — {exp_reason}")
            continue
        is_inv, inv_reason = is_signal_invalidated(sig, ltp)
        if is_inv:
            if remove_signal(sig["id"], inv_reason, sig.get("setup_name", "")):
                invalidated.append({"symbol": symbol, "reason": inv_reason})
                log.info(f"Invalidated: {symbol} — {inv_reason}")

    remaining = [s for s in active
                 if not any(s["symbol"] == e["symbol"] for e in expired + invalidated)]
    displaced = enforce_max_signals(remaining)

    summary = {
        "checked":     len(active),
        "expired":     expired,
        "invalidated": invalidated,
        "displaced":   [{"symbol": s["symbol"]} for s in displaced],
        "remaining":   len(remaining) - len(displaced),
    }
    log.info(f"Decay check complete — removed:{len(expired)+len(invalidated)+len(displaced)} active:{summary['remaining']}")
    return summary

if __name__ == "__main__":
    import json
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [ATLAS-DECAY] %(message)s")
    result = run_decay_check()
    print(json.dumps(result, indent=2))
