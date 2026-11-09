"""
THE F&O BHAVCOPY -- every futures and options contract, every session (2026-11-10).
=================================================================================

    python -m engine.fo_bhavcopy                       # today's session (cron)
    python -m engine.fo_bhavcopy --date 2026-11-09
    python -m engine.fo_bhavcopy --backfill 2025-11-10 2026-11-09
    python -m engine.fo_bhavcopy --status

WHAT IT IS. NSE's daily derivatives bhavcopy, from the same archive and in the
same UDiFF layout as the equity file (2026 layout: TradDt, TckrSymb, ...):

    https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip

One row per contract: instrument type (STF/IDF futures, STO/IDO options),
expiry, strike, option type, OHLC, close, settlement price, underlying price,
OI and its change, volume, turnover, and the contract's lot size
(NewBrdLotQty). 9 Nov 2026: 41,392 contracts, 218 stocks + 6 indices.

WHY IT IS FETCHED. The F&O books are signal generators (operator, 2026-11-10;
no F&O order path). Their inputs -- the F&O universe, lot sizes PER CONTRACT
(35 stocks carried different lots across expiries on 9 Nov, so a per-stock
table would be wrong), premiums, and IV computed from them (meridian/iv_bhav.py)
-- are all in this file. jugaad-data's bhavcopy_fo_save uses NSE's pre-2024
filename and no longer works.

WHAT COUNTS AS GOT. A file is accepted only if it unzips, carries the expected
columns, has rows, and its OWN TradDt is the session asked for -- the equity
archive has misdated files (cm14Sep2026bhav.csv holds 11-Sep), and a file
taken by its name would count one session twice.

HEARTBEAT AND STALENESS. Every run -- holiday, success or failure -- writes
data/raw/fo_bhavcopy/_heartbeat.json and a log line. daily_line() puts the
latest session held, and STALE if it is not the last trading session, into the
19:50 IST message (engine/volume_alerts). A failed fetch on a session day
alerts on Telegram.

LICENCE. Same source and class as the equity bhavcopy: NSE archive, processing
rights unconfirmed (compliance/data_rights.csv). Stored on the box only; never
served.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("fo-bhavcopy")
IST = timezone(timedelta(hours=5, minutes=30))
RAW_DIR = ROOT / "data" / "raw" / "fo_bhavcopy"
PARQUET_DIR = ROOT / "data" / "processed" / "fo"
HEARTBEAT = RAW_DIR / "_heartbeat.json"
# THE CURRENT FILENAME, on jugaad-data's own F&O route (operator, 2026-11-10,
# OPEN D-078). jugaad's bhavcopy_fo_raw still points at NSE's pre-2024 name
# (content/historical/DERIVATIVES/.../foDDMMMYYYYbhav.csv.zip), which no longer
# exists. Only the route is replaced: the session, its headers and the request
# are the library's own -- the same library and site the box already uses for
# the equity bhavcopy. No request is hand-crafted here and no header is varied.
FO_ROUTE = "/content/fo/BhavCopy_NSE_FO_0_0_0_{yyyy}{mm}{dd}_F_0000.csv.zip"


def _archives():
    from jugaad_data.nse.archives import NSEArchives

    class _FOArchives(NSEArchives):
        def __init__(self):
            super().__init__()
            self._routes["bhavcopy_fo"] = FO_ROUTE

        def bhavcopy_fo_raw(self, dt):
            # jugaad's own method, with mm added: the 2024+ name is YYYYMMDD.
            return self.get("bhavcopy_fo", yyyy=dt.year, mm=dt.strftime("%m"),
                            dd=dt.strftime("%d"), MMM=dt.strftime("%b").upper())
    return _FOArchives()
REQUIRED = ("TradDt", "FinInstrmTp", "TckrSymb", "XpryDt", "StrkPric", "OptnTp",
            "OpnPric", "HghPric", "LwPric", "ClsPric", "SttlmPric", "UndrlygPric",
            "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol", "TtlTrfVal", "NewBrdLotQty")
KEEP = REQUIRED + ("ISIN", "LastPric", "PrvsClsgPric", "FininstrmActlXpryDt")
RETRIES = 2


def _today() -> date:
    return datetime.now(IST).date()


def csv_path(d: date) -> Path:
    return RAW_DIR / f"BhavCopy_NSE_FO_{d.strftime('%Y%m%d')}.csv"


def parquet_path(d: date) -> Path:
    return PARQUET_DIR / f"fo_{d.isoformat()}.parquet"


def validate(text: str, d: date):
    """-> (DataFrame, None) or (None, reason). The file's own date must be d."""
    import pandas as pd
    try:
        df = pd.read_csv(io.StringIO(text))
    except Exception as e:
        return None, f"not a CSV ({type(e).__name__}: {e})"
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        return None, f"columns missing: {missing}"
    if not len(df):
        return None, "no rows"
    dates = set(str(x)[:10] for x in df["TradDt"].dropna().unique())
    if dates != {d.isoformat()}:
        return None, f"the file's own TradDt is {sorted(dates)}, not {d.isoformat()} -- misdated"
    return df, None


def have(d: date) -> bool:
    return parquet_path(d).exists() and csv_path(d).exists()


