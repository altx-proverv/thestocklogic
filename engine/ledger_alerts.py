"""
The observation ledger's two Telegram messages: REFUSED, and FIRST SUCCESS.

REFUSED. engine.observations.record() is fail-closed: a publisher whose rows
cannot be recorded does not publish. That is correct, and on its own it is
silent -- the page just stops changing. Every refusal now sends a Telegram
naming the publisher, the observation type and session, the symbols and the
error. A publisher that re-sends every five minutes would repeat it every five
minutes, and an alarm that repeats is one people mute, so repeats from the same
publisher and type are held for HOLD_MINUTES and counted into the next alert.

FIRST SUCCESS. The first time record() actually stores rows in production, one
message says so, with the ledger's row count. Then nothing: the marker file
makes it once per box, claimed with O_EXCL so two publishers finishing together
cannot both send it. If the send is not accepted the marker is removed, so the
next success tries again rather than the news being lost.

ACCEPTED IS NOT DELIVERED (docs/learnings.md, "HTTP 200 is not delivery").
send() logs Telegram's message_id and chat.id for every message, so whether a
specific alert reached a person can be asked of that message.

NEVER RAISES. These run inside every publisher. A failure here is logged and
swallowed; it must not turn a refusal into a crash or a success into a refusal.

PROOF MODE. TSL_LEDGER_ALERT_PROOF=1 prefixes every message with a PROOF banner,
skips the hold, and gives first_success its own marker, so proving the message
reaches the chat does not consume tomorrow's real one.

    python -m engine.ledger_alerts --prove refusal   # through record(), for real
    python -m engine.ledger_alerts --prove first
"""
import html
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("observations")

ROOT = Path(__file__).resolve().parent.parent
# data/reference/ is gitignored, so the box's five-minute `git reset --hard`
# leaves these files alone.
STATE_DIR = Path(os.environ.get("TSL_LEDGER_ALERT_DIR",
                                ROOT / "data" / "reference" / "ledger_alerts"))
HOLD_MINUTES = 60
MAX_SYMBOLS = 8
IST = timezone(timedelta(hours=5, minutes=30))


def _proof() -> bool:
    return os.environ.get("TSL_LEDGER_ALERT_PROOF") == "1"


def _banner(msg: str) -> str:
    if not _proof():
        return msg
    return ("🧪 <b>PROOF — not a real event.</b> This is the message the ledger "
            "sends; it was fired on purpose to prove it reaches you.\n\n" + msg)


