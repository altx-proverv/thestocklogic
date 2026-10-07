"""
THE STOCK LOGIC — Push signals to Supabase
==========================================
Runs after 03b_score.py — reads today's signals
and upserts them into Supabase.

Run: python3 engine/06_push_supabase.py
"""

# ── RUN-ANYWHERE BOOTSTRAP ──────────────────────────────────────────
# cron invokes this script BY FILE PATH:
#     cd /home/ubuntu/thestocklogic && venv/bin/python3 06_push_supabase.py
# Python then puts the SCRIPT'S OWN directory (engine/) on sys.path, not the repo
# root, so `import engine.paths` cannot resolve and the module dies before it
# opens its log file -- which leaves no log behind. Contrast
# `python -m engine.x`, which puts the working directory on the path.
#
# This happened on 2026-10-05 (6125216) and killed the nightly chain for three
# sessions: the chain is &&-joined, so 06_push and mark_signals never ran either.
# The repo root goes on the path FIRST, before any package import, so the script
# works however it is invoked.
import sys as _sys, pathlib as _pathlib
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1]))
# ────────────────────────────────────────────────────────────────────


import os, sys, json, logging, warnings
from pathlib import Path
from datetime import date, datetime, timezone
import pandas as pd
import requests

# Measurement yardstick (2R/3R off the structural stop). Single definition --
# update_outcomes.py and trade_review.py import the same helper.
try:
    from engine.zone_entry import measurement_targets
except ModuleNotFoundError:
    from zone_entry import measurement_targets

# NOT a blanket ignore. This line used to be warnings.filterwarnings("ignore"),
# which suppressed FutureWarning along with everything else -- so pandas spent a
# year telling us that assigning int64 values into an int16 column would stop
# working, and this module silenced it. On 2026-10-02 pandas 3 on the box turned
# that warning into a TypeError, 03b crashed, and nothing published.
#
# Deprecations are the one category that must stay audible: they are the only
# advance notice that a library upgrade will break the pipeline. Numeric noise is
# suppressed by name instead.
warnings.filterwarnings("ignore", category=RuntimeWarning)
warnings.filterwarnings("ignore", message="invalid value encountered")
warnings.filterwarnings("ignore", message="divide by zero encountered")
warnings.filterwarnings("ignore", message="Mean of empty slice")
warnings.filterwarnings("ignore", message="All-NaN slice encountered")
warnings.filterwarnings("ignore", message="numpy.ndarray size changed")

from engine.provenance import engine_sha          # noqa: E402

# Resolved once per process, not per row: the SHA cannot change mid-run, and a
# subprocess per row would be absurd.
ENGINE_SHA = engine_sha()
RUN_AT = datetime.now(timezone.utc).isoformat()
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────
SUPABASE_URL     = os.environ.get("SUPABASE_URL",
                   "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_SERVICE_KEY", "")
from engine.paths import ALL_SCORES as SIGNALS_FILE   # noqa: E402
# Valid setups whose price was not at the zone at the close, within one ATR of it.
# The loop watches these; near_zone re-tests the distance live every cycle.
from engine.paths import CANDIDATES as CANDIDATES_FILE  # noqa: E402
# MIN_SCORE removed. 03b retired the score gate ("score is non-predictive per
# validation -- disqualifiers alone decide qualification"), but this copy
# survived and was the real filter: it dropped every accumulation setup, which
# scores low by construction, and was additionally masking a scoring-order bug
# that left 113/154 qualifying signals at total_score = 0.0. Qualification is
# decided upstream by 03b's disqualifiers plus the zone-entry gate below.


_SIGNALS_HAS = {}


