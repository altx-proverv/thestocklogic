"""
The only module in Radar that talks to Supabase.

EVERYTHING ELSE IS PURE, which is what makes the replay trustworthy: detection,
levels, heat, cards and outcomes can all be run over historical sessions with no
database at all, and they produce the same answers the live run produced.

READ BACK, DO NOT TRUST THE POST. requests.post does not raise on 4xx and
PostgREST rejects an entire insert that names an unknown column -- returning a
200-shaped error body that looks like success to a caller that only checks for
an exception. This project has shipped that exact failure twice. So every write
here returns a count the caller can check, and the runner's completion check
SELECTs what it just wrote.
"""

from __future__ import annotations

import os
import json
import logging
from datetime import date, datetime

log = logging.getLogger("RADAR-STORE")

TIMEOUT = 45
# PostgREST caps a response at 1,000 rows and no explicit limit raises it, so
# any read that could exceed that must page. This project lost three silently
# truncated fetches to that cap.
PAGE = 1000


class StoreUnavailable(RuntimeError):
    """No service key, or Supabase refused. The runner exits non-zero."""


def _cfg() -> tuple:
    url = os.environ.get("SUPABASE_URL",
                         "https://eibdlcanpudjgmkjxrga.supabase.co")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        raise StoreUnavailable(
            "SUPABASE_SERVICE_KEY is not set. Radar writes with the service "
            "role only -- the anon key cannot write these tables by design.")
    return url, key


def _headers(extra: dict | None = None) -> dict:
    _, key = _cfg()
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json"}
    if extra:
        h.update(extra)
    return h


def _json_safe(obj):
    """dates and Decimals -> JSON. Applied to every payload before it is sent.

    A date object reaches requests as a non-serialisable type and the POST
    raises -- which at least fails loudly. The worse case is a numpy float64,
    which serialises to a string on some versions and silently lands in a
    numeric column as NULL.
    """
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if hasattr(obj, "item"):                 # numpy scalar
        try:
            return obj.item()
        except Exception:
            return str(obj)
    return obj


def _post(table: str, rows: list, on_conflict: str | None = None,
          merge: bool = True) -> int:
    """-> rows written. Raises StoreUnavailable on refusal."""
    if not rows:
        return 0
    import requests
    url_base, _ = _cfg()
    url = f"{url_base}/rest/v1/{table}"
    if on_conflict:
        url += f"?on_conflict={on_conflict}"
    pref = "return=representation"
    if merge and on_conflict:
        pref += ",resolution=merge-duplicates"
    try:
        r = requests.post(url, headers=_headers({"Prefer": pref}),
                          json=_json_safe(rows), timeout=TIMEOUT)
    except Exception as e:
        raise StoreUnavailable(f"{table}: POST failed "
                               f"({type(e).__name__}: {e})") from e
    if r.status_code not in (200, 201):
        raise StoreUnavailable(f"{table}: HTTP {r.status_code} {r.text[:300]}")
    try:
        got = r.json()
    except Exception:
        got = []
    n = len(got) if isinstance(got, list) else 0
    if n != len(rows):
        # NAMED, NOT IGNORED. A short return means PostgREST accepted some rows
        # and not others, which is the shape of a partial write and the thing
        # the completion check exists to catch.
        log.warning(f"{table}: sent {len(rows)} row(s), {n} came back")
    return n


def _get(table: str, query: str) -> list:
    import requests
    url_base, _ = _cfg()
    out, offset = [], 0
    while True:
        sep = "&" if "?" in query else "?"
        url = f"{url_base}/rest/v1/{table}{query}{sep}limit={PAGE}&offset={offset}"
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        except Exception as e:
            raise StoreUnavailable(f"{table}: GET failed "
                                   f"({type(e).__name__}: {e})") from e
        if r.status_code != 200:
            raise StoreUnavailable(f"{table}: HTTP {r.status_code} "
                                   f"{r.text[:300]}")
        batch = r.json() or []
        out.extend(batch)
        if len(batch) < PAGE:
            return out
        offset += PAGE


def count(table: str, query: str) -> int:
    """An exact count via Prefer: count=exact. -1 when unreadable.

    -1 RATHER THAN 0. "The read failed" and "there are none" lead to opposite
    conclusions in the completion check, and a 0 would make a broken write look
    like a quiet night.
    """
    import requests
    url_base, _ = _cfg()
    try:
        r = requests.get(f"{url_base}/rest/v1/{table}{query}",
                         headers=_headers({"Prefer": "count=exact",
                                           "Range": "0-0"}), timeout=TIMEOUT)
        if r.status_code not in (200, 206):
            log.error(f"{table}: count HTTP {r.status_code} {r.text[:160]}")
            return -1
        return int((r.headers.get("content-range") or "*/0").split("/")[-1])
    except Exception as e:
        log.error(f"{table}: count failed ({type(e).__name__}: {e})")
        return -1


