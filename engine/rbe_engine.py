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

# Breadth snapshot cadence, and any symbol excluded from the denominator.
BREADTH_INTERVAL_SECONDS = 300
# EMPTY SINCE 2026-11-09. It held CHOLAFIN, citing its zero live_prices row --
# but this process prices from Kite ticks (ZERODHA_TOKEN_MAP, built only from
# NSE/EQ instruments), never from live_prices, and its own log shows real
# CHOLAFIN prices throughout (e.g. SIGNAL CHOLAFIN @ 1533.7 on 21 Oct against a
# 1530.90 bhavcopy close the next day). The zero row was Upstox subscribed to a
# debenture (fixed in 57b2f6e). An entry here needs a reason that is true of
# THIS process's price source.
BREADTH_EXCLUDE = set()

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
# The heartbeat cadence, and the interval atlas/freshness.py measured its
# threshold on. Changing this invalidates that derivation -- the monitor says so
# out loud if the two stop agreeing.
HEARTBEAT_SECONDS = 60
BREAKOUT_BUFFER = 0.002   # 0.2% beyond range boundary
RVOL_MIN        = 1.5
MARKET_OPEN     = (9, 15)
# TWO WINDOWS, NOT ONE. Until 2026-11-04 a single MARKET_CLOSE = (15, 15) ended
# the whole process, because this began (adfb324, 11 Jun) as a breakout SCANNER
# -- "continuous scan 9:15 AM - 3:15 PM", a window for breakouts still tradeable
# before MIS square-off. Breadth, heavy_quiet and intraday_prices were added to
# the process later and inherited that window, so the last fifteen minutes of
# every session -- the closing auction, the heaviest volume of the day -- were
# never captured. NSE trades until 15:30.
#
#   MARKET_CLOSE  NSE's close. Data capture and the process's life run to it.
#   SCAN_CLOSE    the trading window. Breakout signals and the feed-dead
#                 stand-down of ATLAS stop here, exactly as they did before.
MARKET_CLOSE    = (15, 30)
SCAN_CLOSE      = (15, 15)


