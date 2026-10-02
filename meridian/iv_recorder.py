"""
MERIDIAN — the IV recorder.

    python3 -m meridian.iv_recorder --label close      # 15:45 IST
    python3 -m meridian.iv_recorder --label eod        # 18:35 IST
    python3 -m meridian.iv_recorder --dry-run --limit 6

WHY THIS IS THE FIRST THING BUILT. IV percentile needs a year of daily history and
no API sells that retrospectively. Every other part of MERIDIAN can be built in a
week whenever; this one cannot be caught up, so it starts accumulating while the
rest is still a document.

ONE CALL PER UNDERLYING. Upstox /option/chain returns the whole chain for one
expiry -- every strike, both sides, with iv, greeks, oi, volume,
underlying_spot_price and pcr already computed. 217 underlyings (4 indices + 213
F&O stocks as of 2026-10-02) is 217 calls. Against the Standard API limits of
50/sec, 500/min and 2000/30min, the PER-MINUTE one binds, so the pacing targets
300/min rather than the 50/sec headline and the run takes about 45 seconds.

TWO THINGS MAKE IT FAIL LOUDLY, because one is not enough
---------------------------------------------------------
  this module      writes a meridian_recorder_runs row on EVERY invocation, exits
                   non-zero on failure, and alerts when fewer than 90% of
                   expected underlyings are written.
  the engine report  checks independently that meridian_iv_daily has a recent
                   trade_date, and fires when it does not.

The second is the one that matters. The failure to prevent is "wrote nothing for
three months", and a recorder that never starts cannot report its own absence.

THE RUN TIME IS NOT YET SETTLED, AND snapshot_taken_at IS WHY THAT IS SURVIVABLE.
15:45 IST reflects the close; 18:35 follows the EOD chain; open interest settles
somewhere between. Both run for one session and are compared. Every row carries
the instant it was fetched, so if the choice turns out wrong the history stays
interpretable rather than being a silently drifting mixture.
"""

import sys
import json
import time
import math
import logging
import argparse
from datetime import datetime, timezone, timedelta

import requests

from meridian.config import (
    SUPABASE_URL, SUPABASE_KEY, UPSTOX_BASE, UPSTOX_TOKEN_FILE, INDIA_VIX_KEY,
    REQ_PER_SEC, MAX_RETRIES, RETRY_BACKOFF_S, HTTP_TIMEOUT, STRIKE_BAND,
    MIN_WRITE_RATIO, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from meridian.fno_universe import resolve_universe, UniverseUnavailable

log = logging.getLogger("MERIDIAN-IV")
IST = timezone(timedelta(hours=5, minutes=30))


class TokenUnavailable(Exception):
    """No Upstox token. Nothing can be fetched; the run fails rather than writes 0."""


# ══════════════════════════════════════════════════════════════════
# PLUMBING
# ══════════════════════════════════════════════════════════════════

def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def upstox_headers() -> dict:
    """Bearer header from the shared token file. READ ONLY.

    The equity price feed owns this file and refreshes it. Meridian must never
    write it: a bad rewrite here is an outage in the other vertical, and that is
    the single way this recorder could damage ATLAS.
    """
    try:
        data = json.loads(UPSTOX_TOKEN_FILE.read_text())
    except Exception as e:
        raise TokenUnavailable(f"{UPSTOX_TOKEN_FILE} unreadable: "
                               f"{type(e).__name__}: {e}") from e
    tok = data.get("access_token")
    if not tok:
        raise TokenUnavailable(f"{UPSTOX_TOKEN_FILE} has no access_token")
    return {"Authorization": f"Bearer {tok}", "Accept": "application/json"}


def alert(title: str, body: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning(f"ALERT (telegram not configured) {title}: {body}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"🔷 <b>MERIDIAN — {title}</b>\n{body}",
                  "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"alert not sent: {e}")


class Pacer:
    """Keeps the run under 300 req/min without sleeping more than it must."""

    def __init__(self, per_sec: float):
        self.min_gap = 1.0 / float(per_sec)
        self.last = 0.0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last = time.monotonic()


# ══════════════════════════════════════════════════════════════════
# FETCH
# ══════════════════════════════════════════════════════════════════

def fetch_chain(key: str, expiry_keyword: str, headers: dict, pacer: Pacer) -> list:
    """One expiry's full chain. Raises on exhausted retries.

    429 and 5xx are retried with backoff; a 4xx that is not 429 is NOT -- a bad
    instrument key or an expiry keyword the underlying does not have answers
    identically every time, and spending three attempts to learn that delays the
    other 216 symbols.
    """
    url = (f"{UPSTOX_BASE}/option/chain"
           f"?instrument_key={requests.utils.quote(key, safe='')}"
           f"&expiry_date={expiry_keyword}")
    last = ""
    for attempt in range(MAX_RETRIES):
        pacer.wait()
        try:
            r = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT)
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                body = r.json()
                data = body.get("data")
                if not isinstance(data, list):
                    raise RuntimeError(f"no data array: {str(body)[:140]}")
                return data
            last = f"HTTP {r.status_code} {r.text[:160]}"
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(last)
        if attempt < MAX_RETRIES - 1:
            time.sleep(RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)])
    raise RuntimeError(f"exhausted {MAX_RETRIES} attempts: {last}")