def _signals_has(col: str, headers: dict) -> bool:
    """Does public.signals have this column? Probed once per process, cached.

    WHY THIS IS NEEDED AT ALL. push_signals has sent engine_sha and engine_ran_at
    since 2026-10-02, and the only migration that adds them to `signals` is
    PENDING_signals_features_provenance.sql, which was deliberately deferred --
    "nothing reads the features table for months". The columns were load-bearing in
    a way the deferral did not account for: PostgREST rejects an insert naming an
    unknown column by discarding the WHOLE payload, so the first chain run after
    that commit would have failed the entire signal push, not just the provenance.
    106 signals on the Thursday, zero on the Monday, and the only notice a 400 in
    reports/cron.log.

    So the payload adapts to the schema instead of assuming it. This works before
    and after the migration: apply it and provenance starts being recorded with no
    code change, which is the behaviour the deferral needed in the first place.

    Same pattern as atlas_entry._trades_has_product, for the same reason.
    """
    if col not in _SIGNALS_HAS:
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/signals?select={col}&limit=1",
                             headers=headers, timeout=10)
            _SIGNALS_HAS[col] = r.status_code in (200, 206)
            if not _SIGNALS_HAS[col]:
                log.warning(
                    f"signals.{col} does not exist — publishing without it. "
                    f"Apply migrations/PENDING_signals_features_provenance.sql to "
                    f"start recording it; the batch publishes either way.")
        except Exception as e:
            # A FAILED PROBE MUST NOT DROP THE BATCH. Assume absent: omitting a
            # column publishes a row with one field missing, naming one that is not
            # there publishes nothing at all.
            log.warning(f"could not probe signals.{col} ({e}) — publishing without it")
            _SIGNALS_HAS[col] = False
    return _SIGNALS_HAS[col]


