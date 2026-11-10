#!/usr/bin/env python3
"""ATLAS LONG-EQUITY SIZING (atlas/sizing.py), decided 2026-11-10.

Every expected number below is derived from the rule's own statement, worked by
hand in the comment beside it -- not read back from the code. Nothing is
fetched; the share store, the price frame and the traded values are planted.
"""
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
import pandas as pd                                             # noqa: E402
import atlas.sizing as Z                                        # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


def near(a, b, tol=1e-6):
    return a is not None and abs(a - b) <= tol


print("\nBANDS: each includes its lower edge")
for mcap, want in ((150_000, 0.08), (100_000, 0.08), (99_999.99, 0.05), (35_000, 0.05),
                   (34_999, 0.03), (10_000, 0.03), (9_999, 0.015), (2_000, 0.015),
                   (1_999, 0.0075), (500, 0.0075), (499.99, 0.004), (1, 0.004)):
    check(f"{mcap:>10,} cr -> {want:.2%}", Z.band(mcap)[1], want)

print("\nTHE CEILING IS A SHARE OF THE LONG BOOK (60%), NOT OF TOTAL CAPITAL")
TV = [1e12] * 20                         # liquidity far from binding
# 10 crore total -> book 6 crore; 1,00,000 cr company -> 8% of 6 cr = 48,00,000
r = Z.size("X", 1000.0, 1e12 / 1000.0, 20.0, [], TV, 10 * 1e7)
# shares x price = 1e9 x 1000 = 1e12 rupees = 1,00,000 crore
check("market cap at use = shares x entry, in crore", near(r["mcap_cr"], 100_000), True)
check("ceiling = 8% x 60% x 10 cr = Rs 48,00,000", near(r["ceiling"], 4_800_000), True)
check("bound is the ceiling", r["bound"], "ceiling")

print("\nLIQUIDITY: 2% OF THE 20-SESSION MEDIAN, NOT THE MEAN")
tv = [10_000_000.0] * 19 + [10_000_000_000.0]   # one block-deal day
r = Z.size("X", 1000.0, 1e12 / 1000.0, 20.0, [], tv, 10 * 1e7)
# median of 19 x 1 cr and one 1000 cr = 1 cr; 2% = 2,00,000. The mean would be ~51 cr.
check("median traded value ignores the block deal", r["median_traded_value"], 10_000_000.0)
check("liquidity cap = 2% x 1 cr = Rs 2,00,000", near(r["liquidity_cap"], 200_000), True)
check("bound is liquidity when tighter", r["bound"], "liquidity")
check("position is the tighter of the two", near(r["position"], 200_000), True)
check("tranches 40/30/30 of the position",
      [round(t["amount"]) for t in r["tranches"]], [80_000, 60_000, 60_000])
check("19 sessions of traded value -> refused",
      "of the last 20" in (Z.size("X", 1000.0, 1e9, 20.0, [], tv[:19], 1e8)["refused"] or ""), True)
check("a None among the 20 -> refused",
      Z.size("X", 1000.0, 1e9, 20.0, [], tv[:19] + [None], 1e8)["refused"] is not None, True)

