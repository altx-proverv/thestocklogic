#!/usr/bin/env python3
"""GSM/ASM SNAPSHOTS: THREE RUNS ARE ONE RECORD, A MISSED DAY IS AN ALARM.

NSE serves only today's list, so a session nobody captured is gone for good.
This file proves the four things that make that survivable:

  1. THREE RUNS, ONE RECORD. Morning, afternoon and close capture the same
     list; the store keeps one row per (symbol, framework, stage, session) and
     three ok entries in the capture log. A stage that changes mid-day is a new
     row, and the earlier one stays -- both were true that day.
  2. ONE FAILURE COSTS NOTHING. A failed fetch in the morning is recovered by
     the afternoon; a morning capture Supabase refused is on disk and replayed.
  3. NOTHING IS INVENTED. A list still stamped yesterday is not labelled today;
     replay never touches another day's file; a holiday fetches nothing.
  4. A MISSED DAY REACHES A HUMAN. The alarm goes out through the REAL
     telegram.send -- only requests.post is replaced -- naming the date, and a
     store that cannot be read alarms too.

The store here is an in-memory stand-in that enforces the same unique key as
the migration. The real key and triggers were verified on the live database
when the migration was applied (2026-10-27): catalog checked, and a TRUNCATE
probe refused by surveillance_append_only().
"""
import json
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import surveillance_snapshot as ss                  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
failures = []


def check(label, got, want, note=""):
    ok = got == want
    if not ok:
        failures.append(f"{label}: got {got!r}, want {want!r}")
    print(f"  {label:<70} {str(got)[:20]:<22} {'ok' if ok else 'FAIL':<6} {note}")


def head(title):
    print()
    print(title)
    print("-" * 112)


def payload(day="27-Oct-2026", gsm_stage="IBC I & GSM 0 (58)"):
    """The shape probed from NSE on 2026-10-26, cut down."""
    asm = {"longterm": {"data": [
        {"symbol": "AAA", "asmSurvIndicator": "Stage I", "survCode": "LTASM - I (13)",
         "survDesc": "Long Term ASM - Stage I", "isin": "INE1", "asmTime": day},
        {"symbol": "BBB", "asmSurvIndicator": "Stage IV", "survCode": "LTASM - IV (16)",
         "survDesc": "Long Term ASM - Stage IV", "isin": "INE2", "asmTime": day}]},
        "shortterm": {"data": [
        {"symbol": "AAA", "asmSurvIndicator": "Stage I", "survCode": "STASM - I (11)",
         "survDesc": "Short Term ASM - Stage I", "isin": "INE1", "asmTime": day}]}}
    gsm = [{"symbol": "CCC", "gsmStage": "0", "survCode": gsm_stage,
            "survDesc": "GSM Stage 0", "isin": "INE3", "gsmTime": f"{day} 08:06:02"}]
    return asm, gsm


class FakeStore:
    """Enforces the migration's UNIQUE (symbol, framework, stage, session_date)
    with ignore-duplicates, and is append-only: it has no update or delete."""

    def __init__(self, down=0):
        self.rows, self.captures, self.down = {}, [], down

    def insert_snapshots(self, rows):
        if self.down > 0:
            self.down -= 1
            raise RuntimeError("HTTP 503 supabase down")
        for r in rows:
            k = (r["symbol"], r["framework"], r["stage"], r["session_date"])
            self.rows.setdefault(k, r)          # ON CONFLICT DO NOTHING

    def keys(self, session):
        return {k[:3] for k in self.rows if k[3] == session.isoformat()}

    def log_capture(self, row):
        # The live table's CHECKs (20261026185608 + 20261026195429), mirrored:
        # a row the database would refuse must fail here too.
        if row["ok"]:
            assert row["error"] is None
            assert None not in (row["asm_rows"], row["gsm_rows"],
                                row["rows_received"], row["rows_stored"])
            assert row["rows_stored"] <= row["rows_received"]
            assert row["rows_stored"] == row["asm_rows"] + row["gsm_rows"]
        else:
            assert row["error"] is not None
        self.captures.append(row)

    def captured(self, session):
        return any(c["ok"] and c["session_date"] == session.isoformat()
                   for c in self.captures)

    def last_error(self, session):
        bad = [c for c in self.captures if not c["ok"]
               and c["session_date"] == session.isoformat()]
        return f"{bad[-1]['run_label']}: {bad[-1]['error']}" if bad else None


