#!/usr/bin/env python3
"""
THE STOCK LOGIC — score the observation ledger at T+3.

Runs after the close, after 01b has ingested the session's bhavcopy. Finds every
original observation whose T+3 session has arrived and is not yet scored, and
appends ONE row to observation_scores: a net move, or a reason there is none.

T+3 IS THREE NSE TRADING SESSIONS, resolved by engine.trading_calendar from the
fetched holiday master. Never date arithmetic. A Friday observation scores
against Wednesday; a window that crosses a holiday gets LONGER, because the
holiday is not a session. tests/test_observations.py pins both, and proves each
assertion fails against the two wrong implementations a person would write.

"HAS ARRIVED" MEANS THE DATA HAS, not the clock. T+3 is due only once a bhavcopy
for that session is in the parquets -- the newest bar across the universe is the
evidence the ingest ran. A clock-based "due" would score against a missing bar
the evening 01b runs late.

A NUMBER ONLY WHERE A NUMBER IS TRUE. Otherwise a skip_reason, and every reason
that applied goes in skip_detail; skip_reason is the first in SKIP_ORDER.

  not_a_trading_session     T0 itself was not an NSE session
  not_directional           the observation claimed no direction
  no_price_data             no bar at T0 (or no reference price) or at T+3
  lookback_too_short        fewer bars up to T0 than the type's lookback
  price_basis_discontinuity engine.price_basis flags a date inside the
                            lookback or the scored window
  result_in_window          a financial-results board meeting in [T0, T+3]
  results_calendar_unavailable  the results check could not be made
  gsm_asm                   on NSE's GSM or ASM list on a session in the window
  surveillance_unknown      a session in the window has no surveillance
                            snapshot -- unknown is not clean

The last two pairs are the house rule that absence of data is not absence of
news: a check that did not run is recorded as not run, never as passed.

NO HIT RATE IS COMPUTED HERE, and nothing here averages outcomes.
"""

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import trading_calendar as tc                    # noqa: E402
from engine.observations import TYPES, UNSCORED_TYPES        # noqa: E402

log = logging.getLogger("score-observations")
IST = timezone(timedelta(hours=5, minutes=30))

HORIZON = 3
# THE ROUND-TRIP FLOOR, as specified by the operator. atlas/risk/costs.py
# estimates 0.22% for a Rs10k delivery long and 0.11% for an intraday short on
# rates it marks UNVERIFIED; this is a single floor applied to every
# observation, so the cost basis never depends on which product a reader
# imagines. Recorded on every score row as cost_basis.
ROUND_TRIP_FLOOR_PCT = 0.23
COST_BASIS = "round_trip_floor_0.23pct"

STOCKS_DIR = ROOT / "data/processed/stocks"
EVENT_CALENDAR = ("https://www.nseindia.com/api/event-calendar"
                  "?index=equities&from_date={frm}&to_date={to}")

SKIP_ORDER = ["not_a_trading_session", "not_directional", "no_price_data",
              "lookback_too_short", "price_basis_discontinuity",
              "result_in_window", "results_calendar_unavailable",
              "gsm_asm", "surveillance_unknown"]


# ── THE CALENDAR ──────────────────────────────────────────────────

def t_plus(session: date, n: int = HORIZON):
    """The n-th NSE trading session after `session`. None if the calendar cannot
    resolve it -- an uncovered year, or no cache at all. Never a guess."""
    days = tc.next_n_trading_days(session, n)
    if len(days) != n:
        return None
    return days[-1]


def window_sessions(t0: date, t3: date) -> list:
    """T0 and every trading session after it up to and including T+3."""
    out, d = [t0], t0
    while d < t3:
        d += timedelta(days=1)
        if tc.is_trading_day(d):
            out.append(d)
    return out


# ── EXTERNAL CHECKS ───────────────────────────────────────────────

