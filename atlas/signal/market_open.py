"""
ATLAS — Market-Hours Entry Engine
=================================
One long-lived process, polling every 60 seconds from 09:20 to 15:20 IST. This
replaces the single 09:37 cron; there is no parallel system.

EACH CYCLE
  1. live prices for the whole universe from Upstox, one batched request
  2. matched against the nightly zone map, held in memory for the session
  3. symbols within MAX_ENTRY_DIST_PCT of their zone right now
  4. the full gate stack per candidate, unchanged
  5. enter at market, record to atlas_trades
  6. periodically, reconcile the ledger against the broker

POLLING, NOT A SOCKET. Zones update nightly, so a 60-second poll loses nothing
a tick stream would give. The one persistent socket in this repo ran a full
session receiving zero ticks and logged no error -- live_prices was written once
a day, at the close, because the box runs Etc/UTC and three of four scheduled
windows matched nothing. A request that fails is visible; a subscription that
goes quiet is not.

THE UTC TRAP IS THE SAME TRAP. Every window comparison here goes through
now_ist(). A naive datetime.now() on this box is UTC and would put the trading
window five and a half hours out -- which is exactly how that socket bug
happened, not a hypothetical.

WHAT IS HELD IN MEMORY, AND WHAT IS NOT
---------------------------------------
Memory holds only what is immutable for the session or purely advisory:

    the zone map          fixed once the batch is published; reloaded if the
                          batch date changes under us
    instrument keys       static
    the alert ledger      throttling only

Everything that can BLOCK a trade is read fresh every cycle -- open positions,
funds, kill-switch mode, halt state. That is what makes "a restart resumes from
the broker, not from memory" true by construction rather than by discipline: an
empty process has no state to be wrong about, because nothing that matters was
ever in the process.

RESTART
-------
Startup takes the lock, reconciles PENDING rows against the broker, and only
then begins cycling. A restart is therefore indistinguishable from a cold
start, which is the point.

SHADOW
------
With LIVE_TRADING_ENABLED false nothing is sent to the broker; enter_trade logs
a SHADOW row and returns SHADOW_INTENT. One thing does change under a loop: a
SHADOW row does not block Gate 3b -- deliberately, so a shadow run cannot block
itself -- so at 360 cycles a day the same symbol would be logged 360 times. The
loop therefore treats a same-day SHADOW row as committed, which gives shadow
the same one-per-symbol-per-day shape live has, and keeps the source of truth
in the ledger rather than in a set held in this process.
"""

import os
import sys
import time
import fcntl
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta, time as dtime

import requests

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY, LIVE_TRADING_ENABLED, BLOCKING_STATUSES,
)
from atlas.execution.atlas_entry import enter_trade
from atlas.execution import reconcile
from atlas.risk import breaker
from atlas.risk.kill_switch import get_agent_state, RiskDataUnavailable
from atlas.reporting.telegram import send

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

WINDOW_START = dtime(9, 20)
WINDOW_END   = dtime(15, 20)
CYCLE_SECONDS = 60

# Full broker reconcile cadence. Every cycle would be ~360 kite.orders() calls
# a day against the rate limit for a check that only has something to say after
# an entry. Five minutes bounds how long a PENDING row can sit unsettled.
RECONCILE_EVERY_CYCLES = 5

# An alert that fires 360 times is as unread as one that never fires.
ALERT_COOLDOWN_SECONDS = 900

MAX_BATCH_AGE_DAYS = 5

try:
    from engine.zone_entry import MAX_ENTRY_DIST_PCT
except Exception:                                   # pragma: no cover
    MAX_ENTRY_DIST_PCT = 0.30


def now_ist() -> datetime:
    """The only clock in this module. Never datetime.now()."""
    return datetime.now(IST)


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════
# ALERTING
# ══════════════════════════════════════════════════════════════════

_alerted = {}


