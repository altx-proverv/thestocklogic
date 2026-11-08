#!/usr/bin/env python3
"""VOLUME ALERTS: the multiple excludes the session, every send needs a
message_id, a quiet day still speaks, a symbol is sent once per session, and a
failed send alarms. Plus 06_push's notify_atlas docstring held to its body.

Every send is captured; nothing here reaches Telegram or Supabase.
"""
import ast
import importlib.util
import os
import re
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TSL_VOLUME_ALERT_DIR"] = tempfile.mkdtemp()

import pandas as pd                                            # noqa: E402
from engine import volume_alerts as V                          # noqa: E402
from engine import trading_calendar as tc                      # noqa: E402
from radar import bars                                         # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<68} {str(got)[:18]:<20} {'ok' if ok else 'FAIL'}")


# ── synthetic bhavcopy ──────────────────────────────────────────────
D = date(2026, 11, 2)                       # a real session in the calendar
check("2026-11-02 is a trading day in the calendar", tc.is_trading_day(D), True)
days = [D]
while len(days) < 30:
    days.insert(0, tc.prev_trading_day(days[0]))

stocks = Path(tempfile.mkdtemp())
bars.STOCKS_DIR = stocks


def write(sym, vols, ranges=None, closes=None):
    n = len(vols)
    ranges = ranges or [2.0] * n
    closes = closes or [100.0] * n
    prev = [closes[0]] + closes[:-1]
    df = pd.DataFrame({"date": days[-n:], "volume": vols,
                       "close": closes, "prev_close": prev,
                       "high": [c + r / 2 for c, r in zip(closes, ranges)],
                       "low": [c - r / 2 for c, r in zip(closes, ranges)]})
    df.to_parquet(stocks / f"{sym}.parquet")


BASE = 1000
SPIKE = 50_000
write("SPIKE", [BASE] * 29 + [SPIKE], ranges=[2.0] * 29 + [16.0],
      closes=[100.0] * 29 + [120.0])
write("ARE&M", [BASE] * 29 + [BASE * 12])
write("NINE", [BASE] * 29 + [BASE * 9.99])
write("STALE", [BASE] * 29, )                 # no row for D
pd.read_parquet(stocks / "STALE.parquet").assign(        # shift: ends the session before D
    date=days[-30:-1]).to_parquet(stocks / "STALE.parquet")
write("YOUNG", [BASE] * 10 + [SPIKE])         # under 20 prior sessions
bars.clear_cache()

print("\nMEASUREMENT — radar's basis, the session excluded")
m = V.measure("SPIKE", D)
want = SPIKE / BASE                                   # derived: prior mean is BASE
incl = SPIKE / ((BASE * (V.VOL_WINDOW - 1) + SPIKE) / V.VOL_WINDOW)
check("computed multiple = volume / prior-20 mean", round(m["mult"], 6), round(want, 6))
check("  and is not the including-the-day figure", round(m["mult"], 2) != round(incl, 2), True)
check("move from prev_close", round(m["move"], 6), round((120 / 100 - 1) * 100, 6))
check("range multiple = (H-L) / prior-20 mean (H-L)", round(m["range_mult"], 6), round(16.0 / 2.0, 6))
check("no row for the session -> no_row", V.measure("STALE", D), "no_row")
check("under 20 prior sessions -> short", V.measure("YOUNG", D), "short")

U = ["SPIKE", "ARE&M", "NINE", "STALE", "YOUNG"]
res = V.scan(D, U)
check("hits are the two at or above 10x, highest first",
      [h["symbol"] for h in res["hits"]], ["SPIKE", "ARE&M"])
check("9.99x is not a hit", "NINE" in [h["symbol"] for h in res["hits"]], False)
check("counts: measured / no_row / short", (res["measured"], res["no_row"], res["short"]), (3, 1, 1))


# ── surfaces ─────────────────────────────────────────────────────────
def getter_factory(tables):
    def g(table, query):
        v = tables.get(table)
        if isinstance(v, Exception):
            raise v
        if table == "signals":
            return v or []
        return v or []
    return g


