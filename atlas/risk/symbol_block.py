"""
BLOCK THE SYMBOL, NOT THE ACCOUNT.
==================================

Hemal's ruling, 2026-10-16: ATLAS does not switch itself off. A condition that
used to stand the whole day down now blocks the ONE symbol it is about and
alerts, naming what tripped and how to pause if the operator wants to.

WHAT THIS REPLACED. On 2026-10-15 ATHERENERG's overnight GTT was refused
because 1418.86 is not a multiple of its 0.10 tick. That is a fact about one
instrument's tick size. It set the account-wide halt sentinel, and the next
morning 443 published rows -- every signal and every candidate in the universe,
in every sector -- were refused by a flag raised over one stock's rounding.
The blast radius was four orders of magnitude larger than the fault.

TWO PROPERTIES, BOTH LOAD-BEARING:

  PER SYMBOL. Other symbols are unaffected. An open position in the blocked
  symbol is also unaffected -- this blocks NEW entries; the stop owns the exit.

  PER SESSION. The file is keyed to the session date and a new day ignores an
  older one, so no block survives the close. A fresh morning always starts live
  unless the operator paused it themselves. That is the half that matters most:
  a stand-down nobody clears is a stand-down that lasts until someone notices,
  and on 2026-10-16 that was seventeen hours.

WHAT THIS IS NOT. It is not the operator pause (/pause, atlas_state.mode), not
the kill switch and not the drill fence. Those three are untouched and still
stop everything: the operator pausing ATLAS is the entire point.

MODELLED ON range_breaker's blocked-symbol store, deliberately -- same day
keying, same fail-open read, same refusal to write the live file under the
drill fence. A second mechanism with different semantics would be a second
thing to reason about at 09:20.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_STATE_DIR = Path(os.environ.get("ATLAS_STATE_DIR", "/var/lib/atlas"))
BLOCK_FILE = Path(os.environ.get("ATLAS_SYMBOL_BLOCK_FILE",
                                 str(_STATE_DIR / "symbol_block.json")))

REASON_PROTECTION_FAILED = "PROTECTION_FAILED"
REASON_LEDGER = "LEDGER_INCOMPLETE"


def _now_ist():
    return datetime.now(IST)


def _load() -> dict:
    day = _now_ist().date().isoformat()
    blank = {"session_date": day, "symbols": {}}
    try:
        if BLOCK_FILE.exists():
            d = json.loads(BLOCK_FILE.read_text())
            if d.get("session_date") == day:
                return {**blank, **d}
            log.info(f"symbol-block file is from {d.get('session_date')}, today "
                     f"is {day} — ignoring it. No block survives the close.")
    except Exception as e:
        # FAIL OPEN. An unreadable file cannot prove a symbol should be
        # blocked, and the stop order is the real protection.
        log.error(f"could not read {BLOCK_FILE} ({e}) — no symbols blocked")
    return blank


def _save(d: dict) -> None:
    try:
        from atlas.risk.breaker import is_drill
        drilling, why = is_drill()
    except Exception:
        drilling, why = False, ""
    if drilling and str(BLOCK_FILE) == "/var/lib/atlas/symbol_block.json":
        log.warning(f"DRILL ({why}) — refusing to write the live symbol-block "
                    f"file at {BLOCK_FILE}")
        return
    try:
        BLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = BLOCK_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d))
        tmp.replace(BLOCK_FILE)
    except Exception as e:
        log.error(f"could not persist symbol blocks to {BLOCK_FILE}: {e}")


def blocked() -> dict:
    """{symbol: {"reason", "detail", "since"}} for this session."""
    return dict(_load().get("symbols", {}))


def is_blocked(symbol: str) -> tuple:
    """(blocked, reason, detail) for one symbol."""
    d = _load().get("symbols", {})
    e = d.get(symbol)
    if not e:
        return False, "", ""
    return True, str(e.get("reason") or ""), str(e.get("detail") or "")


def block(symbol: str, reason: str, detail: str, send=None) -> bool:
    """Block NEW entries in one symbol for the rest of this session.

    Alerts once per symbol per session, naming what tripped, the reason, and
    how to pause -- which is the shape Hemal asked every former stand-down to
    take. Returns True if this call is what blocked it.
    """
    d = _load()
    if symbol in d.get("symbols", {}):
        return False
    d.setdefault("symbols", {})[symbol] = {
        "reason": reason, "detail": str(detail)[:500],
        "since": _now_ist().isoformat()}
    _save(d)
    log.error(f"{symbol} BLOCKED for the rest of the session ({reason}): "
              f"{str(detail)[:300]}")
    if send is None:
        try:
            from atlas.reporting.telegram import send as _s
            send = _s
        except Exception:
            return True
    try:
        send(f"⛔ <b>{symbol} — NO NEW ENTRIES TODAY</b>\n"
             f"what tripped  <b>{reason}</b>\n"
             f"why  <code>{str(detail)[:300]}</code>\n\n"
             f"This blocks <b>{symbol} only</b>. Every other symbol trades "
             f"normally and the day is NOT stood down — ATLAS does not switch "
             f"itself off. An open position in {symbol} is unaffected; its "
             f"stop owns the exit.\n"
             f"The block expires at the close. Send <b>/pause</b> if you want "
             f"ATLAS to stop taking entries entirely.")
    except Exception as e:
        log.error(f"could not send the symbol-block alert for {symbol}: {e}")
    return True


def state() -> dict:
    d = _load()
    return {"file": str(BLOCK_FILE), "session_date": d.get("session_date"),
            "blocked": len(d.get("symbols", {})),
            "symbols": sorted(d.get("symbols", {}))}


def reset_for_tests() -> None:
    _save({"session_date": _now_ist().date().isoformat(), "symbols": {}})