def alert(kind: str, text: str, key: str = "") -> None:
    """Throttled per (kind, key). Everything fails loudly once, not 360 times."""
    k = (kind, key)
    now = time.time()
    last = _alerted.get(k, 0)
    if now - last < ALERT_COOLDOWN_SECONDS:
        log.warning(f"[{kind}] {text}  (alert suppressed, cooling down)")
        return
    _alerted[k] = now
    log.warning(f"[{kind}] {text}")
    try:
        send(f"<b>ATLAS — {kind}</b>\n{text}")
    except Exception as e:
        log.error(f"could not send alert: {e}")


# ══════════════════════════════════════════════════════════════════
# SESSION STATE — only the immutable and the advisory
# ══════════════════════════════════════════════════════════════════

class Session:
    def __init__(self):
        self.batch_date = None
        self.zone_map = {}          # (symbol, direction) -> signal
        self.cycle_n = 0
        self.entered = []
        self.started_at = now_ist()

    def summary(self) -> str:
        return (f"cycles {self.cycle_n}, batch {self.batch_date}, "
                f"{len(self.zone_map)} zones, {len(self.entered)} entered")


# ══════════════════════════════════════════════════════════════════
# THE NIGHTLY ZONE MAP
# ══════════════════════════════════════════════════════════════════

def get_latest_batch_date() -> str:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals?select=signal_date"
        f"&order=signal_date.desc&limit=1", headers=_headers(), timeout=15)
    if r.status_code != 200 or not r.json():
        return ""
    return r.json()[0].get("signal_date", "")


def get_signals(batch_date: str) -> list:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/signals?signal_date=eq.{batch_date}"
        f"&select=symbol,direction,entry_ref,entry_low,entry_high,sl,stop_pct,"
        f"setup_name,sector,zone_source,score,grade,structure_trend",
        headers=_headers(), timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"signal fetch failed: HTTP {r.status_code} "
                           f"{r.text[:200]}")
    return r.json()


def batch_is_stale(batch_date: str) -> bool:
    try:
        age = (now_ist().date() - datetime.fromisoformat(batch_date).date()).days
    except Exception as e:
        log.error(f"unparseable batch date {batch_date!r}: {e}")
        return True
    if age > MAX_BATCH_AGE_DAYS:
        log.error(f"STALE BATCH — newest signals are {age} days old ({batch_date})")
        return True
    return False


def load_zone_map(state: Session) -> bool:
    """Refresh the map if the published batch has changed. -> ok."""
    try:
        batch_date = get_latest_batch_date()
    except Exception as e:
        alert("SIGNALS UNREADABLE", f"could not read the signal batch: {e}")
        breaker.record_read(False, "signals", str(e))
        return False

    if not batch_date:
        alert("NO SIGNAL BATCH", "no published signals at all — EOD pipeline down?")
        return False
    if batch_is_stale(batch_date):
        alert("STALE BATCH", f"newest batch is {batch_date}. No entries.",
              key=batch_date)
        return False
    if batch_date == state.batch_date:
        return True                                   # unchanged; keep memory

    try:
        rows = get_signals(batch_date)
    except Exception as e:
        alert("SIGNALS UNREADABLE", f"batch {batch_date}: {e}", key=batch_date)
        breaker.record_read(False, "signals", str(e))
        return False

    breaker.record_read(True, "signals")
    state.zone_map = {
        (r["symbol"], str(r.get("direction", "")).upper()): r
        for r in rows if r.get("symbol")
    }
    state.batch_date = batch_date
    log.info(f"zone map loaded: {len(state.zone_map)} zones from batch {batch_date}")
    return True


# ══════════════════════════════════════════════════════════════════
# PRICES — Upstox, one batched request per cycle
# ══════════════════════════════════════════════════════════════════