def at(h, m=0, d=27):
    return lambda: datetime(2026, 10, d, h, m, tzinfo=IST)


always = lambda d: True                      # noqa: E731
RAW = Path(tempfile.mkdtemp(prefix="surv_raw_"))


# ══════════════════════════════════════════════════════════════════
head("PARSE: NSE's codes, not labels; NSE's stamp, not our clock")
# ══════════════════════════════════════════════════════════════════
rows = ss.parse(*payload())
check("one row per listing across ASM long, ASM short and GSM", len(rows), 4)
check("AAA under LT and ST ASM 'Stage I' is TWO rows (stage = survCode)",
      sorted(r["stage"] for r in rows if r["symbol"] == "AAA"),
      ["LTASM - I (13)", "STASM - I (11)"])
check("frameworks", sorted({r["framework"] for r in rows}), ["ASM", "GSM"])
check("a list stamped today is today's session",
      ss.session_of(rows, datetime(2026, 10, 27, 10, 30, tzinfo=IST)), date(2026, 10, 27))
try:
    ss.session_of(ss.parse(*payload(day="26-Oct-2026")),
                  datetime(2026, 10, 27, 0, 5, tzinfo=IST))
    got = "accepted"
except ValueError:
    got = "refused"
check("a list still stamped 26-Oct, fetched 00:05 on the 27th, is refused", got, "refused",
      "it is yesterday's list")
for bad in ({"longterm": {"data": []}}, ["not", "a", "dict"]):
    try:
        ss.parse(bad, [])
        got = "accepted"
    except (ValueError, KeyError):
        got = "refused"
    check(f"a changed ASM payload shape is refused ({type(bad).__name__})", got, "refused")


# ══════════════════════════════════════════════════════════════════
head("THREE RUNS, ONE RECORD")
# ══════════════════════════════════════════════════════════════════
st = FakeStore()
for label, clock in (("morning", at(10, 30)), ("afternoon", at(14, 35)),
                     ("close", at(16, 30))):
    ss.capture(label, store=st, fetcher=lambda: payload(), clock=clock,
               raw_dir=RAW, is_session=always)
check("three captures of the same list store 4 rows, not 12", len(st.rows), 4)
check("  and the log holds three ok captures",
      [c["run_label"] for c in st.captures if c["ok"]], ["morning", "afternoon", "close"])
check("  each row keeps the FIRST capture's time",
      {r["captured_at"][11:16] for r in st.rows.values()}, {"10:30"})

# NSE'S OWN DUPLICATE, as on 2026-10-26: AAATECH twice in long-term ASM,
# identical in every stored field.
def payload_dup():
    asm, gsm = payload()
    asm["longterm"]["data"].append(dict(asm["longterm"]["data"][0],
                                        companyName="Aaa (other casing)"))
    return asm, gsm


std = FakeStore()
ss.capture("morning", store=std, fetcher=payload_dup, clock=at(10, 30),
           raw_dir=Path(tempfile.mkdtemp(prefix="surv_dup_")), is_session=always)
cap = std.captures[-1] if std.captures else {}
check("NSE sends 5 rows for 4 listings: 4 snapshot rows stored", len(std.rows), 4)
check("  the capture records BOTH: rows_received 5, rows_stored 4",
      (cap.get("ok"), cap.get("rows_received"), cap.get("rows_stored")), (True, 5, 4))
check("  and asm_rows/gsm_rows are the stored, distinct counts",
      (cap.get("asm_rows"), cap.get("gsm_rows")), (3, 1), "3 + 1 = rows_stored")

