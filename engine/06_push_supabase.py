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
MIN_SCORE        = 70


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
        (df["qualifies"] == True) &
        (df["total_score"] >= MIN_SCORE)
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
    records = []
    for _, row in day.iterrows():
        entry = row.get("entry_ref", row.get("close", 0))
        records.append({
            "signal_date":      d.strftime("%Y-%m-%d"),
            "symbol":           str(row.get("symbol", "")),
            "direction":        str(row.get("direction", "long")).upper(),
            "grade":            str(row.get("grade", "B")),
            "score":            float(row.get("total_score", 0)),
            "setup_name":       str(row.get("setup_name", "")),
            "entry_ref":        float(entry) if entry else None,
            "entry_low":        float(row.get("entry_low", entry*0.998)) if entry else None,
            "entry_high":       float(row.get("entry_high", entry*1.002)) if entry else None,
            "sl":               float(row.get("sl", 0)) if row.get("sl") else None,
            "target_1":         float(row.get("target_1", 0)) if row.get("target_1") else None,
            "target_2":         float(row.get("target_2", 0)) if row.get("target_2") else None,
            "sl_pct":           float(row.get("sl_pct", 0)) if row.get("sl_pct") else None,
            "qty":              int(row.get("qty", 0)) if row.get("qty") else None,
            "risk_inr":         float(row.get("risk_inr", 0)) if row.get("risk_inr") else None,
            "rr_1":             float(row.get("rr_1", 2.0)),
            "rr_2":             float(row.get("rr_2", 3.0)),
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

    # Delete existing signals for this date first (clean re-run)
    del_url = f"{SUPABASE_URL}/rest/v1/signals?signal_date=eq.{d.strftime('%Y-%m-%d')}"
    del_r = requests.delete(del_url, headers=headers)
    log.info(f"Cleared existing signals for {d.date()}: {del_r.status_code}")

    # Insert new signals
    ins_url = f"{SUPABASE_URL}/rest/v1/signals"
    ins_r = requests.post(ins_url, headers=headers, json=records)

    if ins_r.status_code in (200, 201):
        log.info(f"✓ Pushed {len(records)} signals to Supabase")
    else:
        log.error(f"Push failed: {ins_r.status_code} — {ins_r.text}")
        sys.exit(1)

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


def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    os.chdir(Path(__file__).parent.parent)
    push_signals(target)
    log.info("Done. Signals are live on Supabase.")


if __name__ == "__main__":
    main()
