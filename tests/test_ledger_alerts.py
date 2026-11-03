#!/usr/bin/env python3
"""THE LEDGER SPEAKS: ONCE WHEN IT FIRST WORKS, EVERY TIME IT REFUSES, NEVER IN A DRY RUN.

1. A REFUSAL ALERTS. record() is fail-closed; each refusal sends a Telegram
   naming the publisher, type, session, symbols and error. Repeats from the same
   publisher and type are held for HOLD_MINUTES and counted into the next one.

2. FIRST SUCCESS SPEAKS ONCE. The first stored write sends one message with the
   ledger's count; nothing after. Two publishers finishing together send one.
   A send Telegram did not accept leaves no marker, so the next success retries.
   A re-publish that stores nothing new is not a success to announce.

3. PROOF MODE DOES NOT SPEND THE REAL ONE. A proof run has its own marker and a
   banner saying it is a proof.

4. A DRY SCORER RUN SENDS NO ALARM. score_observations --dry skips the
   surveillance check; a real run on a trading day still makes it.

No network: every send is captured. Nothing here can reach Telegram.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests                                                # noqa: E402
from engine import ledger_alerts as la                         # noqa: E402
from engine import observations as obs                         # noqa: E402

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


def fresh_state():
    la.STATE_DIR = Path(tempfile.mkdtemp())


class Box:
    """A sender that records what it was handed."""
    def __init__(self, accept=True):
        self.msgs, self.accept = [], accept

    def __call__(self, m):
        self.msgs.append(m)
        return (self.accept, 41 if self.accept else None, 900 if self.accept else None)


os.environ.pop("TSL_LEDGER_ALERT_PROOF", None)


def row(sym, src="engine/publish_orb.py", otype="orb_break"):
    return obs.build(otype, symbol=sym, session_date="2026-11-04", direction="LONG",
                     reference_price=10, computed_values={"s": sym}, source_file=src)


# ══════════════════════════════════════════════════════════════════
head("1. A REFUSAL ALERTS, NAMING PUBLISHER, SYMBOLS AND ERROR; REPEATS ARE HELD")
# ══════════════════════════════════════════════════════════════════
fresh_state()
b = Box()
T0 = 1_800_000_000
rows = [row("TCS"), row("INFY")]
check("a first refusal sends", la.refused(rows, "HTTP 503 <html>down</html>",
                                          sender=b, now=T0), True)
m = b.msgs[0] if b.msgs else ""
check("  it names the publisher", "LEDGER REFUSED — engine/publish_orb.py did NOT publish" in m, True)
check("  the type and session", "Type: orb_break · session 2026-11-04" in m, True)
check("  every symbol, sorted", "Symbols (2): INFY, TCS\n" in m, True)
check("  the error, HTML-escaped (Telegram 400s on raw markup)",
      "Error: HTTP 503 &lt;html&gt;down&lt;/html&gt;" in m, True)
check("a repeat within the hold is NOT sent", la.refused(rows, "e", sender=b, now=T0 + 60), False)
check("  a second repeat is not sent either", la.refused(rows, "e", sender=b, now=T0 + 120), False)
check("  nothing went out for either", len(b.msgs), 1)
check("another publisher is not held by the first one's hold",
      la.refused([row("SBIN", src="radar/store.py", otype="radar_event")], "e",
                 sender=b, now=T0 + 180), True)
check("after the hold, the next refusal sends",
      la.refused(rows, "e", sender=b, now=T0 + la.HOLD_MINUTES * 60 + 1), True)
check("  and says how many were held", "2 further refusal(s) from this publisher were held" in b.msgs[-1], True)
check("  the count resets after it is reported",
      la.refused(rows, "e", sender=b, now=T0 + 2 * la.HOLD_MINUTES * 60 + 2) and
      "further refusal" not in b.msgs[-1], True)
fresh_state()
nb = Box(accept=False)
check("a refusal Telegram did not accept returns False", la.refused(rows, "e", sender=nb, now=T0), False)
check("  and starts no hold: the next refusal tries again",
      la.refused(rows, "e", sender=nb, now=T0 + 1) is False and len(nb.msgs), 2)
many = [row(f"S{i:02d}") for i in range(12)]
fresh_state()
b = Box()
la.refused(many, "e", sender=b, now=T0)
check("more than MAX_SYMBOLS symbols are counted, not dropped",
      f"Symbols (12): " in b.msgs[0] and f"and {12 - la.MAX_SYMBOLS} more" in b.msgs[0], True)


def _raise(m):
    raise RuntimeError("telegram exploded")


fresh_state()
check("a sender that raises does not raise out of refused()", la.refused(rows, "e", sender=_raise), False)
check("refused() with no rows still sends, naming an unknown publisher",
      (lambda bb: la.refused([], "e", sender=bb, now=T0) and "unknown publisher" in bb.msgs[0])(Box()), True)

# ══════════════════════════════════════════════════════════════════
head("2. FIRST SUCCESS SPEAKS ONCE, WITH THE COUNT")
# ══════════════════════════════════════════════════════════════════
fresh_state()
b = Box()
cnt = lambda u, k: 7
check("the first success sends", la.first_success(rows, "u", "k", sender=b, counter=cnt), True)
check("  it carries the ledger's count", "Ledger now holds: 7 observation(s)" in b.msgs[0], True)
check("  and the publisher that wrote it", "Written by: engine/publish_orb.py · orb_break" in b.msgs[0], True)
check("the second success says nothing", la.first_success(rows, "u", "k", sender=b, counter=cnt), False)
check("  nothing went out", len(b.msgs), 1)
check("the marker records the message_id Telegram returned",
      '"message_id": 41' in la._marker().read_text(), True)
fresh_state()
# THE RACE: both publishers pass exists() before either has created the marker.
# The O_EXCL create is the only thing that can stop the second one, so the test
# must get past exists() to reach it.
la._marker().parent.mkdir(parents=True, exist_ok=True)
fd = os.open(la._marker(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)  # the other publisher, mid-send


class _Racy(type(la._marker())):
    def exists(self, *a, **k):
        return False                       # it did not exist when we looked


_real_marker = la._marker
la._marker = lambda: _Racy(_real_marker())
b = Box()
check("two publishers racing past exists(): the second sends nothing",
      la.first_success(rows, "u", "k", sender=b, counter=cnt), False)
check("  nothing went out", len(b.msgs), 0)
la._marker = _real_marker
os.close(fd)
fresh_state()
nb = Box(accept=False)
check("a send Telegram did not accept returns False",
      la.first_success(rows, "u", "k", sender=nb, counter=cnt), False)
check("  and removes the marker, so the next success retries", la._marker().exists(), False)
b = Box()
check("  the retry sends", la.first_success(rows, "u", "k", sender=b, counter=cnt), True)
fresh_state()
b = Box()
la.first_success(rows, "u", "k", sender=b, counter=lambda u, k: None)
check("an unreadable count is said, not guessed",
      "Ledger now holds: count could not be read" in b.msgs[0], True)
fresh_state()
check("a counter that raises does not raise out of first_success()",
      la.first_success(rows, "u", "k", sender=Box(), counter=lambda u, k: 1 / 0), False)
check("  and leaves no marker behind", la._marker().exists(), False)

# Through record(): only a write that STORED something is a success.
fresh_state()
_sent, _first = [], []
la.send = lambda m: (_sent.append(m) or (True, 1, 1))
la.ledger_count = lambda u, k: 2
_orig_first = la.first_success
la.first_success = lambda r, u, k, **kw: (_first.append(len(r)) or _orig_first(r, u, k, **kw))
_key = os.environ.get("SUPABASE_SERVICE_KEY")
os.environ["SUPABASE_SERVICE_KEY"] = "test-key-not-real"
_get, _post = requests.get, requests.post


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b, self.text = code, body, str(body)

    def json(self):
        return self._b


stored = []
requests.post = lambda *a, **k: (stored.extend(k["json"]) or _Resp(201, None))
requests.get = lambda *a, **k: _Resp(200, [{"observation_id": f"o{i}", "natural_key": r["natural_key"],
                                              "values_sha256": r["values_sha256"],
                                              "corrects_observation_id": None,
                                              "recorded_at": "x"} for i, r in enumerate(stored)])
check("a stored write: record() is True", obs.record(rows), True)
check("  and it announced the first success, once", (_first, len(_sent)), ([2], 1))
check("  the message is the success message",
      "first production observation recorded" in (_sent[0] if _sent else ""), True)
check("the same rows again (stores nothing new): record() is True", obs.record(rows), True)
check("  and first_success was not even called", _first, [2])
requests.get, requests.post = _get, _post
la.first_success = _orig_first
if _key is None:
    os.environ.pop("SUPABASE_SERVICE_KEY", None)
else:
    os.environ["SUPABASE_SERVICE_KEY"] = _key

# ══════════════════════════════════════════════════════════════════
head("3. PROOF MODE HAS ITS OWN MARKER AND SAYS IT IS A PROOF")
# ══════════════════════════════════════════════════════════════════
fresh_state()
os.environ["TSL_LEDGER_ALERT_PROOF"] = "1"
b = Box()
la.first_success(rows, "u", "k", sender=b, counter=cnt)
check("the proof message carries the PROOF banner", b.msgs[0].startswith("🧪 <b>PROOF"), True)
check("the proof writes the PROOF marker", la._marker().name, "first_success_PROOF.json")
b2 = Box()
check("a proof refusal is never held",
      la.refused(rows, "e", sender=b2, now=T0) and la.refused(rows, "e", sender=b2, now=T0 + 1), True)
os.environ.pop("TSL_LEDGER_ALERT_PROOF")
b = Box()
check("after a proof, the REAL first success still sends",
      la.first_success(rows, "u", "k", sender=b, counter=cnt), True)
check("  without the banner", (b.msgs[0] if b.msgs else "").startswith("✅"), True)

# ══════════════════════════════════════════════════════════════════
head("4. A DRY SCORER RUN SENDS NO ALARM; A REAL ONE STILL CHECKS")
# ══════════════════════════════════════════════════════════════════
from engine import score_observations as so                   # noqa: E402
from engine import surveillance_snapshot as ss                 # noqa: E402
calls = []
_check, _trading, _calread = ss.check, so.tc.is_trading_day, so.tc.calendar_is_readable
ss.check = lambda d, **k: calls.append(d) or True
so.tc.is_trading_day = lambda d: True
so.tc.calendar_is_readable = lambda: False      # stop right after the alarm step
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key-not-real")
so.run(dry=True)
check("--dry on a trading day: the surveillance check is NOT made", calls, [])
so.run(dry=False)
check("a real run on a trading day: the check IS made", len(calls), 1)
ss.check, so.tc.is_trading_day, so.tc.calendar_is_readable = _check, _trading, _calread
if _key is None:
    os.environ.pop("SUPABASE_SERVICE_KEY", None)

print()
print("=" * 112)
if failures:
    print(f"FAILED — {len(failures)} assertion(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("PASSED — refusals alert and hold, first success speaks once, proofs spend "
      "nothing, dry runs stay dry")
