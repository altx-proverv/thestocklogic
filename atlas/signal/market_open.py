"""
ATLAS Signal — Market Open Notifier
=====================================
Runs at 9:15 AM IST daily.
Sends qualifying EOD signals from last night to Telegram.
These are the SMC/OB-based daily chart setups ready for today.
Only sends top 3 by conviction score.
"""

import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY, MIN_CONVICTION_SCORE
from atlas.execution.trade_executor import queue_signal
from atlas.reporting.telegram import send

logging.basicConfig(level=logging.INFO,
                   format="%(asctime)s [ATLAS-OPEN] %(message)s")
log = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _headers():
    return {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


def get_today_signals() -> list:
    """Fetch today's EOD signals from Supabase."""
    today = date.today().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals"
        f"?signal_date=eq.{today}"
        f"&score=gte.{MIN_CONVICTION_SCORE}"
        f"&order=score.desc&limit=3",
        headers=_headers()
    )
    if r.status_code == 200:
        return r.json()
    return []


def run():
    now = datetime.now(IST).strftime("%d %b %Y %H:%M IST")
    log.info(f"Market open signal delivery — {now}")

    signals = get_today_signals()
    if not signals:
        log.info("No qualifying signals for today")
        send(
            f"📊 <b>ATLAS MARKET OPEN</b>\n"
            f"No qualifying signals today (score < {MIN_CONVICTION_SCORE})\n"
            f"Waiting for ORB scan at 9:35 AM"
        )
        return

    log.info(f"Sending {len(signals)} qualifying signals to ATLAS")
    send(
        f"🔔 <b>ATLAS MARKET OPEN — {now}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{len(signals)} qualifying signal(s) ready\n"
        f"ORB scan in 20 minutes"
    )

    for sig in signals:
        atlas_signal = {
            "symbol":     sig.get("symbol"),
            "direction":  sig.get("direction", "").upper(),
            "conviction": float(sig.get("score", 0)),
            "score":      float(sig.get("score", 0)),
            "entry_ref":  float(sig.get("entry_ref", 0)),
            "entry":      float(sig.get("entry_ref", 0)),
            "sl":         float(sig.get("sl", 0)),
            "target_1":   float(sig.get("target_1", 0)),
            "target_2":   float(sig.get("target_2", 0)),
            "setup_name": sig.get("setup_name", ""),
            "sector":     sig.get("sector", ""),
            "grade":      sig.get("grade", "B"),
            "session":    "opening",
        }
        result = queue_signal(atlas_signal)
        log.info(f"Queued: {sig['symbol']} score:{sig.get('score')} — {result.get('status')}")


if __name__ == "__main__":
    run()