def fetch(d: date, archives=None) -> dict:
    """Fetch, validate and store one session. Idempotent. Never raises."""
    if have(d):
        return {"date": d.isoformat(), "ok": True, "status": "already held"}
    last = None
    for attempt in range(RETRIES + 1):
        try:
            arch = archives or _archives()
            r = arch.bhavcopy_fo_raw(d)
            if r.status_code == 404:
                return {"date": d.isoformat(), "ok": False, "status": "not published (HTTP 404)"}
            if r.status_code != 200:
                last = f"HTTP {r.status_code}"
            else:
                try:
                    z = zipfile.ZipFile(io.BytesIO(r.content))
                    name = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
                    text = z.read(name).decode("utf-8", errors="replace")
                except Exception as e:
                    last = f"not a zip with a CSV ({type(e).__name__})"
                else:
                    df, why = validate(text, d)
                    if df is None:
                        return {"date": d.isoformat(), "ok": False, "status": f"REJECTED: {why}"}
                    RAW_DIR.mkdir(parents=True, exist_ok=True)
                    PARQUET_DIR.mkdir(parents=True, exist_ok=True)
                    csv_path(d).write_text(text)
                    keep = [c for c in KEEP if c in df.columns]
                    df[keep].to_parquet(parquet_path(d), index=False)
                    return {"date": d.isoformat(), "ok": True, "status": "fetched",
                            "rows": int(len(df)), "summary": summarise(df)}
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if attempt < RETRIES:
            time.sleep(2 + 3 * attempt)
    return {"date": d.isoformat(), "ok": False, "status": f"FAILED: {last}"}


def summarise(df) -> dict:
    t = df["FinInstrmTp"].value_counts().to_dict()
    opt = df[df["FinInstrmTp"].isin(["STO", "IDO"])]
    return {"contracts": int(len(df)), "by_type": {k: int(v) for k, v in t.items()},
            "stock_underlyings": int(df[df["FinInstrmTp"].isin(["STF", "STO"])]["TckrSymb"].nunique()),
            "index_underlyings": int(df[df["FinInstrmTp"].isin(["IDF", "IDO"])]["TckrSymb"].nunique()),
            "option_expiries": int(opt["XpryDt"].nunique()),
            "options_traded": int((opt["TtlTradgVol"] > 0).sum())}


def _beat(result: dict) -> None:
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        hb = {"last_run": datetime.now(IST).isoformat(timespec="seconds"), "result": result,
              "latest_held": latest_held()}
        tmp = HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(hb, indent=1, default=str))
        tmp.replace(HEARTBEAT)
    except Exception as e:
        log.error(f"heartbeat not written: {e}")


def latest_held():
    if not PARQUET_DIR.exists():
        return None
    ds = sorted(p.stem[3:] for p in PARQUET_DIR.glob("fo_*.parquet"))
    return ds[-1] if ds else None


def run(d: date | None = None, sender=None) -> int:
    """The cron entry: today's session. 0 ok / not a session; 1 failed."""
    d = d or _today()
    try:
        from engine.trading_calendar import is_trading_day
        session = is_trading_day(d)
    except Exception as e:
        res = {"date": d.isoformat(), "ok": False, "status": f"calendar unreadable ({e}) -- cannot tell"}
        log.critical(f"heartbeat: {res['status']}")
        _beat(res)
        return 1
    if not session:
        res = {"date": d.isoformat(), "ok": True, "status": "not an NSE session"}
        log.info(f"heartbeat: {d} not a session -- nothing to fetch")
        _beat(res)
        return 0
    res = fetch(d)
    log.info(f"heartbeat: {d} {res['status']}" + (f" {res.get('summary')}" if res.get("summary") else ""))
    _beat(res)
    if not res["ok"]:
        msg = (f"⚠️ F&O BHAVCOPY NOT STORED for {d}: {res['status']}. The F&O signal "
               f"inputs and IV for this session are missing until it is fetched.")
        try:
            (sender or _send)(msg)
        except Exception as e:
            log.error(f"alert not sent: {e}")
        return 1
    return 0


def _send(msg: str):
    from engine.volume_alerts import send
    return send(msg)


def backfill(start: date, end: date, pause: float = 0.6) -> dict:
    """Every NSE session in [start, end]. Skips what is held. -> counts."""
    from engine.trading_calendar import is_trading_day
    out = {"fetched": 0, "held": 0, "failed": []}
    d = start
    while d <= end:
        if is_trading_day(d):
            r = fetch(d)
            if r["status"] == "already held":
                out["held"] += 1
            elif r["ok"]:
                out["fetched"] += 1
                time.sleep(pause)
            else:
                out["failed"].append((d.isoformat(), r["status"]))
        d += timedelta(days=1)
    _beat({"backfill": f"{start}..{end}", **{k: (v if k != "failed" else len(v)) for k, v in out.items()}})
    return out


def daily_line(on: date | None = None) -> str:
    """For the 19:50 IST message. Never raises."""
    try:
        from engine.trading_calendar import is_trading_day, prev_trading_day
        on = on or _today()
        want = on if is_trading_day(on) else prev_trading_day(on)
        held = latest_held()
        if held is None:
            return "F&O bhavcopy: NONE held"
        if held < want.isoformat():
            return f"F&O bhavcopy: STALE — latest held {held}, last session {want}"
        return f"F&O bhavcopy: {held} held"
    except Exception as e:
        return f"F&O bhavcopy: status could not be read ({type(e).__name__})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--backfill", nargs=2, metavar=("FROM", "TO"))
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if a.status:
        print(daily_line())
        print(HEARTBEAT.read_text() if HEARTBEAT.exists() else "no heartbeat yet")
        return 0
    if a.backfill:
        out = backfill(date.fromisoformat(a.backfill[0]), date.fromisoformat(a.backfill[1]))
        print(json.dumps(out, indent=1))
        return 0 if not out["failed"] else 1
    return run(date.fromisoformat(a.date) if a.date else None)


if __name__ == "__main__":
    sys.exit(main())
