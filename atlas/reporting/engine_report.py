"""
ATLAS — Daily Engine Report
===========================
What the entry engine did today, sent to Telegram at 15:30 IST.

    python3 -m atlas.reporting.engine_report            # today
    python3 -m atlas.reporting.engine_report --date 2026-09-15
    python3 -m atlas.reporting.engine_report --dry-run  # print, do not send

SEPARATE FROM daily_report.py, WHICH RUNS AT 19:05. That one answers "what do I
hold and what is it worth". This one answers "what did the engine do" -- cycles,
candidates, which gate held things back. Different questions, and merging them
would make one report that answers neither well.

RUN BY ITS OWN TIMER, NOT BY THE SERVICE. The window closes at 15:20 and the
process exits, so at 15:30 there is nothing left to ask. Reporting from inside
the service would also mean no report on the day the service died, which is the
day a report matters most.

WHERE THE NUMBERS COME FROM
---------------------------
Two sources, because no single one has all of it:

  the session file    /var/lib/atlas/session-YYYY-MM-DD.json, rewritten by the
                      loop every cycle. Cycles run, quotes fetched, failures by
                      kind, breaker state, reconcile totals. None of this is in
                      the database -- it is what the process knows and what
                      would otherwise exist only in the journal.

  atlas_entry_log     one row per evaluation at decision time, including every
                      skip and its reason. The decisions, with their reasons in
                      the gate's own words.

They are cross-checked rather than merely concatenated: the loop's own candidate
count against the number of entry_log rows. A mismatch means decisions stopped
being logged, which no single source can tell you.

SILENCE IS NOT AN ACCEPTABLE ANSWER
-----------------------------------
On a trading day this always sends something. If there is no session file and no
log rows, the headline is that the engine DID NOT RUN -- an absent report and a
quiet market must not look the same. On an NSE holiday it sends nothing, because
there was nothing to run.
"""

import os
import sys
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, date

import requests

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

from atlas.config import (SUPABASE_URL, SUPABASE_KEY, LIVE_TRADING_ENABLED,
                          GO_LIVE_DATE)

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", "/var/lib/atlas"))

# Telegram caps a message at 4096 characters. Skips are counted rather than
# listed and entries are capped, so a busy day cannot silently lose its tail.
MAX_ENTRIES_SHOWN = 10
MAX_SKIP_REASONS_SHOWN = 8
TELEGRAM_LIMIT = 4000


def now_ist() -> datetime:
    return datetime.now(IST)


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def load_session(day: date) -> dict:
    path = STATE_DIR / f"session-{day.isoformat()}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        log.error(f"session file unreadable ({path}): {e}")
        return {"_error": str(e)}