def fetch_quotes(symbols: list) -> dict:
    """{symbol: ltp}. Empty dict on failure -- the caller must treat that as
    'no prices', never as 'no candidates'."""
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent.parent / "engine"))
        from universe import get_instrument_key
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "upstox_ws", Path(__file__).parent.parent.parent / "engine/upstox_ws.py")
        ux = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ux)

        keymap = {}
        for s in symbols:
            k = get_instrument_key(s)
            if k:
                keymap[k] = s
        if not keymap:
            return {}

        quotes = ux.get_market_quotes(list(keymap.keys()))
        out = {}
        for _, q in (quotes or {}).items():
            sym = q.get("symbol") or ""
            ik = q.get("instrument_token") or ""
            name = keymap.get(ik) or (sym if sym in symbols else None)
            ltp = q.get("last_price")
            if name and ltp:
                out[name] = float(ltp)
        # Upstox keys its response by its own symbol string rather than the
        # instrument key it was asked for, so fall back to matching on the
        # trailing segment when the direct lookup misses.
        if not out and quotes:
            rev = {k.split("|")[-1]: v for k, v in keymap.items()}
            for key, q in quotes.items():
                ltp = q.get("last_price")
                tail = str(key).split("|")[-1].split(":")[-1]
                if ltp and tail in rev:
                    out[rev[tail]] = float(ltp)
        return out
    except Exception as e:
        log.error(f"quote fetch failed: {type(e).__name__}: {e}")
        return {}


def near_zone(sig: dict, ltp: float) -> bool:
    """Within MAX_ENTRY_DIST_PCT of the zone edge right now."""
    ref = float(sig.get("entry_ref") or 0)
    if ref <= 0 or ltp <= 0:
        return False
    return abs(ltp - ref) / ltp * 100.0 <= MAX_ENTRY_DIST_PCT


# ══════════════════════════════════════════════════════════════════
# WHAT IS ALREADY COMMITTED — one query, every cycle
# ══════════════════════════════════════════════════════════════════

def committed_today() -> tuple:
    """
    (readable, {(symbol, direction)}) already committed.

    LIVE: anything in a BLOCKING status -- PENDING, OPEN, GTT_PENDING.
    SHADOW: additionally today's SHADOW rows, because a SHADOW row does not
    block Gate 3b and without this the loop would re-log the same symbol every
    cycle.

    FAILS CLOSED. An unreadable ledger returns readable=False and the caller
    skips the cycle. Returning an empty set would read as 'nothing is held',
    which is the shape of bug that closed six positions from one exception.
    """
    statuses = list(BLOCKING_STATUSES)
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?status=in.({','.join(statuses)})&select=symbol,direction",
            headers=_headers(), timeout=15)
        if r.status_code != 200:
            return False, set()
        held = {(x.get("symbol"), str(x.get("direction", "")).upper())
                for x in r.json()}

        if not LIVE_TRADING_ENABLED:
            today = now_ist().date().isoformat()
            r2 = requests.get(
                f"{SUPABASE_URL}/rest/v1/atlas_trades"
                f"?status=eq.SHADOW&entry_date=eq.{today}&select=symbol,direction",
                headers=_headers(), timeout=15)
            if r2.status_code != 200:
                return False, set()
            held |= {(x.get("symbol"), str(x.get("direction", "")).upper())
                     for x in r2.json()}
        return True, held
    except Exception as e:
        log.error(f"committed-today read failed: {type(e).__name__}: {e}")
        return False, set()


# ══════════════════════════════════════════════════════════════════
# PAUSE
# ══════════════════════════════════════════════════════════════════

def paused() -> tuple:
    """
    (halted, why). Checked at the TOP of every cycle, not at Gate 7.

    /pause must stop a running loop mid-session. The kill switch would catch it
    eventually, but only after the gates above it have already spent an LTP
    read and a funds call per candidate. Unreadable state counts as paused:
    not knowing whether the operator has halted us is not permission to trade.
    """
    halted, why = breaker.is_halted()
    if halted:
        return True, f"self-halt: {why}"
    try:
        state = get_agent_state()
    except RiskDataUnavailable as e:
        return True, f"agent state unreadable — treating as paused ({e})"
    except Exception as e:
        return True, f"agent state unreadable — treating as paused ({e})"
    mode = str(state.get("mode", "")).upper()
    if mode == "PAUSED":
        return True, "operator PAUSED"
    return False, mode