def load_surveillance(sessions, store=None) -> dict:
    """{session: set(symbols) | None} from surveillance_snapshots.

    None unless surveillance_captures holds an OK capture for that session --
    "captured and nobody listed" and "never captured" must not look alike, so
    the rows alone are not evidence. Anything that cannot be read is None,
    never an empty set: unknown is not clean.
    """
    from engine.surveillance_snapshot import Store
    try:
        store = store or Store()
    except Exception:
        return {s: None for s in sessions}
    out = {}
    for s in sessions:
        try:
            out[s] = ({k[0] for k in store.keys(s)} if store.captured(s) else None)
        except Exception as e:
            log.warning(f"surveillance for {s} unreadable ({e}) — unknown")
            out[s] = None
    return out


def fetch_results_calendar(frm: date, to: date):
    """{symbol: [dates]} of financial-results board meetings in [frm, to], or
    None when the check could not be made. Reads NSE's event calendar after
    the fact, so a rescheduled meeting appears at the date it was held."""
    import time
    import requests
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120 Safari/537.36")
    s = requests.Session()
    s.headers.update({"User-Agent": ua, "Accept": "application/json,text/plain,*/*"})
    try:
        s.get("https://www.nseindia.com", timeout=30)
        time.sleep(0.5)
        r = s.get(EVENT_CALENDAR.format(frm=frm.strftime("%d-%m-%Y"),
                                        to=to.strftime("%d-%m-%Y")), timeout=40)
        if r.status_code != 200:
            log.error(f"event calendar: HTTP {r.status_code}")
            return None
        rows = r.json()
        if not isinstance(rows, list):
            log.error("event calendar: payload is not a list")
            return None
    except Exception as e:
        log.error(f"event calendar unavailable: {type(e).__name__}: {e}")
        return None
    return results_by_symbol(rows)


def results_by_symbol(rows) -> dict:
    """Financial-results meetings only. A rescheduled meeting counts when its
    description mentions results: over-skipping one window is the safe error."""
    out = {}
    for x in rows:
        purpose = str(x.get("purpose") or "")
        desc = str(x.get("bm_desc") or "")
        if not ("financial results" in purpose.lower()
                or ("reschedul" in purpose.lower() and "result" in desc.lower())):
            continue
        try:
            d = datetime.strptime(x["date"], "%d-%b-%Y").date()
        except Exception:
            continue
        out.setdefault(str(x.get("symbol")), []).append(d)
    return out


# ── PRICES ────────────────────────────────────────────────────────

def load_prices(symbol, directory=STOCKS_DIR):
    import pandas as pd
    p = Path(directory) / f"{symbol}.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.sort_values("date").reset_index(drop=True)


def latest_ingested_session(directory=STOCKS_DIR):
    """The newest bar across the universe: positive evidence of the last
    bhavcopy that actually landed, rather than a belief about the clock."""
    import pyarrow.parquet as pq
    newest = None
    for p in Path(directory).glob("*.parquet"):
        try:
            col = pq.read_table(p, columns=["date"]).column("date").to_pylist()
        except Exception:
            continue
        if col:
            m = max(col)
            m = m.date() if hasattr(m, "date") else m
            newest = m if newest is None or m > newest else newest
    return newest


# ── THE DECISION ──────────────────────────────────────────────────

