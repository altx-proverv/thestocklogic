#!/usr/bin/env python3
"""CAPTURE RUNS TO NSE'S CLOSE; TRADING STOPS WHERE IT ALWAYS DID.

Until 2026-11-04 rbe_engine had one window, MARKET_CLOSE = (15, 15), and the
process exited at it. That was a trading deadline -- the breakout scanner's
window before MIS square-off -- and the captures added to the process later
(breadth_snapshots, heavy_quiet, intraday_prices) inherited it, so the last
fifteen minutes of every session were never recorded.

1. CAPTURE RUNS TO 15:30. market_is_open() is true at 15:20 and 15:30, false at
   15:31, and the five-minute writer writes at 15:25.

2. TRADING DOES NOT MOVE. No breakout fires after 15:15. A dead feed stands
   ATLAS down only inside the scan window. What fires before 09:15 is
   untouched -- the change bounds the END of the scan, nothing else.

Run against the real module with a fake clock; nothing touches the network.
"""
import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

import engine.rbe_engine as R                                  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<70} {str(got)[:18]:<20} {'ok' if ok else 'FAIL':<6} {note}")


def head(t):
    print(f"\n{t}\n" + "-" * 110)


def at(hhmm, day="2026-11-03"):
    h, m = map(int, hhmm.split(":"))
    t = datetime.fromisoformat(day).replace(hour=h, minute=m, second=5, tzinfo=IST)
    R.now_ist = lambda: t


R.is_session_today = lambda: True            # a trading day; the calendar has its own suite

# ══════════════════════════════════════════════════════════════════
head("THE TWO WINDOWS")
# ══════════════════════════════════════════════════════════════════
check("MARKET_CLOSE is NSE's close", R.MARKET_CLOSE, (15, 30))
check("SCAN_CLOSE is where trading stopped before", R.SCAN_CLOSE, (15, 15))
for hhmm, cap, scan in [("09:15", True, True), ("15:15", True, True),
                        ("15:16", True, False), ("15:25", True, False),
                        ("15:30", True, False), ("15:31", False, False)]:
    at(hhmm)
    check(f"{hhmm}: capture open / scan open", (R.market_is_open(), R.scan_is_open()),
          (cap, scan))
R.is_session_today = lambda: False
at("11:00")
check("a holiday: neither window is open", (R.market_is_open(), R.scan_is_open()),
      (False, False), "the scan window inherits the session gate")
R.is_session_today = lambda: True


# ══════════════════════════════════════════════════════════════════
head("A TICK: PRICE ALWAYS CAPTURED, BREAKOUT ONLY INSIDE THE SCAN")
# ══════════════════════════════════════════════════════════════════
def engine():
    e = R.RBEngine.__new__(R.RBEngine)
    e.ranges = {"ABC": {"range_high": 100.0, "range_low": 90.0, "pdh": 99.0,
                        "pdl": 89.0, "avg_vol_20d": 1000, "prev_close": 95.0}}
    e.curve = {"09:15": 0.05}
    e.fired, e.tick_count = set(), 0
    e.last_px, e.last_vol, e.breadth_last_write = {}, {}, 0.0
    e.index_tokens, e.index_px, e.index_prev = {}, {}, {}
    e.fires, e.writes = [], []
    e.fire_signal = lambda *a: e.fires.append(a[:2])
    e._maybe_write_breadth = lambda: None
    return e


R.TOKEN_SYMBOL_MAP[999001] = "ABC"
BREAKOUT = {"instrument_token": 999001, "last_price": 101.0, "volume_traded": 10_000}
for hhmm, fires in [("09:14", True), ("14:00", True), ("15:15", True),
                    ("15:16", False), ("15:29", False)]:
    at(hhmm)
    e = engine()
    e.process_tick(dict(BREAKOUT))
    check(f"{hhmm}: price captured", e.last_px.get("ABC"), 101.0)
    check(f"{hhmm}: breakout fires", bool(e.fires), fires,
          "09:14 unchanged: only the end of the scan was bounded" if hhmm == "09:14" else "")

# ══════════════════════════════════════════════════════════════════
head("THE FIVE-MINUTE WRITER RUNS TO 15:30")
# ══════════════════════════════════════════════════════════════════
import requests                                                # noqa: E402
_post = requests.post


class _Ok:
    status_code = 201
    text = ""


for hhmm, writes in [("15:10", True), ("15:25", True), ("15:30", True), ("15:31", False)]:
    at(hhmm)
    e = R.RBEngine.__new__(R.RBEngine)
    e.ranges = {"ABC": {"prev_close": 95.0}}
    e.last_px, e.last_vol, e.breadth_last_write = {"ABC": 101.0}, {"ABC": 10}, 0.0
    called = []
    e._write_heavy_quiet = lambda n: called.append("heavy_quiet")
    e._write_intraday_prices = lambda: called.append("intraday_prices")
    requests.post = lambda *a, **k: (called.append("breadth") or _Ok())
    e._maybe_write_breadth()
    check(f"{hhmm}: breadth, heavy_quiet and intraday_prices written",
          sorted(called) == ["breadth", "heavy_quiet", "intraday_prices"], writes)
requests.post = _post

# ══════════════════════════════════════════════════════════════════
head("A DEAD FEED STANDS ATLAS DOWN ONLY INSIDE THE SCAN WINDOW")
# ══════════════════════════════════════════════════════════════════
# Structural, because main() is the process: the call to feed_is_dead must sit
# under an `if` whose test is scan_is_open(), and nowhere else.
src = (ROOT / "engine" / "rbe_engine.py").read_text()
main = next(n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "main")
guarded, unguarded = 0, 0
for node in ast.walk(main):
    if isinstance(node, ast.If):
        test = ast.unparse(node.test)
        for c in ast.walk(ast.Module(body=node.body, type_ignores=[])):
            if isinstance(c, ast.Call) and ast.unparse(c.func) == "feed_is_dead":
                if test == "scan_is_open()":
                    guarded += 1
