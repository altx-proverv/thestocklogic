#!/usr/bin/env python3
"""IV FROM THE BHAVCOPY (meridian/iv_bhav.py, D-079): Black-76 on the future, a
parity forward when there is none, and an untraded strike is UNKNOWN, never a
number. Planted frames only."""
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import pandas as pd                                             # noqa: E402
from meridian import iv_bhav as V                               # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<64} {str(got)[:22]:<24} {'ok' if ok else 'FAIL'}")


print("\nBLACK-76 ROUND TRIP")
F, K, T = 1320.0, 1320.0, 17 / 365
for vol in (0.12, 0.25, 0.60):
    for call in (True, False):
        px = V.black76(F, K, T, vol, call)
        check(f"price at {vol:.0%} {'call' if call else 'put'} -> IV back", round(V.implied_vol(px, F, K, T, call), 2),
              round(vol * 100, 2))
check("a price below intrinsic has no IV -> None", V.implied_vol(1.0, 1400.0, 1300.0, T, True), None)
check("a zero price -> None", V.implied_vol(0.0, F, K, T, True), None)
# put-call parity of the model itself, at r: C - P = DF (F - K)
c, p = V.black76(1350, 1300, T, 0.3, True), V.black76(1350, 1300, T, 0.3, False)
check("model parity C - P = DF(F - K)", round(c - p, 6), round(math.exp(-V.RISK_FREE * T) * 50, 6))


def frame(rows):
    base = {"FinInstrmTp": "STO", "TckrSymb": "AAA", "XpryDt": "2026-11-23", "StrkPric": 100.0,
            "OptnTp": "CE", "ClsPric": 0.0, "TtlTradgVol": 0, "UndrlygPric": 100.4}
    return pd.DataFrame([{**base, **r} for r in rows])


D = date(2026, 11, 6)
T2 = 17 / 365
cpx = round(V.black76(101.0, 100.0, T2, 0.30, True), 2)
ppx = round(V.black76(101.0, 100.0, T2, 0.30, False), 2)
print("\nTHE FUTURE IS THE FORWARD; UNTRADED IS UNKNOWN")
df = frame([{"FinInstrmTp": "STF", "OptnTp": None, "StrkPric": float("nan"), "ClsPric": 101.0, "TtlTradgVol": 900},
            {"OptnTp": "CE", "ClsPric": cpx, "TtlTradgVol": 50},
            {"OptnTp": "PE", "ClsPric": ppx, "TtlTradgVol": 0},             # NSE-filled, untraded
            {"OptnTp": "CE", "StrkPric": 105.0, "ClsPric": 1.0, "TtlTradgVol": 3}])
r = V.day(df, D)[0]
check("forward = the traded future", (r["forward"], r["forward_source"]), (101.0, "future"))
check("ATM = strike nearest the underlying (100)", r["atm_strike"], 100.0)
check("traded call -> IV ~30%", round(r["call_iv"]), 30)
check("UNTRADED put -> None, not a number", (r["put_traded"], r["put_iv"]), (False, None))
check("atm_iv uses only what exists (the call)", r["atm_iv"], r["call_iv"])
df2 = df.copy()
df2.loc[df2["FinInstrmTp"] == "STF", "TtlTradgVol"] = 0
r = V.day(df2, D)[0]
check("an untraded future -> no forward -> every IV UNKNOWN", (r["forward"], r.get("call_iv")), (None, None))

print("\nNO SAME-EXPIRY FUTURE (index weekly): PARITY FROM TRADED C AND P")
Fw = 25040.0
rows = []
for k in (24900.0, 25000.0, 25100.0):
    for cp, call in (("CE", True), ("PE", False)):
        rows.append({"FinInstrmTp": "IDO", "TckrSymb": "NIFTY", "XpryDt": "2026-11-09", "StrkPric": k,
                     "OptnTp": cp, "ClsPric": round(V.black76(Fw, k, 3 / 365, 0.16, call), 2),
                     "TtlTradgVol": 1000, "UndrlygPric": 25030.0})
r = V.day(pd.DataFrame(rows), D)[0]
check("forward backed out of parity ~ 25040", round(r["forward"]), 25040)
check("  and says so", r["forward_source"].startswith("parity at"), True)
check("  weekly IV ~16%", round(r["atm_iv"]), 16)
rows_u = [dict(x, TtlTradgVol=(0 if x["OptnTp"] == "PE" else 1000)) for x in rows]
r = V.day(pd.DataFrame(rows_u), D)[0]
check("no traded put anywhere -> no parity forward -> UNKNOWN", (r["forward"], r.get("atm_iv")), (None, None))

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — IV is Black-76 off a traded forward, and untraded is UNKNOWN")
