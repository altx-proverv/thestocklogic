"""
ATLAS Signal — Market Open Entry
=================================
Runs at 9:37 AM IST (AFTER the 9:30 opening range completes -- index_state
returns WAIT before 9:30, so an earlier cron can never pass Gate 1).

Fetches the LATEST AVAILABLE signal batch (written ~18:35 IST the previous
trading day) and routes each candidate through the atlas_entry gate stack.

MIGRATION NOTES
---------------
  - REMOVED score gate. MIN_CONVICTION_SCORE=82 blocked every signal (real
    batches top out ~79) and score is non-predictive per validation.
  - Date lookup no longer uses date.today() -- that queried signals that would
    not be written for another 9 hours. Now takes max(signal_date).
  - Dedup by symbol (batches contained duplicates, e.g. BAJFINANCE x2).
  - Passes the STRUCTURAL STOP (sl) through. enter_trade now sizes by risk
    (Rs3k budget) rather than notional, so the stop is mandatory -- a signal
    without one cannot be sized and is rejected.
  - Ordering by score is PROVISIONAL (TODO-STEP4: momentum + RS ranking).
    Score is carried for attribution only; it does not gate.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY, MAX_TRADES_PER_DAY, LIVE_TRADING_ENABLED,
)
from atlas.execution.atlas_entry import enter_trade
from atlas.reporting.telegram import send

# NOTE: getLogger, not basicConfig. kill_switch.py calls basicConfig at import
# time and wins the root logger, so this module's lines were tagged
# [ATLAS-RISK] -- which cost a full diagnostic cycle. Explicit logger avoids it.
log = logging.getLogger("ATLAS-OPEN")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [ATLAS-OPEN] %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

IST = timezone(timedelta(hours=5, minutes=30))

# Abort if the newest signal batch is older than this -- catches a silently
# broken EOD pipeline instead of trading stale data.
MAX_BATCH_AGE_DAYS = 5


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def get_latest_batch_date() -> str:
    """Newest signal_date present. Calendar-free: handles weekends and NSE
    holidays without a trading-calendar module."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals"
            f"?select=signal_date&order=signal_date.desc&limit=1",
            headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            return r.json()[0].get("signal_date", "")
    except Exception as e:
        log.warning(f"batch date fetch failed: {e}")
    return ""