def fetch_india_vix(headers: dict, pacer: Pacer):
    """India VIX, or None. Recorded on every row because it is the market-wide
    reference an IV percentile is read against; a missing one is alerted, not
    silently zeroed -- zero would look like a dead-calm market."""
    pacer.wait()
    try:
        r = requests.get(
            f"{UPSTOX_BASE}/market-quote/quotes"
            f"?instrument_key={requests.utils.quote(INDIA_VIX_KEY, safe='')}",
            headers=headers, timeout=HTTP_TIMEOUT)
        if r.status_code != 200:
            log.warning(f"India VIX HTTP {r.status_code} {r.text[:120]}")
            return None
        for _, q in (r.json().get("data") or {}).items():
            v = q.get("last_price")
            if v:
                return float(v)
    except Exception as e:
        log.warning(f"India VIX failed: {type(e).__name__}: {e}")
    return None


# ══════════════════════════════════════════════════════════════════
# PARSE
# ══════════════════════════════════════════════════════════════════

def _f(d, *path):
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    try:
        v = float(cur)
    except (TypeError, ValueError):
        return None
    return None if (v != v or math.isinf(v)) else v


def parse_chain(rows: list, symbol: str) -> dict:
    """{daily, strikes} from one chain response.

    ATM is the strike nearest the spot, not the strike with the most OI -- the
    latter is a sentiment measure and moves for reasons that have nothing to do
    with where the money actually is, which would make the IV series answer a
    different question on different days.

    The band is ATM +/- STRIKE_BAND by POSITION in the sorted strike list rather
    than by price distance, so it adapts to each underlying's strike interval
    without a per-symbol table.
    """
    strikes = []
    for row in rows:
        sp = _f(row, "strike_price")
        if sp is None:
            continue
        strikes.append((sp, row))
    if not strikes:
        raise RuntimeError("chain carried no strikes")
    strikes.sort(key=lambda x: x[0])

    spot = None
    for _, row in strikes:
        spot = (_f(row, "underlying_spot_price")
                or _f(row, "call_options", "market_data", "underlying_spot_price"))
        if spot:
            break
    if not spot:
        raise RuntimeError("chain carried no underlying_spot_price")

    atm_i = min(range(len(strikes)), key=lambda i: abs(strikes[i][0] - spot))
    atm_strike, atm_row = strikes[atm_i]

    expiry = None
    for _, row in strikes:
        expiry = row.get("expiry")
        if expiry:
            break

    def side(row, which):
        return {
            "iv":     _f(row, which, "option_greeks", "iv"),
            "oi":     _f(row, which, "market_data", "oi"),
            "volume": _f(row, which, "market_data", "volume"),
            "ltp":    _f(row, which, "market_data", "ltp"),
            "delta":  _f(row, which, "option_greeks", "delta"),
            "gamma":  _f(row, which, "option_greeks", "gamma"),
            "theta":  _f(row, which, "option_greeks", "theta"),
            "vega":   _f(row, which, "option_greeks", "vega"),
        }

    lo = max(0, atm_i - STRIKE_BAND)
    hi = min(len(strikes), atm_i + STRIKE_BAND + 1)
    band = []
    for i in range(lo, hi):
        sp, row = strikes[i]
        c, p = side(row, "call_options"), side(row, "put_options")
        band.append({"strike": sp, "strike_offset": i - atm_i, "spot": spot,
                     **{f"call_{k}": v for k, v in c.items()},
                     **{f"put_{k}": v for k, v in p.items()}})

    ac = _f(atm_row, "call_options", "option_greeks", "iv")
    ap = _f(atm_row, "put_options", "option_greeks", "iv")
    # Both legs stored separately so the blend can be recomputed later without
    # re-fetching a year of chains.
    atm_iv = (ac + ap) / 2 if (ac and ap) else (ac or ap)

    pcr = None
    for _, row in strikes:
        pcr = _f(row, "pcr")
        if pcr is not None:
            break

    return {
        "daily": {
            "spot": round(spot, 2),
            "expiry_date": expiry,
            "atm_strike": atm_strike,
            "atm_iv": round(atm_iv, 4) if atm_iv else None,
            "atm_call_iv": round(ac, 4) if ac else None,
            "atm_put_iv": round(ap, 4) if ap else None,
            "pcr": round(pcr, 4) if pcr is not None else None,
            "total_call_oi": sum(_f(r, "call_options", "market_data", "oi") or 0
                                 for _, r in strikes),
            "total_put_oi": sum(_f(r, "put_options", "market_data", "oi") or 0
                                for _, r in strikes),
            "strikes_recorded": len(band),
        },
        "strikes": band,
    }