def push_signals(target_date: str = None):
    if not SUPABASE_KEY:
        log.error("SUPABASE_SERVICE_KEY not set. Export it first:")
        log.error("  export SUPABASE_SERVICE_KEY='your_service_role_key'")
        sys.exit(1)

    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates"
    }

    # Load signals
    if not SIGNALS_FILE.exists():
        log.error(f"Signals file not found: {SIGNALS_FILE}")
        log.error("Run 03b_score.py first.")
        sys.exit(1)

    df = pd.read_parquet(SIGNALS_FILE)
    df["date"] = pd.to_datetime(df["date"])

    # Get target date
    if target_date:
        d = pd.Timestamp(target_date)
    else:
        # Use latest date that has qualifying signals, not just latest data date
        qualifying = df[df["qualifies"] == True]
        if qualifying.empty:
            log.warning("No qualifying signals found in dataset")
            return
        d = qualifying["date"].max()

    log.info(f"Pushing signals for: {d.date()}")

    # Filter: qualifying signals for this date
    day = df[
        (df["date"] == d) &
        (df["qualifies"] == True)
    ].copy()

    # REGIME, RECORDED AND LOGGED -- NOT A FILTER.
    # Read so the run states the market context it published into. It does not
    # decide what gets published; see the note below the composition log.
    try:
        r_regime = requests.get(
            f"{SUPABASE_URL}/rest/v1/sector_heatmap?order=signal_date.desc&limit=1",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        )
        regime_data = r_regime.json()
        market_dir = regime_data[0]["market_direction"] if regime_data else "mixed"
        log.info(f"Market regime: {market_dir.upper()}")

        # THE COMPOSITION BY DIRECTION, always logged. It was added when
        # "suppressed 5 LONG signals" could not be told apart from "there were no
        # shorts to suppress": shorts stopped reaching this function on 13 Aug 2026
        # when MAX_ENTRY_DIST_PCT went 8.0 -> 0.30, and for six weeks the absence
        # looked like the regime filter doing its job. The filter is gone now and
        # the line stays, because what the screener found by side is worth knowing
        # whether or not anything acts on it.
        _n_long  = int((day["direction"].astype(str).str.lower() == "long").sum())
        _n_short = int((day["direction"].astype(str).str.lower() == "short").sum())
        log.info(f"Qualifying for {d.date()}: {_n_long} long / {_n_short} short")

        # NOTHING IS SUPPRESSED HERE ANY MORE. The screener publishes what it
        # found and the page presents it against the regime; the decision about
        # whether to TRADE a side belongs to the entry gate, which is where it is
        # enforced and tested.
        #
        # This used to drop longs in a bearish regime and shorts in a bullish one.
        # On 2026-09-29 it logged "Qualifying before the regime filter: 6 long / 0
        # short" and then "No qualifying signals" -- six findings the screener had
        # made, withheld from the product because the agent would not have traded
        # them. Those are different questions and only the second is the agent's.
        #
        # NOTHING BECOMES TRADEABLE THAT WAS NOT. atlas_entry Gate 1 refuses a
        # long outside a bull regime with positive breadth, and ALLOW_SHORT_ENTRIES
        # is False, checked first and unconditionally in the SHORT branch. Both are
        # swept in tests/test_regime_gate.py across all 32 regime x hedge x
        # sentiment combinations. The publisher was never the thing keeping ATLAS
        # out of a trade -- and it FAILED OPEN on a regime-fetch exception, which
        # made it the weaker of the two barriers anyway.
        log.info(f"Regime {market_dir.upper()}: publishing all {len(day)} "
                 f"qualifying signal(s) unfiltered. The entry gate, not the "
                 f"publisher, decides what is tradeable.")
    except Exception as e:
        # Everything is published regardless, so an unreadable regime costs only
        # the log line that would have named it. It is NOT a permissive fallback
        # any more -- there is nothing to fall back from.
        log.warning(f"Could not read the market regime ({e}) — publishing "
                    f"unfiltered, as always; the context line is missing from "
                    f"this run's log")

    if day.empty:
        log.warning(f"No qualifying signals for {d.date()}")
        # Still push empty — website shows "no signals today"
        return

    log.info(f"Signals to push: {len(day)}")

    # Build records
    # Zone-entry gate: only signals with a validated zone + structural stop.
    if "entry_valid" in day.columns:
        _before = len(day)
        day = day[day["entry_valid"].fillna(False).astype(bool)]
        log.info(f"Zone-entry filter: {len(day)}/{_before} signals valid")
        if day.empty:
            log.warning("No signals passed zone-entry validation")
            return

    records = []
    _skipped = 0
    kind = "signal"          # this pass publishes the actionable set
    for _, row in day.iterrows():
        # NO fallback to close. That fallback was the original defect.
        entry = row.get("entry_ref")
        try:
            entry = float(entry)
        except (TypeError, ValueError):
            entry = 0.0
        if entry <= 0:
            _skipped += 1
            continue

        # Measurement targets. zone_entry drops target_1/target_2 as TRADE
        # levels (open target, trailed exits) -- these are the fixed 2R/3R
        # yardstick the accuracy record is scored against. ATLAS ignores them.
        _sl_raw = row.get("sl")
        try:
            _sl_val = float(_sl_raw)
        except (TypeError, ValueError):
            _sl_val = 0.0
        _t1, _t2 = measurement_targets(entry, _sl_val, row.get("direction", "long"))

        records.append({
            "signal_date":      d.strftime("%Y-%m-%d"),
            "symbol":           str(row.get("symbol", "")),
            "direction":        str(row.get("direction", "long")).upper(),
            "grade":            str(row.get("grade", "B")),
            "score":            float(row.get("total_score", 0)),
            "setup_name":       str(row.get("setup_name", "")),
            # 'signal' = actionable and scored; 'candidate' = watched, never
            # scored. Named in the data because mark_signals filters on it: without
            # that, ~90 candidates a session would silently change what the
            # accuracy record measures.
            "publication_kind": kind,
            "entry_ref":        float(entry) if entry else None,
            "entry_low":        float(row.get("entry_low")) if row.get("entry_low") else None,
            "entry_high":       float(row.get("entry_high")) if row.get("entry_high") else None,
            "sl":               float(row.get("sl", 0)) if row.get("sl") else None,
            "stop_pct":         float(row.get("stop_pct", 0)) if row.get("stop_pct") else None,
            # MEASUREMENT ONLY -- consumed by update_outcomes.py and the
            # screener's accuracy record. ATLAS ignores these entirely:
            # accumulation longs are held open with manual exits.
            # 2R / 3R off the structural stop, not off the previous close.
            # Computed above via zone_entry.measurement_targets(); reading them
            # off `row` returned NULL for every signal once zone_entry started
            # dropping the columns, which silently blinded both consumers.
            "target_1":         _t1,
            "target_2":         _t2,
            "rr_1":             2.0 if _t1 else None,
            "rr_2":             3.0 if _t2 else None,
            "entry_dist_pct":   float(row.get("entry_dist_pct", 0)) if row.get("entry_dist_pct") is not None else None,
            "notional":         float(row.get("notional", 0)) if row.get("notional") else None,
            "product":          str(row.get("product", "CNC")),
            # entry_zone_source is the family the ENTRY was computed from,
            # chosen by trade direction. active_zone_source is resolved from
            # structure_trend and was what handed shorts a demand zone, so
            # publishing it would misdescribe the entry. Fall back only if the
            # newer column is absent.
            "zone_source":      str(row.get("entry_zone_source")
                                    or row.get("active_zone_source", "")),
            "qty":              int(row.get("qty", 0)) if row.get("qty") else None,
            "risk_inr":         float(row.get("risk_inr", 0)) if row.get("risk_inr") else None,
            "rsi":              float(row.get("rsi", 0)) if row.get("rsi") else None,
            "rvol":             float(row.get("rvol", 0)) if row.get("rvol") else None,
            "atr_pct":          float(row.get("atr_pct", 0)) if row.get("atr_pct") else None,
            "delivery_pct":     float(row.get("delivery_pct", 0)) if row.get("delivery_pct") else None,
            "vix_close":        float(row.get("vix_close", 0)) if row.get("vix_close") else None,
            # SECTOR, CARRIED AT LAST. 03b computes it from
            # universe.SYMBOL_SECTOR_MAP via load_symbol_sector() and this literal
            # dropped it, after which update_outcomes substituted the constant
            # "OTHER" for every row since May 2025. The data existed the whole
            # time; the 36-key literal was where it stopped.
            "sector":           str(row.get("sector") or "") or None,
            "sector_bias":      str(row.get("sector_bias") or "") or None,
            # Recorded, not imputed. A row written from here carries the sector in
            # force on the night it was published; the backfill marks itself
            # separately so the two can never be confused.
            "sector_as_of":     "recorded",
            "market_regime":    str(row.get("market_regime", "unknown")),
            "structure_trend":  str(row.get("structure_trend", "ranging")),
            "trade_type":       str(row.get("trade_type", "")),
            "score_regime":     float(row.get("regime_score", 0)),
            "score_smc":        float(row.get("smc_score", 0)),
            "score_technical":  float(row.get("technical_score", 0)),
            "score_volume":     float(row.get("volume_score", 0)),
            "score_rr":         float(row.get("rr_score", 0)),
        })

    # Clean NaN/inf values from records
    import math
    def clean(v):
        if v is None: return None
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)): return None
        return v
    records = [{k: clean(v) for k, v in r.items()} for r in records]

    # INSERT FIRST, THEN DELETE THE OLD ROWS.
    #
    # This used to delete the date and then insert. If the insert failed the
    # script exited 1 having already removed the day's signals, leaving the
    # date EMPTY -- worse than leaving it stale, because market_open takes
    # max(signal_date) and would silently fall back to an older batch and trade
    # it, inside the 5-day staleness window, with nothing indicating a problem.
    #
    # Inverted, the failure modes become:
    #   insert fails  -> old rows still present, nothing deleted. Stale, visible,
    #                    and the next run fixes it.
    #   delete fails  -> both old and new present. Duplicates are recoverable by
    #                    hand and are logged loudly; the new data IS live.
    #
    # The window where both sets exist is milliseconds, and the only reader
    # (market_open, 09:37 IST) runs ~15 hours after this job.
    day_str  = d.strftime("%Y-%m-%d")
    base_url = f"{SUPABASE_URL}/rest/v1/signals"

    old_ids = []
    try:
        r_old = requests.get(f"{base_url}?signal_date=eq.{day_str}&select=id",
                             headers=headers, timeout=30)
        if r_old.status_code == 200:
            old_ids = [row["id"] for row in r_old.json()]
    except requests.RequestException as e:
        log.warning(f"Could not list existing rows for {day_str}: {e}")
    log.info(f"Existing rows for {day_str}: {len(old_ids)}")

    ins_r = requests.post(base_url, headers=headers, json=records, timeout=60)
    if ins_r.status_code not in (200, 201):
        log.error(f"Push failed: {ins_r.status_code} — {ins_r.text[:300]}")
        log.error(f"Nothing was deleted — the {len(old_ids)} existing row(s) for "
                  f"{day_str} are intact.")
        sys.exit(1)
    log.info(f"✓ Pushed {len(records)} signals to Supabase")

    if old_ids:
        del_r = requests.delete(
            f"{base_url}?signal_date=eq.{day_str}&id=in.({','.join(map(str, old_ids))})",
            headers=headers, timeout=30)
        if del_r.status_code in (200, 204):
            log.info(f"Superseded {len(old_ids)} previous row(s) for {day_str}")
        else:
            log.error(f"DUPLICATE ROWS: new signals inserted but the {len(old_ids)} "
                      f"previous row(s) could not be deleted "
                      f"({del_r.status_code} {del_r.text[:120]}). "
                      f"Delete ids {old_ids[:10]}{'...' if len(old_ids) > 10 else ''} by hand.")

    # Verify
    ver_url = (f"{SUPABASE_URL}/rest/v1/signals?signal_date=eq.{d.strftime('%Y-%m-%d')}"
               f"&publication_kind=eq.signal&select=symbol,grade,score")
    ver_r = requests.get(ver_url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    })
    if ver_r.status_code == 200:
        pushed = ver_r.json()
        log.info(f"Verified in Supabase: {len(pushed)} signals")
        for s in pushed:
            log.info(f"  {s['symbol']:<12} {s['grade']} {s['score']}")
    else:
        log.warning("Could not verify — check Supabase dashboard")

    # The watchlist, published separately and AFTER the actionable set. Separate on
    # purpose: the signal path above works and is load-bearing, and a candidate
    # genuinely carries fewer fields -- no qty, risk or notional, because sizing
    # happens after the distance check that candidates fail. Its cleanup is scoped
    # to publication_kind=candidate so it cannot touch a signal row.
    # FEATURES, after both publishes. Last deliberately: it is an analysis
    # sidecar, and nothing about the screener or the trading loop should wait on it
    # or fail with it.
    try:
        push_features(d.strftime("%Y-%m-%d"), headers)
    except Exception as e:
        log.error(f"signals_features write raised ({e}) — signals are published; "
                  f"only the research sidecar is missing")

    try:
        push_candidates(d, headers, base_url)
    except Exception as e:
        # A failed watchlist must not fail the run: the actionable signals are
        # already in and the loop still works from them, it just watches less.
        log.error(f"candidate publish failed ({e}) — actionable signals are "
                  f"published; the loop will watch only those")

    # ── COMPLETION CHECK — the chain cannot pass silently ───────────────
    # READ BACK, do not trust the POST. requests.post does not raise on 4xx and
    # this file has twice shipped a payload that PostgREST discarded whole: the
    # candidate push failed on a NOT NULL grade for the life of the feature, and
    # engine_sha named a column that did not exist. A 200 from the insert is not
    # evidence that rows are there, so the only acceptable proof is a SELECT.
    day_str = d.strftime("%Y-%m-%d")
    try:
        chk = requests.get(
            f"{base_url}?signal_date=eq.{day_str}&publication_kind=eq.signal"
            f"&select=id", headers={**headers, "Prefer": "count=exact",
                                    "Range": "0-0"}, timeout=30)
        n_live = int((chk.headers.get("content-range") or "*/0").split("/")[-1]) \
            if chk.status_code in (200, 206) else -1
    except Exception as e:
        _chain_failed(f"published, then could not verify the batch: {e}")
        return records
    if n_live < 0:
        _chain_failed(f"could not read back the batch for {day_str}: "
                      f"HTTP {chk.status_code}")
    elif n_live == 0:
        _chain_failed(f"zero signals are live for {day_str}. "
                      f"The scored frame held {len(records)} record(s) to publish, "
                      f"so either the push was rejected or the engine qualified "
                      f"nothing.")
    else:
        log.info(f"✓ COMPLETION CHECK: {n_live} signal(s) live for {day_str}")

    return records