# ── EVENTS ────────────────────────────────────────────────────────

def upsert_events(events: list) -> list:
    """Upsert on (symbol, event_date). -> the rows as stored, with ids.

    THE UNIQUE KEY IS WHAT MAKES THE NIGHTLY RUN IDEMPOTENT. A re-run of the
    same session must not append a second row for the same symbol-day: the
    base-rate library would double-count it, and that is the one consumer that
    cannot tolerate duplication.
    """
    rows = []
    for e in events:
        rows.append({
            "symbol": e["symbol"],
            "sector": e.get("sector"),
            "trigger_type": e["trigger_type"],
            "event_date": e["event_date"],
            "status": e.get("status", "live"),
            "heat": e.get("heat", 0),
            "severity": e.get("severity"),
            "move_pct": e.get("move_pct"),
            "vol_multiple": e.get("vol_multiple"),
            "direction": e.get("direction"),
        })
    import requests
    url_base, _ = _cfg()
    if not rows:
        return []
    try:
        r = requests.post(
            f"{url_base}/rest/v1/radar_events?on_conflict=symbol,event_date",
            headers=_headers({"Prefer": "return=representation,"
                                        "resolution=merge-duplicates"}),
            json=_json_safe(rows), timeout=TIMEOUT)
    except Exception as e:
        raise StoreUnavailable(f"radar_events: POST failed "
                               f"({type(e).__name__}: {e})") from e
    if r.status_code not in (200, 201):
        raise StoreUnavailable(f"radar_events: HTTP {r.status_code} "
                               f"{r.text[:300]}")
    return r.json() or []


def open_events() -> list:
    """Every event whose outcome window is still open.

    live AND displaced. docs/TSL_FLASH.md §07: a displaced event keeps
    recording forward returns, so the sweep must not filter by status -- and
    this function is deliberately the only way the runner gets the list, so
    there is no second call site that could filter it.
    """
    return _get("radar_events",
                "?status=neq.closed&select=id,symbol,sector,event_date,status,"
                "heat,severity,trigger_type,move_pct,vol_multiple,direction")


def update_event(event_id: int, fields: dict) -> bool:
    import requests
    url_base, _ = _cfg()
    try:
        r = requests.patch(f"{url_base}/rest/v1/radar_events?id=eq.{event_id}",
                           headers=_headers({"Prefer": "return=minimal"}),
                           json=_json_safe(fields), timeout=TIMEOUT)
        if r.status_code not in (200, 204):
            log.error(f"radar_events {event_id}: PATCH HTTP {r.status_code} "
                      f"{r.text[:160]}")
            return False
        return True
    except Exception as e:
        log.error(f"radar_events {event_id}: PATCH failed ({e})")
        return False


# ── THE REST ──────────────────────────────────────────────────────

def upsert_documents(docs: list) -> int:
    rows = [{"source": d["source"], "url": d.get("url"),
             "published_at": d.get("published_at"),
             "symbols": d.get("symbols") or [],
             "title": d.get("title"), "body": d.get("body"),
             "category": d.get("category"), "text_hash": d["text_hash"]}
            for d in docs if d.get("text_hash")]
    return _post("radar_documents", rows, on_conflict="text_hash")


def upsert_levels(event_id: int, level_rows: list) -> int:
    rows = [{"event_id": event_id, "level_type": r["level_type"],
             "price": r["price"], "dist_pct": r.get("dist_pct"),
             "ref_date": r.get("ref_date")} for r in level_rows]
    return _post("radar_levels", rows, on_conflict="event_id,level_type")


def upsert_card(event_id: int, trade_date: date, payload: dict,
                status: str, notes: str | None) -> int:
    return _post("radar_card_versions",
                 [{"event_id": event_id, "trade_date": trade_date,
                   "payload": payload, "validation_status": status,
                   "validation_notes": notes}],
                 on_conflict="event_id,trade_date")


def upsert_scanner(rows: list) -> int:
    out = [{"trade_date": r["trade_date"], "symbol": r["symbol"],
            "side": r["side"], "sector": r.get("sector"),
            "close": r.get("close"), "extreme_price": r.get("extreme_price"),
            "pct_from_extreme": r.get("pct_from_extreme"),
            "vol_multiple": r.get("vol_multiple"), "volume": r.get("volume"),
            "delivery_pct": r.get("delivery_pct"),
            "filing_recent": bool(r.get("filing_recent")),
            "promoted": bool(r.get("promoted")),
            "rank_in_side": r.get("rank_in_side")} for r in rows]
    return _post("radar_scanner_daily", out,
                 on_conflict="trade_date,symbol,side")
