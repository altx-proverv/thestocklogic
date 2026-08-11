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

    # REGIME-AWARE FILTER
    # Fetch current market regime from Supabase sector_heatmap
    try:
        r_regime = requests.get(
            f"{SUPABASE_URL}/rest/v1/sector_heatmap?order=signal_date.desc&limit=1",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
        )
        regime_data = r_regime.json()
        market_dir = regime_data[0]["market_direction"] if regime_data else "mixed"
        log.info(f"Market regime: {market_dir.upper()}")

        before = len(day)
        if market_dir == "bearish":
            day = day[day["direction"] != "long"]
            log.info(f"Bearish regime: suppressed {before - len(day)} LONG signals")
        elif market_dir == "bullish":
            day = day[day["direction"] != "short"]
            log.info(f"Bullish regime: suppressed {before - len(day)} SHORT signals")
    except Exception as e:
        log.warning(f"Could not fetch regime — pushing all signals: {e}")

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
            "zone_source":      str(row.get("active_zone_source", "")),
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
    ver_url = f"{SUPABASE_URL}/rest/v1/signals?signal_date=eq.{d.strftime('%Y-%m-%d')}&select=symbol,grade,score"
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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    os.chdir(Path(__file__).parent.parent)
    records = push_signals(target)
    if records:
        notify_atlas(records)
    log.info("Done. Signals are live on Supabase.")


if __name__ == "__main__":
    main()