def notify_atlas(records: list):
    """
    Send qualifying signals to ATLAS trade executor.
    Only Grade A signals (score >= 78) trigger Telegram alerts.
    ATLAS bot listener must be running to receive.
    """
    # ATLAS entry happens at 09:37 via atlas/signal/market_open.py, which
    # reads zone-validated signals from Supabase directly.
    #
    # The old path here queued signals into the retired executor module, which
    # carries rule-violating SL/GTT order calls, and passed fixed profit levels
    # that no longer exist. It was inert only because the score >= 82 gate
    # zeroed it out; with that gate removed it would have routed every signal
    # into retired code. Removed entirely.
    log.info(f"ATLAS: {len(records)} zone-validated signals available for 09:37 entry")



# The 54 columns 03b computes and this file used to discard. Names are taken
# verbatim from the scored frame -- no renaming, no aliasing -- so a column added
# upstream reaches the table by being added to this tuple and nowhere else. Seven
# field-name mismatches in this repo came from a literal that renamed things on
# the way through; this one deliberately does not.
FEATURE_COLS = (
    "adx", "adx_ranging", "adx_trending", "atr", "recent_bos_choch",
    "zone_age_days", "zone_dist_pct",
    "near_demand_ob", "near_supply_ob", "price_in_bull_fvg", "price_in_bear_fvg",
    "bos_bull", "bos_bear", "choch_bull", "choch_bear",
    "bull_liq_sweep", "bear_liq_sweep",
    "active_demand_ob_high", "active_demand_ob_low",
    "active_supply_ob_high", "active_supply_ob_low",
    "active_bull_fvg_high", "active_bull_fvg_low",
    "active_bear_fvg_high", "active_bear_fvg_low",
    "advance_count", "decline_count", "nifty_close", "ad_ratio", "no_trade_zone",
    "rs_5d", "rs_20d", "rs_positive",
    "in_discount", "in_premium", "equilibrium",
    "delivery_avg", "high_delivery", "institutional_buying",
    "vol_avg20", "vol_spike", "vol_confirming",
    "accumulation_score", "is_accumulation",
    "close", "prev_close", "high", "low", "volume",
    "is_warmup", "qualifies", "entry_valid",
    "disqualify_reason", "reject_reason",
)


