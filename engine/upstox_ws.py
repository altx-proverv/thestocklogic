"""
THE STOCK LOGIC — Upstox WebSocket Feed
========================================
Streams live 1-minute candles for all Nifty 500 stocks.
Updates sector heatmap and signal rankings every session.

Sessions:
  Pre-market  : 08:45 - 09:15
  Morning     : 09:15 - 11:30 (ORB at 09:15-09:30)
  Afternoon   : 11:30 - 13:30
  Closing     : 13:30 - 15:30

Run: python3 engine/upstox_ws.py
"""

import os, sys, json, logging, asyncio, time
from pathlib import Path
from datetime import datetime, date, time as dtime
import pandas as pd
import requests

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"
DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

# Session definitions (IST)
SESSIONS = {
    "pre_market": (dtime(8,45),  dtime(9,15)),
    "morning":    (dtime(9,15),  dtime(11,30)),
    "afternoon":  (dtime(11,30), dtime(13,30)),
    "closing":    (dtime(13,30), dtime(15,30)),
}


def get_headers() -> dict:
    token_file = DATA_DIR / "upstox_token.json"
    if not token_file.exists():
        raise ValueError("No token found. Run: python3 engine/upstox_auth.py --login")
    data = json.loads(token_file.read_text())
    token = data.get("access_token")
    if not token:
        raise ValueError("Invalid token file")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }


def get_current_session() -> str:
    now = datetime.now().time()
    for name, (start, end) in SESSIONS.items():
        if start <= now <= end:
            return name
    return "closed"