# ══════════════════════════════════════════════════════════════════
# WRITE
# ══════════════════════════════════════════════════════════════════

def _upsert(table: str, rows: list, conflict: str) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 500):
        chunk = rows[i:i + 500]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}",
            headers={**_sb_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=chunk, timeout=60)
        if r.status_code not in (200, 201, 204):
            raise RuntimeError(f"{table} write failed: HTTP {r.status_code} "
                               f"{r.text[:200]}")
        written += len(chunk)
    return written


def write_run(rec: dict) -> None:
    """Run accounting. Best-effort, and loud if it fails -- but never raises,
    because a failure to record the run must not also lose the data the run
    just wrote."""
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/meridian_recorder_runs",
                          headers={**_sb_headers(), "Prefer": "return=minimal"},
                          json=rec, timeout=20)
        if r.status_code not in (200, 201, 204):
            log.error(f"run record NOT written: HTTP {r.status_code} "
                      f"{r.text[:200]}")
    except Exception as e:
        log.error(f"run record NOT written: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════════
# RUN
# ══════════════════════════════════════════════════════════════════

def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [MERIDIAN-IV] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="eod",
                    help="close (15:45 IST) or eod (18:35 IST). Both run for one "
                         "session so the timing can be compared before it is fixed.")
    ap.add_argument("--limit", type=int, default=0,
                    help="first N underlyings only, for a smoke test")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and parse, write nothing")
    a = ap.parse_args()

    started = datetime.now(timezone.utc)
    trade_date = datetime.now(IST).date().isoformat()
    run = {"run_label": a.label, "trade_date": trade_date,
           "started_at": started.isoformat(), "status": "FAILED",
           "underlyings_expected": 0, "underlyings_written": 0,
           "strikes_written": 0, "api_calls": 0, "failures": [], "list_diff": {}}

    try:
        headers = upstox_headers()
    except TokenUnavailable as e:
        # FAILS, does not write zero. A run that records 0 of 217 looks identical
        # to a quiet market in any later query; a FAILED row with a reason does not.
        log.error(f"no Upstox token: {e}")
        run.update(status="FAILED", notes=f"token unavailable: {e}",
                   finished_at=datetime.now(timezone.utc).isoformat())
        if not a.dry_run:
            write_run(run)
        alert("RECORDER FAILED", f"No Upstox token — nothing recorded for "
                                f"{trade_date}. {e}")
        return 1

    try:
        underlyings, diff = resolve_universe()
    except UniverseUnavailable as e:
        log.error(f"no universe: {e}")
        run.update(status="FAILED", notes=f"universe unavailable: {e}",
                   finished_at=datetime.now(timezone.utc).isoformat())
        if not a.dry_run:
            write_run(run)
        alert("RECORDER FAILED", f"F&O universe could not be resolved — nothing "
                                f"recorded for {trade_date}. {e}")
        return 1

    if a.limit:
        underlyings = underlyings[:a.limit]
    run["list_diff"] = diff
    run["underlyings_expected"] = len(underlyings)
    log.info(f"[{a.label}] {len(underlyings)} underlyings "
             f"({sum(1 for u in underlyings if u['kind'] == 'index')} indices), "
             f"pacing {REQ_PER_SEC}/s")

    pacer = Pacer(REQ_PER_SEC)
    vix = fetch_india_vix(headers, pacer)
    run["api_calls"] += 1
    if vix is None:
        log.warning("India VIX unavailable — rows will carry NULL, not 0")
    else:
        log.info(f"India VIX {vix}")
    run["india_vix"] = vix

    daily_rows, strike_rows, failures = [], [], []
    snap = datetime.now(timezone.utc).isoformat()

    for n, u in enumerate(underlyings, 1):
        try:
            chain = fetch_chain(u["key"], u["expiry"], headers, pacer)
            run["api_calls"] += 1
            parsed = parse_chain(chain, u["symbol"])
        except Exception as e:
            failures.append({"symbol": u["symbol"], "kind": u["kind"],
                             "error": f"{type(e).__name__}: {str(e)[:160]}"})
            log.warning(f"{u['symbol']}: {type(e).__name__}: {str(e)[:120]}")
            continue

        d = parsed["daily"]
        dte = None
        if d["expiry_date"]:
            try:
                dte = (datetime.fromisoformat(str(d["expiry_date"])[:10]).date()
                       - datetime.now(IST).date()).days
            except Exception:
                dte = None
        daily_rows.append({
            "trade_date": trade_date, "underlying_symbol": u["symbol"],
            "underlying_key": u["key"], "kind": u["kind"],
            "india_vix": vix, "days_to_expiry": dte,
            "expiry_keyword": u["expiry"], "snapshot_taken_at": snap, **d})
        for s in parsed["strikes"]:
            strike_rows.append({
                "trade_date": trade_date, "underlying_symbol": u["symbol"],
                "expiry_date": d["expiry_date"], "snapshot_taken_at": snap, **s})

        if n % 50 == 0:
            log.info(f"  {n}/{len(underlyings)} — {len(daily_rows)} recorded, "
                     f"{len(failures)} failed")

    run["failures"] = failures[:80]
    dur = (datetime.now(timezone.utc) - started).total_seconds()

    if a.dry_run:
        log.info(f"DRY RUN — would write {len(daily_rows)} daily, "
                 f"{len(strike_rows)} strike rows; {len(failures)} failures; "
                 f"{run['api_calls']} API calls in {dur:.1f}s")
        for row in daily_rows[:6]:
            log.info(f"  {row['underlying_symbol']:<12} spot {row['spot']:>10} "
                     f"ATM {row['atm_strike']:>9} IV {row['atm_iv']} "
                     f"exp {row['expiry_date']} dte {row['days_to_expiry']} "
                     f"strikes {row['strikes_recorded']}")
        return 0

    try:
        nd = _upsert("meridian_iv_daily", daily_rows,
                     "trade_date,underlying_symbol")
        ns = _upsert("meridian_iv_strikes", strike_rows,
                     "trade_date,underlying_symbol,expiry_date,strike")
    except Exception as e:
        log.error(f"write failed: {e}")
        run.update(status="FAILED", notes=f"write failed: {str(e)[:300]}",
                   duration_s=round(dur, 1),
                   finished_at=datetime.now(timezone.utc).isoformat())
        write_run(run)
        alert("RECORDER FAILED", f"{len(daily_rows)} underlyings fetched for "
                                f"{trade_date} but the write failed: {str(e)[:200]}")
        return 1

    ratio = nd / max(len(underlyings), 1)
    status = "OK" if ratio >= MIN_WRITE_RATIO else "PARTIAL"
    run.update(status=status, underlyings_written=nd, strikes_written=ns,
               duration_s=round(dur, 1),
               finished_at=datetime.now(timezone.utc).isoformat())
    write_run(run)

    log.info(f"[{a.label}] {status}: {nd}/{len(underlyings)} underlyings, "
             f"{ns} strike rows, {len(failures)} failures, "
             f"{run['api_calls']} API calls, {dur:.1f}s")

    if status != "OK":
        alert("RECORDER PARTIAL",
              f"{trade_date} [{a.label}]: only {nd} of {len(underlyings)} "
              f"underlyings recorded ({ratio*100:.0f}%). "
              f"First failures: "
              + "; ".join(f"{f['symbol']} {f['error'][:50]}"
                          for f in failures[:4]))
        return 1
    if vix is None:
        alert("RECORDER OK, NO VIX",
              f"{trade_date} [{a.label}]: {nd} underlyings recorded but India VIX "
              f"was unavailable. Rows carry NULL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
