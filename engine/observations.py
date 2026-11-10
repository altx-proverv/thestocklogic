"""
THE STOCK LOGIC — the observation ledger's only writer.

EVERY OBSERVATION PERSISTS BEFORE IT RENDERS. A publisher builds its rows, calls
record(), and publishes only if record() returned True. A ledger that is written
after the page -- or alongside it, best effort -- is a ledger of what we
remembered to keep, and the claims most worth checking are the ones a flaky
write would lose. tests/test_observations.py walks every publisher and fails if
the call is missing or comes after the publish.

FAILS CLOSED. No key, no URL, an HTTP error, or a read-back that does not find
every row: record() returns False and the caller does not publish. It never
raises, so a publisher's own error handling decides what "not published" looks
like (exit 1, return 0, raise StoreUnavailable -- whatever that module already
does when its publish fails).

APPEND-ONLY. The database refuses UPDATE and DELETE (migrations/
20261103181336_observation_ledger.sql). This module therefore never tries: a re-publish
of the same values is a no-op, and a re-publish of DIFFERENT values appends a
correction that references the original. Which of the two a type gets is
REPUBLISH below, because some publishers legitimately re-send rows every five
minutes with a live field that moves -- those keep their first sighting.

IMPORTS NOTHING FROM atlas/. publish_orb calls this, and
tests/test_page_cannot_write_breaker.py walks that module's import graph.
"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone

log = logging.getLogger("observations")

TABLE = "observations"
DEFAULT_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
TIMEOUT = 15
PAGE = 1000          # Supabase's max-rows; a longer read is paged, never truncated

# ── PER-TYPE DECLARATIONS ─────────────────────────────────────────
#
# lookback   sessions of history the observation's computation needs. Each is
#            the longest window in the code that produced it, not a choice:
#              signal / candidate  252  02b high_52w rolling(252); 03b gates the
#                                       enterable universe on the same 252
#              btst                200  btst_engine EMA200 (compute_ema(...,200))
#              detection           20   rbe_startup avg_vol_20d = tail(20)
#              heavy_quiet         20   the same avg_vol_20d is its denominator
#              radar_event         26   radar/detect: vol_avg_window (20) + 6
#              orb_break           1    the session's own opening range
# basis      what reference_price means.
#              close_T0             the observation session's close. Every
#                                   type published after the close, or built
#                                   from the close.
#              price_at_publication the price the publisher saw when it fired.
# scored     whether the T+3 scorer may score it. False only for radar_event:
#            RADAR gives "observable triggers, dated catalysts and historical
#            base rates, not predictions" (radar-spec.md), and its trigger fires
#            on a big move in EITHER direction. Its up/down describes what the
#            stock did. Scoring it as a continuation would invent a call we never
#            made. Its forward returns at 5/20/60 sessions and max drawdown are
#            recorded by radar/outcomes.py for the base-rate library (spec §6),
#            not here. It is still LEDGERED, because it is still rendered.
# republish  what a second publish of the same natural key means.
#              correct  a re-run that changed values -- append a correction
#              ignore   a live re-send (every 5 minutes); the first sighting is
#                       the observation and later ones are the same claim
TYPES = {
    "signal":      dict(lookback=252, directional=True,  basis="close_T0",
                        republish="correct"),
    "candidate":   dict(lookback=252, directional=True,  basis="close_T0",
                        republish="correct"),
    "btst":        dict(lookback=200, directional=True,  basis="close_T0",
                        republish="correct"),
    "radar_event": dict(lookback=26,  directional=False, basis="close_T0",
                        republish="correct", scored=False),
    "detection":   dict(lookback=20,  directional=True,  basis="price_at_publication",
                        republish="ignore"),
    "orb_break":   dict(lookback=1,   directional=True,  basis="price_at_publication",
                        republish="ignore"),
    "heavy_quiet": dict(lookback=20,  directional=False, basis="price_at_publication",
                        republish="ignore"),
    # ATLAS's long book (2026-11-10, migrations/20261110_observation_types_atlas.sql).
    # atlas_entry is a real fill priced at what was paid; lookback 252 because
    # the zone and the enterable universe it came from are gated on 02b/03b's
    # 252-session window, as signal is. atlas_decision is every refusal and
    # every watch-only would-enter, once per symbol per status per session --
    # never scored, because a refusal is not a call.
    "atlas_entry":    dict(lookback=252, directional=True, basis="price_at_publication",
                           republish="ignore"),
    "atlas_decision": dict(lookback=252, directional=True, basis="price_at_publication",
                           republish="ignore", scored=False),
}

for _spec in TYPES.values():
    _spec.setdefault("scored", True)

# The types the T+3 scorer must never score. The migration's
# observation_scores_scorable() trigger carries the same list, and
# tests/test_observations.py fails if the two disagree.
UNSCORED_TYPES = frozenset(t for t, s in TYPES.items() if not s["scored"])

HORIZON_SESSIONS = 3


def _direction(d):
    """LONG / SHORT, or None. radar says up/down; everyone else says LONG/SHORT."""
    s = str(d or "").strip().upper()
    return {"LONG": "LONG", "SHORT": "SHORT", "UP": "LONG", "DOWN": "SHORT"}.get(s)


def _jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, float) and v != v:          # NaN is not JSON
        return None
    if hasattr(v, "item"):                       # numpy scalar
        return _jsonable(v.item())
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v


def values_hash(direction, reference_price, computed_values) -> str:
    canon = json.dumps({"d": direction, "p": reference_price,
                        "v": computed_values}, sort_keys=True,
                       separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


def natural_key(observation_type, session_date, symbol, direction=None,
                qualifier="") -> str:
    """What makes two publishes the same observation.

    session | symbol | direction [| qualifier]. The qualifier separates what a
    type can publish twice in one session for one symbol -- a signal and a
    candidate never share a type, so for most types it is empty.
    """
    parts = [str(session_date)[:10], str(symbol), direction or "-"]
    if qualifier:
        parts.append(str(qualifier))
    return "|".join(parts)


def build(observation_type, *, symbol, session_date, direction=None,
          reference_price=None, computed_values, source_file, qualifier="",
          engine_sha=None, published_at=None) -> dict:
    """One ledger row. Raises on a type this ledger does not know -- a typo here
    must stop the publisher in testing, not write a row the CHECK refuses."""
    spec = TYPES[observation_type]
    d = _direction(direction) if spec["directional"] else None
    try:
        ref = float(reference_price) if reference_price not in (None, "") else None
        if ref is not None and not ref > 0:
            ref = None
    except (TypeError, ValueError):
        ref = None
    cv = _jsonable(dict(computed_values or {}))
    return {
        "observation_type": observation_type,
        "natural_key": natural_key(observation_type, session_date, symbol, d, qualifier),
        "symbol": str(symbol),
        "direction": d,
        "session_date": str(session_date)[:10],
        "published_at": (published_at or datetime.now(timezone.utc)).isoformat(),
        "reference_price": ref,
        "reference_basis": spec["basis"],
        "horizon_sessions": HORIZON_SESSIONS,
        "lookback_window": spec["lookback"],
        "source_file": source_file,
        "engine_sha": engine_sha,
        "computed_values": cv,
        "values_sha256": values_hash(d, ref, cv),
    }


def from_rows(observation_type, rows, *, source_file, date_key, symbol_key="symbol",
              direction_key="direction", price_key=None, key_direction=True,
              engine_sha=None) -> list:
    """Ledger rows from the exact dicts a publisher is about to send. The whole
    published row is computed_values, so the ledger holds what the page shows.

    key_direction=False for a table whose own unique key has no direction
    (radar_events is unique on symbol, event_date): a re-run that flips the
    direction is then a correction, not a second original.
    """
    out = []
    for r in rows:
        d = r.get(direction_key) if direction_key else None
        row = build(observation_type, symbol=r[symbol_key],
                    session_date=r[date_key], direction=d,
                    reference_price=r.get(price_key) if price_key else None,
                    computed_values=r, source_file=source_file,
                    engine_sha=engine_sha)
        if not key_direction:
            row["natural_key"] = natural_key(observation_type, row["session_date"],
                                             row["symbol"])
        out.append(row)
    return out


# ── TRANSPORT ─────────────────────────────────────────────────────

def _cfg():
    url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    return url, key


def _headers(key, extra=None):
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json"}
    h.update(extra or {})
    return h


def _existing(url, key, observation_type, session_date) -> list:
    """Every ledger row for one type and session, paged. Raises on failure --
    a partial read would make a changed row look new."""
    import requests
    out, offset = [], 0
    while True:
        r = requests.get(
            f"{url}/rest/v1/{TABLE}",
            params={"observation_type": f"eq.{observation_type}",
                    "session_date": f"eq.{session_date}",
                    "select": "observation_id,natural_key,values_sha256,"
                              "corrects_observation_id,recorded_at",
                    "order": "recorded_at.asc,observation_id.asc"},
            headers=_headers(key, {"Range-Unit": "items",
                                   "Range": f"{offset}-{offset + PAGE - 1}"}),
            timeout=TIMEOUT)
        if r.status_code not in (200, 206):
            raise RuntimeError(f"read {observation_type} {session_date}: "
                               f"HTTP {r.status_code} {r.text[:160]}")
        rows = r.json()
        out.extend(rows)
        if len(rows) < PAGE:
            return out
        offset += PAGE


def plan(batch: list, existing: list) -> list:
    """Which rows of `batch` to insert, given what the ledger already holds.

    Pure, so the decision is testable without a database:
      new natural key                         -> an original
      known key, same values as in force      -> nothing
      known key, different values, 'correct'  -> a correction of the original
      known key, different values, 'ignore'   -> nothing (first sighting holds)
    """
    originals, current = {}, {}
    for e in existing:                       # oldest first
        k = e["natural_key"]
        if not e.get("corrects_observation_id"):
            originals[k] = e["observation_id"]
        current[k] = e["values_sha256"]      # the latest version wins
    out, seen = [], set()
    for row in batch:
        k = row["natural_key"]
        if k in seen:                        # the same key twice in one batch
            continue
        seen.add(k)
        if k not in originals:
            # EVERY ROW CARRIES THE SAME KEYS (D-083). PostgREST refuses a bulk
            # insert whose objects differ in their keys (PGRST102, "All object
            # keys must match"). Originals lacked the two correction fields that
            # corrections carry, so any batch mixing a new symbol with a changed
            # one was refused whole -- the 2026-11-10 holiday run's watchlist.
            out.append({**row, "corrects_observation_id": None,
                        "correction_reason": None})
            continue
        if current.get(k) == row["values_sha256"]:
            continue
        if TYPES[row["observation_type"]]["republish"] == "correct":
            out.append({**row, "corrects_observation_id": originals[k],
                        "correction_reason": "republished with different values"})
    return out


def record(rows: list, logger=None) -> bool:
    """Persist observations. True only when every row is READ BACK as stored.

    Never raises. A False means: do not publish.
    """
    lg = logger or log
    if not rows:
        return True
    from engine import ledger_alerts

    def refuse(msg, which):
        # Fail-closed is correct; fail-closed and SILENT is the defect. Every
        # refusal is logged and sent to Telegram (engine/ledger_alerts.py).
        lg.error(msg)
        ledger_alerts.refused(which, msg)
        return False

    url, key = _cfg()
    if not key:
        return refuse("observation ledger: SUPABASE_SERVICE_KEY is not set — "
                      "refusing to publish what cannot be recorded", rows)
    import requests
    groups = {}
    for r in rows:
        groups.setdefault((r["observation_type"], r["session_date"]), []).append(r)
    batch, inserted = rows, []
    try:
        for (otype, sess), batch in groups.items():
            todo = plan(batch, _existing(url, key, otype, sess))
            if todo:
                resp = requests.post(f"{url}/rest/v1/{TABLE}",
                                     headers=_headers(key, {"Prefer": "return=minimal"}),
                                     json=todo, timeout=TIMEOUT)
                # 409: a concurrent writer stored the same original first. The
                # read-back below decides whether that is fine.
                if resp.status_code not in (200, 201, 204, 409):
                    return refuse(f"observation ledger: {otype} {sess}: HTTP "
                                  f"{resp.status_code} {resp.text[:200]}", batch)
                if resp.status_code != 409:
                    inserted.extend(todo)
            # READ BACK. requests.post does not raise on 4xx, and this repo has
            # twice shipped a payload PostgREST discarded whole.
            after = _existing(url, key, otype, sess)
            stored = {e["natural_key"] for e in after}
            missing = sorted({b["natural_key"] for b in batch} - stored)
            if missing:
                return refuse(f"observation ledger: {otype} {sess}: {len(missing)} "
                              f"row(s) not stored, e.g. {missing[:3]}", batch)
            # A 'correct' type must have the values being published IN FORCE,
            # not merely some earlier version under the same key.
            if TYPES[otype]["republish"] == "correct":
                current = {}
                for e in after:
                    current[e["natural_key"]] = e["values_sha256"]
                stale = sorted(b["natural_key"] for b in batch
                               if current.get(b["natural_key"]) != b["values_sha256"])
                if stale:
                    return refuse(f"observation ledger: {otype} {sess}: {len(stale)} "
                                  f"row(s) stored with other values, e.g. {stale[:3]}",
                                  batch)
    except Exception as e:
        return refuse(f"observation ledger unavailable ({type(e).__name__}: {e}) — "
                      f"refusing to publish", batch)
    if inserted:
        ledger_alerts.first_success(inserted, url, key)
    return True
