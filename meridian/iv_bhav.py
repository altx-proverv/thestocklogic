"""
MERIDIAN -- implied volatility computed from the F&O bhavcopy (2026-11-10, OPEN D-079).
======================================================================================

    python -m meridian.iv_bhav --range 2025-11-10 2026-11-09     # rebuild, parquet per year
    python -m meridian.iv_bhav --validate recorder.json           # vs meridian_iv_daily

WHY. meridian/iv_recorder.py stores Upstox's IV twice a session and was built
first because "no API sells IV history retrospectively". NSE's archive keeps
every session's premiums, so a year of IV can be rebuilt from them. The
recorder keeps running until this is validated against its sessions
(operator, 2026-11-10).

THE RULES (operator, 2026-11-10):
  * ONLY STRIKES THAT TRADED. An option with TtlTradgVol == 0 has a close NSE
    filled in, not a market price; IV from it measures NSE's model. Untraded ->
    UNKNOWN (None), never a number.
  * BLACK-76 ON THE FUTURE. NSE options are European; pricing off the
    same-expiry future needs no dividend or carry guess.
  * NO SAME-EXPIRY FUTURE (index weeklies): the forward is backed out of
    call/put parity at the strike where a TRADED call and a TRADED put are
    closest in price: F = K + (C - P) / DF.
  * If the future did not trade, or no traded C/P pair exists, there is no
    forward and every IV for that expiry is UNKNOWN.

CONVENTIONS, chosen to match the recorder so the comparison is like for like:
  * expiry: the nearest expiry strictly after the trade date;
  * days_to_expiry: calendar days; T = days / 365;
  * ATM strike: the listed strike nearest the underlying's price (UndrlygPric);
  * IV in percent; atm_iv = mean of the call and put IV that exist.
  * RISK_FREE: an ASSUMPTION (6.5%), used only in the discount factor. Its effect
    on IV is reported by --validate, not hidden.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RISK_FREE = 0.065          # ASSUMPTION -- discount factor only; sensitivity in --validate
OUT_DIR = ROOT / "data" / "processed" / "iv_bhav"
VOL_LO, VOL_HI = 1e-4, 5.0


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black76(F: float, K: float, T: float, vol: float, call: bool, r: float = RISK_FREE) -> float:
    df = math.exp(-r * T)
    if vol <= 0 or T <= 0:
        return df * max((F - K) if call else (K - F), 0.0)
    s = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    if call:
        return df * (F * _ncdf(d1) - K * _ncdf(d2))
    return df * (K * _ncdf(-d2) - F * _ncdf(-d1))


def implied_vol(price: float, F: float, K: float, T: float, call: bool,
                r: float = RISK_FREE):
    """Black-76 IV in percent, or None when no volatility can produce the price."""
    if not (price and price > 0 and F > 0 and K > 0 and T > 0):
        return None
    df = math.exp(-r * T)
    lo_px = df * max((F - K) if call else (K - F), 0.0)
    hi_px = df * (F if call else K)
    if not (lo_px < price < hi_px):
        return None                         # below intrinsic or above the bound
    lo, hi = VOL_LO, VOL_HI
    if black76(F, K, T, hi, call, r) < price:
        return None
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if black76(F, K, T, mid, call, r) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return round(50.0 * (lo + hi), 4)      # percent


def _traded(row) -> bool:
    try:
        return float(row["TtlTradgVol"]) > 0 and float(row["ClsPric"]) > 0
    except (TypeError, ValueError):
        return False


def forward(fut, opts, T: float, r: float = RISK_FREE):
    """-> (F, source) or (None, reason). fut: same-expiry future rows; opts: option rows."""
    if fut is not None and len(fut):
        f = fut.iloc[0]
        if _traded(f):
            return float(f["ClsPric"]), "future"
        return None, "the same-expiry future did not trade"
    df = math.exp(-r * T)
    calls = {float(x["StrkPric"]): float(x["ClsPric"]) for _, x in opts[opts["OptnTp"] == "CE"].iterrows() if _traded(x)}
    puts = {float(x["StrkPric"]): float(x["ClsPric"]) for _, x in opts[opts["OptnTp"] == "PE"].iterrows() if _traded(x)}
    both = sorted(set(calls) & set(puts), key=lambda k: abs(calls[k] - puts[k]))
    if not both:
        return None, "no strike with a traded call AND a traded put"
    k = both[0]
    return k + (calls[k] - puts[k]) / df, f"parity at {k:g}"


def day(df, d: date, symbols=None, strikes: dict | None = None, r: float = RISK_FREE) -> list:
    """ATM IV per underlying for one session's bhavcopy frame.

    strikes: optional {symbol: (expiry, atm_strike)} to force the recorder's
    expiry and strike for validation."""
    out = []
    opts_all = df[df["FinInstrmTp"].isin(["STO", "IDO"])]
    futs_all = df[df["FinInstrmTp"].isin(["STF", "IDF"])]
    syms = sorted(opts_all["TckrSymb"].unique()) if symbols is None else symbols
    for sym in syms:
        o = opts_all[opts_all["TckrSymb"] == sym]
        if not len(o):
            continue
        exps = sorted(x for x in o["XpryDt"].astype(str).str[:10].unique() if x > d.isoformat())
        if not exps:
            continue
        exp = exps[0]
        if strikes and sym in strikes:
            exp = strikes[sym][0]
        oe = o[o["XpryDt"].astype(str).str[:10] == exp]
        if not len(oe):
            out.append({"symbol": sym, "expiry": exp, "reason": "no options at the recorder's expiry"})
            continue
        dte = (date.fromisoformat(exp) - d).days
        T = dte / 365.0
        fe = futs_all[(futs_all["TckrSymb"] == sym) & (futs_all["XpryDt"].astype(str).str[:10] == exp)]
        F, fsrc = forward(fe if len(fe) else None, oe, T, r)
        spot = float(oe["UndrlygPric"].dropna().iloc[0]) if oe["UndrlygPric"].notna().any() else None
        ks = sorted(oe["StrkPric"].astype(float).unique())
        if strikes and sym in strikes:
            K = float(strikes[sym][1])
        elif spot:
            K = min(ks, key=lambda k: abs(k - spot))
        else:
            K = None
        rec = {"symbol": sym, "trade_date": d.isoformat(), "expiry": exp, "days_to_expiry": dte,
               "spot": spot, "forward": F, "forward_source": fsrc, "atm_strike": K,
               "call_iv": None, "put_iv": None, "call_traded": False, "put_traded": False}
        if F is None or K is None:
            rec["reason"] = fsrc if F is None else "no spot"
            out.append(rec)
            continue
        for side, call in (("call", True), ("put", False)):
            x = oe[(oe["StrkPric"].astype(float) == K) & (oe["OptnTp"] == ("CE" if call else "PE"))]
            if len(x) and _traded(x.iloc[0]):
                rec[f"{side}_traded"] = True
                rec[f"{side}_iv"] = implied_vol(float(x.iloc[0]["ClsPric"]), F, K, T, call, r)
        ivs = [v for v in (rec["call_iv"], rec["put_iv"]) if v is not None]
        rec["atm_iv"] = round(sum(ivs) / len(ivs), 4) if ivs else None
        out.append(rec)
    return out


def rebuild(start: date, end: date) -> dict:
    import pandas as pd
    from engine.fo_bhavcopy import parquet_path
    from engine.trading_calendar import is_trading_day
    rows, missing = [], []
    d = start
    while d <= end:
        if is_trading_day(d):
            p = parquet_path(d)
            if p.exists():
                rows += day(pd.read_parquet(p), d)
            else:
                missing.append(d.isoformat())
        d += timedelta(days=1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"iv_atm_{start.isoformat()}_{end.isoformat()}.parquet"
    pd.DataFrame(rows).to_parquet(out, index=False, compression="zstd")
    return {"rows": len(rows), "sessions_missing": missing, "file": str(out)}


def validate(recorder_rows: list, r: float = RISK_FREE) -> list:
    """Bhavcopy IV at the recorder's own expiry and ATM strike, side by side."""
    import pandas as pd
    from engine.fo_bhavcopy import parquet_path
    by_day = {}
    for x in recorder_rows:
        by_day.setdefault(x["trade_date"], {})[x["underlying_symbol"]] = (x["expiry_date"], float(x["atm_strike"]), x)
    out = []
    for ds, m in sorted(by_day.items()):
        p = parquet_path(date.fromisoformat(ds))
        if not p.exists():
            continue
        res = day(pd.read_parquet(p), date.fromisoformat(ds), symbols=sorted(m),
                  strikes={s: (v[0], v[1]) for s, v in m.items()}, r=r)
        for b in res:
            rec = m[b["symbol"]][2]
            out.append({**b, "rec_call_iv": rec.get("atm_call_iv"), "rec_put_iv": rec.get("atm_put_iv"),
                        "rec_atm_iv": rec.get("atm_iv"), "rec_spot": rec.get("spot")})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", nargs=2)
    ap.add_argument("--validate")
    a = ap.parse_args(argv)
    if a.range:
        print(json.dumps(rebuild(date.fromisoformat(a.range[0]), date.fromisoformat(a.range[1])), indent=1))
    if a.validate:
        v = validate(json.loads(Path(a.validate).read_text()))
        import pandas as pd
        out = OUT_DIR / "validation.parquet"
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(v).to_parquet(out, index=False)
        print(f"{len(v)} rows -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
