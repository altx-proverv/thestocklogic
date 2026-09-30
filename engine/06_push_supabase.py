"""
THE STOCK LOGIC — Push signals to Supabase
==========================================
Runs after 03b_score.py — reads today's signals
and upserts them into Supabase.

Run: python3 engine/06_push_supabase.py
"""

import os, sys, json, logging, warnings
from pathlib import Path
from datetime import date
import pandas as pd
import requests

# Measurement yardstick (2R/3R off the structural stop). Single definition --
# update_outcomes.py and trade_review.py import the same helper.
try:
    from engine.zone_entry import measurement_targets
except ModuleNotFoundError:
    from zone_entry import measurement_targets

warnings.filterwarnings("ignore")
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
SIGNALS_FILE     = Path("data/processed/signals_v2/all_scores_v2.parquet")
# Valid setups whose price was not at the zone at the close, within one ATR of it.
# The loop watches these; near_zone re-tests the distance live every cycle.
CANDIDATES_FILE  = Path("data/processed/signals_v2/candidates_v2.parquet")
# MIN_SCORE removed. 03b retired the score gate ("score is non-predictive per
# validation -- disqualifiers alone decide qualification"), but this copy
# survived and was the real filter: it dropped every accumulation setup, which
# scores low by construction, and was additionally masking a scoring-order bug
# that left 113/154 qualifying signals at total_score = 0.0. Qualification is
# decided upstream by 03b's disqualifiers plus the zone-entry gate below.


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
    try:
        push_candidates(d, headers, base_url)
    except Exception as e:
        # A failed watchlist must not fail the run: the actionable signals are
        # already in and the loop still works from them, it just watches less.
        log.error(f"candidate publish failed ({e}) — actionable signals are "
                  f"published; the loop will watch only those")

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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    os.chdir(Path(__file__).parent.parent)
    records = push_signals(target)
    if records:
        notify_atlas(records)
    log.info("Done. Signals are live on Supabase.")


if __name__ == "__main__":
    main()
