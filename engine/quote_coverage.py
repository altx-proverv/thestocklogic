#!/usr/bin/env python3
"""
UPSTOX QUOTE COVERAGE — every key asked for, accounted for, every run.
=====================================================================

WHY. On 2026-09-23 the quotes returned fell from 499 to 498 and stayed there
for seven weeks. The missing one was CHOLAFIN, whose INSTRUMENT_KEYS entry was
a debenture. live_prices kept a frozen ltp=0 row, btst_engine read it and
substituted the previous close, and nothing alarmed. A count in a log line is
not a check.

BY IDENTITY, NOT BY COUNT. Each quote Upstox returns carries
`instrument_token`, the exact key that was asked for (verified 2026-11-09:
NSE_EQ|INE121A01024 came back as instrument_token NSE_EQ|INE121A01024). So
"which keys got no quote" is a set difference, and it names the symbol. A count
would pass if one key dropped while another was added.

TWO THINGS ARE CHECKED:
  MISSING   a key we subscribed got no quote
  RENAMED   a key's quote came back under a different trading symbol than our
            map's (the key is ISIN-based, so it follows a rename -- ICICIPRULI
            returns as ICICILIFE). That is how the 2026-10-23 rename would have
            been caught on the day.

KNOWN EXCEPTIONS ARE NAMED, WITH A REASON, AND RE-CHECKED. Not a tolerance
("alarm below 743"): a tolerance tuned to today's broken state would pass the
next drop if one of today's known gaps healed at the same time. An entry here
is reported as STALE the day it stops being true, so the list cannot quietly
outlive its reason.

ZERO QUOTES IS THE LOUDEST CASE, not a skipped one. upstox_ws returned early
on an empty fetch with a log line; an expired token on a trading day reached
nobody. That path now alarms.

ALARMS ARE HELD per distinct finding per session -- the feed runs four times a
day and a repeat of the same missing key is one message, not four. A NEW
missing key alarms at once.

SILENCE IS DISTINGUISHABLE. Every run is appended to
data/reference/quote_coverage/<session>.json. engine/volume_alerts reads it
and prints one line in its daily message: the runs and counts, or
"NO RUN RECORDED", so a dead guard shows up in a message that must arrive.

    python -m engine.quote_coverage --prove   # real keys, real quotes, one forced drop, PROOF banner
"""
from __future__ import annotations

import html
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("quote-coverage")
IST = timezone(timedelta(hours=5, minutes=30))
STATE_DIR = Path(os.environ.get("TSL_QUOTE_COVERAGE_DIR",
                                ROOT / "data" / "reference" / "quote_coverage"))

# Each entry: our symbol -> why its key is expected to return nothing.
KNOWN_MISSING = {
    "JSWDULUX": "key is the pre-split ISIN INE133A01011; the 1:10 split on "
                "2026-10-22 issued INE133A01029. Symbol is in EXCLUDED.",
}
# Each entry: our symbol -> the trading symbol Upstox is expected to return.
KNOWN_RENAMED = {
    "ICICIPRULI": ("ICICILIFE", "renamed 2026-10-23, same ISIN; migration "
                                "option (c) agreed, not started"),
}


def evaluate(keys: dict, quotes: dict) -> dict:
    """keys: {our_symbol: instrument_key}. quotes: Upstox's dict, keyed
    'NSE_EQ:<tradingsymbol>', each with instrument_token."""
    by_key = {}
    for qk, q in (quotes or {}).items():
        tok = (q or {}).get("instrument_token")
        if tok:
            by_key[tok] = q.get("symbol") or qk.split(":", 1)[-1]
    requested = {s: k for s, k in keys.items() if k}
    missing = sorted(s for s, k in requested.items() if k not in by_key)
    renamed = sorted((s, by_key[k]) for s, k in requested.items()
                     if k in by_key and by_key[k] != s)
    unexpected_missing = [s for s in missing if s not in KNOWN_MISSING]
    unexpected_renamed = [(s, n) for s, n in renamed
                          if KNOWN_RENAMED.get(s, (None,))[0] != n]
    stale = ([f"{s}: listed as known-missing but returned a quote"
              for s in KNOWN_MISSING if s in requested and s not in missing]
             + [f"{s}: listed as renamed to {KNOWN_RENAMED[s][0]} but returned "
                f"as {dict(renamed).get(s, s)}"
                for s in KNOWN_RENAMED if s in requested
                and dict(renamed).get(s) != KNOWN_RENAMED[s][0]
                and s not in missing])
    return {"requested": len(requested), "returned": len(by_key),
            "missing": missing, "renamed": renamed,
            "unexpected_missing": unexpected_missing,
            "unexpected_renamed": unexpected_renamed, "stale_exceptions": stale,
            "expected_returned": len(requested) - len(
                [s for s in KNOWN_MISSING if s in requested])}


def _state_path(session: str) -> Path:
    return STATE_DIR / f"{session}.json"


