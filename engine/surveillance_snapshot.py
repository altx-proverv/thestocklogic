#!/usr/bin/env python3
"""
THE STOCK LOGIC — capture NSE's GSM and ASM lists, three times every session.

    python -m engine.surveillance_snapshot --run morning|afternoon|close
    python -m engine.surveillance_snapshot --check [YYYY-MM-DD]

WHY THREE RUNS. api/reportASM and api/reportGSM serve TODAY's list and nothing
else -- probed 2026-10-26, no date parameter. A session nobody captured is gone
permanently, and the observation scorer then skips every window containing it
as `surveillance_unknown`. One network failure must not cost a day that can
never be recovered, so the job runs mid-morning, mid-afternoon and after the
close, and the database's unique key (symbol, framework, stage, session_date)
makes the repeats a no-op.

WHAT A RUN DOES, IN THIS ORDER, AND WHY THE ORDER:
  1. trading-day gate         a holiday has no list worth calling a session's
  2. fetch, with retries
  3. SAVE THE RAW PAYLOAD     before any validation or upload, so a capture
                              Supabase refused, or that failed a check, is
                              still on disk and can be replayed or audited
  4. stamp check              a capture is labelled session S only if NSE's own
                              asmTime/gsmTime says S (the bhavcopy rule)
  5. insert, ignore duplicates, READ BACK
  6. log the attempt          surveillance_captures, ok or not
  7. replay                   any of TODAY's raw files whose session still has
                              no ok capture -- a morning capture saved while
                              Supabase was down is stored by the afternoon run
  8. (close run) the ALARM

THE ALARM IS A TELEGRAM MESSAGE, NOT A LOG LINE. If the session ends with no ok
capture, the close run sends one naming the date. A failure that reaches only
cron.log is silence with a different log line.

THE GAP THAT REMAINS: if this job never fires at all, its own close run cannot
alarm. engine/score_observations.py closes that by running check() from a
separate cron line -- but it ships with the observation ledger, so until then
the close run is the only alarm.

NEVER BACKFILLS. Replay only ever uploads a payload that was fetched on the day
it describes. There is no code path that labels a capture with any session but
the one NSE stamped on it, and no path that writes a past session from a later
fetch.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("surveillance-snapshot")
IST = timezone(timedelta(hours=5, minutes=30))
RAW_DIR = ROOT / "data/reference/surveillance/raw"
ASM = "https://www.nseindia.com/api/reportASM"
GSM = "https://www.nseindia.com/api/reportGSM"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
RETRY_WAITS = (5, 20, 60)          # seconds between fetch attempts
TIMEOUT = 40
DEFAULT_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"


def now_ist() -> datetime:
    return datetime.now(IST)


# ── PARSE ─────────────────────────────────────────────────────────

def _stamp_date(s: str) -> date:
    """'26-Oct-2026' or '26-Oct-2026 08:06:02' -> 2026-10-26."""
    return datetime.strptime(str(s).strip()[:11], "%d-%b-%Y").date()


def parse(asm: dict, gsm: list) -> list:
    """One row per (symbol, framework, stage). Raises on a payload whose shape
    is not the one probed on 2026-10-26: a renamed key must fail the capture,
    not store an empty list that reads as a clean market."""
    if not isinstance(asm, dict) or not {"longterm", "shortterm"} <= set(asm):
        raise ValueError(f"ASM payload shape changed: "
                         f"{sorted(asm)[:6] if isinstance(asm, dict) else type(asm).__name__}")
    if not isinstance(gsm, list):
        raise ValueError(f"GSM payload is {type(gsm).__name__}, not a list")
    rows = []
    for part in ("longterm", "shortterm"):
        for r in asm[part]["data"]:
            rows.append({"symbol": r["symbol"], "framework": "ASM",
                         "stage": r["survCode"], "stage_label": r.get("asmSurvIndicator"),
                         "description": r.get("survDesc"), "isin": r.get("isin"),
                         "nse_stamp": r["asmTime"], "source_url": ASM})
    for r in gsm:
        # NOT gsmStage: that field is the survCode NUMBER in Roman numerals
        # ('IBC - Receipt & GSM 0 (62)' -> 'LXII'), checked across all 77 rows
        # on 2026-10-26. Stored as a label it would read as GSM Stage LXII.
        # The stage is in survCode and survDesc.
        rows.append({"symbol": r["symbol"], "framework": "GSM",
                     "stage": r["survCode"], "stage_label": None,
                     "description": r.get("survDesc"), "isin": r.get("isin"),
                     "nse_stamp": r["gsmTime"], "source_url": GSM})
    if not rows:
        raise ValueError("both lists are empty — NSE has never published that")
    return rows


def session_of(rows: list, fetched: datetime) -> date:
    """The session these rows describe. Raises unless NSE's stamp on EVERY row
    is the IST date the payload was fetched on. A list fetched after midnight
    still carrying yesterday's stamp is yesterday's list, and labelling it
    today would be inventing a session."""
    stamps = {_stamp_date(r["nse_stamp"]) for r in rows}
    if stamps != {fetched.date()}:
        raise ValueError(f"NSE stamp {sorted(str(s) for s in stamps)} is not the "
                         f"capture date {fetched.date()} — refused")
    return fetched.date()


# ── FETCH AND KEEP ────────────────────────────────────────────────

def fetch() -> tuple:
    """(asm, gsm) JSON. Retries each failure; raises after the last."""
    import requests
    last = None
    for i, wait in enumerate((0,) + RETRY_WAITS):
        if wait:
            time.sleep(wait)
        try:
            s = requests.Session()
            s.headers.update({"User-Agent": UA,
                              "Accept": "application/json,text/plain,*/*"})
            s.get("https://www.nseindia.com", timeout=TIMEOUT)
            time.sleep(0.5)
            a = s.get(ASM, timeout=TIMEOUT)
            time.sleep(0.5)
            g = s.get(GSM, timeout=TIMEOUT)
            if a.status_code == 200 and g.status_code == 200:
                return a.json(), g.json()
            last = f"ASM HTTP {a.status_code}, GSM HTTP {g.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        log.warning(f"fetch attempt {i + 1} failed: {last}")
    raise RuntimeError(f"NSE fetch failed after {1 + len(RETRY_WAITS)} attempts: {last}")


def save_raw(asm, gsm, fetched: datetime, raw_dir=RAW_DIR) -> Path:
    """The payload exactly as NSE sent it, before anything can reject it."""
    raw_dir = Path(raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out = raw_dir / f"{fetched.strftime('%Y-%m-%d_%H%M%S')}.json"
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps({"fetched_at": fetched.isoformat(),
                               "asm": asm, "gsm": gsm}))
    tmp.replace(out)
    return out


# ── STORE ─────────────────────────────────────────────────────────

class Store:
    """Supabase over PostgREST. Every method raises on failure."""

    def __init__(self):
        self.url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
        self.key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if not self.key:
            raise RuntimeError("SUPABASE_SERVICE_KEY is not set")

    def _h(self, extra=None):
        h = {"apikey": self.key, "Authorization": f"Bearer {self.key}",
             "Content-Type": "application/json"}
        h.update(extra or {})
        return h

    def insert_snapshots(self, rows):
        import requests
        r = requests.post(
            f"{self.url}/rest/v1/surveillance_snapshots"
            f"?on_conflict=symbol,framework,stage,session_date",
            headers=self._h({"Prefer": "resolution=ignore-duplicates,return=minimal"}),
            json=rows, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"snapshots: HTTP {r.status_code} {r.text[:200]}")

    def keys(self, session: date) -> set:
        import requests
        out, off = set(), 0
        while True:
            r = requests.get(f"{self.url}/rest/v1/surveillance_snapshots",
                             params={"session_date": f"eq.{session}",
                                     "select": "symbol,framework,stage"},
                             headers=self._h({"Range-Unit": "items",
                                              "Range": f"{off}-{off + 999}"}),
                             timeout=60)
            if r.status_code not in (200, 206):
                raise RuntimeError(f"snapshots read: HTTP {r.status_code} {r.text[:200]}")
            page = r.json()
            out |= {(x["symbol"], x["framework"], x["stage"]) for x in page}
            if len(page) < 1000:
                return out
            off += 1000

    def log_capture(self, row):
        import requests
        r = requests.post(f"{self.url}/rest/v1/surveillance_captures",
                          headers=self._h({"Prefer": "return=minimal"}),
                          json=row, timeout=30)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"captures: HTTP {r.status_code} {r.text[:200]}")

    def captured(self, session: date) -> bool:
        import requests
        r = requests.get(f"{self.url}/rest/v1/surveillance_captures",
                         params={"session_date": f"eq.{session}", "ok": "is.true",
                                 "select": "id", "limit": "1"},
                         headers=self._h(), timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"captures read: HTTP {r.status_code} {r.text[:200]}")
        return bool(r.json())

    def last_error(self, session: date):
        import requests
        try:
            r = requests.get(f"{self.url}/rest/v1/surveillance_captures",
                             params={"session_date": f"eq.{session}", "ok": "is.false",
                                     "select": "run_label,error",
                                     "order": "captured_at.desc", "limit": "1"},
                             headers=self._h(), timeout=30)
            rows = r.json() if r.status_code == 200 else []
            return f"{rows[0]['run_label']}: {rows[0]['error']}" if rows else None
        except Exception:
            return None


def store_capture(store, rows, session: date, fetched: datetime, label: str) -> int:
    """Insert, read back, log ok. -> rows NSE listed. Raises if any row is not
    stored -- a 201 is not evidence the rows are there."""
    payload = [{**r, "session_date": session.isoformat(),
                "captured_at": fetched.isoformat()} for r in rows]
    store.insert_snapshots(payload)
    want = {(r["symbol"], r["framework"], r["stage"]) for r in rows}
    missing = want - store.keys(session)
    if missing:
        raise RuntimeError(f"{len(missing)} row(s) not stored, e.g. {sorted(missing)[:3]}")
    store.log_capture({"session_date": session.isoformat(),
                       "captured_at": fetched.isoformat(), "run_label": label,
                       "ok": True, "error": None,
                       "asm_rows": sum(r["framework"] == "ASM" for r in rows),
                       "gsm_rows": sum(r["framework"] == "GSM" for r in rows)})
    return len(rows)


# ── THE RUN ───────────────────────────────────────────────────────

def capture(label: str, *, store=None, fetcher=fetch, clock=now_ist,
            raw_dir=RAW_DIR, is_session=None) -> bool:
    """One capture attempt. True if this session now has an ok capture."""
    from engine.trading_calendar import require_trading_day
    t = clock()
    gate = is_session or (lambda d: require_trading_day(d, "surveillance_snapshot", log))
    if not gate(t.date()):
        return True          # nothing to capture is not a failure
    store = store or Store()
    try:
        asm, gsm = fetcher()
        save_raw(asm, gsm, t, raw_dir)
        rows = parse(asm, gsm)
        session = session_of(rows, t)
        n = store_capture(store, rows, session, t, label)
        log.info(f"{label}: session {session} captured, {n} listing(s)")
    except Exception as e:
        _log_failure(store, t.date(), t, label, f"{type(e).__name__}: {e}")
    # Even after a failed fetch: an earlier run's file may be on disk unstored.
    replay(store, t.date(), raw_dir)
    try:
        return store.captured(t.date())
    except Exception as e:
        log.error(f"cannot confirm the capture: {e}")
        return False


def _log_failure(store, session, t, label, err):
    log.error(f"{label}: capture FAILED for {session}: {err}")
    try:
        store.log_capture({"session_date": session.isoformat(),
                           "captured_at": t.isoformat(), "run_label": label,
                           "ok": False, "error": err[:500],
                           "asm_rows": None, "gsm_rows": None})
    except Exception as e:
        log.error(f"and the failure could not be logged either: {e}")


def replay(store, session: date, raw_dir=RAW_DIR) -> int:
    """Upload today's raw captures that never reached the database. Only files
    fetched ON `session` are considered, and session_of() re-checks NSE's stamp
    against the fetch date, so this cannot write a session from a later day."""
    try:
        if store.captured(session):
            return 0
    except Exception:
        return 0
    n = 0
    for p in sorted(Path(raw_dir).glob(f"{session.isoformat()}_*.json")):
        try:
            blob = json.loads(p.read_text())
            fetched = datetime.fromisoformat(blob["fetched_at"])
            rows = parse(blob["asm"], blob["gsm"])
            s = session_of(rows, fetched)
            store_capture(store, rows, s, fetched, "replay")
            log.info(f"replayed {p.name} into session {s}")
            n += 1
            break
        except Exception as e:
            log.warning(f"replay of {p.name} failed: {e}")
    return n


# ── THE ALARM ─────────────────────────────────────────────────────

def check(session: date, *, store=None, send=None, raised_by="close run") -> bool:
    """End-of-session alarm. True if captured. Otherwise sends Telegram naming
    the date and returns False. A store that cannot be read is NOT treated as
    captured -- "cannot confirm" alarms too."""
    if send is None:
        from atlas.reporting.telegram import send as _send
        send = _send
    reason = None
    try:
        store = store or Store()
        if store.captured(session):
            return True
        reason = store.last_error(session) or "no capture attempt was recorded at all"
    except Exception as e:
        reason = f"the capture log could not be read ({type(e).__name__}: {e})"
    # ESCAPED: telegram.send uses parse_mode=HTML, and the last error is often
    # an NSE or PostgREST body with HTML in it. Telegram answers an unparseable
    # message with 400 -- the alarm would fail on exactly the day it matters.
    import html
    reason = html.escape(str(reason))
    msg = (f"⚠️ <b>NO GSM/ASM CAPTURE for {session.isoformat()}</b>\n"
           f"NSE serves only the current list: this session cannot be recovered "
           f"after today. Every observation window containing it will score as "
           f"surveillance_unknown.\nLast error: {reason}\n"
           f"Raised by: {raised_by}. Run now: python -m engine.surveillance_snapshot "
           f"--run close")
    ok = False
    try:
        ok = bool(send(msg))
    except Exception as e:
        log.error(f"alarm send raised: {e}")
    if not ok:
        log.critical(f"ALARM NOT DELIVERED — no GSM/ASM capture for {session}: {reason}")
    else:
        log.error(f"alarm sent: no GSM/ASM capture for {session}")
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", choices=("morning", "afternoon", "close"))
    ap.add_argument("--check", nargs="?", const="today")
    a = ap.parse_args(argv)
    if a.run:
        try:
            ok = capture(a.run)
        except Exception as e:
            log.error(f"{a.run}: {type(e).__name__}: {e}")
            ok = False
        if a.run == "close":
            from engine.trading_calendar import require_trading_day
            today = now_ist().date()
            if require_trading_day(today, "surveillance_snapshot", log):
                return 0 if check(today) else 1
        return 0 if ok else 1
    if a.check:
        d = now_ist().date() if a.check == "today" else date.fromisoformat(a.check)
        return 0 if check(d, raised_by="manual check") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