print("\nTHE LADDER, PER TRANCHE")
# entry 100, ATR 4: a support counts for T2 only at or below 96.
L = Z.ladder(100.0, 4.0, [])
check("no supports: T2 = entry - 1.5 ATR = 94", L[1], (94.0, "atr"))
check("            T3 = T2 - 1.5 ATR = 88", L[2], (88.0, "atr"))
L = Z.ladder(100.0, 4.0, [99.0, 97.0])
check("supports within 1 ATR of entry are skipped -> ATR step", L[1], (94.0, "atr"))
L = Z.ladder(100.0, 4.0, [95.0, 93.0, 90.0])
check("T2 = nearest support at least 1 ATR below entry (95)", L[1], (95.0, "support"))
check("T3 = nearest support at least 1 ATR below T2 (<=91 -> 90)", L[2], (90.0, "support"))
L = Z.ladder(100.0, 4.0, [95.0, 93.0])
check("support for T2, none 1 ATR below it -> T3 = 95 - 6 = 89", L[2], (89.0, "atr"))
L = Z.ladder(100.0, 4.0, [80.0])
# As decided: the nearest support >= 1 ATR below entry, however deep. A lone
# deep support is T2; T3 then steps 1.5 ATR below it (80 - 6 = 74).
check("a lone deep support is T2 (80); T3 = 80 - 6 = 74", (L[1], L[2]), ((80.0, "support"), (74.0, "atr")))
# JSWENERGY 2026-11-09: entry 523.15, supports 513.60 and 512.85 (from the
# measurement). With ATR 14.65 neither is 1 ATR below entry (508.50), so both
# tranches step by ATR and cannot coincide.
L = Z.ladder(523.15, 14.65, [513.60, 512.85])
check("JSWENERGY's stacked supports no longer stack T2/T3", L[1][1] == "atr" and L[2][0] < L[1][0] - 14.65, True)

print("\nTHREE REAL TRANCHES OR NO POSITION (D-073)")
# book Rs2,00,000 direct, 3% band -> ceiling Rs6,000; T2/T3 = Rs1,800 each.
# At a Rs3,890 share T1 (Rs2,400) rounds up to 1 share, but T2 buys 0 -> refused.
_tv = [1e12] * 20
_sh = 20000 * 1e7 / 3890.0                       # a 20,000 cr company at Rs3,890
r = Z.size("X", 3890.0, _sh, 50.0, [], _tv, None, book_capital=200000.0)
check("T2 rounds to zero shares -> the symbol is refused", "T2 of Rs 1,800 buys no share" in (r["refused"] or ""), True)
r = Z.size("X", 500.0, 20000 * 1e7 / 500.0, 10.0, [], _tv, None, book_capital=200000.0)
check("a Rs500 share carries all three tranches -> sized", (r["refused"], [t["qty"] for t in r["tranches"]]),
      (None, [4, 3, 3]))
check("nothing is rounded up past T1", any(t.get("rounded_up") for t in r["tranches"][1:]), False)

print("\nTHE BOOK: 70% FOR NEW NAMES, AVERAGING DOWN TO A 10% FLOOR")
# total 10 cr -> book 6 cr; new names to 4.2 cr; averaging to 5.4 cr
check("new-name headroom with 4.0 cr deployed = 20 lakh", near(Z.headroom(1e8, 4.0e7, "new"), 2.0e6), True)
check("new-name headroom at 4.2 cr deployed = 0", Z.headroom(1e8, 4.2e7, "new"), 0.0)
check("averaging headroom at 4.2 cr deployed = 1.2 cr", near(Z.headroom(1e8, 4.2e7, "average"), 1.2e7), True)
check("averaging headroom at the 10% floor = 0", Z.headroom(1e8, 5.4e7, "average"), 0.0)

print("\nTHE SHARE COUNT'S QUARTER: deadline 21 days + weekly fetch 7 days")
check("10 Nov 2026 expects the Sep-30 filing", Z.expected_quarter(date(2026, 11, 10)), date(2026, 9, 30))
check("27 Oct 2026 (27 days after Sep 30) still expects Jun 30", Z.expected_quarter(date(2026, 10, 27)), date(2026, 6, 30))
check("28 Oct 2026 (28 days after Sep 30) expects Sep 30", Z.expected_quarter(date(2026, 10, 28)), date(2026, 9, 30))
check("10 Jan 2027 expects Sep 30 2026", Z.expected_quarter(date(2027, 1, 10)), date(2026, 9, 30))
check("28 Jan 2027 expects Dec 31 2026", Z.expected_quarter(date(2027, 1, 28)), date(2026, 12, 31))


def store(sym, total, qe="30-SEP-2026"):
    return {"symbols": {sym: {"shares": {"total": total, "quarter_end": qe,
                                         "filed": "21-OCT-2026 16:25:15", "notes": []}}}}