def classify(obs: dict, df, t3: date, sessions: list, surveillance: dict,
             results) -> dict:
    """The score row for one observation, minus ids and timestamps. Pure.

    obs          a ledger row (the version in force)
    df           that symbol's daily bars, or None
    t3           T+3, already resolved by t_plus()
    sessions     window_sessions(T0, T+3)
    surveillance load_surveillance(sessions)
    results      {symbol: [dates]} or None if the check could not be made
    """
    from engine.price_basis import discontinuities
    t0 = date.fromisoformat(str(obs["session_date"])[:10])
    sym = obs["symbol"]
    spec = TYPES[obs["observation_type"]]
    reasons, detail = [], []

    if not tc.is_trading_day(t0):
        reasons.append("not_a_trading_session")
        detail.append(f"{t0} is not an NSE session")
    if not obs.get("direction"):
        reasons.append("not_directional")

    bars = {} if df is None else dict(zip(df["date"], df["close"].astype(float)))
    if obs["reference_basis"] == "close_T0":
        ref = bars.get(t0)
    else:
        ref = obs.get("reference_price")
        ref = float(ref) if ref else None
    exit_px = bars.get(t3)
    if ref is None or exit_px is None:
        reasons.append("no_price_data")
        detail.append(f"reference {'missing' if ref is None else 'ok'}, "
                      f"T+3 {t3} bar {'missing' if exit_px is None else 'ok'}")

    if df is not None:
        hist = df[df["date"] <= t0]
        if len(hist) < spec["lookback"]:
            reasons.append("lookback_too_short")
            detail.append(f"{len(hist)} bars to T0, lookback {spec['lookback']}")
        start = (hist["date"].iloc[-spec["lookback"]] if len(hist) >= spec["lookback"]
                 else (hist["date"].iloc[0] if len(hist) else t0))
        upto = df[df["date"] <= t3]
        hits = [d for d in discontinuities(upto) if start < d <= t3]
        if hits:
            reasons.append("price_basis_discontinuity")
            detail.append(f"discontinuity {hits[0]} in {start}..{t3}")

    if results is None:
        reasons.append("results_calendar_unavailable")
    else:
        inside = [d for d in results.get(sym, []) if t0 <= d <= t3]
        if inside:
            reasons.append("result_in_window")
            detail.append(f"results meeting {inside[0]}")

    flagged = [s for s in sessions if surveillance.get(s) and sym in surveillance[s]]
    unknown = [s for s in sessions if surveillance.get(s) is None]
    if flagged:
        reasons.append("gsm_asm")
        detail.append(f"GSM/ASM on {flagged[0]}")
    elif unknown:
        reasons.append("surveillance_unknown")
        detail.append(f"no snapshot for {len(unknown)} of {len(sessions)} sessions")

    row = {"t3_session": t3.isoformat(), "cost_basis": COST_BASIS,
           "reference_price": ref, "exit_price": exit_px}
    if reasons:
        reasons.sort(key=SKIP_ORDER.index)
        return {**row, "skip_reason": reasons[0], "all_reasons": reasons,
                "skip_detail": "; ".join(detail) or None,
                "move_pct": None, "cost_pct": None, "net_pct": None,
                "outcome": None}
    sign = 1.0 if obs["direction"] == "LONG" else -1.0
    move = round(sign * (exit_px / ref - 1.0) * 100.0, 4)
    net = round(move - ROUND_TRIP_FLOOR_PCT, 4)
    outcome = ("net_positive" if net > 0 else
               "net_negative" if net < 0 else "net_flat")
    return {**row, "skip_reason": None, "all_reasons": [], "skip_detail": None,
            "move_pct": move, "cost_pct": ROUND_TRIP_FLOOR_PCT, "net_pct": net,
            "outcome": outcome}


def score_batch(observations: list, as_of: date, *, prices=load_prices,
                surveillance=load_surveillance, results_fetch=fetch_results_calendar):
    """(scores, not_due, unresolvable). Writes nothing.

    One results-calendar call covers every due observation's window.
    """
    due, not_due, unresolvable = [], 0, []
    for o in observations:
        # NOT SCORED AT ALL -- not skipped, which would still put a row in the
        # accuracy record. See observations.TYPES["radar_event"].
        if o["observation_type"] in UNSCORED_TYPES:
            continue
        t0 = date.fromisoformat(str(o["session_date"])[:10])
        t3 = t_plus(t0)
        if t3 is None:
            unresolvable.append(o)
        elif t3 > as_of:
            not_due += 1
        else:
            due.append((o, t0, t3))
    if not due:
        return [], not_due, unresolvable
    results = results_fetch(min(t0 for _, t0, _ in due), max(t3 for *_, t3 in due))
    cache, out = {}, []
    all_sessions = sorted({s for _, t0, t3 in due for s in window_sessions(t0, t3)})
    surv = surveillance(all_sessions)          # one read per session, not per row
    for o, t0, t3 in due:
        if o["symbol"] not in cache:
            cache[o["symbol"]] = prices(o["symbol"])
        sess = window_sessions(t0, t3)
        out.append((o, classify(o, cache[o["symbol"]], t3, sess,
                                {s: surv.get(s) for s in sess}, results)))
    return out, not_due, unresolvable


# ── THE LEDGER ────────────────────────────────────────────────────