def _load(session: str) -> dict:
    try:
        return json.loads(_state_path(session).read_text())
    except Exception:
        return {"runs": [], "alarmed": []}


def _save(session: str, st: dict) -> None:
    try:
        p = _state_path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(p)
    except Exception as e:
        log.critical(f"quote coverage state not written: {e}")


def _findings(r: dict) -> list:
    f = []
    if r["returned"] == 0:
        f.append("ZERO quotes returned — the whole feed failed (token, network, or Upstox)")
    else:
        f += [f"MISSING {s}" for s in r["unexpected_missing"]]
    f += [f"RENAMED {s} -> {n}" for s, n in r["unexpected_renamed"]]
    f += [f"STALE EXCEPTION {x}" for x in r["stale_exceptions"]]
    return f


def check(keys: dict, quotes: dict, run_label: str, *, sender=None, now=None,
          proof=False) -> dict:
    """Evaluate, record the run, alarm on anything new. Never raises."""
    try:
        now = now or datetime.now(IST)
        session = now.date().isoformat()
        r = evaluate(keys, quotes)
        found = _findings(r)
        log.info(f"quote coverage [{run_label}]: {r['returned']}/{r['requested']} "
                 f"(expected {r['expected_returned']}; known missing: "
                 f"{', '.join(s for s in r['missing'] if s in KNOWN_MISSING) or 'none'})"
                 f"{' — ' + '; '.join(found) if found else ' — ok'}")
        if proof:
            new = found
            st = None
        else:
            st = _load(session)
            st["runs"].append({"at": now.isoformat(timespec="seconds"),
                               "label": run_label, "requested": r["requested"],
                               "returned": r["returned"],
                               "expected": r["expected_returned"],
                               "findings": found})
            new = [x for x in found if x not in st["alarmed"]]
        if new:
            if sender is None:
                from engine.volume_alerts import send as sender
            e = html.escape
            msg = (f"📡 <b>UPSTOX QUOTE COVERAGE — {e(session)} {e(run_label)}</b>\n"
                   f"{r['returned']} of {r['requested']} keys returned a quote; "
                   f"expected {r['expected_returned']}.\n"
                   + "\n".join(f"• {e(x)}" for x in new[:25])
                   + (f"\n… and {len(new) - 25} more" if len(new) > 25 else "")
                   + "\nA missing key means that symbol's live_prices row stops "
                     "updating and keeps its last value.")
            if proof:
                msg = ("🧪 <b>PROOF — not a real event.</b> A drop was forced on "
                       "purpose to prove the guard reaches you. The other numbers "
                       "are real.\n\n" + msg)
            mid = sender(msg)
            if mid is not None and st is not None:
                st["alarmed"] += new
            if mid is None:
                log.critical("quote coverage alarm NOT ACCEPTED — findings: "
                             + "; ".join(new))
            r["message_id"] = mid
        if st is not None:
            _save(session, st)
        r["findings"] = found
        return r
    except Exception as ex:
        log.critical(f"quote coverage check failed: {type(ex).__name__}: {ex}")
        return {"error": str(ex)}


def daily_line(session: str) -> str:
    """One line for volume_alerts' daily message. Never raises."""
    try:
        p = _state_path(session)
        if not p.exists():
            return (f"Upstox coverage {session}: NO RUN RECORDED — the feed "
                    f"guard did not run this session")
        runs = json.loads(p.read_text()).get("runs", [])
        if not runs:
            return f"Upstox coverage {session}: NO RUN RECORDED"
        cnt = ", ".join(f"{x['returned']}/{x['requested']}" for x in runs)
        bad = sorted({f for x in runs for f in x.get("findings", [])})
        return (f"Upstox coverage {session}: {len(runs)} run(s) — {cnt}"
                + (f" — {len(bad)} finding(s): {'; '.join(bad)[:300]}" if bad
                   else f" — as expected ({runs[-1]['expected']})"))
    except Exception as e:
        return f"Upstox coverage {session}: could not be read ({type(e).__name__})"


def _prove() -> int:
    """Real keys, real quotes from Upstox, one returned quote removed."""
    sys.path.insert(0, str(ROOT / "engine"))
    from engine.universe import INSTRUMENT_KEYS
    import upstox_ws
    quotes = upstox_ws.get_market_quotes([v for v in INSTRUMENT_KEYS.values() if v])
    if not quotes:
        log.error("proof: Upstox returned no quotes (token?) — nothing to force")
        return 1
    victim = sorted(quotes)[0]
    log.info(f"proof: removing {victim} from {len(quotes)} real quotes")
    forced = {k: v for k, v in quotes.items() if k != victim}
    r = check(INSTRUMENT_KEYS, forced, "PROOF", proof=True)
    log.info(f"proof: findings={r.get('findings')} message_id={r.get('message_id')}")
    return 0 if r.get("message_id") else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if sys.argv[1:] == ["--prove"]:
        sys.exit(_prove())
    print(__doc__)
    sys.exit(2)