def _headers():
    return {"apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def now_ist():
    return datetime.now(IST)


_trading_day_memo = {}


def is_session_today() -> bool:
    """Is today an NSE trading day? Memoised per date, fails closed.

    THIS WAS THE HOLE. market_is_open() below tested `weekday() >= 5` and the
    clock and nothing else, so every NSE holiday that falls on a weekday looked
    like a full session. On 2026-10-21 and 2026-10-22 this process ran both days
    end to end -- 5,920,860 and 6,007,262 ticks, 89 signals fired against a
    closed market -- and wrote breadth_snapshots and heavy_quiet rows for two
    sessions that never happened. market_flash read those rows and published a
    sentence about a market that did not open.

    The calendar is not copied here. engine/trading_calendar.require_trading_day
    is the one gate; this is a memo in front of it, because market_is_open() is
    reached from the tick path and the answer cannot change within a day.
    """
    day = now_ist().date()
    if day not in _trading_day_memo:
        try:
            from engine.trading_calendar import require_trading_day
            _trading_day_memo[day] = require_trading_day(day, "rbe_engine", log)
        except Exception as e:
            # Fails closed, same as the gate itself: a scanner that cannot tell
            # whether the market is open must not publish a session.
            log.error(f"trading calendar unreadable ({e}) — treating today as "
                      f"CLOSED")
            _trading_day_memo[day] = False
    return _trading_day_memo[day]


def market_is_open():
    if not is_session_today():
        return False
    t = now_ist()
    mins = t.hour * 60 + t.minute
    return MARKET_OPEN[0]*60+MARKET_OPEN[1] <= mins <= MARKET_CLOSE[0]*60+MARKET_CLOSE[1]


def scan_is_open():
    """The trading window: a session and not yet past SCAN_CLOSE. Built on
    market_is_open() so it inherits the holiday gate rather than repeating it.
    Governs what can lead to a trade or an ATLAS stand-down; capture does not
    ask this."""
    if not market_is_open():
        return False
    return not past_scan_close()


def past_scan_close():
    """Later today than SCAN_CLOSE. The end of the trading window only."""
    t = now_ist()
    return t.hour * 60 + t.minute > SCAN_CLOSE[0]*60 + SCAN_CLOSE[1]


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
        # THE INDEX STRIP (see add_index_instruments). Kept apart from the
        # equity state above so nothing that scans equities can see them.
        self.index_tokens = {}
        self.index_px = {}
        self.index_prev = {}
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
        # EQUITY TICKS ONLY. tick_count is what the feed-dead monitor differences
        # to decide whether to stand ATLAS down. Counting index ticks would let
        # a live index stream mask a dead equity feed, and ATLAS would keep
        # entering against frozen prices -- so the count means what it meant
        # before the indices were subscribed.
        self.tick_count += sum(1 for t in ticks
                               if t.get("instrument_token") not in self.index_tokens)
        for tick in ticks:
            try:
                self.process_tick(tick)
            except Exception as e:
                log.error(f"Tick error: {e}")

    def process_tick(self, tick):
        token = tick["instrument_token"]
        # AN INDEX TICK IS A PRICE FOR THE STRIP AND NOTHING ELSE: no breadth,
        # no range, no breakout. It returns before any of that is reached.
        if token in self.index_tokens:
            px = tick.get("last_price")
            if px:
                self.index_px[token] = px
            return
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

        # NO BREAKOUT AFTER SCAN_CLOSE. The price above is still captured until
        # 15:30; a signal is not. Before the two windows were split, the
        # process exiting at 15:15 was the only thing that stopped firing.
        # The END only: what fires before 09:15 is untouched by this change.
        if past_scan_close():
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

    def _maybe_write_breadth(self) -> None:
        """One snapshot every BREADTH_INTERVAL_SECONDS. Never raises.

        FIVE MINUTES: over 09:15-15:30 that is 75 points, which draws as a
        sparkline without thinning and stores ~75 rows a day. One minute would
        be 375 points for a line nobody can read at that width; fifteen would
        miss the shape of a morning that turns at 10:30.

        BREADTH_EXCLUDE is empty. It once held CHOLAFIN on the strength of its
        Upstox live_prices row, which this process does not read -- see the
        note at the constant.
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
        from engine import observations
        if not observations.record(observations.from_rows(
                "heavy_quiet", rows, date_key="session_date", direction_key=None,
                price_key="ltp", source_file="engine/rbe_engine.py"), log):
            log.warning("heavy/quiet not published: ledger refused the sweep")
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
        # PERSIST BEFORE IT RENDERS. record() never raises; a False means the
        # detection is not published. It stays in self.fired, so a ledger
        # outage costs that detection rather than a retry on every tick.
        from engine import observations
        if not observations.record(observations.from_rows(
                "detection", [record], date_key="signal_date", price_key="entry",
                source_file="engine/rbe_engine.py"), log):
            log.error(f"detection {direction} {sym} not published: ledger refused it")
            return
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

    # ── THE INDEX STRIP, LIVE ─────────────────────────────────────
    #
    # index_quotes was written four times a session by engine/index_quotes.py,
    # "aligned to the live_prices refreshes so the strip and the heatmap are
    # never from different moments". The heatmap moved to intraday_prices at
    # five minutes, so that alignment came to guarantee the mismatch it was
    # meant to prevent. This socket already streams all session; the indices
    # cost six tokens and the near-month USDINR future one more.
    #
    # ONE MINUTE, ON ITS OWN CADENCE: write_index_quotes is called from the
    # main loop's 60s heartbeat. The equity five-minute writer is untouched.
    #
    # PREVIOUS CLOSE FROM THE 08:45 RUN, not from the tick. index_quotes.py
    # writes prev_close from Kite's quote ohlc.close before the open; what an
    # INDEX tick carries in its own close field has not been verified, so it
    # is not used.

    def add_index_instruments(self, kite) -> None:
        """Resolve the strip's instruments. Never raises: the equity scanner must
        start whether or not this can."""
        try:
            from index_quotes import INDICES, near_month_usdinr
        except Exception as e:
            log.error(f"index strip disabled — cannot import index_quotes: {e}")
            return
        # BY NAME, as index_quotes.py reads them: a recorded token that drifted
        # would silently point a tile at the wrong index.
        try:
            master = {r["tradingsymbol"]: r["instrument_token"]
                      for r in kite.instruments("NSE") if r.get("segment") == "INDICES"}
        except Exception as e:
            log.warning(f"NSE instrument master unreadable ({e}) — using recorded tokens")
            master = {}
        for sym, name, instrument, recorded in INDICES:
            tok = master.get(name, recorded)
            if master and tok != recorded:
                log.warning(f"{name}: token {tok} in the master, {recorded} recorded")
            self.index_tokens[tok] = {"symbol": sym, "display_name": name,
                                      "instrument": instrument, "exchange": "NSE",
                                      "is_future": False, "expiry": None}
        try:
            usd = near_month_usdinr(kite)
            if usd:
                self.index_tokens[usd["token"]] = {
                    "symbol": "USDINR", "display_name": "USDINR FUT",
                    "instrument": usd["tradingsymbol"], "exchange": "CDS",
                    "is_future": True, "expiry": usd["expiry"].isoformat()}
        except Exception as e:
            log.error(f"USDINR not added to the strip: {e}")
        self.index_prev = self._index_prev_closes()
        log.info(f"index strip: {len(self.index_tokens)} instrument(s), previous close "
                 f"known for {len(self.index_prev)}")

    def _index_prev_closes(self) -> dict:
        """{symbol: prev_close} from rows written TODAY, for the same instrument.
        A prev_close from an earlier day, or from a USDINR contract we are not
        streaming, would put a wrong change on the tile; it is left unknown."""
        today = now_ist().date().isoformat()
        want = {m["symbol"]: m["instrument"] for m in self.index_tokens.values()}
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/index_quotes",
                             params={"select": "symbol,instrument,prev_close,updated_at"},
                             headers=_headers(), timeout=10)
            rows = r.json() if r.status_code == 200 else []
        except Exception as e:
            log.warning(f"index prev_close unreadable: {e}")
            return {}
        out = {}
        for row in rows:
            sym, prev = row.get("symbol"), row.get("prev_close")
            if sym not in want or not prev:
                continue
            if str(row.get("instrument")) != str(want[sym]):
                continue
            ts = str(row.get("updated_at") or "")
            try:
                day = datetime.fromisoformat(ts).astimezone(IST).date().isoformat()
            except ValueError:
                continue
            if day == today:
                out[sym] = float(prev)
        return out

    def write_index_quotes(self) -> None:
        """Upsert every instrument that has ticked. Called once a heartbeat.
        Never raises -- a failed write must not take the scanner down.

        The row's freshness is updated_at; whether the PRICE moved is decided
        by the database (index_quotes.price_changed_at), not here."""
        if not self.index_px:
            return
        stamp = now_ist().replace(microsecond=0).isoformat()
        rows = []
        for tok, px in self.index_px.items():
            meta = self.index_tokens.get(tok)
            if not meta:
                continue
            prev = self.index_prev.get(meta["symbol"])
            rows.append({**meta, "ltp": float(px), "prev_close": prev,
                         "change_pct": (round((px - prev) / prev * 100.0, 2)
                                        if prev else None),
                         "updated_at": stamp})
        if not rows:
            return
        try:
            resp = requests.post(f"{SUPABASE_URL}/rest/v1/index_quotes",
                                 headers={**_headers(),
                                          "Prefer": "resolution=merge-duplicates"},
                                 json=rows, timeout=10)
            if resp.status_code not in (200, 201, 204):
                log.warning(f"index quotes rejected: HTTP {resp.status_code} "
                            f"{resp.text[:120]}")
        except Exception as e:
            log.warning(f"index quotes write failed: {e}")

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
        # The indices ride the same socket: six index tokens and one CDS future
        # beside ~498 equities, under Kite's documented 3,000 per connection.
        idx = list(self.index_tokens)
        ws.subscribe(tokens + idx)
        ws.set_mode(ws.MODE_QUOTE, tokens + idx)
        log.info(f"WebSocket connected — subscribed {len(tokens)} tokens (quote mode)"
                 f" + {len(idx)} index/FX token(s) for the strip")

    def on_close(self, ws, code, reason):
        log.info(f"WebSocket closed: {code} {reason}")

    def on_error(self, ws, code, reason):
        log.error(f"WebSocket error: {code} {reason}")


def feed_is_dead(engine, kws, verdict: dict) -> None:
    """The WebSocket has stopped delivering. Stand the session down. Never raises.

    THREE THINGS, IN THIS ORDER, and the order is the point: stop ATLAS from
    entering, tell the operator, then stop this process. The stand-down goes
    first because it is the only one of the three that protects money -- an
    alert nobody reads at 09:18 and a process that exits quietly both leave the
    entry engine evaluating a frozen price, which is what happened for six
    hours on 2026-10-20.

    WHAT IT DOES NOT TOUCH. Exits. sentiment_window's stand-down is an entry
    control by construction -- stops, targets, trailing and the 15:20 GTT
    handover all continue through it. A dead price feed is a reason to stop
    opening risk and never a reason to stop protecting what is already open.
    """
    detail = verdict.get("detail", "")
    log.error("=" * 70)
    log.error(f"FEED DEAD — {detail}")
    log.error("=" * 70)

    # 1. STAND ATLAS DOWN. One-way, persisted, entry-only, expires at the date
    #    rollover. Carries its own reason code so a frozen feed is countable
    #    separately from a market reversal in atlas_entry_log.blocked_by.
    try:
        from atlas.execution import sentiment_window as SD
        SD.stand_down("FEED_STALE", detail,
                      reason_code=SD.REASON_FEED_STALE)
    except Exception as e:
        log.error(f"could not stand ATLAS down ({e}) — the entry engine may "
                  f"still be evaluating a frozen price. Halt it by hand.")

    # 2. TELL THE OPERATOR. stand_down() sends its own message; this one names
    #    the feed and the numbers, because "ATLAS stood down" and "the Zerodha
    #    WebSocket stopped at 09:18 after 498 ticks" call for different actions.
    try:
        from atlas.reporting.telegram import send
        send("📉 <b>RBE FEED IS DEAD</b>\n"
             f"<code>{str(detail)[:400]}</code>\n\n"
             f"Ticks received this session: {engine.tick_count}\n"
             f"Signals fired: {len(engine.fired)}\n\n"
             "ATLAS has been stood down for the session — no new entries "
             "either side. EXITS ARE UNAFFECTED. The range scanner is "
             "stopping; it does not re-arm if the feed comes back.")
    except Exception as e:
        log.error(f"could not send the feed-dead alert: {e}")

    # 3. STOP SCANNING. A socket that has delivered nothing for
    #    MAX_IDENTICAL_INTERVALS cannot produce a breakout, and every further
    #    cycle against a frozen price is a decision made on a number that is
    #    not a price.
    try:
        kws.close()
    except Exception as e:
        log.warning(f"could not close the websocket cleanly: {e}")


def main():
    log.info("=" * 50)
    log.info("RBE ENGINE — Range Breakout Scanner")
    log.info("=" * 50)

    # NOT A TRADING DAY, NOTHING TO SCAN. This refusal did not exist until
    # 2026-10-22 and its absence published two holidays as sessions; see
    # is_session_today(). It sits before the token read and the socket so a
    # holiday costs one log line rather than a connection and 6M phantom ticks.
    if not is_session_today():
        log.info("Not an NSE trading day — not connecting. Nothing to scan, "
                 "and nothing may be published for a session that does not "
                 "exist.")
        sys.exit(0)

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

    # THE STRIP'S INSTRUMENTS, resolved before the socket opens so on_connect
    # subscribes them with the equities. A failure here leaves the strip on
    # its 08:45 row and the scanner untouched.
    try:
        from kiteconnect import KiteConnect
        _kite = KiteConnect(api_key=ZERODHA_API_KEY)
        _kite.set_access_token(token)
        engine.add_index_instruments(_kite)
    except Exception as e:
        log.error(f"index strip not started: {e}")

    kws = KiteTicker(ZERODHA_API_KEY, token)
    kws.on_ticks   = engine.on_ticks
    kws.on_connect = engine.on_connect
    kws.on_close   = engine.on_close
    kws.on_error   = engine.on_error

    kws.connect(threaded=True)

    # ── THE HEARTBEAT, DIFFERENCED ────────────────────────────────
    #
    # This loop used to log engine.tick_count and nothing else. On 2026-10-20 it
    # printed "Heartbeat — ticks: 498" three hundred and sixty times: the
    # WebSocket had delivered one tick per subscribed token at 09:14 and then
    # stopped, and because the number was reported rather than DIFFERENCED,
    # six hours of total silence read exactly like a busy session. Monday had
    # delivered 5,770,958 ticks.
    #
    # The monitor owns the threshold and the evidence for it; see
    # atlas/freshness.py. All this loop does is hand it one number per interval
    # and act on the verdict.
    from atlas.freshness import Monitor
    # baseline=0: no ticks yet. Anything above it proves the socket delivered,
    # even if the delivery happened before the first heartbeat -- which is
    # exactly what happened on 2026-10-20.
    ticks = Monitor("rbe tick count", interval_seconds=HEARTBEAT_SECONDS,
                    baseline=0)
    prev_count = engine.tick_count

    while True:
        time.sleep(HEARTBEAT_SECONDS)
        if not market_is_open():
            log.info(f"Market closed — shutting down. Ticks processed: {engine.tick_count}")
            kws.close()
            break

        # THE STRIP'S OWN CADENCE: once a heartbeat, i.e. once a minute.
        engine.write_index_quotes()

        count = engine.tick_count
        delta = count - prev_count
        prev_count = count
        verdict = ticks.observe(count)

        # THE DELTA IS IN THE LINE. A heartbeat whose number a reader has to
        # difference against the previous line by eye is exactly the thing that
        # failed on 2026-10-20, and "+0" is the whole finding in two characters.
        log.info(f"Heartbeat — ticks: {count} (+{delta} this "
                 f"{HEARTBEAT_SECONDS}s), signals fired: {len(engine.fired)}")

        if verdict["stale"]:
            if scan_is_open():
                feed_is_dead(engine, kws, verdict)
            else:
                # AFTER SCAN_CLOSE THE FEED ONLY FEEDS CAPTURE. Standing ATLAS
                # down here would be a new trading action in a window that
                # never had one before the windows were split; it does not
                # happen. Capture ends early, and the page's own freshness rule
                # shows the gap as STALE.
                log.error(f"feed stopped after scan close — capture ends early. "
                          f"{verdict.get('detail', '')}")
                kws.close()
            break

    sys.exit(0)


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