def _unscored(url, key) -> list:
    """Originals with no score, each with the values of its current version."""
    import requests
    h = {"apikey": key, "Authorization": f"Bearer {key}"}

    def paged(path, params):
        out, off = [], 0
        while True:
            r = requests.get(f"{url}/rest/v1/{path}", params=params, timeout=30,
                             headers={**h, "Range-Unit": "items",
                                      "Range": f"{off}-{off + 999}"})
            if r.status_code not in (200, 206):
                raise RuntimeError(f"{path}: HTTP {r.status_code} {r.text[:160]}")
            rows = r.json()
            out += rows
            if len(rows) < 1000:
                return out
            off += 1000

    roots = paged("v_observations", {
        "scored": "is.false", "select": "*", "order": "session_date.asc",
        "observation_type": f"not.in.({','.join(sorted(UNSCORED_TYPES))})"})
    need = [r["current_version_id"] for r in roots
            if r["current_version_id"] != r["observation_id"]]
    current = {}
    for i in range(0, len(need), 100):
        ids = ",".join(need[i:i + 100])
        for r in paged("observations", {"observation_id": f"in.({ids})", "select": "*"}):
            current[r["observation_id"]] = r
    out = []
    for r in roots:
        v = current.get(r["current_version_id"], r)
        out.append({**v, "root_id": r["observation_id"],
                    "version_id": r["current_version_id"]})
    return out


def run(dry: bool = False) -> int:
    from engine.observations import _cfg
    from engine.provenance import engine_sha
    url, key = _cfg()
    # THE SECOND ALARM, FIRST. The snapshot job's close run raises its own; this
    # is a separate cron line in a separate process, so a snapshot job that
    # never fired at all is still caught. Before the ledger work, so it fires
    # even while the ledger itself is not live.
    today = datetime.now(IST).date()
    if tc.is_trading_day(today):
        from engine.surveillance_snapshot import check
        check(today, raised_by="the T+3 scorer (19:40 IST)")
    if not key:
        log.error("SUPABASE_SERVICE_KEY is not set")
        return 1
    if not tc.calendar_is_readable():
        log.error("trading calendar unreadable — T+3 cannot be resolved, nothing scored")
        return 1
    as_of = latest_ingested_session()
    if as_of is None:
        log.error("no parquet bars found — cannot tell which sessions have landed")
        return 1
    obs = _unscored(url, key)
    scores, not_due, unresolvable = score_batch(obs, as_of)
    by_reason = {}
    for _, s in scores:
        if s["skip_reason"]:
            by_reason[s["skip_reason"]] = by_reason.get(s["skip_reason"], 0) + 1
    n_skip = sum(by_reason.values())
    log.info(f"as of {as_of}: {len(obs)} unscored, {not_due} not due, "
             f"{len(unresolvable)} unresolvable, {len(scores) - n_skip} scored, "
             f"{n_skip} skipped {by_reason}")
    if dry:
        return 0
    import requests
    sha = engine_sha()
    rows = [{"observation_id": o["root_id"], "scored_version_id": o["version_id"],
             "engine_sha": sha,
             **{k: v for k, v in s.items() if k != "all_reasons"}}
            for o, s in scores]
    h = {"apikey": key, "Authorization": f"Bearer {key}",
         "Content-Type": "application/json", "Prefer": "return=minimal"}
    errors = 0
    for i in range(0, len(rows), 500):
        r = requests.post(f"{url}/rest/v1/observation_scores", headers=h,
                          json=rows[i:i + 500], timeout=60)
        if r.status_code not in (200, 201, 204):
            errors += 1
            log.error(f"observation_scores: HTTP {r.status_code} {r.text[:200]}")
    r = requests.post(f"{url}/rest/v1/observation_scorer_runs", headers=h, timeout=30,
                      json={"as_of_session": as_of.isoformat(), "considered": len(obs),
                            "not_due": not_due, "scored": len(scores) - n_skip,
                            "skipped": n_skip, "skipped_by_reason": by_reason,
                            "errors": errors + len(unresolvable), "engine_sha": sha})
    if r.status_code not in (200, 201, 204):
        log.error(f"observation_scorer_runs: HTTP {r.status_code} {r.text[:200]}")
        return 1
    return 1 if errors else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(run(dry="--dry" in sys.argv))