calls = sum(1 for c in ast.walk(main)
            if isinstance(c, ast.Call) and ast.unparse(c.func) == "feed_is_dead")
check("main() calls feed_is_dead exactly once", calls, 1)
check("  and only under `if scan_is_open():`", guarded, 1)
loop_exit = [ast.unparse(n.test) for n in ast.walk(main) if isinstance(n, ast.If)
             and any(isinstance(c, ast.Break) for c in n.body)]
check("the process exits on market_is_open(), i.e. at 15:30",
      "not market_is_open()" in loop_exit, True)

# ══════════════════════════════════════════════════════════════════
head("THE INDEX STRIP RIDES THE SOCKET WITHOUT TOUCHING WHAT ATLAS DEPENDS ON")
# ══════════════════════════════════════════════════════════════════
at("11:00")
NIFTY_TOK, USD_TOK = 256265, 999777
e = engine()
e.index_tokens = {NIFTY_TOK: {"symbol": "NIFTY50", "display_name": "NIFTY 50",
                              "instrument": "NIFTY 50", "exchange": "NSE",
                              "is_future": False, "expiry": None},
                  USD_TOK: {"symbol": "USDINR", "display_name": "USDINR FUT",
                            "instrument": "USDINR26NOVFUT", "exchange": "CDS",
                            "is_future": True, "expiry": "2026-11-26"}}
e.process_tick = R.RBEngine.process_tick.__get__(e)
e.on_ticks(None, [{"instrument_token": NIFTY_TOK, "last_price": 23700.5},
                  {"instrument_token": USD_TOK, "last_price": 96.2},
                  dict(BREAKOUT)])
check("index ticks do NOT feed tick_count (the feed-dead monitor's input)",
      e.tick_count, 1, "a live index stream must not mask a dead equity feed")
check("  the equity tick still does", "ABC" in e.last_px, True)
check("an index tick is a price for the strip", e.index_px.get(NIFTY_TOK), 23700.5)
check("  and reaches no equity state", (NIFTY_TOK in e.last_px, "NIFTY50" in e.last_px),
      (False, False))
check("  and fires nothing", [f for f in e.fires if f[0] != "ABC"], [])

posted = []
requests.post = lambda url, **k: (posted.append((url, k.get("json"))) or _Ok())
e.index_prev = {"NIFTY50": 23440.8}
e.write_index_quotes()
requests.post = _post
rows = {r["symbol"]: r for r in (posted[0][1] if posted else [])}
check("one write to index_quotes per heartbeat", [u.rsplit("/", 1)[-1] for u, _ in posted],
      ["index_quotes"])
check("prev_close is the 08:45 row's, carried as read",
      rows.get("NIFTY50", {}).get("prev_close"), 23440.8)
check("  and the change is against it",
      rows.get("NIFTY50", {}).get("change_pct"), round((23700.5 - 23440.8) / 23440.8 * 100, 2))
check("no known prev_close: the change is unknown, not zero",
      (rows.get("USDINR", {}).get("prev_close"), rows.get("USDINR", {}).get("change_pct")),
      (None, None))
check("the payload never carries price_changed_at (the database sets it)",
      any("price_changed_at" in r for r in rows.values()), False)


class _R:
    status_code = 200

    def __init__(self, b): self._b = b

    def json(self): return self._b


_get = requests.get
requests.get = lambda *a, **k: _R([
    {"symbol": "NIFTY50", "instrument": "NIFTY 50", "prev_close": 23440.8,
     "updated_at": "2026-11-03T08:45:02+05:30"},
    {"symbol": "USDINR", "instrument": "USDINR26N06FUT", "prev_close": 96.21,
     "updated_at": "2026-11-03T08:45:02+05:30"}])
check("prev_close from today's row is used",
      e._index_prev_closes().get("NIFTY50"), 23440.8)
check("  a USDINR prev_close for a DIFFERENT contract is not",
      "USDINR" in e._index_prev_closes(), False, "a weekly's close against the monthly's price")
requests.get = lambda *a, **k: _R([
    {"symbol": "NIFTY50", "instrument": "NIFTY 50", "prev_close": 23000.0,
     "updated_at": "2026-11-02T08:45:02+05:30"}])
check("  a prev_close written on an EARLIER day is not", e._index_prev_closes(), {})
requests.get = _get


class _WS:
    def __init__(self): self.subs, self.closed = [], False

    def subscribe(self, t): self.subs = list(t)

    def set_mode(self, m, t): pass

    def close(self): self.closed = True

    MODE_QUOTE = "quote"


ws = _WS()
R.ZERODHA_TOKEN_MAP["ABC"] = 999001
e.on_connect(ws, None)
check("on_connect subscribes the equities AND the strip",
      sorted(ws.subs), sorted([999001, NIFTY_TOK, USD_TOK]))
e2 = engine()
e2.ranges = {}
e2.index_tokens = e.index_tokens
ws2 = _WS()
e2.on_connect(ws2, None)
check("  no equities: the socket still closes rather than idle on indices alone",
      (ws2.closed, ws2.subs), (True, []))

print("\n" + "=" * 110)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — capture runs to 15:30; breakouts and the stand-down stop at 15:15 as before")