TABLES = {
    "radar_card_versions": [{"rank": "1", "radar_events": {"symbol": "SPIKE"}},
                            {"rank": "2", "radar_events": {"symbol": "OTHER"}}],
    "radar_scanner_daily": [],                          # nothing written that session
    "signals": [{"symbol": "SPIKE", "publication_kind": "signal", "rvol": 14.49},
                {"symbol": "OTHER", "publication_kind": "candidate", "rvol": None}],
    "heavy_quiet": RuntimeError("HTTP 503 <html>&bad"),
}

print("\nSURFACES — '?' when nothing was written or the read failed")
surf = V.surfaces(D, getter_factory(TABLES))
check("Hot 10 rank read from the card", V._cell(surf["Hot10"], "SPIKE"), "#1")
check("Hot 10 has rows, symbol absent -> no", V._cell(surf["Hot10"], "ARE&M"), "no")
check("Scanner with zero rows for the session -> ?", V._cell(surf["Scanner"], "SPIKE"), "?")
check("Signal present", V._cell(surf["Signal"], "SPIKE"), "yes")
check("Watchlist has rows, symbol absent -> no", V._cell(surf["Watchlist"], "SPIKE"), "no")
check("Heavy unreadable -> ?", V._cell(surf["Heavy"], "SPIKE"), "?")
check("stored rvol only from signal rows", surf["stored"], {"SPIKE": [14.49]})

msgs, groups = V.compose(D, res, res["hits"], surf)
body = "\n".join(msgs)
print("\nTEXT — escaped, labelled, under Telegram's limit")
check("one message", len(msgs), 1)
check("ARE&M is escaped", "ARE&amp;M" in body and "ARE&M" not in body, True)
check("the error string is escaped", "&lt;html&gt;&amp;bad" in body and "<html>" not in body, True)
check("computed and stored are both labelled",
      "50.0× computed" in body and "stored rvol 14.49×" in body, True)
check("no signal row says so, not a zero", "stored rvol — (no signal row)" in body, True)
check("the session date is in the message", D.isoformat() in body, True)
check("the scanner's empty session is explained", "Scanner: no rows at all" in body, True)

many = {"hits": [dict(res["hits"][0], symbol=f"S{i:03d}") for i in range(300)],
        "universe": 705, "measured": 700, "no_row": 3, "short": 2}
big, groups = V.compose(D, many, many["hits"], surf)
check("300 hits split into several messages", len(big) > 1, True)
check("  each under 4096 characters", all(len(x) <= 4096 for x in big), True)
check("  every symbol in exactly one part",
      sorted(s for g in groups for s in g) == sorted(h["symbol"] for h in many["hits"]), True)


# ── delivery ─────────────────────────────────────────────────────────
class Box:
    def __init__(self, mids=None):
        self.msgs, self.mids, self.n = [], mids, 0

    def __call__(self, m):
        self.msgs.append(m)
        self.n += 1
        if self.mids is None:
            return 1000 + self.n
        return self.mids.pop(0) if self.mids else 1000 + self.n


def fresh_state():
    V.STATE_DIR = Path(tempfile.mkdtemp())


G = getter_factory(TABLES)
print("\nDELIVERY")
fresh_state()
b = Box()
check("first run exits 0", V.run(D, sender=b, getter=G, symbols=U), 0)
check("  one message sent", len(b.msgs), 1)
check("  message_id recorded per symbol", V.load_state(D)["symbols"], {"SPIKE": 1001, "ARE&M": 1001})
b2 = Box()
check("second run exits 0", V.run(D, sender=b2, getter=G, symbols=U), 0)
check("  and sends nothing", len(b2.msgs), 0)