def _clean(v):
    """JSON-safe, and NaN becomes NULL rather than the string 'nan'.

    market_regime holds the literal string 'nan' on 567 rows of `signals` because
    something upstream passed a float NaN through str(). That is the single worst
    field in the record -- it passes NOT NULL, it is not NULL, and it equals
    nothing. Every value here goes through this.
    """
    import math
    if v is None:
        return None
    if isinstance(v, (bool,)):
        return bool(v)
    if isinstance(v, (int,)):
        return int(v)
    try:
        f = float(v)
    except (TypeError, ValueError):
        sv = str(v).strip()
        return sv or None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


_FEATURES_TABLE = None


def _features_table_exists(headers: dict) -> bool:
    """Is public.signals_features there? Probed once per process.

    Same reasoning as _signals_has, one level up: that guards a COLUMN the publisher
    names, this guards the whole TABLE. A deferred migration should make a writer
    quiet, not make it fail nightly -- otherwise the log fills with 404s that look
    like a broken pipeline and the real faults get lost among them.
    """
    global _FEATURES_TABLE
    if _FEATURES_TABLE is None:
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/signals_features?select=signal_date&limit=1",
                headers=headers, timeout=10)
            _FEATURES_TABLE = r.status_code in (200, 206)
            if not _FEATURES_TABLE:
                log.info("signals_features does not exist — skipping the feature "
                         "write. Apply migrations/"
                         "PENDING_signals_features_provenance.sql when the learning "
                         "loop needs it; nothing reads it until then.")
        except Exception as e:
            log.warning(f"could not probe signals_features ({e}) — skipping the "
                        f"feature write")
            _FEATURES_TABLE = False
    return _FEATURES_TABLE


