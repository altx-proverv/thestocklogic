"""
THE STOCK LOGIC — RBE: Range Breakout Engine (Zerodha WebSocket)
=================================================================
Continuous scan 9:15 AM - 3:15 PM IST.

Signal logic per stock:
  1. Breakout: LTP crosses range_high*1.002 (LONG) or range_low*0.998 (SHORT)
  2. Volume:   time-of-day normalized RVOL >= 1.5
  3. PDH/PDL:  LONG requires LTP > PDH, SHORT requires LTP < PDL
  4. One signal per stock per direction per day

Levels:
  LONG : entry=LTP, SL=range_high*0.995, T1=entry+1.5R, T2=Fib 127.2% ext
  SHORT: entry=LTP, SL=range_low*1.005,  T1=entry-1.5R, T2=Fib 127.2% ext

Reads : data/processed/rbe/range_map.json (built by rbe_startup.py at 9:00)
Writes: Supabase live_signals (session='rbe')

Run: python3 engine/rbe_engine.py
"""
import os, sys, json, logging, threading, time
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
from zerodha_tokens import ZERODHA_TOKEN_MAP, TOKEN_SYMBOL_MAP
from atlas.execution.broker import get_access_token

Path("reports").mkdir(exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("reports/rbe.log"),
              logging.StreamHandler(sys.stdout)])
log = logging.getLogger("RBE")

IST = timezone(timedelta(hours=5, minutes=30))

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ZERODHA_API_KEY = os.environ.get("ZERODHA_API_KEY", "")

RANGE_MAP_FILE  = Path("data/processed/rbe/range_map.json")
BREAKOUT_BUFFER = 0.002   # 0.2% beyond range boundary
RVOL_MIN        = 1.5
MARKET_OPEN     = (9, 15)
MARKET_CLOSE    = (15, 15)


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def now_ist():
    return datetime.now(IST)


def market_is_open():
    t = now_ist()
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return MARKET_OPEN[0]*60+MARKET_OPEN[1] <= mins <= MARKET_CLOSE[0]*60+MARKET_CLOSE[1]


def volume_bucket(curve: dict) -> float:
    """Expected fraction of daily volume traded by now (time-of-day curve)."""
    t = now_ist()
    hhmm = f"{t.hour:02d}:{t.minute:02d}"
    keys = sorted(curve.keys())
    frac = 0.05  # before 9:30 assume 5%
    for k in keys:
        if hhmm >= k:
            frac = curve[k]
    return max(frac, 0.05)


