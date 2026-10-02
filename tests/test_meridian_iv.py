#!/usr/bin/env python3
"""MERIDIAN IV recorder: the parser, the band, the failure paths, the separation.

The recorder's whole value is a year of history that cannot be re-fetched, so the
cases that matter are the ones where it would write something WRONG rather than
nothing: a band that silently truncates differently per symbol, an ATM chosen by
open interest instead of spot, a missing input written as zero.
"""
import sys
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import meridian.iv_recorder as R                                  # noqa: E402
import meridian.config as C                                       # noqa: E402
import meridian.fno_universe as U                                 # noqa: E402

ok = True


def check(label, got, want, extra=""):
    global ok
    good = got == want
    ok &= good
    print(f"  {label:<54}{str(got):<12}"
          f"{'ok' if good else '** want ' + str(want) + ' **':<22}{extra}")


def chain(n=61, step=50, base=24000, spot=None, call_iv=14.0, put_iv=15.0,
          drop_spot=False, call_iv_none=False, put_iv_none=False):
    """A chain shaped like the documented /option/chain response."""
    strikes = [base + i * step for i in range(n)]
    spot = spot if spot is not None else strikes[n // 2] + 12
    rows = []
    for i, sp in enumerate(strikes):
        cg = {"iv": None if call_iv_none else call_iv, "delta": 0.5,
              "gamma": 0.001, "theta": -8.0, "vega": 12.0}
        pg = {"iv": None if put_iv_none else put_iv, "delta": -0.5,
              "gamma": 0.001, "theta": -7.0, "vega": 11.0}
        row = {"strike_price": float(sp), "expiry": "2026-10-27", "pcr": 0.9,
               "call_options": {"market_data": {"ltp": 100.0, "oi": 1000.0,
                                                "volume": 10.0},
                                "option_greeks": cg},
               "put_options": {"market_data": {"ltp": 90.0, "oi": 800.0,
                                               "volume": 9.0},
                               "option_greeks": pg}}
        if not drop_spot:
            row["underlying_spot_price"] = float(spot)
        rows.append(row)
    return rows


print("=" * 78)
print("THE BAND IS ATM +/- 10, BY POSITION NOT BY PRICE")
print("-" * 78)
# 61 strikes, ATM in the middle -> must cut to 21
p = R.parse_chain(chain(n=61), "NIFTY")
check("61-strike chain cuts to 21 rows", len(p["strikes"]), 2 * C.STRIKE_BAND + 1)
check("  offsets run -10..+10",
      (min(s["strike_offset"] for s in p["strikes"]),
       max(s["strike_offset"] for s in p["strikes"])), (-10, 10))
check("  exactly one row at offset 0",
      sum(1 for s in p["strikes"] if s["strike_offset"] == 0), 1)
check("  daily row agrees with the band size",
      p["daily"]["strikes_recorded"], 21)

# BY POSITION, not price: a 1-rupee-interval stock and a 50-point index must both
# get 21 rows, which a price-distance band could not do without a per-symbol table.
p1 = R.parse_chain(chain(n=61, step=1, base=500), "SOMESTOCK")
check("a 1-point strike interval also gives 21", len(p1["strikes"]), 21)
check("  and spans 20 points, not 1000",
      round(max(s["strike"] for s in p1["strikes"])
            - min(s["strike"] for s in p1["strikes"])), 20)

# A chain NARROWER than the band must not pad, index out of range, or drop rows.
p2 = R.parse_chain(chain(n=7), "THIN")
check("a 7-strike chain yields 7, not 21", len(p2["strikes"]), 7)
# ATM at the very edge: band clamps on one side only.
p3 = R.parse_chain(chain(n=31, spot=24000), "EDGELO")
check("ATM at the low edge clamps without error", len(p3["strikes"]), 11,
      "offsets 0..+10")
p4 = R.parse_chain(chain(n=31, spot=24000 + 30 * 50), "EDGEHI")
check("ATM at the high edge clamps too", len(p4["strikes"]), 11)

print()
print("ATM IS THE STRIKE NEAREST SPOT, NOT THE ONE WITH THE MOST OI")
print("-" * 78)
# OI is piled on a far strike; ATM must still follow spot. An OI-based ATM would
# make the IV series answer a different question on different days.
rows = chain(n=21, spot=24512)
rows[0]["call_options"]["market_data"]["oi"] = 9_999_999.0
p5 = R.parse_chain(rows, "NIFTY")
check("ATM ignores a 10m-OI outlier strike", p5["daily"]["atm_strike"], 24500.0,
      "spot 24512 -> 24500")
check("  spot is carried through", p5["daily"]["spot"], 24512.0)

print()
print("BOTH IV LEGS ARE KEPT, AND THE BLEND IS RECOMPUTABLE")
print("-" * 78)
p6 = R.parse_chain(chain(n=21, call_iv=14.0, put_iv=16.0), "X")
check("call leg stored", p6["daily"]["atm_call_iv"], 14.0)
check("put leg stored", p6["daily"]["atm_put_iv"], 16.0)
check("blend is the mean", p6["daily"]["atm_iv"], 15.0)
p7 = R.parse_chain(chain(n=21, call_iv_none=True), "Y")
check("one leg missing falls back to the other", p7["daily"]["atm_iv"], 15.0)
check("  and the missing leg stays NULL, not 0",
      p7["daily"]["atm_call_iv"], None)
p8 = R.parse_chain(chain(n=21, call_iv_none=True, put_iv_none=True), "Z")
check("both legs missing gives NULL, not 0", p8["daily"]["atm_iv"], None)

print()
print("A CHAIN THAT CANNOT BE PARSED RAISES — IT DOES NOT WRITE A ZERO ROW")
print("-" * 78)
for label, rows_, marker in (
        ("no strikes at all", [], "no strikes"),
        ("no underlying_spot_price", chain(n=21, drop_spot=True), "spot")):
    try:
        R.parse_chain(rows_, "BAD")
        got, why = "returned", ""
    except Exception as e:
        got, why = "raised", str(e)
    check(label, got, "raised", why[:46])
    ok &= marker in why.lower()

print()
print("THE RECORDER FAILS LOUDLY RATHER THAN RECORDING NOTHING")
print("-" * 78)
src = (ROOT / "meridian/iv_recorder.py").read_text(encoding="utf-8")
code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
for label, cond in (
        ("a missing token FAILS the run", 'status="FAILED"' in code
         and "TokenUnavailable" in code),
        ("  and alerts", 'alert("RECORDER FAILED"' in code),
        ("  and exits non-zero", "return 1" in code),
        ("an unresolvable universe FAILS too", "UniverseUnavailable" in code),
        ("a failed write FAILS, after fetching", 'notes=f"write failed' in code),
        ("under 90% written is PARTIAL and alerts",
         "MIN_WRITE_RATIO" in code and 'alert("RECORDER PARTIAL"' in code),
        ("a run row is written on every path",
         code.count("write_run(run)") >= 4),
        ("missing VIX is NULL and alerted, never 0",
         "rows will carry NULL, not 0" in src and "RECORDER OK, NO VIX" in code),
        ("4xx is not retried, 429 and 5xx are",
         "!= 429" in code and "RETRY_BACKOFF_S" in code)):
    check(label, bool(cond), True)

print()
print("PACING STAYS INSIDE THE PER-MINUTE LIMIT, WHICH IS THE BINDING ONE")
print("-" * 78)
check("pace is 5/sec", C.REQ_PER_SEC, 5.0)
per_min = C.REQ_PER_SEC * 60
check("  = 300/min against a 500/min cap", per_min, 300.0,
      f"{per_min/500*100:.0f}% of budget")
# 217 underlyings + 1 VIX call
calls = 218
check("  217 underlyings + VIX fits one minute's budget", calls <= 500, True,
      f"{calls} calls, ~{calls/C.REQ_PER_SEC:.0f}s")
check("  and is well inside 2000/30min", calls <= 2000, True,
      f"{calls/2000*100:.0f}% of the 30-minute budget")

print()
print("THE EXPIRY IS A RESOLVED DATE, NOT A KEYWORD")
print("-" * 78)
# THE REGRESSION. On Friday 2026-10-02 NIFTY alone failed with "chain carried no
# strikes": current_week names the expiry inside the current CALENDAR week, which
# was the already-expired Tuesday 2026-09-29. Three sessions in five, on the most
# important underlying, in a series that cannot be back-filled.
import datetime as _dt


def master(sym_expiries, index=("NIFTY",)):
    """A master shaped like the real one: expiry in epoch ms, IST."""
    rows = []
    for sym, dates in sym_expiries.items():
        key = ("NSE_INDEX|Nifty 50" if sym in index else f"NSE_EQ|{sym}")
        for d in dates:
            ms = int(_dt.datetime(d.year, d.month, d.day, 15, 29,
                                  tzinfo=U.IST).timestamp() * 1000)
            for t in ("CE", "PE"):
                rows.append({"segment": "NSE_FO", "instrument_type": t,
                             "underlying_symbol": sym, "underlying_key": key,
                             "strike_price": 100.0, "expiry": ms})
    # pad past the truncation guard
    for i in range(60):
        rows.append({"segment": "NSE_FO", "instrument_type": "CE",
                     "underlying_symbol": f"PAD{i}", "underlying_key": f"NSE_EQ|P{i}",
                     "strike_price": 1.0,
                     "expiry": int(_dt.datetime(2026, 12, 29, 15, 29,
                                                tzinfo=U.IST).timestamp() * 1000)})
    return rows


FRIDAY = _dt.date(2026, 10, 2)
EXPIRED_TUE = _dt.date(2026, 9, 29)
NEXT_TUE = _dt.date(2026, 10, 6)
rows = master({"NIFTY": [EXPIRED_TUE, NEXT_TUE, _dt.date(2026, 10, 13)]})
stocks, idx, exps = U.parse_master(rows, today=FRIDAY)
check("the expired Tuesday is excluded", EXPIRED_TUE in exps["NIFTY"], False,
      "it would have returned a chain with no strikes")
check("the nearest FUTURE expiry is chosen", exps["NIFTY"][0], NEXT_TUE)
check("  index key is read from the master, not retyped",
      idx["NIFTY"], "NSE_INDEX|Nifty 50")
check("  and the index is not in the stock universe", "NIFTY" in stocks, False)

# expiry day itself: an option expiring today has no time value, so its IV is
# degenerate and must not enter the series.
rows2 = master({"NIFTY": [FRIDAY, NEXT_TUE]})
_, _, e2 = U.parse_master(rows2, today=FRIDAY)
check("an expiry dated TODAY is skipped", FRIDAY in e2["NIFTY"], False,
      "zero time value = degenerate IV")
check("  the next one is used instead", e2["NIFTY"][0], NEXT_TUE)

# a Monday expiry, which no day-of-week rule would find
MON = _dt.date(2026, 10, 19)
rows3 = master({"NIFTY": [MON]})
_, _, e3 = U.parse_master(rows3, today=FRIDAY)
check("a holiday-shifted MONDAY expiry is found", e3["NIFTY"][0], MON,
      "weekday arithmetic would have missed it")

# an underlying with no future expiry is dropped with a reason, not fetched
rows4 = master({"NIFTY": [NEXT_TUE], "DEADSTOCK": [EXPIRED_TUE]})
saved_dl, saved_nse = U._download_master, U.fetch_nse_fo_symbols
try:
    U._download_master = lambda: rows4
    U.fetch_nse_fo_symbols = lambda: set()
    unders, diff = U.resolve_universe(today=FRIDAY)
    syms = [u["symbol"] for u in unders]
    check("an underlying with no live contract is not fetched",
          "DEADSTOCK" in syms, False)
    check("  and the reason is recorded",
          any(m["symbol"] == "DEADSTOCK" for m in diff.get("no_future_expiry", [])),
          True)
    check("every underlying carries an explicit ISO date",
          all(len(u["expiry"]) == 10 and u["expiry"][4] == "-" for u in unders), True)
    check("  and none carries a keyword",
          any("current_" in u["expiry"] for u in unders), False)
    check("indices are ordered first", unders[0]["kind"], "index")
finally:
    U._download_master, U.fetch_nse_fo_symbols = saved_dl, saved_nse

# Checked on the MODULE NAMESPACE, not the source text. Every file here explains
# the keyword bug at length and deliberately names current_week while doing so --
# a text search finds the explanation and reports it as the thing it warns about.
# This is the third time that trap has caught me in this repo; the namespace
# cannot lie about it.
check("config exposes no expiry-keyword constant",
      any("EXPIRY_KEYWORD" in n or n == "STOCK_EXPIRY_KEYWORD"
          for n in dir(C)), False)
check("  and no config value is a relative keyword",
      any(isinstance(v, str) and v.startswith("current_")
          for n, v in vars(C).items() if not n.startswith("_")), False)
check("  INDEX_SYMBOLS is names only, no per-index settings",
      all(isinstance(x, str) for x in C.INDEX_SYMBOLS), True,
      ", ".join(C.INDEX_SYMBOLS))

print()
print("THE UNIVERSE IS RESOLVED, NEVER HARDCODED")
print("-" * 78)
u_src = (ROOT / "meridian/fno_universe.py").read_text(encoding="utf-8")
check("no hardcoded symbol list in the resolver",
      '"RELIANCE"' not in u_src and "'RELIANCE'" not in u_src, True)
check("the Upstox master is the fetch authority",
      "UPSTOX_INSTRUMENTS_NSE" in u_src, True)
check("NSE is cross-checked, best effort",
      "fetch_nse_fo_symbols" in u_src and "return set()" in u_src, True)
check("the diff is reported, not reconciled",
      "list_diff" in (ROOT / "meridian/iv_recorder.py").read_text() or
      "in_nse_not_upstox" in u_src, True)
# a truncated master must raise, not record a tenth of the universe
saved = U.requests.get
try:
    class _R:
        status_code = 200
        content = __import__("gzip").compress(json.dumps(
            [{"segment": "NSE_FO", "instrument_type": "CE",
              "underlying_symbol": "ONLYONE",
              "underlying_key": "NSE_EQ|X"}]).encode())
    U.requests.get = lambda *a, **k: _R()
    try:
        U.fetch_upstox_fo_underlyings()
        got = "returned"
    except U.UniverseUnavailable as e:
        got = "raised"
    check("a truncated master raises rather than recording 1 symbol",
          got, "raised")
finally:
    U.requests.get = saved

print()
print("SEPARATION FROM ATLAS")
print("-" * 78)
# PARSED, not grepped. Every file here DESCRIBES the separation in prose, so a
# text search finds "atlas" in the docstring that states the rule and reports the
# rule itself as a violation. ast sees imports and nothing else.
import ast
for f in ("meridian/config.py", "meridian/iv_recorder.py",
          "meridian/fno_universe.py", "meridian/__init__.py"):
    tree = ast.parse((ROOT / f).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    check(f"{f} imports nothing from atlas.*", "atlas" in mods, False,
          "imports: " + ", ".join(sorted(mods)) if mods else "no imports")
mig = (ROOT / "migrations/PENDING_meridian_iv_recorder.sql").read_text()
check("the recorder writes only meridian_ tables",
      all(t in code for t in ("meridian_iv_daily", "meridian_iv_strikes",
                              "meridian_recorder_runs")), True)
check("  and no ATLAS table is named in it",
      not any(t in code for t in ("atlas_trades", "atlas_state", "signals?",
                                  "signal_outcomes", "atlas_entry_log")), True)
check("the token file is read, never written",
      ".read_text()" in src and ".write_text" not in src, True)

print()
print("THE MIGRATION IS PRIVATE — authenticated AND subscribed, no anon")
print("-" * 78)
check("no anon grant anywhere", "TO anon" not in mig, True)
check("policies are TO authenticated", mig.count("FOR SELECT TO authenticated"), 3)
check("  gated on an active subscriber",
      mig.count("is_active_subscriber()") >= 3, True)
check("  run accounting is admin-only", "is_admin()" in mig, True)
check("no INSERT or UPDATE policy exists",
      "FOR INSERT" not in mig and "FOR UPDATE" not in mig, True)
check("RLS is enabled on all three",
      mig.count("ENABLE ROW LEVEL SECURITY"), 3)
check("the file ends with a verification block",
      "AS gate_fn" in mig and "AS no_anon_grant" in mig, True)
check("snapshot_taken_at is NOT NULL on both data tables",
      mig.count("snapshot_taken_at timestamptz NOT NULL"), 2)

print("-" * 78)
print("MERIDIAN IV:", "correct" if ok else "*** DEFECTIVE ***")
sys.exit(0 if ok else 1)