def get_market_quotes(instrument_keys: list) -> dict:
    """Fetch current quotes for a batch of instruments."""
    headers = get_headers()
    # Upstox allows max 500 instruments per request
    batch_size = 500
    all_quotes = {}

    for i in range(0, len(instrument_keys), batch_size):
        batch = instrument_keys[i:i+batch_size]
        keys_str = ",".join(batch)
        r = requests.get(
            f"{BASE_URL}/market-quote/quotes?instrument_key={keys_str}",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            all_quotes.update(data)
        else:
            log.warning(f"Quote fetch failed: {r.status_code}")

    return all_quotes


def get_ohlc_quotes(instrument_keys: list, interval: str = "1d") -> dict:
    """Fetch OHLC quotes for instruments."""
    headers = get_headers()
    batch_size = 500
    all_ohlc = {}

    for i in range(0, len(instrument_keys), batch_size):
        batch = instrument_keys[i:i+batch_size]
        keys_str = ",".join(batch)
        r = requests.get(
            f"{BASE_URL}/market-quote/ohlc?instrument_key={keys_str}&interval={interval}",
            headers=headers, timeout=10
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            all_ohlc.update(data)
        else:
            log.warning(f"OHLC fetch failed: {r.status_code} {r.text[:100]}")

    return all_ohlc


def compute_live_sector_heatmap(quotes: dict, symbol_map: dict, sector_map: dict) -> pd.DataFrame:
    """Compute sector rankings from live quotes."""
    rows = []
    # Build symbol lookup from quote response keys
    # Upstox returns as NSE_EQ:SYMBOL format
    quote_by_sym = {}
    for k, v in quotes.items():
        # Extract symbol from NSE_EQ:SYMBOL format
        parts = k.split(":")
        if len(parts) >= 2:
            quote_by_sym[parts[-1]] = v

    for sym, inst_key in symbol_map.items():
        # Look up by trading symbol directly
        q = quote_by_sym.get(sym, {})
        if not q:
            continue

        ohlc     = q.get("ohlc", {})
        ltp      = q.get("last_price") or ohlc.get("close", 0)
        prev_cls = ohlc.get("close", ltp)
        open_p   = ohlc.get("open", ltp)

        if prev_cls <= 0:
            continue

        intraday_ret = (ltp - open_p) / open_p * 100 if open_p > 0 else 0
        day_ret      = (ltp - prev_cls) / prev_cls * 100 if prev_cls > 0 else 0

        rows.append({
            "symbol":        sym,
            "sector":        sector_map.get(sym, "OTHER"),
            "ltp":           ltp,
            "open":          open_p,
            "prev_close":    prev_cls,
            "intraday_ret":  round(intraday_ret, 2),
            "day_ret":       round(day_ret, 2),
            "volume":        q.get("volume", 0),
        })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Sector aggregation
    sector_df = df.groupby("sector").agg(
        stock_count   = ("symbol", "count"),
        ret_median    = ("intraday_ret", "median"),
        ret_mean      = ("intraday_ret", "mean"),
        advancing     = ("intraday_ret", lambda x: (x > 0).sum()),
        declining     = ("intraday_ret", lambda x: (x < 0).sum()),
    ).reset_index()

    sector_df = sector_df.sort_values("ret_median", ascending=False).reset_index(drop=True)
    sector_df["rank"] = range(1, len(sector_df) + 1)

    n = len(sector_df)
    top3 = sector_df.head(3).index.tolist()
    bot3 = sector_df.tail(3).index.tolist()

    sector_df["classification"] = "neutral"
    sector_df.loc[top3, "classification"] = "strong"
    sector_df.loc[bot3, "classification"] = "weak"

    sector_df["trade_bias"] = "avoid"
    sector_df.loc[sector_df["classification"]=="strong", "trade_bias"] = "long"
    sector_df.loc[sector_df["classification"]=="weak",   "trade_bias"] = "short"

    all_pos = (sector_df["ret_median"] > 0).all()
    all_neg = (sector_df["ret_median"] < 0).all()
    market_direction = "bullish" if all_pos else "bearish" if all_neg else "mixed"
    sector_df["market_direction"] = market_direction

    # REGIME OVERRIDE — trade_bias must agree with market_direction
    if market_direction == "bearish":
        sector_df.loc[sector_df["trade_bias"] == "long", "trade_bias"] = "avoid"
    elif market_direction == "bullish":
        sector_df.loc[sector_df["trade_bias"] == "short", "trade_bias"] = "avoid"

    return sector_df, df


def compute_orb(quotes: dict, symbol_map: dict, orb_data: dict = None) -> list:
    """
    Detect Opening Range Breakout signals.
    ORB = high/low of first 15 minutes (9:15-9:30 AM)
    """
    now = datetime.now()
    signals = []

    for sym, inst_key in symbol_map.items():
        lookup_key = inst_key.replace("|",":")
        q = quotes.get(lookup_key, {})
        if not q:
            continue

        ohlc = q.get("ohlc", {})
        ltp  = q.get("last_price", 0)
        vol  = q.get("volume", 0)

        if not ohlc or ltp <= 0:
            continue

        day_high = ohlc.get("high", 0)
        day_low  = ohlc.get("low", 0)
        day_open = ohlc.get("open", 0)

        if not orb_data or sym not in orb_data:
            continue

        orb_high = orb_data[sym].get("orb_high", 0)
        orb_low  = orb_data[sym].get("orb_low", 0)
        orb_range = orb_high - orb_low

        if orb_range <= 0 or orb_high <= 0:
            continue

        # Breakout detection
        if ltp > orb_high * 1.001:  # 0.1% buffer
            signals.append({
                "symbol":     sym,
                "direction":  "LONG",
                "setup_name": "ORB Breakout — Long",
                "entry":      round(orb_high, 2),
                "sl":         round(orb_low, 2),
                "target_1":   round(orb_high + orb_range * 1.5, 2),
                "target_2":   round(orb_high + orb_range * 2.5, 2),
                "ltp":        ltp,
                "orb_high":   orb_high,
                "orb_low":    orb_low,
                "session":    "morning",
                "signal_time": now.strftime("%H:%M"),
            })
        elif ltp < orb_low * 0.999:  # 0.1% buffer
            signals.append({
                "symbol":     sym,
                "direction":  "SHORT",
                "setup_name": "ORB Breakdown — Short",
                "entry":      round(orb_low, 2),
                "sl":         round(orb_high, 2),
                "target_1":   round(orb_low - orb_range * 1.5, 2),
                "target_2":   round(orb_low - orb_range * 2.5, 2),
                "ltp":        ltp,
                "orb_high":   orb_high,
                "orb_low":    orb_low,
                "session":    "morning",
                "signal_time": now.strftime("%H:%M"),
            })

    return signals


def push_live_signals(signals: list, session: str):
    """Push live intraday signals to Supabase."""
    if not signals:
        return

    url = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        log.warning("SUPABASE_SERVICE_KEY not set")
        return

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
    }

    today = date.today().isoformat()

    # Delete existing live signals for today's session
    requests.delete(
        f"{url}/rest/v1/live_signals?signal_date=eq.{today}&session=eq.{session}",
        headers=headers
    )

    records = []
    for s in signals:
        records.append({
            "signal_date":  today,
            "session":      session,
            "symbol":       s["symbol"],
            "direction":    s["direction"],
            "setup_name":   s["setup_name"],
            "entry":        s.get("entry", 0),
            "sl":           s.get("sl", 0),
            "target_1":     s.get("target_1", 0),
            "target_2":     s.get("target_2", 0),
            "ltp":          s.get("ltp", 0),
            "signal_time":  s.get("signal_time", ""),
        })

    r = requests.post(f"{url}/rest/v1/live_signals", headers=headers, json=records)
    if r.status_code in (200, 201):
        log.info(f"Pushed {len(records)} live signals for session: {session}")
    else:
        log.error(f"Push failed: {r.status_code} — {r.text[:100]}")



def push_live_regime(sector_df: pd.DataFrame, session: str, stock_df=None):
    """Push live sector heatmap + regime to Supabase after each session."""
    supabase_url = os.environ.get("SUPABASE_URL","https://eibdlcanpudjgmkjxrga.supabase.co")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY","")
    if not supabase_key or sector_df.empty:
        return

    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M")

    headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }

    # Delete existing live sector data for today's session
    requests.delete(
        f"{supabase_url}/rest/v1/sector_heatmap?signal_date=eq.{today}",
        headers=headers
    )

    records = []
    for _, row in sector_df.iterrows():
        records.append({
            "signal_date":      today,
            "sector":           row["sector"],
            "rank":             int(row["rank"]),
            "momentum_score":   round(float(row.get("ret_median", 0)), 2),
            "ret_1d":           round(float(row.get("ret_median", 0)), 2),
            "ret_5d":           0.0,
            "ret_20d":          0.0,
            "stock_count":      int(row.get("stock_count", 0)),
            "advancing":        int(row.get("advancing", 0)),
            "declining":        int(row.get("declining", 0)),
            "classification":   row.get("classification", "neutral"),
            "trade_bias":       row.get("trade_bias", "avoid"),
            "market_direction": row.get("market_direction", "mixed"),
        })

    r = requests.post(
        f"{supabase_url}/rest/v1/sector_heatmap",
        headers=headers, json=records
    )
    if r.status_code in (200, 201):
        log.info(f"Live regime pushed to Supabase — session: {session}")
        regime = sector_df.iloc[0].get("market_direction","mixed").upper()
        top2   = sector_df[sector_df["trade_bias"]=="long"]["sector"].tolist()[:2]
        bot2   = sector_df[sector_df["trade_bias"]=="short"]["sector"].tolist()[:2]
        log.info(f"Regime: {regime} | Long: {top2} | Short: {bot2}")
    else:
        log.warning(f"Live regime push failed: {r.status_code}")