def push_features(day_str: str, headers: dict) -> int:
    """Write signals_features for every row 03b PERSISTED, qualifying or not.

    WHAT THAT DOES AND DOES NOT COVER, stated precisely because the gap matters.
    03b writes two artifacts and discards the rest:

      all_scores_v2.parquet   qualifying rows only -- the published signals
      candidates_v2.parquet   valid unmitigated zones that did NOT qualify

    So the negative class available here is the WATCHLIST: rows with real
    geometry that failed the disqualifier block or the stop cap. The ~370,000
    stock-days a night that are rejected outright -- no zone, no structure, warmup
    -- are not persisted anywhere and cannot be recovered without 03b writing a
    third artifact, which would be a different change and two orders of magnitude
    more rows.
    #
    # That is arguably the negative class worth having anyway: rows that nearly
    # qualified discriminate, and "had no active zone at all" does not. But it is
    # a limitation, not a design choice, and it should not be read as one.

    NEVER FATAL. This is an analysis sidecar: if it fails the batch has still
    published, and taking the chain down over a feature table would trade a
    working screener for a research input. Logged loudly and dropped.
    """
    # DOES THE TABLE EXIST? signals_features is created by
    # PENDING_signals_features_provenance.sql, deliberately deferred -- "nothing
    # reads it for months". Until it is applied this function parsed two parquets,
    # built the frame, POSTed it and took a 404, every night, logging and
    # continuing. Work for nothing and a line in the log that looks like a fault.
    #
    # Checked once per process, before any of that work. Apply the migration and it
    # starts writing with no code change; leave it deferred and it costs nothing.
    if not _features_table_exists(headers):
        return 0

    frames = []
    for f in (SIGNALS_FILE, CANDIDATES_FILE):
        if not f.exists():
            continue
        try:
            fr = pd.read_parquet(f)
        except Exception as e:
            log.warning(f"{f.name} unreadable for features: {e}")
            continue
        if "date" in fr.columns:
            fr = fr[pd.to_datetime(fr["date"]).dt.strftime("%Y-%m-%d") == day_str]
        if len(fr):
            frames.append(fr)
    if not frames:
        log.info("no scored rows for features")
        return 0
    scored = pd.concat(frames, ignore_index=True)
    # A symbol can appear in both artifacts across directions; the table key is
    # (date, symbol, direction) so keep the LAST occurrence per key rather than
    # letting the upsert decide arbitrarily.
    if {"symbol", "direction"} <= set(scored.columns):
        scored = scored.drop_duplicates(subset=["symbol", "direction"], keep="last")
    rows = []
    for _, r in scored.iterrows():
        rec = {"signal_date": day_str,
               "symbol": str(r.get("symbol", "")),
               "direction": str(r.get("direction", "")).upper(),
               "engine_sha": ENGINE_SHA}
        for c in FEATURE_COLS:
            rec[c] = _clean(r.get(c))
        if rec["symbol"] and rec["direction"]:
            rows.append(rec)
    if not rows:
        return 0
    written = 0
    try:
        for i in range(0, len(rows), 500):
            chunk = rows[i:i + 500]
            rr = requests.post(
                f"{SUPABASE_URL}/rest/v1/signals_features"
                f"?on_conflict=signal_date,symbol,direction",
                headers={**headers,
                         "Prefer": "resolution=merge-duplicates,return=minimal"},
                json=chunk, timeout=60)
            if rr.status_code not in (200, 201, 204):
                log.error(f"signals_features write failed: HTTP {rr.status_code} "
                          f"{rr.text[:200]}")
                return written
            written += len(chunk)
    except Exception as e:
        log.error(f"signals_features write failed: {type(e).__name__}: {e}")
        return written
    log.info(f"signals_features: {written} row(s) for {day_str} "
             f"({len(FEATURE_COLS)} columns each)")
    return written