st2 = FakeStore()
ss.capture("morning", store=st2, fetcher=lambda: payload(), clock=at(10, 30),
           raw_dir=RAW, is_session=always)
ss.capture("afternoon", store=st2, fetcher=lambda: payload(gsm_stage="GSM I (59)"),
           clock=at(14, 35), raw_dir=RAW, is_session=always)
check("a GSM stage that changes mid-day adds a row and keeps the earlier one",
      sorted(k[2] for k in st2.rows if k[0] == "CCC"),
      ["GSM I (59)", "IBC I & GSM 0 (58)"])


# ══════════════════════════════════════════════════════════════════
head("ONE FAILURE COSTS NOTHING")
# ══════════════════════════════════════════════════════════════════
def boom():
    raise RuntimeError("NSE fetch failed after 4 attempts: HTTP 401")


# Its own raw dir: RAW already holds today's files from the runs above, and the
# morning run would (correctly) replay one and hide the failure being tested.
raw3 = Path(tempfile.mkdtemp(prefix="surv_raw3_"))
st3 = FakeStore()
ok_m = ss.capture("morning", store=st3, fetcher=boom, clock=at(10, 30),
                  raw_dir=raw3, is_session=always)
ok_a = ss.capture("afternoon", store=st3, fetcher=lambda: payload(), clock=at(14, 35),
                  raw_dir=raw3, is_session=always)
check("morning fetch fails, afternoon succeeds: session captured", (ok_m, ok_a),
      (False, True))
check("  the morning failure is in the log, not just cron.log",
      [(c["run_label"], c["ok"]) for c in st3.captures],
      [("morning", False), ("afternoon", True)])

raw4 = Path(tempfile.mkdtemp(prefix="surv_raw4_"))
st4 = FakeStore(down=1)                         # Supabase refuses the morning
ss.capture("morning", store=st4, fetcher=lambda: payload(), clock=at(10, 30),
           raw_dir=raw4, is_session=always)
check("Supabase down in the morning: the raw payload is on disk anyway",
      len(list(raw4.glob("2026-10-27_*.json"))), 1)
ok = ss.capture("afternoon", store=st4, fetcher=boom, clock=at(14, 35),
                raw_dir=raw4, is_session=always)
check("  afternoon's fetch ALSO fails, but the morning file is replayed", ok, True)
check("  stored under the morning's capture time, logged as replay",
      ({r["captured_at"][11:16] for r in st4.rows.values()},
       [c["run_label"] for c in st4.captures if c["ok"]]), ({"10:30"}, ["replay"]))


# ══════════════════════════════════════════════════════════════════
head("NOTHING IS INVENTED")
# ══════════════════════════════════════════════════════════════════
raw5 = Path(tempfile.mkdtemp(prefix="surv_raw5_"))
ss.save_raw(*payload(day="26-Oct-2026"), datetime(2026, 10, 26, 23, 19, tzinfo=IST), raw5)
st5 = FakeStore()
ss.replay(st5, date(2026, 10, 27), raw5)
check("replay for the 27th ignores a file captured on the 26th", len(st5.rows), 0)
ss.save_raw(*payload(day="26-Oct-2026"), datetime(2026, 10, 27, 0, 5, tzinfo=IST), raw5)
ss.replay(st5, date(2026, 10, 27), raw5)
check("  and refuses a 27th file whose NSE stamp is the 26th", len(st5.rows), 0)

calls = []
st6 = FakeStore()
ss.capture("morning", store=st6, fetcher=lambda: calls.append(1) or payload(),
           clock=at(10, 30, d=20), raw_dir=RAW)          # REAL calendar gate
check("on 2026-10-20 (Dussehra, per the fetched master) nothing is fetched",
      (len(calls), len(st6.captures)), (0, 0))
src = (ROOT / "engine/surveillance_snapshot.py").read_text()
check("there is no backfill entry point (no --date / --session on --run)",
      ("--date" in src, "--session" in src), (False, False))