fresh_state()
fail = Box(mids=[None])                    # the alert is not accepted, the alarm is
check("a failed send exits 1", V.run(D, sender=fail, getter=G, symbols=U), 1)
check("  and the alarm went out through the sender", "VOLUME ALERT FAILED" in fail.msgs[-1], True)
check("  naming the unsent symbols", "SPIKE" in fail.msgs[-1] and "ARE&amp;M" in fail.msgs[-1], True)
check("  nothing recorded as sent", V.load_state(D)["symbols"], {})
retry = Box()
check("the next run retries and exits 0", V.run(D, sender=retry, getter=G, symbols=U), 0)
check("  sending both symbols", "SPIKE" in retry.msgs[0] and "ARE&amp;M" in retry.msgs[0], True)

print("\nSILENCE MEANS BREAKAGE")
fresh_state()
z = Box()
check("a quiet day exits 0", V.run(D, sender=z, getter=G, symbols=["NINE", "STALE"]), 0)
check("  and still sends one line", len(z.msgs), 1)
check("  with the date and the count",
      D.isoformat() in z.msgs[0] and "no symbol crossed" in z.msgs[0]
      and "Measured 1 of 2" in z.msgs[0], True)
z2 = Box()
V.run(D, sender=z2, getter=G, symbols=["NINE", "STALE"])
check("  a rerun of the quiet day sends nothing", len(z2.msgs), 0)

fresh_state()
nod = Box()
check("no row for any symbol exits 1", V.run(D, sender=nod, getter=G, symbols=["STALE"]), 1)
check("  and alarms instead of saying nothing crossed",
      len(nod.msgs) == 1 and "VOLUME ALERT FAILED" in nod.msgs[0]
      and "no symbol crossed" not in nod.msgs[0], True)

fresh_state()
sat = D + timedelta(days=(5 - D.weekday()) % 7)
h = Box()
check(f"{sat} (Saturday) exits 0", V.run(sat, sender=h, getter=G, symbols=U), 0)
check("  and says it was not a session", "not an NSE trading session" in h.msgs[0], True)

print("\nHTTP 200 IS NOT DELIVERY")
from engine import ledger_alerts as LA                       # noqa: E402
_orig = LA.send
LA.send = lambda m: (True, None, None)
check("accepted without a message_id -> failure", V.send("x"), None)
LA.send = lambda m: (True, 4242, -1)
check("accepted with a message_id -> that id", V.send("x"), 4242)
LA.send = _orig


# ── docstrings held to their bodies ───────────────────────────────────
print("\nDOCSTRING CLAIMS")
CLAIM = re.compile(r"telegram|\b(trigger|send)s\b[^.]{0,60}\balert", re.I)

spec = importlib.util.spec_from_file_location("p6", ROOT / "engine/06_push_supabase.py")
p6 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p6)

calls = []
import requests                                               # noqa: E402
import atlas.reporting.telegram as AT                          # noqa: E402
saved = (requests.post, AT.send, LA.send)
requests.post = lambda *a, **k: calls.append(("post", a)) or None
AT.send = lambda *a, **k: calls.append(("atlas", a)) or True
LA.send = lambda *a, **k: calls.append(("ledger", a)) or (True, 1, 1)
p6.notify_atlas([{"symbol": "X", "grade": "A", "score": 90}])
requests.post, AT.send, LA.send = saved

# The docstring is read from the SOURCE with ast, not from the imported
# function, so the check sees exactly what is committed.
src = (ROOT / "engine/06_push_supabase.py").read_text(encoding="utf-8")
fn = next(n for n in ast.walk(ast.parse(src))
          if isinstance(n, ast.FunctionDef) and n.name == "notify_atlas")
doc = ast.get_docstring(fn) or ""
claims = bool(CLAIM.search(doc))
check("notify_atlas: docstring claims a send == body sends", claims, bool(calls))

vdoc = V.__doc__ or ""
check("volume_alerts docstring claims a Telegram send", bool(CLAIM.search(vdoc)), True)
fresh_state()
vb = Box()
V.run(D, sender=vb, getter=G, symbols=U)
check("  and run() does call the sender", len(vb.msgs) > 0, True)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — volume alerts measure on radar's basis, need a message_id, "
      "speak on a quiet day, and send each symbol once")