def load_decisions(day: date) -> tuple:
    """(readable, rows) from atlas_entry_log for the day."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_entry_log"
            f"?run_date=eq.{day.isoformat()}"
            f"&select=symbol,direction,status,reason,qty,entry_price,stop_price,"
            f"risk_inr,agent_mode,run_at&order=run_at.asc",
            headers=_headers(), timeout=30)
        if r.status_code != 200:
            log.error(f"entry log read failed: HTTP {r.status_code} {r.text[:200]}")
            return False, []
        return True, r.json()
    except Exception as e:
        log.error(f"entry log read failed: {type(e).__name__}: {e}")
        return False, []


def expected_cycles(sess: dict) -> int:
    """How many cycles a full session should have produced."""
    try:
        start, end = sess.get("window", ["09:20", "15:20"])
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
        minutes = (eh * 60 + em) - (sh * 60 + sm)
        every = int(sess.get("cycle_seconds") or 60)
        return max(1, minutes * 60 // every)
    except Exception:
        return 0


def compose(day: date, sess: dict, log_ok: bool, rows: list) -> str:
    mode = sess.get("mode") or ("LIVE" if LIVE_TRADING_ENABLED else "SHADOW")
    title = "ATLAS DAILY REPORT"
    head = [f"<b>{title}</b> · {day.strftime('%a %d %b %Y')}"]

    if mode == "SHADOW":
        left = (GO_LIVE_DATE - day).days
        head.append(f"👁 SHADOW · live from {GO_LIVE_DATE}"
                    + (f" ({left} day{'s' if left != 1 else ''})" if left > 0 else ""))
    else:
        head.append("🟢 LIVE · real orders")

    # ── the engine did not run ────────────────────────────────────
    if not sess and not rows:
        head.append("")
        head.append("🔴 <b>THE ENGINE DID NOT RUN TODAY.</b>")
        head.append("No session file and no logged decisions. A trading day "
                    "with no session is a failure, not a quiet market.")
        head.append("")
        head.append("Check:  systemctl status atlas-market-hours")
        head.append("        journalctl -u atlas-market-hours --since today")
        return "\n".join(head)

    out = list(head)

    # ── engine ───────────────────────────────────────────────────
    out.append("")
    out.append("<b>ENGINE</b>")
    if sess:
        cycles = int(sess.get("cycles") or 0)
        exp = expected_cycles(sess)
        note = ""
        if exp and cycles < exp * 0.9:
            note = f"  ⚠️ expected ~{exp} — the session ended early"
        elif exp:
            note = f"  (expected ~{exp})"
        out.append(f"  cycles     {cycles}{note}")
        started = str(sess.get("started_at", ""))[11:19]
        last = str(sess.get("last_update", ""))[11:19]
        ended = sess.get("ended_at")
        out.append(f"  ran        {started} → {last}"
                   + ("" if ended else "  ⚠️ no clean close recorded"))
        out.append(f"  quotes     {sess.get('quotes_last', 0)} symbols last fetch"
                   f" · {sess.get('quotes_ok', 0)} ok / "
                   f"{sess.get('quotes_failed', 0)} failed")
        if sess.get("paused_cycles"):
            out.append(f"  paused     {sess['paused_cycles']} cycle(s)")
    else:
        out.append("  ⚠️ no session file — process facts unavailable "
                   "(cycles, quotes, failures)")

    # Without a session file these are unknown, not clear. Reporting "none"
    # from an absent source is the same mistake as an error path returning a
    # plausible value.
    fails = (sess.get("failures") or {}) if sess else None
    if fails:
        out.append("  failures   " + ", ".join(f"{k} ×{v}" for k, v in
                                               sorted(fails.items())))
    elif sess:
        out.append("  failures   none")
    else:
        out.append("  failures   unknown (no session file)")

    if not sess:
        out.append("  breaker    unknown (no session file)")
    elif sess.get("breaker_tripped"):
        out.append(f"  🛑 BREAKER TRIPPED — {str(sess.get('breaker_reason',''))[:160]}")
        out.append("     No entries after that point. Clear the sentinel, then /normal.")
    else:
        out.append("  breaker    not tripped")

    rec = sess.get("reconcile") or {}
    if rec:
        out.append(f"  reconcile  {rec.get('passes', 0)} pass(es) · "
                   f"{rec.get('opened', 0)} adopted · "
                   f"{rec.get('cancelled', 0)} cancelled · "
                   f"{rec.get('unsettled', 0)} unsettled")
        if rec.get("opened"):
            out.append("     ⚠️ an adopted row was a position live at the broker "
                       "with an incomplete ledger. Check its stop.")

    # ── zones and candidates ─────────────────────────────────────
    out.append("")
    out.append("<b>ZONES</b>")
    out.append(f"  batch      {sess.get('batch_date') or '—'} · "
               f"{sess.get('zones', 0)} signal(s)")
    cand = int(sess.get("candidates_total") or 0)
    out.append(f"  candidates {cand} came within {MAX_ENTRY_DIST_PCT_STR} of a zone")
    if log_ok and sess and cand != len(rows):
        out.append(f"  ⚠️ {cand} candidates but {len(rows)} logged decisions — "
                   f"decisions are not all reaching atlas_entry_log")

    # ── entries ──────────────────────────────────────────────────
    entered = [r for r in rows if r.get("status") in ("ENTERED", "SHADOW_INTENT")]
    verb = "ENTERED" if mode == "LIVE" else "WOULD HAVE ENTERED"
    out.append("")
    out.append(f"<b>{verb} ({len(entered)})</b>")
    if not entered:
        out.append("  nothing")
    for r in entered[:MAX_ENTRIES_SHOWN]:
        out.append(f"  {r.get('symbol')} {r.get('direction')}  "
                   f"Rs{_num(r.get('entry_price')):,.2f} × {r.get('qty') or 0}  "
                   f"stop Rs{_num(r.get('stop_price')):,.2f}  "
                   f"risk Rs{_num(r.get('risk_inr')):,.0f}")
    if len(entered) > MAX_ENTRIES_SHOWN:
        out.append(f"  … and {len(entered) - MAX_ENTRIES_SHOWN} more")

    # ── skips, counted by reason ─────────────────────────────────
    skipped = [r for r in rows if r.get("status") not in ("ENTERED", "SHADOW_INTENT")]
    out.append("")
    out.append(f"<b>SKIPPED ({len(skipped)})</b>")
    if not log_ok:
        out.append("  ⚠️ atlas_entry_log unreadable — decisions unavailable")
    elif not skipped:
        out.append("  nothing skipped")
    else:
        counts = {}
        for r in skipped:
            counts[r.get("status") or "?"] = counts.get(r.get("status") or "?", 0) + 1
        for status, n in sorted(counts.items(), key=lambda kv: -kv[1])[:MAX_SKIP_REASONS_SHOWN]:
            out.append(f"  {status:<26}{n}")
        if len(counts) > MAX_SKIP_REASONS_SHOWN:
            out.append(f"  … {len(counts) - MAX_SKIP_REASONS_SHOWN} more kind(s)")

    # ── why nothing qualified ────────────────────────────────────
    # The requirement this exists for: a day with no entries must still say why,
    # in the gate's own words rather than as a bare count.
    if not entered:
        out.append("")
        out.append("<b>WHY NOTHING QUALIFIED</b>")
        if not log_ok:
            out.append("  Unknown — atlas_entry_log could not be read, so "
                       "whether anything qualified is not established here.")
            return "\n".join(out)
        reason = _dominant_reason(skipped) or sess.get("last_gate_reason") or ""
        if reason:
            out.append(f"  {reason[:300]}")
        elif cand == 0:
            out.append("  Nothing came within range of a zone all session. "
                       "The engine ran; no price reached a published zone.")
        else:
            out.append("  No reason recorded — check atlas_entry_log.")

    return "\n".join(out)


MAX_ENTRY_DIST_PCT_STR = "0.30%"
try:
    from engine.zone_entry import MAX_ENTRY_DIST_PCT as _M
    MAX_ENTRY_DIST_PCT_STR = f"{_M}%"
except Exception:
    pass


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _dominant_reason(skipped: list) -> str:
    """The reason that held back the most candidates, verbatim."""
    if not skipped:
        return ""
    counts = {}
    for r in skipped:
        key = (r.get("status"), (r.get("reason") or "").strip())
        counts[key] = counts.get(key, 0) + 1
    (status, reason), n = max(counts.items(), key=lambda kv: kv[1])
    return f"{status} ×{n} — {reason}" if reason else f"{status} ×{n}"


def run(day: date = None, dry: bool = False) -> int:
    day = day or now_ist().date()

    try:
        from trading_calendar import is_trading_day
        trading = is_trading_day(day)
    except Exception as e:
        # Cannot tell. Report anyway: a spurious report on a holiday is a
        # nuisance, a missing one on a trading day is the failure this guards.
        log.error(f"trading calendar unreadable ({e}) — reporting anyway")
        trading = True

    if not trading:
        log.info(f"{day} is not an NSE trading day — no report")
        return 0

    sess = load_session(day)
    log_ok, rows = load_decisions(day)
    body = compose(day, sess, log_ok, rows)

    if len(body) > TELEGRAM_LIMIT:
        body = body[:TELEGRAM_LIMIT] + "\n… truncated"

    if dry:
        print(body)
        return 0

    from atlas.reporting.telegram import send
    ok = send(body)
    if not ok:
        log.error("report could not be sent — printing instead")
        print(body)
        return 1
    log.info(f"engine report sent for {day}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    argv = sys.argv
    d = None
    if "--date" in argv:
        d = date.fromisoformat(argv[argv.index("--date") + 1])
    sys.exit(run(day=d, dry="--dry-run" in argv))
