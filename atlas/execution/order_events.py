"""
ORDER-PATH EVENTS — every refusal and every substitution, recorded with its reason.
==================================================================================

WHY. On 2026-11-09 the question "was any real order placed on a substituted
price?" could not be answered: atlas_entry.py:885 replaced a missing quote with
entry_ref and logged nothing, so a fallback that fired and one that never fired
left identical records. This module is what makes the next version of that
question one query:

    select * from order_path_events where event = 'SUBSTITUTED';
    select * from order_path_events where event = 'REFUSED' and session_date = '...';

TWO SINKS, AND THE LOCAL ONE ALWAYS WORKS. Each event is appended to
reports/order_path_events.jsonl first, then inserted into public.order_path_events
(append-only by trigger, service role only). A database outage cannot lose the
local line, and the local file is what the box keeps even if the insert fails.

ONE ROW PER (symbol, event, code) PER SESSION PER PROCESS. The market-hours loop
re-evaluates every candidate each cycle; a missing quote would otherwise write
360 identical rows a day. Repeats are counted in-process and the count goes
into the local line.

NEVER RAISES, NEVER DECIDES. Recording is not gating: the caller has already
decided to refuse. A failure here is logged and swallowed.

EVENTS
  REFUSED       the path stopped because an input was missing, zero, stale or
                unreadable (not an ordinary gate like "price outside the zone")
  SUBSTITUTED   a value other than the measured one was used. After 2026-11-09
                no order-path site should emit this; it exists so that any
                substitution left in place can be counted rather than guessed.
  PRICE_USED    the price an entry was gated on, and where it came from, for
                every order that is about to be placed
  CHECK_SKIPPED a gate that fails open went ahead without its input (an
                unreadable block file, an unevaluable breaker, an unreadable
                position or trigger list). Behaviour unchanged; now countable.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("order-events")
IST = timezone(timedelta(hours=5, minutes=30))
ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL = Path(os.environ.get("TSL_ORDER_EVENTS_FILE",
                            ROOT / "reports" / "order_path_events.jsonl"))
EVENTS = ("REFUSED", "SUBSTITUTED", "PRICE_USED", "CHECK_SKIPPED")

_seen: dict = {}


def record(event: str, source: str, symbol: str | None, code: str, reason: str,
           detail: dict | None = None, trade_id=None, *, poster=None, now=None) -> bool:
    """True if this was the first occurrence this session (and was written)."""
    try:
        now = now or datetime.now(IST)
        session = now.date().isoformat()
        key = (session, event, source, symbol, code)
        n = _seen.get(key, 0) + 1
        _seen[key] = n
        if event not in EVENTS:
            log.error(f"order event with unknown kind {event!r} — recorded anyway")
        row = {"at": now.isoformat(timespec="seconds"), "session_date": session,
               "event": event, "source": source, "symbol": symbol, "code": code,
               "reason": str(reason)[:1000], "detail": detail or {},
               "trade_id": trade_id}
        if n > 1 and event != "PRICE_USED":
            # Repeat: counted, not re-sent.
            if n in (10, 100, 1000):
                log.warning(f"order event repeated {n}x this session: "
                            f"{event} {source} {symbol} {code}")
            return False
        _local({**row, "occurrence": n})
        (poster or _post)(row)
        log.info(f"order event {event} {source} {symbol or '-'} {code}: {reason}")
        return True
    except Exception as e:
        log.error(f"order event not recorded ({type(e).__name__}: {e}): "
                  f"{event} {source} {symbol} {code} {reason}")
        return False


def _local(row: dict) -> None:
    try:
        LOCAL.parent.mkdir(parents=True, exist_ok=True)
        with open(LOCAL, "a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        log.error(f"order event local write failed: {e}")


def _post(row: dict) -> None:
    try:
        import requests
        url = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not key:
            log.error("order event not inserted: SUPABASE_SERVICE_KEY unset "
                      "(the local line was written)")
            return
        r = requests.post(f"{url}/rest/v1/order_path_events",
                          headers={"apikey": key, "Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json",
                                   "Prefer": "return=minimal"},
                          json=row, timeout=10)
        if r.status_code not in (200, 201, 204):
            log.error(f"order event insert HTTP {r.status_code} {r.text[:200]} "
                      f"(the local line was written)")
    except Exception as e:
        log.error(f"order event insert failed: {e} (the local line was written)")