class RBEngine:
    def __init__(self):
        with open(RANGE_MAP_FILE) as f:
            data = json.load(f)
        self.ranges = data["stocks"]
        self.curve  = data["volume_curve"]
        self.fired  = self._load_fired_today()
        self.tick_count = 0
        log.info(f"Range map loaded: {len(self.ranges)} stocks | "
                 f"Already fired today: {len(self.fired)}")

    def _load_fired_today(self) -> set:
        """One signal per stock per direction per day — restore state."""
        today = now_ist().date().isoformat()
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/live_signals"
                f"?signal_date=eq.{today}&session=eq.rbe&select=symbol,direction",
                headers=_headers(), timeout=10)
            return {f"{x['symbol']}:{x['direction']}" for x in r.json()}
        except Exception as e:
            log.warning(f"Could not load fired state: {e}")
            return set()

    def on_ticks(self, ws, ticks):
        self.tick_count += len(ticks)
        for tick in ticks:
            try:
                self.process_tick(tick)
            except Exception as e:
                log.error(f"Tick error: {e}")

    def process_tick(self, tick):
        token = tick["instrument_token"]
        sym = TOKEN_SYMBOL_MAP.get(token)
        if not sym or sym not in self.ranges:
            return

        ltp = tick.get("last_price", 0)
        vol = tick.get("volume_traded", 0) or tick.get("volume", 0)
        if not ltp:
            return

        r = self.ranges[sym]
        rh, rl = r["range_high"], r["range_low"]
        height = rh - rl
        if height <= 0:
            return

        # Time-normalized RVOL
        expected_vol = r["avg_vol_20d"] * volume_bucket(self.curve)
        rvol = round(vol / expected_vol, 2) if expected_vol > 0 else 0

        # LONG breakout
        if ltp > rh * (1 + BREAKOUT_BUFFER) and f"{sym}:LONG" not in self.fired:
            if rvol >= RVOL_MIN and ltp > r["pdh"]:
                self.fire_signal(sym, "LONG", ltp, rh, rl, height, rvol)

        # SHORT breakout
        if ltp < rl * (1 - BREAKOUT_BUFFER) and f"{sym}:SHORT" not in self.fired:
            if rvol >= RVOL_MIN and ltp < r["pdl"]:
                self.fire_signal(sym, "SHORT", ltp, rh, rl, height, rvol)

    def fire_signal(self, sym, direction, ltp, rh, rl, height, rvol):
        self.fired.add(f"{sym}:{direction}")

        if direction == "LONG":
            sl = round(rh * 0.995, 2)
            risk = ltp - sl
            t1 = round(ltp + 1.5 * risk, 2)
            t2 = round(rl + 1.272 * height, 2)
        else:
            sl = round(rl * 1.005, 2)
            risk = sl - ltp
            t1 = round(ltp - 1.5 * risk, 2)
            t2 = round(rh - 1.272 * height, 2)

        if risk <= 0:
            return

        record = {
            "signal_date": now_ist().date().isoformat(),
            "symbol":      sym,
            "direction":   direction,
            "session":     "rbe",
            "trade_type":  "RBE",
            "entry":       round(ltp, 2),
            "sl":          sl,
            "target_1":    t1,
            "target_2":    t2,
            "rvol":        rvol,
            "setup_name":  f"Range Breakout — {direction}",
            "prev_day_high": self.ranges[sym]["pdh"],
            "prev_day_low":  self.ranges[sym]["pdl"],
            "signal_time": now_ist().strftime("%H:%M"),
        }
        try:
            r = requests.post(f"{SUPABASE_URL}/rest/v1/live_signals",
                              headers=_headers(), json=record, timeout=10)
            if r.status_code in (200, 201):
                log.info(f"SIGNAL {direction} {sym} @ {ltp} | SL {sl} T1 {t1} "
                         f"T2 {t2} RVOL {rvol}")
            else:
                log.error(f"Push failed {sym}: {r.status_code} {r.text[:80]}")
        except Exception as e:
            log.error(f"Push error {sym}: {e}")

    def on_connect(self, ws, response):
        tokens = [ZERODHA_TOKEN_MAP[s] for s in self.ranges if s in ZERODHA_TOKEN_MAP]
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_QUOTE, tokens)
        log.info(f"WebSocket connected — subscribed {len(tokens)} tokens (quote mode)")

    def on_close(self, ws, code, reason):
        log.info(f"WebSocket closed: {code} {reason}")

    def on_error(self, ws, code, reason):
        log.error(f"WebSocket error: {code} {reason}")


def main():
    log.info("=" * 50)
    log.info("RBE ENGINE — Range Breakout Scanner")
    log.info("=" * 50)

    if not RANGE_MAP_FILE.exists():
        log.error("No range map — run rbe_startup.py first")
        sys.exit(1)

    token = get_access_token()
    if not token:
        log.error("No Zerodha access token — login required")
        sys.exit(1)

    from kiteconnect import KiteTicker
    engine = RBEngine()
    kws = KiteTicker(ZERODHA_API_KEY, token)
    kws.on_ticks   = engine.on_ticks
    kws.on_connect = engine.on_connect
    kws.on_close   = engine.on_close
    kws.on_error   = engine.on_error

    kws.connect(threaded=True)

    # Run until market close
    while True:
        time.sleep(60)
        if not market_is_open():
            log.info(f"Market closed — shutting down. Ticks processed: {engine.tick_count}")
            kws.close()
            break
        log.info(f"Heartbeat — ticks: {engine.tick_count}, signals fired: {len(engine.fired)}")

    sys.exit(0)


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