def run_session_update():
    """Run one complete session update cycle."""
    sys.path.insert(0, str(Path(__file__).parent))
    from universe import SYMBOL_SECTOR_MAP, INSTRUMENT_KEYS

    session = get_current_session()
    log.info(f"Running session update: {session}")

    if session == "closed":
        log.info("Market closed. No update needed.")
        return

    # Get all instrument keys
    inst_keys = [v for v in INSTRUMENT_KEYS.values() if v]
    log.info(f"Fetching quotes for {len(inst_keys)} instruments...")

    # Fetch live quotes
    quotes = get_market_quotes(inst_keys)
    log.info(f"Got {len(quotes)} quotes")

    if not quotes:
        log.error("No quotes received. Check token.")
        return

    # Compute live sector heatmap
    result = compute_live_sector_heatmap(quotes, INSTRUMENT_KEYS, SYMBOL_SECTOR_MAP)
    if isinstance(result, tuple):
        sector_df, stock_df = result
    else:
        log.error("Failed to compute sector heatmap")
        return

    log.info(f"\nLIVE SECTOR HEATMAP — {session.upper()}")
    log.info(f"{'='*50}")
    for _, row in sector_df.iterrows():
        icon = "🟢" if row["classification"]=="strong" else "🔴" if row["classification"]=="weak" else "⚪"
        log.info(f"{icon} {int(row['rank']):<3} {row['sector']:<12} {row['ret_median']:>+6.2f}%  {row['trade_bias']}")

    # Save sector data
    sector_df["signal_date"] = date.today().isoformat()
    sector_df["session"]     = session
    sector_df.to_parquet(DATA_DIR / f"live_sector_{session}.parquet", index=False)

    # Compute top/bottom 10% stocks
    if isinstance(stock_df, pd.DataFrame) and len(stock_df):
        stock_df["sector_bias"] = stock_df["sector"].map(
            dict(zip(sector_df["sector"], sector_df["trade_bias"]))
        )

        top_sectors  = sector_df[sector_df["trade_bias"]=="long"]["sector"].tolist()
        bot_sectors  = sector_df[sector_df["trade_bias"]=="short"]["sector"].tolist()

        long_stocks  = stock_df[stock_df["sector"].isin(top_sectors)].copy()
        short_stocks = stock_df[stock_df["sector"].isin(bot_sectors)].copy()

        top10_pct  = int(len(long_stocks)  * 0.1) or 1
        bot10_pct  = int(len(short_stocks) * 0.1) or 1

        top_candidates  = long_stocks.nlargest(top10_pct,  "intraday_ret")
        short_candidates= short_stocks.nsmallest(bot10_pct,"intraday_ret")

        log.info(f"\nTOP 10% LONG CANDIDATES ({len(top_candidates)}):")
        for _, r in top_candidates.head(5).iterrows():
            log.info(f"  {r['symbol']:<12} {r['intraday_ret']:>+6.2f}%  LTP:{r['ltp']:.1f}  [{r['sector']}]")

        log.info(f"\nBOTTOM 10% SHORT CANDIDATES ({len(short_candidates)}):")
        for _, r in short_candidates.head(5).iterrows():
            log.info(f"  {r['symbol']:<12} {r['intraday_ret']:>+6.2f}%  LTP:{r['ltp']:.1f}  [{r['sector']}]")

        # Save candidates
        top_candidates.to_parquet(DATA_DIR / f"live_longs_{session}.parquet", index=False)
        short_candidates.to_parquet(DATA_DIR / f"live_shorts_{session}.parquet", index=False)

    # Push live regime to Supabase
    push_live_regime(sector_df, session)

    # Push live prices to Supabase
    push_live_prices(quotes, INSTRUMENT_KEYS)

    log.info(f"\nSession update complete: {session}")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "--update"

    if cmd == "--update":
        run_session_update()
    elif cmd == "--test":
        # Test mode — just fetch a few quotes
        log.info("Test mode — fetching sample quotes...")
        from universe import INSTRUMENT_KEYS
        sample_keys = list(INSTRUMENT_KEYS.values())[:10]
        quotes = get_market_quotes(sample_keys)
        for sym, q in quotes.items():
            ltp = q.get("last_price", 0)
            ohlc = q.get("ohlc", {})
            log.info(f"{sym:<30} LTP:{ltp:>10.2f}  H:{ohlc.get('high',0):.2f}  L:{ohlc.get('low',0):.2f}")
    else:
        print("Usage:")
        print("  python3 engine/upstox_ws.py --update   # run session update")
        print("  python3 engine/upstox_ws.py --test     # test quotes")


def push_live_prices(quotes: dict, instrument_keys: dict):
    """Push latest LTP for all stocks to live_prices table."""
    supabase_url = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_key:
        return

    headers = {
        "apikey":        supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }

    records = []
    for key, data in quotes.items():
        # Quote keys are "NSE_EQ:SYMBOL" format
        symbol = key.split(":")[-1] if ":" in key else key
        if not symbol:
            continue
        ltp        = data.get("last_price", 0)
        close      = data.get("ohlc", {}).get("close", ltp) or ltp
        change_pct = round((ltp - close) / close * 100, 2) if close else 0
        records.append({
            "symbol":     symbol,
            "ltp":        round(float(ltp), 2),
            "change_pct": change_pct,
            "updated_at": datetime.now().isoformat(),
        })

    if not records:
        return

    # Push in batches of 200
    for i in range(0, len(records), 200):
        batch = records[i:i+200]
        r = requests.post(
            f"{supabase_url}/rest/v1/live_prices",
            headers=headers,
            json=batch,
        )
        if r.status_code not in (200, 201):
            log.warning(f"live_prices push failed: {r.status_code}")

    log.info(f"Live prices updated: {len(records)} stocks")
