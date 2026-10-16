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
# StreamHandler ONLY -- cron redirects stdout to reports/rbe.log, so a
# FileHandler on the same path duplicated every line. See rbe_startup.py.
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("RBE")

IST = timezone(timedelta(hours=5, minutes=30))

SUPABASE_URL = "https://eibdlcanpudjgmkjxrga.supabase.co"
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
ZERODHA_API_KEY = os.environ.get("ZERODHA_API_KEY", "")

RANGE_MAP_FILE  = Path("data/processed/rbe/range_map.json")

# Breadth snapshot cadence, and the one symbol excluded from the denominator.
BREADTH_INTERVAL_SECONDS = 300
BREADTH_EXCLUDE = {"CHOLAFIN"}

# ── HEAVY VOLUME, LITTLE MOVEMENT ─────────────────────────────────
#
# Above 3x its own average volume while inside +/-0.5% on the day. Both halves
# come from this process and nowhere else:
#
#   volume   volume_traded on the tick, against avg_vol_20d in the range map
#   change   ltp against prev_close in the range map -- NOT
#            live_prices.change_pct, which reads 0.0 for all 745 rows
#
# THE RATIO IS PACE, NOT CUMULATIVE, and that is a real decision rather than a
# detail. "3x average volume" is ambiguous intraday: raw cumulative volume
# cannot reach 3x a full day's average until very late in the session, so a
# raw test leaves this block empty every morning and then fills it near the
# close -- which reads as broken rather than as information. The
# time-normalised ratio asks "is this running at 3x its normal pace for this
# time of day", which is what the phrase means to a reader and is the same
# quantity rbe_engine already computes for its breakout filter.
HEAVY_VOL_RATIO = 3.0
QUIET_MOVE_PCT = 0.5
# Before 09:45 volume_bucket is working off an assumed 5% floor or a single
# curve point, so the ratio divides by a number nobody measured. Two curve
# points in, it means something.
HEAVY_QUIET_FROM = (9, 45)
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
        self.generated_at = data.get("generated_at", "")
        self.fired  = self._load_fired_today()
        self.tick_count = 0
        # ── BREADTH, SNAPSHOTTED ──────────────────────────────────
        #
        # WHY HERE AND NOT FROM live_prices. live_prices refreshes four times a
        # session (08:45 / 09:45 / 11:30 / 15:15 IST) and is an upsert, so it
        # keeps no history at all -- a breadth sparkline built on it is four
        # points, three of them hours apart, and reads as a flat market. This
        # process already receives last_price for every subscribed symbol all
        # session, and the range map already carries prev_close, so advancing
        # vs declining is a subtraction on data that is here anyway.
        #
        # THE DENOMINATOR IS ~498, NOT 745, and the page says so. These are the
        # symbols on the websocket; the universe is larger. Reporting it as
        # full-universe breadth would be a bigger number and a false one.
        self.last_px = {}
        self.last_vol = {}
        self.breadth_last_write = 0.0
        log.info(f"Range map loaded: {len(self.ranges)} stocks | "
                 f"generated {self.generated_at or 'unknown'} | "
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

        # EVERY TICK FEEDS BREADTH, before any breakout logic can return
        # early -- a symbol that never breaks out still counts as advancing or
        # declining, and most of them never break out.
        self.last_px[sym] = ltp
        self.last_vol[sym] = vol
        self._maybe_write_breadth()

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

    def _maybe_write_breadth(self) -> None:
        """One snapshot every BREADTH_INTERVAL_SECONDS. Never raises.

        FIVE MINUTES: over 09:15-15:30 that is 75 points, which draws as a
        sparkline without thinning and stores ~75 rows a day. One minute would
        be 375 points for a line nobody can read at that width; fifteen would
        miss the shape of a morning that turns at 10:30.

        CHOLAFIN IS EXCLUDED. Its live_prices row has read ltp=0 since
        22 September, so COUNT(*) reports full coverage while it has no usable
        price. Counted here it would be a decline in every single snapshot.
        """
        now = time.time()
        if now - self.breadth_last_write < BREADTH_INTERVAL_SECONDS:
            return
        if not market_is_open():
            return
        self.breadth_last_write = now

        adv = dec = unch = 0
        for sym, px in self.last_px.items():
            if sym in BREADTH_EXCLUDE:
                continue
            prev = (self.ranges.get(sym) or {}).get("prev_close")
            if not prev or not px:
                continue
            if px > prev:
                adv += 1
            elif px < prev:
                dec += 1
            else:
                unch += 1
        tracked = adv + dec + unch
        if not tracked:
            return
        self._write_heavy_quiet(tracked)
        self._write_intraday_prices()
        row = {"session_date": now_ist().date().isoformat(),
               "at": now_ist().replace(microsecond=0).isoformat(),
               "advancing": adv, "declining": dec, "unchanged": unch,
               "tracked": tracked,
               "pct_advancing": round(adv / tracked * 100.0, 2)}
        try:
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/breadth_snapshots",
                headers={**_headers(),
                         "Prefer": "resolution=merge-duplicates"},
                json=row, timeout=10)
            if r.status_code not in (200, 201, 204):
                log.warning(f"breadth snapshot rejected: HTTP "
                            f"{r.status_code} {r.text[:120]}")
            else:
                log.info(f"breadth {adv}/{dec} of {tracked} tracked "
                         f"({row['pct_advancing']}% advancing)")
        except Exception as e:
            # A failed snapshot must never take the breakout scanner down.
            log.warning(f"breadth snapshot failed: {e}")

    def _write_intraday_prices(self) -> None:
        """Day change for every symbol on the feed. Never raises.

        WHY THIS EXISTS AT ALL. live_prices.change_pct is 0.0 for all 745 rows
        and always has been: upstox_ws computes (ltp - ohlc.close)/ohlc.close
        and reads ohlc.close as the PREVIOUS close, while the same ohlc block
        is read elsewhere in that file as TODAY'S high/low/open. Both cannot be
        true. ohlc.close is today's close-so-far, which during a session IS the
        last price, so the expression is (ltp - ltp)/ltp = exactly 0 for every
        symbol -- which is why the column has one distinct value across 745
        rows while ltp has 741.

        Here the previous close is prev_close from the range map, which is a
        different field from a different endpoint and is unambiguous. Same
        source as breadth and heavy/quiet, so all three agree by construction.

        ONE ROW PER SYMBOL, upserted, exactly like live_prices: the table stays
        at ~498 rows and the page's read is trivial. volume_ratio rides along
        so the mover rule has a real intraday denominator instead of the
        end-of-day rvol on the signals table.
        """
        now = now_ist()
        frac = volume_bucket(self.curve)
        stamp = now.replace(microsecond=0).isoformat()
        rows = []
        for sym, px in self.last_px.items():
            if sym in BREADTH_EXCLUDE:
                continue
            r = self.ranges.get(sym) or {}
            prev, avg = r.get("prev_close"), r.get("avg_vol_20d")
            if not prev or not px:
                continue
            vol = self.last_vol.get(sym)
            ratio = (round(vol / (avg * frac), 2)
                     if (vol and avg and frac) else None)
            rows.append({"symbol": sym, "ltp": round(float(px), 2),
                         "prev_close": round(float(prev), 2),
                         "change_pct": round((px - prev) / prev * 100.0, 2),
                         "volume_ratio": ratio, "snapshot_at": stamp})
        if not rows:
            return
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/intraday_prices",
                headers={**_headers(),
                         "Prefer": "resolution=merge-duplicates"},
                json=rows, timeout=20)
            if resp.status_code not in (200, 201, 204):
                log.warning(f"intraday_prices rejected: HTTP "
                            f"{resp.status_code} {resp.text[:120]}")
            else:
                log.info(f"intraday prices: {len(rows)} symbol(s) written")
        except Exception as e:
            # Must never take the breakout scanner down.
            log.warning(f"intraday_prices write failed: {e}")

    def _write_heavy_quiet(self, tracked: int) -> None:
        """The CURRENT heavy-but-quiet set, every 5 minutes. Never raises.

        A SHARED snapshot_at across the sweep is what lets the page show only
        the latest set. This is a condition that stops being true -- a stock
        heavy and quiet at 10:00 may have moved 3% by 15:00 -- and a one-way
        publisher cannot delete. So every row carries the timestamp of the
        sweep that found it, the page reads only the newest, and a symbol that
        stops qualifying simply stops being in it.
        """
        now = now_ist()
        if (now.hour, now.minute) < HEAVY_QUIET_FROM:
            return
        frac = volume_bucket(self.curve)
        stamp = now.replace(microsecond=0).isoformat()
        rows = []
        for sym, px in self.last_px.items():
            if sym in BREADTH_EXCLUDE:
                continue
            r = self.ranges.get(sym) or {}
            prev, avg = r.get("prev_close"), r.get("avg_vol_20d")
            vol = self.last_vol.get(sym)
            if not prev or not avg or not px or not vol:
                continue
            chg = (px - prev) / prev * 100.0
            if abs(chg) > QUIET_MOVE_PCT:
                continue
            ratio = vol / (avg * frac)
            if ratio < HEAVY_VOL_RATIO:
                continue
            rows.append({"session_date": now.date().isoformat(),
                         "symbol": sym, "snapshot_at": stamp,
                         "volume_ratio": round(ratio, 2),
                         "change_pct": round(chg, 2),
                         "ltp": round(float(px), 2), "tracked": tracked})
        if not rows:
            log.info(f"heavy/quiet: none of {tracked} tracked qualify")
            return
        try:
            resp = requests.post(
                f"{SUPABASE_URL}/rest/v1/heavy_quiet",
                headers={**_headers(),
                         "Prefer": "resolution=merge-duplicates"},
                json=rows, timeout=10)
            if resp.status_code not in (200, 201, 204):
                log.warning(f"heavy/quiet rejected: HTTP {resp.status_code} "
                            f"{resp.text[:120]}")
            else:
                log.info(f"heavy/quiet: {len(rows)} of {tracked} tracked "
                         f"above {HEAVY_VOL_RATIO}x pace inside "
                         f"+/-{QUIET_MOVE_PCT}%")
        except Exception as e:
            # Must never take the breakout scanner down.
            log.warning(f"heavy/quiet write failed: {e}")

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
        if not tokens:
            # Kite answers subscribe([]) with "error parsing request" and then
            # sits there. main() should have caught this already; belt and
            # braces so we never hold an idle socket for a whole session.
            log.error("No subscribable tokens — closing socket instead of "
                      "subscribing to an empty list.")
            ws.close()
            return
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

    # Validate the map BEFORE opening a socket. On 2026-08-11 an expired token
    # left rbe_startup writing a map with 0 stocks; this process then subscribed
    # to an empty token list, Kite replied "error parsing request", and it ran
    # 09:14-15:16 IST logging 0-tick heartbeats. Neither condition is
    # recoverable at runtime, so refuse to start.
    if not engine.ranges:
        log.error("Range map is EMPTY (0 stocks) — refusing to start. "
                  "Re-run rbe_startup.py; if it also reports 0, the Zerodha "
                  "access token is missing or expired.")
        sys.exit(1)

    today = now_ist().date().isoformat()
    if engine.generated_at != today:
        log.error(f"Range map is STALE — generated {engine.generated_at or 'unknown'}, "
                  f"today is {today}. Ranges and PDH/PDL would be wrong. "
                  f"Re-run rbe_startup.py.")
        sys.exit(1)

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