check("no record -> refused", Z.share_count("X", date(2026, 11, 10), {"symbols": {}})[2] is not None, True)
check("a Jun-30 count in November -> refused as stale",
      "due and not on file" in Z.share_count("X", date(2026, 11, 10), store("X", 1e9, "30-JUN-2026"))[2], True)
check("a Sep-30 count in November -> used",
      Z.share_count("X", date(2026, 11, 10), store("X", 1e9))[0], 1e9)
check("total None (parse failed) -> refused",
      Z.share_count("X", date(2026, 11, 10), store("X", None))[0], None)

print("\nREADING THE FRAME: 02b's ATR, the discontinuity gate, the zones")
import radar.bars as B                                          # noqa: E402
B.session_traded_value = lambda sym, d: 5e8                    # Rs 50 cr every day
dates = pd.bdate_range("2026-06-01", periods=120)
fr = pd.DataFrame({"date": dates, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                   "prev_close": 100.0, "atr": 2.5, "history_discontinuity": False,
                   "is_demand_ob": False, "ob_high": float("nan"), "ob_low": float("nan"),
                   "bullish_fvg": False, "fvg_high": float("nan"), "fvg_low": float("nan")})
r = Z.size_symbol("X", 1e8, today=date(2026, 11, 10), store=store("X", 1e9), frame=fr)
check("the ATR used is the frame's own `atr` column (2.5)", r.get("atr"), 2.5)
check("  so T2 = 100 - 1.5 x 2.5 = 96.25", r["tranches"][1]["price"], 96.25)
# a demand zone 3% below, formed 10 sessions ago, never traded through
fr2 = fr.copy()
fr2.loc[109, ["is_demand_ob", "ob_high", "ob_low"]] = [True, 97.0, 96.0]
fr2.loc[110:, "low"] = 98.5
r = Z.size_symbol("X", 1e8, today=date(2026, 11, 10), store=store("X", 1e9), frame=fr2)
check("a live zone 1.2 ATR below entry becomes T2 (97.0)", (r["tranches"][1]["price"], r["tranches"][1]["source"]), (97.0, "support"))
fr3 = fr.copy()
fr3.loc[119, "history_discontinuity"] = True
check("history_discontinuity on the last row -> refused",
      "history_discontinuity" in (Z.size_symbol("X", 1e8, today=date(2026, 11, 10),
                                                 store=store("X", 1e9), frame=fr3)["refused"] or ""), True)
fr4 = fr.copy()                       # a 1:2 split on 2 Nov, after the Sep-30 count
i = fr4.index[fr4["date"] >= "2026-11-02"][0]
fr4.loc[i:, ["open", "high", "low", "close"]] = [50.0, 50.5, 49.5, 50.0]
fr4.loc[i + 1:, "prev_close"] = 50.0
check("a split after the count's quarter -> refused",
      "after the" in (Z.size_symbol("X", 1e8, today=date(2026, 11, 10), store=store("X", 1e9),
                                    frame=fr4)["refused"] or ""), True)

print("\nNO HISTORY FILE IS A REFUSAL, NOT A CRASH")
_orig = Z.SMC_DIR
Z.SMC_DIR = Path(tempfile.mkdtemp())
r = Z.size_symbol("NOFILE", 1e8, today=date(2026, 11, 10), store=store("NOFILE", 1e9))
check("a symbol with no SMC parquet is refused, not raised", "no price history file" in (r.get("refused") or ""), True)
(Z.SMC_DIR / "BAD.parquet").write_text("not a parquet")
r = Z.size_symbol("BAD", 1e8, today=date(2026, 11, 10), store=store("BAD", 1e9))
check("an unreadable parquet is refused, not raised", "unreadable" in (r.get("refused") or ""), True)
r = Z.size_symbol("E", 1e8, today=date(2026, 11, 10), store=store("E", 1e9), frame=fr.iloc[0:0])
check("an empty frame is refused", r.get("refused"), "price history is empty")
Z.SMC_DIR = _orig

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the sizing rule does what was decided")
