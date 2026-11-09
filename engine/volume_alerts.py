#!/usr/bin/env python3
"""
VOLUME >= 10x, ON TELEGRAM, EVERY SESSION. One message after the close.
=======================================================================

WHY THIS EXISTS. On 2026-11-02 ITI traded 103x its prior-20-session average
volume and took the #1 Hot 10 slot. Nothing told anyone: the only per-symbol
"alert" in the pipeline was engine/06_push_supabase.notify_atlas, whose
docstring said Grade A signals triggered Telegram and whose body wrote a log
line. It was found six days later by asking.

THIS SENDS A TELEGRAM MESSAGE. After the close, it lists every symbol in
engine.universe.ALL_SYMBOLS (705 as of 2026-11-08) whose session volume
reached VOL_MULT x the mean of its prior VOL_WINDOW sessions, with the move, the
range multiple, and whether the symbol appeared on each surface.

A SEPARATE JOB, NOT PART OF THE && CHAIN. 06_push runs only if bhavcopy, 02b,
07 and 03b all succeed, and it only sees qualifying signals -- six of the ten
2 Nov Hot 10 names were not in that batch. This job reads the per-stock
bhavcopy parquets directly, so a failure upstream in the chain does not
silence it.

THE MULTIPLE IS RADAR'S, EXCLUDING THE SESSION. radar.bars.vol_average with
through_prior=True: volume / mean(volume of the previous 20 rows on file). The
stored rvol in signals.rvol comes from 02b's rolling(20) mean, which INCLUDES
the session. A 100x day raises its own mean about 6x, so the stored figure is
understated. It is shown beside the computed one, labelled, where a signal row
exists, and it is never used in the threshold. "Previous 20 rows" means rows on
file: where the archive is missing a session, the window reaches further back.

RANGE MULTIPLE, same basis: (high - low) / mean(high - low) of the previous 20
rows, the session excluded.

SURFACES are read for the session itself, never "today":
    Hot 10      radar_card_versions, trade_date = session, passed (= served)
    Scanner     radar_scanner_daily, trade_date = session
    Signal      signals, signal_date = session, publication_kind = signal
    Watchlist   signals, signal_date = session, publication_kind = candidate
    Heavy       heavy_quiet, session_date = session (the intraday panel)
A surface with NO rows at all for the session reads "?" rather than "no":
absence of rows is not evidence the symbol was absent, only that nothing was
written. A surface that cannot be read also reads "?", and the reason is in
the message.

SILENCE MEANS BREAKAGE. A day with no symbol over the threshold still sends one
line with the session date and the count measured. A holiday sends one line
saying so. If the data for the session is not on disk, that is an alarm.

ACCEPTED MEANS message_id. A 200 from the Bot API is Telegram accepting the
request. A send counts only if the response carries a result.message_id, which
is logged. Anything else is a failure.

A FAILED SEND ALARMS through the same bot, as a short message naming the
symbols that were not sent, and the run exits non-zero. If Telegram itself is
unreachable the alarm cannot reach anyone either; then the missing daily
message is the signal, which is why a quiet day still sends.

ONCE PER SYMBOL PER SESSION. Each symbol's message_id is recorded in
data/reference/volume_alerts/<session>.json (gitignored, so the box's
five-minute reset leaves it alone). A second run sends only symbols not yet
sent, and if there are none it sends nothing and logs that it sent nothing.

    python -m engine.volume_alerts                      # the scheduled run
    python -m engine.volume_alerts --date 2026-11-02 --dry-run   # print, send nothing
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("volume-alerts")
IST = timezone(timedelta(hours=5, minutes=30))

VOL_MULT = 10.0
VOL_WINDOW = 20             # radar_config.vol_avg_window's shipped default
STATE_DIR = Path(os.environ.get("TSL_VOLUME_ALERT_DIR",
                                ROOT / "data" / "reference" / "volume_alerts"))
# Telegram rejects a message over 4096 characters. Leave room for the part
# marker and the escaping already applied.
CHUNK_CHARS = 3800

SURFACES = ("Hot10", "Scanner", "Signal", "Watchlist", "Heavy")


# ── SEND ───────────────────────────────────────────────────────────

def send(message: str):
    """(message_id or None). None is a failure, whatever the HTTP status was.

    engine.ledger_alerts.send treats HTTP 200 as accepted and returns the
    message_id it found, possibly None. Here a missing message_id IS the
    failure.
    """
    from engine import ledger_alerts
    ok, mid, cid = ledger_alerts.send(message)
    if ok and isinstance(mid, int):
        log.info(f"accepted: message_id={mid} chat.id={cid}")
        return mid
    log.critical(f"NOT ACCEPTED: ok={ok} message_id={mid!r} — counted as a failure")
    return None


def alarm(text: str, sender=None) -> bool:
    """The failure path. The same bot as every other alarm in this repo."""
    sender = sender or send
    mid = sender(f"⛔ <b>VOLUME ALERT FAILED</b>\n{html.escape(text)[:1500]}")
    if mid is None:
        log.critical("the failure alarm was not accepted either — log-only")
    return mid is not None


# ── MEASURE ────────────────────────────────────────────────────────

def measure(symbol: str, on: date):
    """-> ("no_row" | "short" | dict). Reads the parquet through radar.bars."""
    from radar import bars
    df = bars.frame_through(symbol, on)
    if df is None or not len(df) or df["date"].iloc[-1] != on:
        return "no_row"
    avg = bars.vol_average(df, VOL_WINDOW, through_prior=True)
    if not avg:
        return "short"
    row = df.iloc[-1]
    try:
        vol = float(row["volume"])
        close, prev = float(row["close"]), float(row["prev_close"])
        rng = (df["high"].astype(float) - df["low"].astype(float))
    except (TypeError, ValueError, KeyError):
        return "short"
    prior = rng.iloc[:-1].iloc[-VOL_WINDOW:]
    rmean = float(prior.mean()) if len(prior) == VOL_WINDOW else 0.0
    return {
        "symbol": symbol,
        "volume": int(vol),
        "avg": avg,
        "mult": vol / avg,
        "move": (close / prev - 1) * 100 if prev > 0 else None,
        "range_mult": float(rng.iloc[-1]) / rmean if rmean > 0 else None,
        "close": close,
    }


def scan(on: date, symbols) -> dict:
    hits, no_row, short = [], 0, 0
    for s in symbols:
        try:
            m = measure(s, on)
        except Exception as e:
            log.warning(f"{s}: measure raised ({type(e).__name__}: {e})")
            short += 1
            continue
        if m == "no_row":
            no_row += 1
        elif m == "short":
            short += 1
        elif m["mult"] >= VOL_MULT:
            hits.append(m)
    hits.sort(key=lambda h: (-h["mult"], h["symbol"]))
    return {"hits": hits, "universe": len(symbols), "no_row": no_row,
            "short": short, "measured": len(symbols) - no_row - short}


# ── SURFACES ───────────────────────────────────────────────────────

def _read(table, query, getter):
    try:
        return getter(table, query), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def surfaces(on: date, getter=None) -> dict:
    """{surface: {"rows": n|None, "err": str|None, "by": {symbol: label}}}
    plus {"stored": {symbol: [rvol, ...]}}."""
    if getter is None:
        from radar import store
        getter = store._get
    d = on.isoformat()
    out = {}

    rows, err = _read("radar_card_versions",
                      f"?trade_date=eq.{d}&validation_status=eq.passed"
                      f"&select=rank:payload->>rank,radar_events(symbol)", getter)
    by = {}
    for r in rows or []:
        sym = (r.get("radar_events") or {}).get("symbol")
        if sym:
            by[sym] = f"#{r.get('rank')}" if r.get("rank") else "yes"
    out["Hot10"] = {"rows": None if rows is None else len(rows), "err": err, "by": by}

    rows, err = _read("radar_scanner_daily",
                      f"?trade_date=eq.{d}&select=symbol,side,rank_in_side", getter)
    by = {}
    for r in rows or []:
        by[r["symbol"]] = f"52w {r.get('side')} #{r.get('rank_in_side')}"
    out["Scanner"] = {"rows": None if rows is None else len(rows), "err": err, "by": by}

    rows, err = _read("signals",
                      f"?signal_date=eq.{d}&select=symbol,publication_kind,rvol", getter)
    sig, cand, stored = {}, {}, {}
    for r in rows or []:
        if r.get("publication_kind") == "candidate":
            cand[r["symbol"]] = "yes"
        else:
            sig[r["symbol"]] = "yes"
            if r.get("rvol") is not None:
                stored.setdefault(r["symbol"], []).append(float(r["rvol"]))
    n_sig = None if rows is None else sum(1 for r in rows if r.get("publication_kind") != "candidate")
    n_cand = None if rows is None else len(rows) - n_sig
    out["Signal"] = {"rows": n_sig, "err": err, "by": sig}
    out["Watchlist"] = {"rows": n_cand, "err": err, "by": cand}

    rows, err = _read("heavy_quiet",
                      f"?session_date=eq.{d}&select=symbol,volume_ratio", getter)
    by = {}
    for r in rows or []:
        by[r["symbol"]] = "yes"
    out["Heavy"] = {"rows": None if rows is None else len(rows), "err": err, "by": by}

    out["stored"] = stored
    return out


def _cell(surf: dict, sym: str) -> str:
    if surf["rows"] is None or surf["rows"] == 0:
        return "?"
    return surf["by"].get(sym, "no")


# ── COMPOSE ────────────────────────────────────────────────────────

def _fmt_entry(h: dict, surf: dict) -> str:
    e = html.escape
    mv = f"{h['move']:+.1f}%" if h["move"] is not None else "move n/a"
    rg = f"{h['range_mult']:.1f}×" if h["range_mult"] is not None else "n/a"
    st = surf["stored"].get(h["symbol"])
    stored = ("stored rvol " + " / ".join(f"{v:.2f}×" for v in st) if st
              else "stored rvol — (no signal row)")
    cells = " · ".join(f"{name} {e(_cell(surf[name], h['symbol']))}" for name in SURFACES)
    return (f"<b>{e(h['symbol'])}</b> {mv} · close {h['close']:,.2f}\n"
            f"  vol {h['mult']:.1f}× computed | {e(stored)} · range {rg}\n"
            f"  {cells}")


def _notes(surf: dict) -> list:
    notes = []
    for name in SURFACES:
        s = surf[name]
        if s["err"]:
            notes.append(f"{name} could not be read: {html.escape(s['err'][:200])}")
        elif s["rows"] == 0:
            notes.append(f"{name}: no rows at all for this session — '?' means "
                         f"not written, not 'absent'")
    return notes


def compose(on: date, res: dict, hits: list, surf: dict, *, earlier: int = 0) -> list:
    """The message text(s). Several when the list exceeds one Telegram message."""
    wd = on.strftime("%a")
    head = (f"📊 <b>VOLUME ≥{VOL_MULT:g}× — {on.isoformat()} ({wd})</b>\n"
            f"{len(hits)} symbol(s){' new since the earlier run' if earlier else ''} · "
            f"measured {res['measured']} of {res['universe']} "
            f"({res['no_row']} no row for the session, {res['short']} under "
            f"{VOL_WINDOW} prior sessions)")
    if earlier:
        head += f"\n{earlier} symbol(s) were already sent for this session."
    basis = (f"<i>computed = volume ÷ mean of the prior {VOL_WINDOW} sessions on "
             f"file, this session excluded (radar vol_average). stored = "
             f"signals.rvol from 02b, whose mean includes the session. range = "
             f"(H−L) ÷ prior-{VOL_WINDOW} mean (H−L), same basis.</i>")
    notes = _notes(surf)
    tail = "\n".join([basis] + notes + [html.escape(_coverage(on)),
                                       html.escape(_tick_map()),
                                       html.escape(_exit_alarms())])

    entries = [_fmt_entry(h, surf) for h in hits]
    chunks, cur = [], []
    budget = CHUNK_CHARS - len(head) - len(tail) - 40
    size = 0
    for t in entries:
        if cur and size + len(t) + 2 > budget:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(t)
        size += len(t) + 2
    if cur:
        chunks.append(cur)

    msgs = []
    for i, c in enumerate(chunks, start=1):
        part = f" ({i}/{len(chunks)})" if len(chunks) > 1 else ""
        msgs.append(head + part + "\n\n" + "\n\n".join(c) + "\n\n" + tail)
    return msgs, [[h["symbol"] for h in hits[sum(len(x) for x in chunks[:i]):
                                             sum(len(x) for x in chunks[:i + 1])]]
                  for i in range(len(chunks))]


def compose_zero(on: date, res: dict) -> str:
    return (f"📊 <b>VOLUME ≥{VOL_MULT:g}× — {on.isoformat()} ({on.strftime('%a')})</b>: "
            f"no symbol crossed. Measured {res['measured']} of {res['universe']} "
            f"({res['no_row']} no row for the session, {res['short']} under "
            f"{VOL_WINDOW} prior sessions).\n{html.escape(_coverage(on))}"
            f"\n{html.escape(_tick_map())}"
            f"\n{html.escape(_exit_alarms())}")


def _exit_alarms() -> str:
    try:
        from atlas.execution.exit_alarms import daily_line
        return daily_line()
    except Exception as e:
        return f"Exit alarms: could not be read ({type(e).__name__})"


def _tick_map() -> str:
    """Is the real tick map present and sane (tools/check_tick_map.py)? The
    exit paths price every order through it. Never raises."""
    try:
        from tools.check_tick_map import line
        return line()
    except Exception as e:
        return f"Tick map: could not be checked ({type(e).__name__})"


def _coverage(on: date) -> str:
    """The Upstox feed guard's record for the session, so a dead guard shows
    up in this message rather than as an absence nobody looks for."""
    try:
        from engine.quote_coverage import daily_line
        return daily_line(on.isoformat())
    except Exception as e:
        return f"Upstox coverage {on}: could not be read ({type(e).__name__})"


# ── STATE ──────────────────────────────────────────────────────────

def _state_path(on: date) -> Path:
    return STATE_DIR / f"{on.isoformat()}.json"


def load_state(on: date) -> dict:
    try:
        return json.loads(_state_path(on).read_text())
    except Exception:
        return {"symbols": {}, "zero_message_id": None}


def save_state(on: date, st: dict) -> None:
    p = _state_path(on)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(p)


# ── RUN ────────────────────────────────────────────────────────────

def _session(arg: str | None) -> date:
    return (date.fromisoformat(arg) if arg else datetime.now(IST).date())


def run(on: date, *, dry_run=False, sender=None, getter=None, symbols=None,
        printer=print) -> int:
    sender = sender or send
    log.info(f"heartbeat: session {on} dry_run={dry_run}")

    from engine import trading_calendar as tc
    if not tc.calendar_is_readable():
        why = f"{on}: the NSE trading calendar is unreadable — cannot tell whether this was a session"
        if dry_run:
            printer(why)
            return 1
        alarm(why, sender)
        return 1
    if not tc.is_trading_day(on):
        msg = (f"📊 <b>VOLUME ≥{VOL_MULT:g}× — {on.isoformat()} ({on.strftime('%a')})</b>: "
               f"not an NSE trading session. Nothing to measure.")
        if dry_run:
            printer(msg)
            return 0
        st = load_state(on)
        if st.get("zero_message_id"):
            log.info(f"{on}: holiday line already sent (message_id="
                     f"{st['zero_message_id']}) — sending nothing")
            return 0
        mid = sender(msg)
        if mid is None:
            alarm(f"{on}: the holiday line was not accepted by Telegram", sender)
            return 1
        st["zero_message_id"] = mid
        save_state(on, st)
        return 0

    if symbols is None:
        from engine.universe import ALL_SYMBOLS
        symbols = ALL_SYMBOLS
    res = scan(on, symbols)
    log.info(f"{on}: {len(res['hits'])} hit(s); measured {res['measured']} of "
             f"{res['universe']} ({res['no_row']} no row, {res['short']} short)")
    if res["no_row"] == res["universe"]:
        why = (f"{on}: no symbol in the {res['universe']}-symbol universe has a "
               f"row for this session. The bhavcopy has not landed in "
               f"data/processed/stocks — nothing was measured.")
        if dry_run:
            printer(why)
            return 1
        alarm(why, sender)
        return 1

    st = {"symbols": {}, "zero_message_id": None} if dry_run else load_state(on)
    sent = st["symbols"]
    new = [h for h in res["hits"] if h["symbol"] not in sent]

    if not res["hits"]:
        msg = compose_zero(on, res)
        if dry_run:
            printer(msg)
            return 0
        if st.get("zero_message_id"):
            log.info(f"{on}: zero line already sent (message_id="
                     f"{st['zero_message_id']}) — sending nothing new")
            return 0
        mid = sender(msg)
        if mid is None:
            alarm(f"{on}: the no-symbol-crossed line was not accepted by Telegram", sender)
            return 1
        st["zero_message_id"] = mid
        save_state(on, st)
        return 0

    if not new:
        log.info(f"{on}: all {len(res['hits'])} symbol(s) already sent "
                 f"(message_ids {sorted(set(sent.values()))}) — sending nothing new")
        if dry_run:
            printer("(nothing new)")
        return 0

    surf = surfaces(on, getter)
    msgs, groups = compose(on, res, new, surf,
                           earlier=len([s for s in sent if s]))
    if dry_run:
        for m in msgs:
            printer(m)
            printer("─" * 40)
        return 0

    unsent = []
    for m, group in zip(msgs, groups):
        mid = sender(m)
        if mid is None:
            unsent.extend(group)
            continue
        for s in group:
            sent[s] = mid
        save_state(on, st)            # after each part, so a later failure keeps these
    if unsent:
        alarm(f"{on}: {len(unsent)} symbol(s) over {VOL_MULT:g}× were NOT delivered: "
              f"{', '.join(unsent[:30])}{' …' if len(unsent) > 30 else ''}. "
              f"The next run retries them.", sender)
        return 1
    log.info(f"{on}: sent {len(new)} symbol(s) in {len(msgs)} message(s)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.dry_run:
            return run(_session(a.date), dry_run=True)
        # ONE RUN AT A TIME. Two overlapping runs would both read an empty
        # state file and both send.
        import fcntl
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(STATE_DIR / ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            return run(_session(a.date))
    except Exception as e:
        log.critical(f"crashed: {type(e).__name__}: {e}")
        if not a.dry_run:
            alarm(f"engine.volume_alerts crashed: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main())