# ══════════════════════════════════════════════════════════════════
# ONE CYCLE
# ══════════════════════════════════════════════════════════════════

def cycle(state: Session) -> dict:
    """One pass. Returns a small summary; never raises."""
    state.cycle_n += 1
    out = {"cycle": state.cycle_n, "candidates": 0, "entered": 0, "skipped": 0}

    is_paused, why = paused()
    if is_paused:
        out["paused"] = why
        log.info(f"cycle {state.cycle_n}: paused — {why}")
        alert("PAUSED", why, key=why[:40])
        return out

    if not load_zone_map(state):
        out["error"] = "no usable zone map"
        return out

    ok, held = committed_today()
    if not ok:
        # Not knowing what we hold is not the same as holding nothing.
        alert("LEDGER UNREADABLE",
              "cannot read committed positions — skipping this cycle")
        if not breaker.record_read(False, "atlas_trades", "committed_today"):
            out["error"] = "halted"
            return out
        out["error"] = "ledger unreadable"
        return out
    breaker.record_read(True, "atlas_trades")

    symbols = sorted({s for s, _ in state.zone_map})
    quotes = fetch_quotes(symbols)
    if not quotes:
        alert("NO PRICES", f"Upstox returned no quotes for {len(symbols)} symbols")
        if not breaker.record_read(False, "upstox", "no quotes"):
            out["error"] = "halted"
        out["error"] = out.get("error", "no quotes")
        return out
    breaker.record_read(True, "upstox")

    candidates = []
    for (sym, direction), sig in state.zone_map.items():
        if (sym, direction) in held:
            continue
        ltp = quotes.get(sym)
        if ltp and near_zone(sig, ltp):
            candidates.append((sym, direction, sig, ltp))

    out["candidates"] = len(candidates)
    if not candidates:
        log.info(f"cycle {state.cycle_n}: {len(quotes)} quotes, nothing at a zone")
        return out

    for sym, direction, sig, ltp in candidates:
        signal = {
            "symbol": sym, "direction": direction,
            "entry_ref":  float(sig.get("entry_ref", 0) or 0),
            "entry":      float(sig.get("entry_ref", 0) or 0),
            "entry_low":  float(sig.get("entry_low", 0) or 0),
            "entry_high": float(sig.get("entry_high", 0) or 0),
            "sl":         float(sig.get("sl", 0) or 0),
            "stop_pct":   float(sig.get("stop_pct", 0) or 0),
            "structure_trend": sig.get("structure_trend", ""),
            "setup_name": sig.get("setup_name", ""),
            "sector":     sig.get("sector", ""),
            "zone_source": sig.get("zone_source", ""),
            "score":      float(sig.get("score", 0) or 0),
            "grade":      sig.get("grade", ""),
            "session":    "market_hours",
            "ltp":        ltp,          # from Upstox; Zerodha is execution only
        }
        try:
            result = enter_trade(signal)
        except Exception as e:
            log.exception(f"{sym}: enter_trade raised")
            alert("ENTRY ERROR", f"{sym}: {type(e).__name__}: {e}", key=sym)
            continue

        status = result.get("status", "?")
        log_decision(signal, result)

        if status in ("ENTERED", "SHADOW_INTENT"):
            out["entered"] += 1
            state.entered.append(f"{sym} {direction}")
            log.warning(f"cycle {state.cycle_n}: {status} {sym} {direction} @ {ltp}")
            alert(status, f"{sym} {direction} @ Rs{ltp:.1f} "
                          f"(stop Rs{signal['sl']:.1f})", key=sym)
        else:
            out["skipped"] += 1
            log.info(f"cycle {state.cycle_n}: {sym} {direction} — {status} "
                     f"— {result.get('reason','')}")
            if status.startswith("BLOCKED_"):
                alert(status, f"{sym}: {result.get('reason','')}", key=sym)

    return out