def send(message: str):
    """(accepted, message_id, chat_id). Same bot and chat as every cron job:
    TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the environment."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        log.critical("ledger alert NOT SENT: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")
        return False, None, None
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": message,
                                "parse_mode": "HTML"}, timeout=10)
        if r.status_code != 200:
            log.critical(f"ledger alert NOT ACCEPTED: Telegram HTTP {r.status_code} "
                         f"{r.text[:160]}")
            return False, None, None
        res = r.json().get("result", {})
        mid, cid = res.get("message_id"), res.get("chat", {}).get("id")
        log.info(f"ledger alert accepted by Telegram: message_id={mid} chat.id={cid}")
        return True, mid, cid
    except Exception as e:
        log.critical(f"ledger alert NOT SENT: {type(e).__name__}: {e}")
        return False, None, None


# ── REFUSED ─────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in s)


def refused(rows: list, error: str, *, sender=None, now=None) -> bool:
    """Alert that `rows` were refused. True if a message was accepted."""
    try:
        sender = sender or send
        now = now or time.time()
        first = rows[0] if rows else {}
        publisher = first.get("source_file") or "unknown publisher"
        otype = first.get("observation_type") or "?"
        sessions = sorted({str(r.get("session_date")) for r in rows}) or ["?"]
        symbols = sorted({str(r.get("symbol")) for r in rows if r.get("symbol")})

        held = 0
        state = STATE_DIR / f"refused_{_slug(publisher)}_{_slug(otype)}.json"
        if not _proof():
            try:
                st = json.loads(state.read_text())
            except Exception:
                st = {"last_sent": 0, "held": 0}
            if now - st.get("last_sent", 0) < HOLD_MINUTES * 60:
                st["held"] = st.get("held", 0) + 1
                _write(state, st)
                log.error(f"ledger refusal alert HELD ({st['held']} since the last "
                          f"one): {publisher} {otype}")
                return False
            held = st.get("held", 0)

        shown = ", ".join(symbols[:MAX_SYMBOLS])
        if len(symbols) > MAX_SYMBOLS:
            shown += f" and {len(symbols) - MAX_SYMBOLS} more"
        e = html.escape
        msg = (f"⛔ <b>LEDGER REFUSED — {e(publisher)} did NOT publish</b>\n"
               f"Type: {e(otype)} · session {e(', '.join(sessions))}\n"
               f"Symbols ({len(symbols)}): {e(shown) or '—'}\n"
               f"Error: {e(str(error)[:400])}\n"
               f"The publisher is fail-closed: nothing reaches the page until the "
               f"ledger records it.")
        if held:
            msg += f"\n{held} further refusal(s) from this publisher were held since the last alert."
        msg += (f"\nRepeats from this publisher are held for {HOLD_MINUTES} min."
                if not _proof() else "")
        ok, _, _ = sender(_banner(msg))
        if ok and not _proof():
            _write(state, {"last_sent": now, "held": 0})
        return ok
    except Exception as ex:
        log.critical(f"ledger refusal alert failed: {type(ex).__name__}: {ex}")
        return False


def _write(path: Path, obj) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj))
        tmp.replace(path)
    except Exception as e:
        log.error(f"ledger alert state not written ({path.name}): {e}")


# ── FIRST SUCCESS ───────────────────────────────────────────────────

def _marker() -> Path:
    return STATE_DIR / ("first_success_PROOF.json" if _proof()
                        else "first_success.json")


def ledger_count(url: str, key: str):
    """Rows in the ledger, from Content-Range. None if it cannot be read --
    the message then says so rather than printing a guess."""
    try:
        import requests
        r = requests.get(f"{url}/rest/v1/observations",
                         params={"select": "observation_id"},
                         headers={"apikey": key, "Authorization": f"Bearer {key}",
                                  "Prefer": "count=exact", "Range-Unit": "items",
                                  "Range": "0-0"}, timeout=15)
        if r.status_code not in (200, 206):
            return None
        return int(r.headers.get("Content-Range", "").rsplit("/", 1)[1])
    except Exception:
        return None


def first_success(rows: list, url: str, key: str, *, sender=None, counter=None) -> bool:
    """Once per box: announce the first rows the ledger stored. True only on
    the call that sends it."""
    marker = _marker()
    if marker.exists():
        return False
    try:
        sender = sender or send
        counter = counter or ledger_count
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False                     # another publisher claimed it
        n = counter(url, key)
        first = rows[0] if rows else {}
        symbols = sorted({str(r.get("symbol")) for r in rows if r.get("symbol")})
        e = html.escape
        msg = (f"✅ <b>Observation ledger: first production observation recorded</b>\n"
               f"Ledger now holds: {n if n is not None else 'count could not be read'} "
               f"observation(s)\n"
               f"Written by: {e(str(first.get('source_file', '?')))} · "
               f"{e(str(first.get('observation_type', '?')))} · session "
               f"{e(str(first.get('session_date', '?')))}\n"
               f"Symbols in that write ({len(symbols)}): "
               f"{e(', '.join(symbols[:MAX_SYMBOLS]))}"
               f"{' …' if len(symbols) > MAX_SYMBOLS else ''}\n"
               f"This is the only success message. From here the ledger speaks "
               f"only when it refuses.")
        ok, mid, cid = sender(_banner(msg))
        if ok:
            os.write(fd, json.dumps({"sent_at": datetime.now(IST).isoformat(),
                                     "count": n, "message_id": mid,
                                     "chat_id": cid}).encode())
            os.close(fd)
            return True
        os.close(fd)
        marker.unlink(missing_ok=True)       # not accepted: the next success retries
        return False
    except Exception as ex:
        log.critical(f"ledger first-success message failed: {type(ex).__name__}: {ex}")
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass
        return False


# ── PROOF ───────────────────────────────────────────────────────────

def _prove(which: str) -> int:
    os.environ["TSL_LEDGER_ALERT_PROOF"] = "1"
    from engine import observations
    row = observations.build("signal", symbol="PROOF", session_date=datetime.now(IST).date(),
                             direction="LONG", reference_price=1.0,
                             computed_values={"proof": True},
                             source_file="engine/ledger_alerts.py --prove",
                             engine_sha=None)
    if which == "refusal":
        # The real path: record() with no service key refuses before it can
        # touch the database, and its refusal sends the alert. Writes nothing.
        os.environ.pop("SUPABASE_SERVICE_KEY", None)
        stored = observations.record([row])
        log.info(f"proof refusal: record() returned {stored} (False is the proof)")
        return 0 if stored is False else 1
    url, key = observations._cfg()
    sent = first_success([row], url, key)
    log.info(f"proof first-success: sent={sent}")
    return 0 if sent else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) == 3 and sys.argv[1] == "--prove" and sys.argv[2] in ("refusal", "first"):
        sys.exit(_prove(sys.argv[2]))
    print(__doc__)
    sys.exit(2)