# ══════════════════════════════════════════════════════════════════
head("A MISSED DAY REACHES A HUMAN — through the real telegram.send")
# ══════════════════════════════════════════════════════════════════
import requests                                                  # noqa: E402
from atlas.reporting import telegram                             # noqa: E402

sent = []


class _R:
    status_code, text = 200, "ok"


_saved = (requests.post, telegram.TELEGRAM_BOT_TOKEN, telegram.TELEGRAM_CHAT_ID,
          telegram.BASE_URL)
telegram.TELEGRAM_BOT_TOKEN, telegram.TELEGRAM_CHAT_ID = "TEST-TOKEN", "TEST-CHAT"
telegram.BASE_URL = "https://api.telegram.org/botTEST-TOKEN"
requests.post = lambda url, json=None, **k: sent.append((url, json)) or _R()
try:
    st7 = FakeStore()
    st7.log_capture({"session_date": "2026-10-27", "run_label": "close", "ok": False,
                     "error": "HTTP 503 <html><body>Service Unavailable</body></html>"})
    r = ss.check(date(2026, 10, 27), store=st7, send=telegram.send)
    check("no ok capture: check() reports False", r, False)
    check("  exactly one Telegram POST went out", len(sent), 1)
    url, body = sent[0] if sent else ("", {})
    check("  to the Telegram sendMessage endpoint",
          url, "https://api.telegram.org/botTEST-TOKEN/sendMessage")
    check("  naming the date", "2026-10-27" in body.get("text", ""), True)
    check("  carrying the last error, HTML-escaped (parse_mode=HTML)",
          ("&lt;html&gt;" in body.get("text", ""), "<html>" in body.get("text", "")),
          (True, False), "an unescaped tag would make Telegram 400 the alarm")

    sent.clear()
    st8 = FakeStore()
    ss.capture("close", store=st8, fetcher=lambda: payload(), clock=at(16, 30),
               raw_dir=RAW, is_session=always)
    check("a captured session sends nothing",
          (ss.check(date(2026, 10, 27), store=st8, send=telegram.send), len(sent)), (True, 0))

    class Unreadable(FakeStore):
        def captured(self, session):
            raise RuntimeError("HTTP 404 relation does not exist")

    sent.clear()
    ss.check(date(2026, 10, 27), store=Unreadable(), send=telegram.send)
    check("a capture log that cannot be read ALARMS (unknown is not captured)",
          len(sent), 1)

    sent.clear()
    ss.check(date(2026, 10, 27), store=FakeStore(), send=telegram.send)
    check("no attempt recorded at all (the job never ran) ALARMS",
          ("no capture attempt was recorded" in (sent[0][1]["text"] if sent else "")),
          True)

    # THE CLOSE RUN ITSELF raises the alarm -- not only check() called by hand.
    sent.clear()
    _cap, _store, _gate = ss.capture, ss.Store, None
    ss.capture = lambda label, **k: False
    ss.Store = FakeStore
    import engine.trading_calendar as tc
    _rtd = tc.require_trading_day
    tc.require_trading_day = lambda d, what="", logger=None: True
    try:
        rc = ss.main(["--run", "close"])
    finally:
        ss.capture, ss.Store, tc.require_trading_day = _cap, _store, _rtd
    check("`--run close` with a failed day exits 1 and sends the alarm",
          (rc, len(sent)), (1, 1))
finally:
    (requests.post, telegram.TELEGRAM_BOT_TOKEN, telegram.TELEGRAM_CHAT_ID,
     telegram.BASE_URL) = _saved

# BITE: a check() that forgot to alarm must be caught by the assertions above.
sent_b = []
r = ss.check(date(2026, 10, 27), store=FakeStore(), send=lambda m: sent_b.append(m) or True)
check("BITE: the alarm's send is actually invoked (not merely logged)", len(sent_b), 1)


print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — three runs dedupe, failures recover, nothing is invented, "
      "a missed day alarms")