def _candidate_grade(row) -> str:
    """The grade computed for a watchlist row, or "C" with a warning.

    Never a silent default. These rows went through score_vectorized -- the failing
    row that surfaced this carried score 48.5 -- so the grade exists and the
    fallback should never fire. If it does, something upstream stopped writing the
    column and that is worth a log line rather than a plausible letter.
    """
    g = row.get("grade")
    g = "" if g is None else str(g).strip()
    if g:
        return g
    log.warning("candidate row has no grade — publishing as C. "
                "03b's scoring frame should always carry one.")
    return "C"


def push_candidates(d, headers: dict, base_url: str) -> int:
    """
    Publish the watchlist for date `d`. -> rows published.

    Candidates are valid setups whose price was not at the zone at the close and
    which sit within one ATR of it. The loop watches them and near_zone re-tests
    the distance against the live price every cycle, so the close-time distance is
    a filter on what is WORTH watching, not on what is tradeable.

    They are marked publication_kind='candidate' and mark_signals ignores them.
    Without that they would silently change what the accuracy record measures,
    from "signals we published as actionable" to "everything we watched".
    """
    if not CANDIDATES_FILE.exists():
        log.info("no candidates artifact — run 03b to produce one")
        return 0
    cdf = pd.read_parquet(CANDIDATES_FILE)
    if cdf.empty:
        log.info("candidates artifact is empty")
        return 0
    cdf["date"] = pd.to_datetime(cdf["date"])
    day = cdf[cdf["date"] == d]
    if day.empty:
        log.info(f"no candidates for {d.date()}")
        return 0

    day_str = d.strftime("%Y-%m-%d")
    recs = []
    for _, row in day.iterrows():
        try:
            entry = float(row.get("entry_ref"))
        except (TypeError, ValueError):
            continue
        if entry <= 0:
            continue
        recs.append({
            "signal_date":      day_str,
            "symbol":           str(row.get("symbol", "")),
            "direction":        str(row.get("direction", "")).upper(),
            "publication_kind": "candidate",
            # GRADE IS NOT NULL WITH NO DEFAULT, and omitting it is why no
            # candidate has EVER published: every push since the feature shipped
            # returned 23502 and the whole batch was discarded. _watchlist returns
            # the full scored frame, so the grade was computed and simply not sent.
            # "C" is the fallback rather than the signal path's "B" because a row
            # that did not qualify should not be handed the better letter, and the
            # warning fires if it is ever actually needed.
            "grade":            _candidate_grade(row),
            "entry_ref":        entry,
            "entry_low":        float(row.get("entry_low") or 0) or None,
            "entry_high":       float(row.get("entry_high") or 0) or None,
            "sl":               float(row.get("sl") or 0) or None,
            "stop_pct":         float(row.get("stop_pct") or 0) or None,
            "entry_dist_pct":   float(row.get("entry_dist_pct") or 0) or None,
            "setup_name":       str(row.get("setup_name", "")),
            "zone_source":      str(row.get("entry_zone_source")
                                    or row.get("active_zone_source", "")),
            "score":            float(row.get("total_score") or 0),
            "structure_trend":  str(row.get("structure_trend", "ranging")),
            "product":          "CNC" if str(row.get("direction", "")).upper() == "LONG" else "MIS",
            "atr_pct":          float(row.get("atr_pct") or 0) or None,
        })
    # WHICH COMMIT WROTE THIS. Every era boundary in the record had to be inferred
    # from which columns are null, which works only while the deployments land weeks
    # apart. Added only when the columns exist -- see _signals_has.
    if recs and _signals_has("engine_sha", headers):
        for _r in recs:
            _r["engine_sha"] = ENGINE_SHA
            _r["engine_ran_at"] = RUN_AT

    if not recs:
        log.info(f"no publishable candidates for {day_str}")
        return 0

    # Insert BEFORE deleting, same order as the signal path: a failed insert must
    # leave the previous watchlist intact rather than emptying it.
    r = requests.post(base_url, headers=headers, json=recs, timeout=60)
    if r.status_code not in (200, 201):
        log.error(f"candidate push failed: {r.status_code} — {r.text[:300]}")
        return 0
    log.info(f"✓ Published {len(recs)} candidate(s) for {day_str}")

    old = requests.get(
        f"{base_url}?signal_date=eq.{day_str}&publication_kind=eq.candidate"
        f"&select=id&order=id.asc", headers=headers, timeout=30)
    if old.status_code == 200:
        ids = [x["id"] for x in old.json()][:-len(recs)] if len(old.json()) > len(recs) else []
        if ids:
            requests.delete(
                f"{base_url}?id=in.({','.join(map(str, ids))})"
                f"&publication_kind=eq.candidate", headers=headers, timeout=30)
            log.info(f"  removed {len(ids)} superseded candidate row(s)")
    return len(recs)


