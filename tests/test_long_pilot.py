#!/usr/bin/env python3
"""THE LONG PILOT (operator, 2026-11-10): the long book places NO stop at the
broker, sizes by atlas/sizing.py on LONG_PILOT_CAPITAL, buys T1 as a LIMIT and
T2/T3 as resting BUY GTTs only after T1 fills, caps at five entries, and refuses
everything if a precondition cannot be shown on this call. The SHORT book keeps
its stops. Every broker, database and price is stubbed; nothing leaves the
process.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp())
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-used")
os.environ["ATLAS_DRILL"] = "1"
os.environ["ATLAS_HALT_FILE"] = str(TMP / "halt")
os.environ["ATLAS_SYMBOL_BLOCK_FILE"] = str(TMP / "block.json")
os.environ["ATLAS_UNPROTECTED_FILE"] = str(TMP / "unprot.json")
os.environ["ATLAS_EXIT_ALARM_FILE"] = str(TMP / "alarms.json")
os.environ["TSL_ORDER_EVENTS_FILE"] = str(TMP / "ev.jsonl")
IST = timezone(timedelta(hours=5, minutes=30))
_day = datetime.now(IST).date().isoformat()
_T = {"TESTSYM": 0.05, "SHORTSYM": 0.05}
(TMP / "tick.json").write_text(json.dumps({"days": {_day: _T}, "last_known": _T}))
os.environ["ATLAS_TICK_MAP_FILE"] = str(TMP / "tick.json")
logging.basicConfig(level=logging.CRITICAL)

import atlas.config as CFG                                     # noqa: E402
import atlas.books as BK                                       # noqa: E402
import atlas.execution.atlas_entry as AE                       # noqa: E402
import atlas.execution.broker as BR                            # noqa: E402
import atlas.execution.order_events as OE                      # noqa: E402
import atlas.execution.exits as X                              # noqa: E402
import atlas.execution.long_pilot as LP                        # noqa: E402
import atlas.execution.tranches as TR                          # noqa: E402
import atlas.execution.protection as PR                        # noqa: E402
import atlas.execution.derived_stop as DS                      # noqa: E402
import atlas.sizing as SZ                                      # noqa: E402
import engine.observations as OBS                              # noqa: E402
from atlas.execution import range_breaker as RB                # noqa: E402
from atlas.risk import breaker, symbol_block as SB             # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<66} {str(got)[:24]:<26} {'ok' if ok else 'FAIL'}")


ledger, alerts, patches = [], [], []
EVF = TMP / "ev.jsonl"


class _Events:
    """order_path_events as the drill fence writes them: the local file only."""
    def __iter__(self):
        if not EVF.exists():
            return iter([])
        return iter(json.loads(l) for l in EVF.read_text().splitlines() if l.strip())

    def clear(self):
        EVF.write_text("")
        OE._seen.clear()


events = _Events()
OBS_record_ok = [True]
OBS.record = lambda rows, logger=None: (ledger.extend(rows) or OBS_record_ok[0])
AE._alert = lambda kind, text: alerts.append((kind, text))
AE._patch_trade = lambda rid, patch, what="": patches.append((rid, patch)) or True


class Kite:
    def __init__(self, gtts=(), positions=(), holdings=(), fail_gtt=False,
                 positions_raise=False, holdings_none=False):
        self.gtts_placed, self.deleted = [], []
        self._g, self._p, self._h = list(gtts), list(positions), list(holdings)
        self.fail_gtt, self.praise, self.hnone = fail_gtt, positions_raise, holdings_none

    def place_gtt(self, **kw):
        if self.fail_gtt:
            raise RuntimeError("stub refuses GTT")
        self.gtts_placed.append(kw)
        return {"trigger_id": 90000 + len(self.gtts_placed)}

    def delete_gtt(self, trigger_id):
        self.deleted.append(str(trigger_id))

    def positions(self):
        if self.praise:
            raise RuntimeError("positions down")
        return {"net": self._p}

    def holdings(self):
        return None if self.hnone else self._h

    def get_gtts(self):
        return self._g


PLAN = {"refused": None, "mcap_cr": 20000.0, "band_edge_cr": 10000, "ceiling_pct_of_book": 0.03,
        "long_book": 200000.0, "ceiling": 6000.0, "median_traded_value": 5e8,
        "liquidity_cap": 1e7, "bound": "ceiling", "position": 6000.0, "atr": 4.0,
        "tranches": [{"price": 100.0, "source": "entry", "qty": 24, "amount": 2400.0},
                     {"price": 95.0, "source": "support", "qty": 18, "amount": 1800.0},
                     {"price": 89.0, "source": "atr", "qty": 20, "amount": 1800.0}]}
SIG = {"symbol": "TESTSYM", "direction": "LONG", "entry_ref": 100.0, "entry_low": 99.5,
       "entry_high": 100.5, "sl": 96.0, "ltp": 100.0}

print("\nTHE BOOK SWITCH IS PER BOOK")
check("LONG + CNC: no broker stops", BK.broker_stops("LONG", "CNC"), False)
check("SHORT + MIS: broker stops", BK.broker_stops("SHORT", "MIS"), True)
check("LONG with no product is the long book (ATLAS longs are CNC)", BK.broker_stops("LONG", None), False)
check("SHORT with no product keeps stops", BK.broker_stops("SHORT", ""), True)
check("LONG explicitly MIS is UNKNOWN -> stops", BK.broker_stops("LONG", "MIS"), True)
check("no direction -> stops", BK.broker_stops("", "CNC"), True)
src = (ROOT / "atlas" / "books.py").read_text()
check("the short book's stops cannot be switched off (import refuses)",
      "SHORT_BOOK_BROKER_STOPS is not True" in src and "raise ImportError" in src, True)

print("\nNO UNPROTECTED ALARM, NO REPAIR, FOR A LONG; UNCHANGED FOR A SHORT")
r = PR.register({"id": 1, "symbol": "TESTSYM", "direction": "LONG", "qty": 5}, "no stop")
check("register() refuses a long-book position", r, {})
check("  and the register is empty", PR.outstanding(), {})
alarms = json.loads((TMP / "alarms.json").read_text()) if (TMP / "alarms.json").exists() else {}
check("  and no exit alarm was raised", [k for k in alarms if k.startswith("unprotected:1")], [])
PR.register({"id": 2, "symbol": "SHORTSYM", "direction": "SHORT", "product": "MIS", "qty": 5},
            "no stop")
check("register() still records a short", "2" in PR.outstanding(), True)
e = PR.ensure_protected({"id": 1, "symbol": "TESTSYM", "direction": "LONG", "product": "CNC",
                         "qty": 5, "entry_price": 100, "stop_price": 95}, kite=object())
check("ensure_protected places nothing for a long", (e["action"], e["ok"]), ("none", True))
import atlas.execution.preclose_gtt as PG                     # noqa: E402


class _R:
    status_code = 200

    def json(self):
        return [{"id": 1, "symbol": "TESTSYM", "direction": "LONG", "product": "CNC"},
                {"id": 3, "symbol": "OLDSTOP", "direction": "LONG", "product": "MIS"}]


_g = PG.requests.get
PG.requests.get = lambda *a, **k: _R()
check("15:20 handover/trail never sees a long-book row", [t["id"] for t in PG.open_longs()[1]], [3])
PG.requests.get = _g

print("\nTHE ENTRY PATH")
saved = {n: getattr(AE, n) for n in ("get_market_context", "regime_allows_side", "get_open_position",
                                     "available_funds", "order_margin", "kill_switch_check",
                                     "_reserve_intent", "place_order", "LIVE_TRADING_ENABLED")}
breaker.is_halted = lambda *a, **k: (False, "")
RB.is_blocked = lambda s: (False, "")
RB.stock_check = lambda *a, **k: {"broke": False, "state": "OK", "detail": ""}
SB.is_blocked = lambda s: (False, "", {})
AE.get_market_context = lambda: {}
AE.regime_allows_side = lambda ctx, d: (True, "", None)
AE.get_open_position = lambda s: (True, None)
AE.available_funds = lambda: {"available": 100000.0, "margin": 100000.0, "gtt_committed": 0.0}
AE.order_margin = lambda **k: {"ok": False, "reason": "stub"}
AE.kill_switch_check = lambda s: True
DS.derive = lambda sym, entry, stop, d: {"stop": stop, "source": "published", "stop_pct": 4.0,
                                         "published_pct": 4.0, "why": ""}
SZ.size_pilot = lambda sym, entry=None: dict(PLAN)
LP.entries_used = lambda getter=None: (True, 0)
LP.would_place_today = lambda getter=None: (True, set())
LP._orderevents_writable = lambda poster=None: (True, "stub")
import atlas.execution.gtt as GT                               # noqa: E402
GT.list_active_gtts = lambda: []
sent, reserved = [], []
AE._reserve_intent = lambda intent: (reserved.append(intent) or (501, None, None))
AE.place_order = lambda **k: (sent.append(k) or {"success": True, "order_id": "OID1"})
protect_calls = []
X.protect_or_flag = lambda **k: (protect_calls.append(k) or {"protected": True, "unprotected": False,
                                                             "prot": {"ok": True, "legs": {}}})
fill = {"v": {"outcome": "FILLED", "filled_qty": 24, "avg_price": 100.0}}
X.await_fill = lambda oid, *a, **k: fill["v"]
kite = Kite()
BR.get_kite = lambda *a, **k: kite

# -- watch-only (dry run)
events.clear(); ledger.clear()
out = AE.enter_trade(dict(SIG), dry_run=True)
check("dry run: WOULD_ENTER", out["status"], "WOULD_ENTER")
check("  qty is T1 from the pilot plan, not the Rs10,000 sizer", out["qty"], 24)
check("  sizing basis is the pilot's", out.get("sizing_basis"), CFG.LONG_PILOT_SIZING_BASIS)
check("  the exact would-be orders went to order_path_events",
      [e["code"] for e in events if e["event"] == "WOULD_PLACE"], ["LONG_PILOT"])
wp = [e for e in events if e["event"] == "WOULD_PLACE"][0]["detail"]
check("  T1 qty / limit, T2 and T3 levels recorded",
      (wp["t1"]["qty"], wp["t1"]["limit"], wp["t2"]["price"], wp["t3"]["price"]), (24, 100.0, 95.0, 89.0))
check("  the decision was ledgered BEFORE anything (WOULD_ENTER)",
      [r["computed_values"]["status"] for r in ledger], ["WOULD_ENTER"])
check("  nothing reserved, nothing sent, no GTT", (len(reserved), len(sent), len(kite.gtts_placed)), (0, 0, 0))

# -- live, filled
AE.LIVE_TRADING_ENABLED = True
events.clear(); ledger.clear(); patches.clear(); alerts.clear()
out = AE.enter_trade(dict(SIG))
check("live: ENTERED", out["status"], "ENTERED")
check("  T1 is ONE LIMIT order for 24 CNC", [(o["qty"], o["order_type"], o["product"]) for o in sent],
      [(24, "LIMIT", "CNC")])
check("  protect_or_flag was NOT called: no day-0 stop", protect_calls, [])
check("  two BUY GTTs placed after the fill", [g["orders"][0]["transaction_type"] for g in kite.gtts_placed],
      ["BUY", "BUY"])
check("  at the ladder triggers (95.0 support, 89.0 atr)",
      [g["trigger_values"][0] for g in kite.gtts_placed], [95.0, 89.0])
check("  each limit ABOVE its trigger (Zerodha's rule for a GTT buy)",
      all(g["orders"][0]["price"] > g["trigger_values"][0] for g in kite.gtts_placed), True)
check("  quantities 18 and 20", [g["orders"][0]["quantity"] for g in kite.gtts_placed], [18, 20])
row = patches[-1][1]
check("  row: no stop confirmed, levels published", (row["stop_confirmed"], row["stop_price"]), (False, 96.0))
check("  row: tranche ids in exit_legs, NOT gtt_trigger_id",
      (sorted(k for k in row["exit_legs"] if k.endswith("_trigger_id")), "gtt_trigger_id" in row),
      (["t2_trigger_id", "t3_trigger_id"], False))
check("  ledger: ENTERING before the order, atlas_entry after the fill",
      [r["observation_type"] for r in ledger], ["atlas_decision", "atlas_entry"])
check("  GTT_SENT x2 in order_path_events", [e["code"] for e in events if e["event"] == "GTT_SENT"],
      ["T2_BUY_GTT", "T3_BUY_GTT"])
check("  no UNPROTECTED alert", [a for a in alerts if "PROTECTION" in a[0]], [])

# -- T1 never fills -> no GTTs
sent.clear(); kite.gtts_placed.clear()
fill["v"] = {"outcome": "INDETERMINATE"}
AE._settle_unfilled_limit = lambda oid, sym: {"outcome": "NO_FILL", "reason": "limit rested, cancelled"}
AE._release_intent = lambda rid, why: True
out = AE.enter_trade(dict(SIG))
check("T1 not filled: NO_FILL_LIMIT", out["status"], "NO_FILL_LIMIT")
check("  and NO GTT was placed", len(kite.gtts_placed), 0)
fill["v"] = {"outcome": "FILLED", "filled_qty": 24, "avg_price": 100.0}

print("\nPRECONDITIONS: ANY FAILURE PLACES NOTHING")
sent.clear()
GT.list_active_gtts = lambda: None
out = AE.enter_trade(dict(SIG))
check("GTT list unreadable -> BLOCKED_PILOT_PREFLIGHT, nothing sent", (out["status"], len(sent)),
      ("BLOCKED_PILOT_PREFLIGHT", 0))
GT.list_active_gtts = lambda: []
LP._orderevents_writable = lambda poster=None: (False, "order_path_events refused the preflight row")
out = AE.enter_trade(dict(SIG))
check("order_path_events unwritable -> BLOCKED, nothing sent", (out["status"], len(sent)),
      ("BLOCKED_PILOT_PREFLIGHT", 0))
LP._orderevents_writable = lambda poster=None: (True, "stub")
OBS_record_ok[0] = False
out = AE.enter_trade(dict(SIG))
check("ledger refuses the decision -> BLOCKED_PILOT_NO_EVIDENCE, nothing sent",
      (out["status"], len(sent)), ("BLOCKED_PILOT_NO_EVIDENCE", 0))
OBS_record_ok[0] = True
AE.available_funds = lambda: (_ for _ in ()).throw(AE.FundsUnavailable("margins down"))
out = AE.enter_trade(dict(SIG))
check("funds unreadable -> BLOCKED_NO_FUNDS_DATA, nothing sent", (out["status"], len(sent)),
      ("BLOCKED_NO_FUNDS_DATA", 0))
AE.available_funds = lambda: {"available": 5000.0, "margin": 5000.0, "gtt_committed": 0.0}
out = AE.enter_trade(dict(SIG))
check("funds cover T1 but not the ladder (Rs6,190) -> SKIPPED_FUNDS", out["status"], "SKIPPED_FUNDS")
AE.available_funds = lambda: {"available": 100000.0, "margin": 100000.0, "gtt_committed": 0.0}
SZ.size_pilot = lambda sym, entry=None: {"refused": "no share count (never fetched)"}
out = AE.enter_trade(dict(SIG))
check("sizing refuses -> SKIPPED_PILOT_SIZING, nothing sent", (out["status"], len(sent)),
      ("SKIPPED_PILOT_SIZING", 0))
SZ.size_pilot = lambda sym, entry=None: dict(PLAN)
orig_stops = CFG.LONG_BOOK_BROKER_STOPS
check("preflight fails if the long book would place stops",
      LP.preflight(gtt_reader=lambda: [], poster=None)[0], True)

print("\nTHE CAP: FIVE, FIRST COME FIRST SERVED")
LP.entries_used = lambda getter=None: (True, 5)
check("5 entries made -> SKIPPED_PILOT_FULL", AE.enter_trade(dict(SIG))["status"], "SKIPPED_PILOT_FULL")
LP.entries_used = lambda getter=None: (False, None)
check("count unreadable -> refused", AE.enter_trade(dict(SIG))["status"], "SKIPPED_PILOT_COUNT_UNREADABLE")
LP.entries_used = lambda getter=None: (True, 3)
LP.would_place_today = lambda getter=None: (True, {"A", "B", "TESTSYM"})
check("watch-only: 3 made + 2 OTHER would-places = full",
      AE.enter_trade(dict(SIG), dry_run=True)["status"], "SKIPPED_PILOT_FULL")
LP.would_place_today = lambda getter=None: (True, {"A", "TESTSYM"})
check("watch-only: 3 made + 1 other = room for this one",
      AE.enter_trade(dict(SIG), dry_run=True)["status"], "WOULD_ENTER")
LP.entries_used = lambda getter=None: (True, 0)

print("\nTHE SHORT BOOK STILL GETS ITS STOP")
protect_calls.clear()
import atlas.risk.position_sizing as PS                         # noqa: E402
AE.size_for_trade = lambda **k: {"qty": 10, "product": "MIS", "stop_price": 104.0, "stop_pct": 4.0,
                                 "risk_actual": 40.0, "notional": 1000.0, "capital_required": 200.0,
                                 "binding_cap": "notional"}
AE.check_entry_range = lambda *a, **k: (True, "")
AE.regime_allows_side = lambda ctx, d: (True, "", None)
import atlas.execution.index_state as IXS                       # noqa: E402
IXS.get_market_direction = lambda: {"direction": "SHORT"}      # the shorts-only opening-range gate
s = AE.enter_trade({"symbol": "SHORTSYM", "direction": "SHORT", "entry_ref": 100.0,
                    "entry_low": 99.5, "entry_high": 100.5, "sl": 104.0, "ltp": 100.0})
print("    short status:", s.get("status"), str(s.get("reason"))[:120])
check("a SHORT entry calls protect_or_flag (its stop)", len(protect_calls), 1)
check("  with product MIS", protect_calls[0].get("product") if protect_calls else None, "MIS")

print("\nTRANCHE GTTS WHEN THE POSITION CLOSES")
row = {"id": 501, "symbol": "TESTSYM", "status": "OPEN",
       "exit_legs": {"t2_trigger_id": "11", "t3_trigger_id": "12"}}
active = [{"id": 11}, {"id": 12}]
k = Kite(positions=[], holdings=[{"tradingsymbol": "TESTSYM", "quantity": 24, "t1_quantity": 0}])
check("held in HOLDINGS (day 2, not in positions) -> nothing cancelled",
      (TR.cleanup([row], k, active)["cancelled"], k.deleted), (0, []))
k = Kite(positions=[], holdings=[{"tradingsymbol": "TESTSYM", "quantity": 0, "t1_quantity": 24}])
check("held as T1 quantity (bought yesterday) -> nothing cancelled", TR.cleanup([row], k, active)["cancelled"], 0)
k = Kite(positions=[], holdings=[])
check("flat in both books -> both tranche GTTs cancelled", (TR.cleanup([row], k, active)["cancelled"],
      sorted(k.deleted)), (2, ["11", "12"]))
k = Kite(holdings_none=True)
o = TR.cleanup([row], k, active)
check("holdings unreadable -> NOTHING cancelled", (o["cancelled"], k.deleted), (0, []))
check("  and an ORPHAN_UNCHECKED alarm is raised",
      any(a.get("kind") == "ORPHAN_UNCHECKED" for a in json.loads((TMP / "alarms.json").read_text()).values()),
      True)
k = Kite(positions=[], holdings=[])
o = TR.cleanup([row], k, None)
check("GTT list unreadable -> nothing cancelled, said so", (o["cancelled"], len(o["alerts"])), (0, 1))
k = Kite(positions=[], holdings=[])
check("a tranche already filled/cancelled at the broker is left alone",
      (TR.cleanup([row], k, [{"id": 12}])["cancelled"], k.deleted), (1, ["12"]))
src = (ROOT / "atlas" / "signal" / "market_open.py").read_text()
check("market_open keeps the long book out of reconcile_exits",
      "_open = [t for t in _open if _bst(t)]" in src and "rec = X.reconcile_exits(_open," in src, True)

print("\n+16% ON BLENDED COST CANCELS THE REMAINING TRANCHES")
events.clear()
both = [{"id": 11}, {"id": 12}]
# T1 24 @ 100 settled in holdings + T2 18 @ 95 bought today (positions):
# blended = (2400 + 1710) / 42 = 97.857. At LTP 113.60 that is +16.09% --
# on T1's own price it would be +13.6% and NOT flagged.
books = dict(positions=[{"tradingsymbol": "TESTSYM", "product": "CNC", "quantity": 18, "average_price": 95.0}],
             holdings=[{"tradingsymbol": "TESTSYM", "quantity": 24, "t1_quantity": 0, "average_price": 100.0}])
k = Kite(**books)
o = TR.flag_cancel([row], k, both, ltp_of=lambda syms: {"TESTSYM": 113.60})
check("+16.09% on BLENDED cost (13.6% on T1 alone) -> both cancelled", (o["cancelled"], sorted(k.deleted)), (2, ["11", "12"]))
check("  flagged and recorded with its own code",
      [e["code"] for e in events if e["event"] == "GTT_CANCELLED"], ["T2_PROFIT_FLAG", "T3_PROFIT_FLAG"])
k = Kite(**books)
check("+15% -> nothing cancelled", TR.flag_cancel([row], k, both, ltp_of=lambda s: {"TESTSYM": 112.5})["cancelled"], 0)
k = Kite(**books)
check("only the still-active tranche is cancelled", (TR.flag_cancel([row], k, [{"id": 12}],
      ltp_of=lambda s: {"TESTSYM": 120.0})["cancelled"], k.deleted), (1, ["12"]))
k = Kite(holdings_none=True)
o = TR.flag_cancel([row], k, both, ltp_of=lambda s: {"TESTSYM": 200.0})
check("books unreadable -> nothing cancelled, said so", (o["cancelled"], len(o["alerts"])), (0, 1))
k = Kite(**books)
o = TR.flag_cancel([row], k, both, ltp_of=lambda s: (_ for _ in ()).throw(RuntimeError("ltp down")))
check("LTP unreadable -> nothing cancelled, said so", (o["cancelled"], len(o["alerts"])), (0, 1))
k = Kite(**books)
check("GTT list unreadable -> nothing", TR.flag_cancel([row], k, None, ltp_of=lambda s: {"TESTSYM": 200.0})["cancelled"], 0)
check("blended cost is ONE definition, shared with the 15:50 report",
      "position_from(sym, pos, hold)" in (ROOT / "atlas/reporting/session_report.py").read_text(), True)
check("market_open runs flag_cancel every cycle",
      "_TR.flag_cancel(_rows, _k, _active)" in (ROOT / "atlas/signal/market_open.py").read_text(), True)

print("\nTHE 15:50 REPORT")
import atlas.reporting.session_report as SR                    # noqa: E402
SR.live_prices = lambda syms: {"TESTSYM": 117.0}
lines = "\n".join(SR.long_book_lines(
    [{"id": 501, "symbol": "TESTSYM", "stop_price": 96, "target_price": 108, "exit_legs": row["exit_legs"]}],
    kite=Kite(positions=[], holdings=[{"tradingsymbol": "TESTSYM", "quantity": 42,
                                       "t1_quantity": 0, "average_price": 100.0}]), gtts=[{"id": 12}]))
check("+17% on blended cost -> flagged, and no sell placed", "+16% REACHED" in lines, True)
check("  says the decision is the operator's", "ATLAS places no sell" in lines, True)
check("  tranche status read from the broker", ("T2 11 not active" in lines, "T3 12 resting" in lines), (True, True))
lines = "\n".join(SR.long_book_lines([{"id": 501, "symbol": "TESTSYM", "stop_price": 96}],
                                     kite=Kite(holdings_none=True), gtts=[]))
check("broker book unreadable -> said, no number", "UNREADABLE" in lines and "blended Rs" not in lines, True)
lines = "\n".join(SR.long_book_lines([{"id": 501, "symbol": "TESTSYM", "stop_price": 96}],
                                     kite=Kite(holdings=[{"tradingsymbol": "TESTSYM", "quantity": 10,
                                                          "average_price": 100.0}]), gtts=[]))
check("+17% -> flagged; +15% would not be", "REACHED" in lines, True)
SR.live_prices = lambda syms: {"TESTSYM": 115.0}
lines = "\n".join(SR.long_book_lines([{"id": 501, "symbol": "TESTSYM", "stop_price": 96}],
                                     kite=Kite(holdings=[{"tradingsymbol": "TESTSYM", "quantity": 10,
                                                          "average_price": 100.0}]), gtts=[]))
check("  +15% is not flagged", "REACHED" in lines, False)

print("\nREFUSALS TO THE LEDGER: QUEUED, BATCHED, ATTEMPTED ONCE")
LP._ledgered.clear(); LP._pending.clear()
calls = []
for st in ("SKIPPED_SENTIMENT", "SKIPPED_SENTIMENT", "SKIPPED_REGIME"):
    LP.ledger_decision(dict(SIG), {"status": st, "reason": "x"})
LP.ledger_decision(dict(SIG, direction="SHORT"), {"status": "SKIPPED_REGIME"})
check("one row per symbol per status; shorts not ledgered here", len(LP._pending), 2)
check("flush writes them in ONE call", (LP.flush_decisions(lambda rows: calls.append(rows) or True), len(calls)), (2, 1))
check("  each with price and source", all(r["computed_values"]["price_source"] for r in calls[0]), True)
LP.ledger_decision(dict(SIG), {"status": "SKIPPED_SENTIMENT"})
check("a refused write is NOT retried next cycle", (len(LP._pending), LP.flush_decisions(lambda r: False)), (0, 0))
src = (ROOT / "atlas" / "signal" / "market_open.py").read_text()
check("market_open flushes once per cycle", src.count("flush_decisions()"), 1)

for n, v in saved.items():
    setattr(AE, n, v)
if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the long pilot places no stop, sizes by the rule, and refuses on any doubt")