def get_signals(batch_date: str) -> list:
    """All signals for the batch date. No score filter."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/signals"
            f"?signal_date=eq.{batch_date}"
            f"&order=score.desc",          # TODO-STEP4: momentum + RS ranking
            headers=_headers(), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.warning(f"signal fetch failed: {e}")
    return []


def dedupe(signals: list) -> list:
    """One row per symbol. Input is score-ordered, so first occurrence wins.

    NOTE: 03b scores long and short passes over the same rows, so a symbol can
    appear in BOTH directions in one batch (BAJFINANCE was LONG 72.0 and
    SHORT 74.0 on 7 Aug). Deduping by symbol alone resolves that contradiction
    arbitrarily -- whichever scored higher wins. Proper resolution belongs with
    the session_selector wiring."""
    seen, out = set(), []
    for s in signals:
        sym = s.get("symbol")
        if sym and sym not in seen:
            seen.add(sym)
            out.append(s)
    return out


def _stale(batch_date: str) -> bool:
    try:
        age = (datetime.now(IST).date() - date.fromisoformat(batch_date)).days
        if age > MAX_BATCH_AGE_DAYS:
            log.error(f"STALE BATCH -- newest signals are {age} days old ({batch_date})")
            return True
        log.info(f"Batch {batch_date} -- {age} day(s) old")
    except Exception as e:
        log.warning(f"staleness check failed: {e}")
    return False


def log_decision(sig: dict, result: dict):
    """Record what was decided about one signal, at the moment it was decided.

    atlas_trades only receives ENTERED / GTT_PENDING / SHADOW rows, so every
    skip and block used to exist only in a Telegram message. daily_report needs
    the reasons, and re-deriving them hours later would report the gates as they
    stand then -- different regime, prices and funds -- rather than the decision
    actually taken. Best-effort: a logging failure must never stop trading.
    """
    rec = {
        "run_date":    datetime.now(IST).date().isoformat(),
        "symbol":      sig.get("symbol"),
        "direction":   (sig.get("direction") or "").upper(),
        "status":      result.get("status", "?"),
        "reason":      (result.get("reason") or "")[:500],
        "qty":         result.get("qty"),
        "entry_price": result.get("entry_price"),
        "stop_price":  result.get("stop_price"),
        "risk_inr":    result.get("risk_actual"),
        "agent_mode":  "LIVE" if LIVE_TRADING_ENABLED else "SHADOW",
    }
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/atlas_entry_log",
                          headers=_headers(), json=rec, timeout=10)
        if r.status_code not in (200, 201, 204):
            log.warning(f"entry-log write failed for {rec['symbol']}: "
                        f"HTTP {r.status_code} {r.text[:120]}")
    except Exception as e:
        log.warning(f"entry-log write failed for {rec['symbol']}: {e}")


def run():
    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    log.info(f"Market open entry run -- {now}")

    batch_date = get_latest_batch_date()
    if not batch_date:
        log.error("No signal batch found at all")
        send("<b>ATLAS</b>\nNo signal batch found -- EOD pipeline may be down.")
        return

    if _stale(batch_date):
        send(f"<b>ATLAS -- STALE DATA</b>\nNewest batch: {batch_date}. No trades taken.")
        return

    signals = dedupe(get_signals(batch_date))
    if not signals:
        log.info(f"Batch {batch_date} contained no signals")
        send(f"<b>ATLAS MARKET OPEN</b>\nBatch {batch_date} -- no signals.")
        return

    log.info(f"{len(signals)} unique candidate(s) from batch {batch_date}")

    entered, results = 0, []
    for sig in signals:
        if entered >= MAX_TRADES_PER_DAY:
            log.info(f"Daily cap {MAX_TRADES_PER_DAY} reached -- stopping")
            break

        atlas_signal = {
            "symbol":     sig.get("symbol"),
            "direction":  (sig.get("direction") or "").upper(),
            "entry_ref":  float(sig.get("entry_ref", 0) or 0),
            "entry":      float(sig.get("entry_ref", 0) or 0),
            "entry_low":  float(sig.get("entry_low", 0) or 0),
            "entry_high": float(sig.get("entry_high", 0) or 0),
            # STRUCTURAL STOP -- mandatory. Risk-based sizing cannot work
            # without it, and it is the trade's invalidation level.
            "sl":         float(sig.get("sl", 0) or 0),
            "stop_pct":   float(sig.get("stop_pct", 0) or 0),
            "structure_trend": sig.get("structure_trend", ""),
            "setup_name": sig.get("setup_name", ""),
            "sector":     sig.get("sector", ""),
            "zone_source": sig.get("zone_source", ""),
            # carried for attribution only -- NOT used to gate or rank
            "score":      float(sig.get("score", 0) or 0),
            "grade":      sig.get("grade", ""),
            "session":    "opening",
        }

        result = enter_trade(atlas_signal)
        status = result.get("status", "?")
        results.append(f"{sig.get('symbol')}: {status}")
        log.info(f"{sig.get('symbol')} -- {status} -- {result.get('reason','')}")
        log_decision(atlas_signal, result)

        if status in ("ENTERED", "SHADOW_INTENT"):
            entered += 1

        # Gate 1 is index-wide: if the market has no direction, no later
        # candidate can pass either. Stop rather than hammer the API.
        if status == "SKIPPED_MARKET_WAIT":
            log.info("Market direction WAIT -- no trades possible today")
            break

    send(
        f"<b>ATLAS MARKET OPEN -- {now}</b>\n"
        f"Batch: {batch_date} | Candidates: {len(signals)} | Entered: {entered}\n"
        + "\n".join(results[:10])
    )


if __name__ == "__main__":
    run()