def _chain_failed(reason: str) -> None:
    """The chain produced nothing. Say so loudly and exit non-zero.

    SAME SHAPE AS zerodha_auto_login's failure path, and for the same reason: a
    scheduled job that fails quietly is indistinguishable from one that succeeded
    with nothing to do.

    WHY THIS EXISTS. On 2026-10-05 an unguarded `import engine.paths` was added to
    03b, which cron invokes by file path. The script died before opening its log,
    the &&-chain stopped, and 06_push and mark_signals never ran. For THREE
    SESSIONS reject_sample, update_outcomes, excursions and the daily report each
    ran afterwards, found stale files, and reported success -- while the page said
    only "Nothing published". Nothing in the system distinguished "the engine found
    no setups tonight" from "the engine never ran".

    EXITING NON-ZERO IS THE POINT. The chain is &&-joined, so mark_signals will not
    run on an empty batch -- which is correct, there is nothing to mark. And an
    empty batch alerts too: a night that genuinely produces no signals is rare
    enough to be worth a message, and it is exactly the state that hid this bug.
    """
    log.error(f"CHAIN INCOMPLETE — {reason}")
    try:
        from atlas.reporting.telegram import send
        sent = send(
            "❌ <b>SIGNAL CHAIN PUBLISHED NOTHING</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"{reason}\n\n"
            "No signals are live for this session. The page will show the last "
            "published batch and mark it stale. Check "
            "reports/cron.log for the stage that failed — the chain is &&-joined, "
            "so the first failure stops everything after it."
        )
        if not sent:
            log.error("Telegram notification did not send — failure is log-only")
    except Exception as e:
        log.error(f"could not send the chain-failure alert ({e}) — "
                  f"failure is log-only")
    sys.exit(1)


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    os.chdir(Path(__file__).parent.parent)
    records = push_signals(target)
    if records:
        notify_atlas(records)
    log.info("Done. Signals are live on Supabase.")


if __name__ == "__main__":
    main()
