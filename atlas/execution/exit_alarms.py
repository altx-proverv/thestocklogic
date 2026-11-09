"""
EXIT ALARMS — fire until a human clears them, day and night (OPEN D-058).
========================================================================

WHY. Every repeating "this needs a human" alert lived inside the market-hours
engine (the 15-minute alert cooldown, the protection register's repeat, the
fill-unreadable alert on each reconcile pass), and the engine exits at 15:50
IST. Under a mandate where a position is held for weeks, overnight is where it
lives almost all the time -- and an unprotected position that stops being
alerted at 15:50 is the failure a missing stop-loss makes possible.

THE REGISTER. One file, ATLAS_EXIT_ALARM_FILE (default
/var/lib/atlas/exit_alarms.json). Exit-side conditions raise an alarm here:
  UNPROTECTED       a position with no stop confirmed at the broker
                    (protection.register -- failed handovers, emergency exits
                    that could not price themselves, adopted positions)
  FILL_UNREADABLE   a COMPLETE order whose fill could not be read (reconcile)
  ORPHAN_UNCHECKED  a closed position whose resting triggers could not be
                    checked because the GTT list was unreadable (exits)
Raising is idempotent per key.

WHERE IT IS SEEN (decided 2026-11-10). There is NO overnight repeat: the
30-minute cron line was installed, proven (message_id 6467, 2026-11-09 18:30
UTC) and removed by decision. Outstanding alarms appear in the 19:50 IST
engine/volume_alerts message (daily_line); immediate alerts come from the
engine during market hours. `python -m atlas.execution.exit_alarms` still
sends the list once if run by hand.

HOW A HUMAN CLEARS ONE. Telegram: `/alarms` lists them with their ids;
`/ack <id>` clears one; `/ack all` clears every one. On the box:
`python -m atlas.execution.exit_alarms --ack <id|all>`. A clear records who
and when; the alarm stays in the file as history and is never shown again.

IF NOBODY EVER CLEARS IT. It stays in the 19:50 IST message every weekday.
Nothing ages out or clears itself.

SILENCE IS DISTINGUISHABLE. Every run writes a heartbeat line, alarms or not,
and engine/volume_alerts' daily message carries "Exit alarms: N outstanding"
or "none outstanding" -- a dead night job and a clean book do not look alike.
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("exit-alarms")
IST = timezone(timedelta(hours=5, minutes=30))
LIVE_PATH = "/var/lib/atlas/exit_alarms.json"
KINDS = ("UNPROTECTED", "FILL_UNREADABLE", "ORPHAN_UNCHECKED", "PROOF")
STALE_HOURS = 24


def _path() -> Path:
    return Path(os.environ.get("ATLAS_EXIT_ALARM_FILE", LIVE_PATH))


def _now() -> datetime:
    return datetime.now(IST)


def _load() -> dict:
    p = _path()
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:
        # UNREADABLE IS NOT "NO ALARMS". Returned as a sentinel the caller
        # reports, never as {}.
        log.critical(f"exit alarm register UNREADABLE at {p}: {e}")
        return {"_unreadable": str(e)}
    return {}


def _save(d: dict) -> bool:
    p = _path()
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = True, "breaker unavailable"
    if drilling and str(p) == LIVE_PATH:
        log.warning(f"DRILL ({why}) — not writing the live exit alarm register")
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, default=str))
        tmp.replace(p)
        return True
    except Exception as e:
        log.critical(f"exit alarm register NOT WRITTEN at {p}: {e}")
        return False


def raise_alarm(key: str, kind: str, symbol, why: str, trade_id=None) -> bool:
    """Record an exit-side condition that needs a human. Idempotent per key;
    a re-raise of a CLEARED key reopens it. Never raises."""
    try:
        d = _load()
        if "_unreadable" in d:
            return False
        now = _now().isoformat(timespec="seconds")
        a = d.get(key)
        if a and not a.get("cleared_at"):
            a["last_seen"] = now
            a["why"] = str(why)[:500]
        else:
            d[key] = {"id": key, "kind": kind if kind in KINDS else "UNPROTECTED",
                      "symbol": symbol, "trade_id": trade_id,
                      "why": str(why)[:500], "raised_at": now, "last_seen": now,
                      "sent": 0, "last_sent": None,
                      "cleared_at": None, "cleared_by": None}
            log.error(f"EXIT ALARM raised: {key} {kind} {symbol}: {why}")
        return _save(d)
    except Exception as e:
        log.critical(f"exit alarm not raised ({key}): {e}")
        return False


def outstanding() -> list:
    d = _load()
    if "_unreadable" in d:
        return None
    return [a for a in d.values() if isinstance(a, dict) and not a.get("cleared_at")]


def ack(target: str, by: str) -> list:
    """Clear one alarm by id, or every outstanding one with 'all'. -> ids cleared."""
    d = _load()
    if "_unreadable" in d:
        raise RuntimeError(f"register unreadable: {d['_unreadable']}")
    now = _now().isoformat(timespec="seconds")
    done = []
    for k, a in d.items():
        if not isinstance(a, dict) or a.get("cleared_at"):
            continue
        # CASE-INSENSITIVE: the Telegram handler lowercases every command.
        if target.lower() == "all" or k.lower() == target.lower():
            a["cleared_at"], a["cleared_by"] = now, by
            done.append(k)
    if done:
        _save(d)
    return done


def message(alarms: list, now=None) -> str:
    now = now or _now()
    e = html.escape
    old = [a for a in alarms if (now - datetime.fromisoformat(a["raised_at"]))
           > timedelta(hours=STALE_HOURS)]
    head = ("🚨 <b>UNACKNOWLEDGED FOR 24h+ — " if old else "🚨 <b>") + \
           f"{len(alarms)} EXIT ALARM(S) NEED A HUMAN</b>"
    lines = [head]
    for a in sorted(alarms, key=lambda a: a["raised_at"]):
        age = now - datetime.fromisoformat(a["raised_at"])
        lines.append(f"• <code>{e(a['id'])}</code> {e(a['kind'])} {e(str(a.get('symbol') or ''))}"
                     f" — {e(a['why'][:200])} (raised {e(a['raised_at'][:16])}, "
                     f"{int(age.total_seconds() // 3600)}h ago, sent {a.get('sent', 0)}x)")
    lines.append("Reply <code>/ack &lt;id&gt;</code> when handled, or "
                 "<code>/ack all</code>. Listed in the 19:50 IST message "
                 "until acknowledged.")
    return "\n".join(lines)


def run(sender=None, now=None) -> int:
    """The night job. 0 sent or nothing outstanding; 1 on a failure."""
    now = now or _now()
    alarms = outstanding()
    if alarms is None:
        log.critical("heartbeat: exit alarm register UNREADABLE — alarming")
        msg = ("🚨 <b>EXIT ALARM REGISTER UNREADABLE</b>\nThe night job cannot "
               f"read {html.escape(str(_path()))}. Outstanding exit alarms are "
               "UNKNOWN, not none. Check the box.")
        _send(msg, sender)
        return 1
    log.info(f"heartbeat: {len(alarms)} exit alarm(s) outstanding")
    if not alarms:
        return 0
    mid = _send(message(alarms, now), sender)
    if mid is None:
        log.critical("exit alarm message NOT ACCEPTED — will retry next run")
        return 1
    d = _load()
    for a in alarms:
        if a["id"] in d:
            d[a["id"]]["sent"] = int(d[a["id"]].get("sent") or 0) + 1
            d[a["id"]]["last_sent"] = now.isoformat(timespec="seconds")
    _save(d)
    log.info(f"exit alarm message accepted: message_id={mid}")
    return 0


def _send(msg: str, sender=None):
    if sender is not None:
        return sender(msg)
    from engine.volume_alerts import send
    return send(msg)


def daily_line() -> str:
    """For engine/volume_alerts' daily message. Never raises."""
    try:
        a = outstanding()
        if a is None:
            return "Exit alarms: register UNREADABLE"
        if not a:
            return "Exit alarms: none outstanding"
        return (f"Exit alarms: {len(a)} OUTSTANDING, unacknowledged — "
                + ", ".join(f"{x['id']}" for x in a[:6]))
    except Exception as e:
        return f"Exit alarms: could not be read ({type(e).__name__})"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if args[:1] == ["--ack"] and len(args) == 2:
        cleared = ack(args[1], by="box CLI")
        print(f"cleared: {cleared or 'nothing matched'}")
        sys.exit(0)
    if args[:1] == ["--raise-proof"]:
        ok = raise_alarm(f"PROOF-{_now().strftime('%Y%m%d%H%M')}", "PROOF", "PROOF",
                         "PROOF — not a real event. Raised on purpose to prove "
                         "the night job fires after the close and keeps firing.")
        print("raised" if ok else "NOT raised")
        sys.exit(0 if ok else 1)
    if args[:1] == ["--list"]:
        print(json.dumps(outstanding(), indent=1))
        sys.exit(0)
    sys.exit(run())
