#!/usr/bin/env python3
"""THE F&O BHAVCOPY FETCH: a file is stored only when its own date is the session,
every run leaves a heartbeat, and a missing or stale file is said out loud.
Planted zips through a fake HTTP session; every path is a temp dir.
"""
import io
import json
import sys
import tempfile
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import logging                                                  # noqa: E402
logging.basicConfig(level=logging.CRITICAL)
from engine import fo_bhavcopy as F                             # noqa: E402

T = Path(tempfile.mkdtemp())
F.RAW_DIR, F.PARQUET_DIR = T / "raw", T / "pq"
F.HEARTBEAT = F.RAW_DIR / "_heartbeat.json"
failures = []


def check(label, got, want):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<62} {str(got)[:24]:<26} {'ok' if ok else 'FAIL'}")


HDR = ",".join(F.REQUIRED + ("ISIN", "LastPric", "PrvsClsgPric", "FininstrmActlXpryDt"))


def csv_for(d, rows=3, drop=None):
    cols = [c for c in HDR.split(",") if c != drop]
    lines = [",".join(cols)]
    for i in range(rows):
        v = {"TradDt": d, "FinInstrmTp": "STO" if i else "STF", "TckrSymb": "AAA",
             "XpryDt": "2026-11-23", "StrkPric": 100 + i, "OptnTp": "CE" if i else "",
             "TtlTradgVol": i, "NewBrdLotQty": 500}
        lines.append(",".join(str(v.get(c, 1.0)) for c in cols))
    return "\n".join(lines)


def zipped(text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("BhavCopy.csv", text)
    return b.getvalue()


class R:
    def __init__(self, code, content=b""):
        self.status_code, self.content = code, content


class S:
    def __init__(self, resp):
        self.resp, self.calls, self.headers = resp, 0, {}

    def get(self, url, timeout=None):
        self.calls += 1
        return self.resp


D = date(2026, 11, 9)
print("\nSTORED ONLY WHEN THE FILE IS WHAT IT CLAIMS")
s = S(R(200, zipped(csv_for("2026-11-11"))))
r = F.fetch(D, session=s)
check("a file whose own TradDt is another day is REJECTED", (r["ok"], "misdated" in r["status"]), (False, True))
check("  and nothing is stored", F.have(D), False)
r = F.fetch(D, session=S(R(200, zipped(csv_for("2026-11-09", drop="NewBrdLotQty")))))
check("a file missing a required column (lot size) is REJECTED", (r["ok"], "NewBrdLotQty" in r["status"]), (False, True))
r = F.fetch(D, session=S(R(404)))
check("404 is 'not published', not a crash", r["status"], "not published (HTTP 404)")
r = F.fetch(D, session=S(R(200, b"<html>error page</html>")))
check("an error page is not a file", r["ok"], False)
s = S(R(200, zipped(csv_for("2026-11-09"))))
r = F.fetch(D, session=s)
check("a good file is stored (raw CSV + parquet)", (r["ok"], F.have(D)), (True, True))
check("  with a summary of what is in it", r["summary"]["contracts"], 3)
s2 = S(R(200, zipped(csv_for("2026-11-09"))))
check("a second fetch is a no-op, no request", (F.fetch(D, session=s2)["status"], s2.calls), ("already held", 0))

print("\nEVERY RUN LEAVES A HEARTBEAT; A FAILED SESSION ALERTS")
import engine.trading_calendar as TC                            # noqa: E402
TC.is_trading_day = lambda d: d.weekday() < 5 and d != date(2026, 11, 10)
TC.prev_trading_day = lambda d: date(2026, 11, 9) if d <= date(2026, 11, 11) else date(2026, 11, 11)
sent = []
check("holiday: exit 0, nothing fetched", F.run(date(2026, 11, 10), sender=sent.append), 0)
hb = json.loads(F.HEARTBEAT.read_text())
check("  and a heartbeat saying so", hb["result"]["status"], "not an NSE session")
F.fetch = lambda d, session=None: {"date": d.isoformat(), "ok": False, "status": "FAILED: HTTP 503"}
check("a session whose file cannot be got: exit 1", F.run(date(2026, 11, 11), sender=sent.append), 1)
check("  and a Telegram alert naming it", len(sent) == 1 and "NOT STORED" in sent[0], True)
check("  and a heartbeat recording the failure",
      json.loads(F.HEARTBEAT.read_text())["result"]["status"], "FAILED: HTTP 503")

print("\nSTALENESS IS SAID IN THE 19:50 MESSAGE")
check("latest held is the 9th", F.latest_held(), "2026-11-09")
check("on the 10th (holiday) the 9th is current", F.daily_line(date(2026, 11, 10)), "F&O bhavcopy: 2026-11-09 held")
check("on the 11th the 9th is STALE", F.daily_line(date(2026, 11, 11)).startswith("F&O bhavcopy: STALE"), True)
src = (ROOT / "engine" / "volume_alerts.py").read_text()
check("volume_alerts carries the line in all three messages", src.count("html.escape(_fo_line(on))"), 3)

if failures:
    print(f"FAILED — {len(failures)}")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASSED — the F&O bhavcopy is stored only when it is what it claims, and its absence is said")