def log_decision(sig: dict, result: dict) -> None:
    """One row per evaluation, at decision time. Skips included."""
    row = {
        "run_date": now_ist().date().isoformat(),
        "symbol": sig.get("symbol"), "direction": sig.get("direction"),
        "status": result.get("status", "?"), "reason": result.get("reason", "")[:500],
        "qty": result.get("qty"), "entry_price": result.get("entry_price"),
        "stop_price": result.get("stop_price"), "risk_inr": result.get("risk_actual"),
        "agent_mode": "LIVE" if LIVE_TRADING_ENABLED else "SHADOW",
    }
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/atlas_entry_log",
                          headers=_headers(), json=row, timeout=10)
        if r.status_code not in (200, 201, 204):
            log.error(f"entry log rejected: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"entry log failed: {e}")


# ══════════════════════════════════════════════════════════════════
# THE SERVICE
# ══════════════════════════════════════════════════════════════════

_lock_handle = None


def acquire_lock() -> bool:
    """One instance. Two would double every entry."""
    global _lock_handle
    path = Path(os.environ.get("ATLAS_LOCK_FILE", "/tmp/atlas_market_hours.lock"))
    try:
        _lock_handle = open(path, "w")
        fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_handle.write(str(os.getpid()))
        _lock_handle.flush()
        return True
    except BlockingIOError:
        log.error(f"another instance holds {path} — exiting")
        return False
    except Exception as e:
        log.error(f"could not take the lock at {path}: {e}")
        return False


def in_window(now=None) -> bool:
    now = now or now_ist()
    return WINDOW_START <= now.time() < WINDOW_END


def serve() -> int:
    mode = "LIVE" if LIVE_TRADING_ENABLED else "SHADOW"
    if not acquire_lock():
        return 1

    state = Session()
    log.info(f"ATLAS market-hours engine starting [{mode}] "
             f"{WINDOW_START}–{WINDOW_END} IST, {CYCLE_SECONDS}s cycle")

    # RESTART RESUMES FROM THE BROKER. Settle anything left PENDING by a
    # previous process before evaluating a single candidate -- a PENDING row
    # from a death mid-order is exactly what must not be re-entered.
    try:
        summary = reconcile.settle()
        log.info(f"startup reconcile: {summary}")
        if summary.get("opened"):
            alert("RECONCILE", f"{summary['opened']} position(s) adopted at startup")
    except Exception as e:
        log.exception("startup reconcile failed")
        alert("RECONCILE FAILED", f"{type(e).__name__}: {e} — not starting")
        return 1

    send(f"<b>ATLAS ENGINE UP [{mode}]</b>\n"
         f"{WINDOW_START.strftime('%H:%M')}–{WINDOW_END.strftime('%H:%M')} IST, "
         f"{CYCLE_SECONDS}s cycles")

    while in_window():
        started = time.time()
        try:
            result = cycle(state)
            if result.get("entered") or result.get("candidates"):
                log.info(f"cycle {state.cycle_n}: {result}")
        except Exception as e:
            log.exception(f"cycle {state.cycle_n} raised")
            alert("CYCLE ERROR", f"{type(e).__name__}: {e}", key=type(e).__name__)

        if state.cycle_n % RECONCILE_EVERY_CYCLES == 0:
            try:
                reconcile.settle()
            except Exception as e:
                log.exception("reconcile failed")
                alert("RECONCILE FAILED", f"{type(e).__name__}: {e}")

        elapsed = time.time() - started
        time.sleep(max(0.0, CYCLE_SECONDS - elapsed))

    log.info(f"window closed — {state.summary()}")
    send(f"<b>ATLAS ENGINE DOWN [{mode}]</b>\n{state.summary()}\n"
         + ("\n".join(state.entered[:10]) if state.entered else "no entries"))
    return 0


def run() -> None:
    """A single cycle, for a manual check. The service is serve()."""
    state = Session()
    print(cycle(state))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if "--once" in sys.argv:
        run()
    else:
        sys.exit(serve())
