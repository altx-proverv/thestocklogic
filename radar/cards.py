"""
The event card payload, and the validator that can refuse it.

docs/TSL_FLASH.md §01 and §10. The three questions are the card's visible
structure, numbered, in the brief's wording:

    01  What happened, and why?
    02  Will it recover, or fall further?
    03  How would my capital behave?

R1 fills 01 and the deterministic half of 02. Question 03 is rendered as a
DECLARED GAP -- it carries a note saying what it needs and why it is empty --
rather than as an empty box or, worse, a generic paragraph that implies an
answer the engine does not have.

VALIDATION IS A GATE, NOT A LINT. A payload that breaks a hard rule is stored
with validation_status='failed' and the read views filter it out, so it cannot
reach the page. The row is kept because a refused card is the most informative
thing in the table.
"""

from __future__ import annotations

import re
import logging
from datetime import date

from radar.documents import NO_CAUSE_TEXT, DELIVERY_DISCLAIMER

log = logging.getLogger("RADAR-CARDS")

# The three questions. Exact wording from the brief; tests assert it.
Q01 = "What happened, and why?"
Q02 = "Will it recover, or fall further?"
Q03 = "How would my capital behave?"

# ── BANNED LANGUAGE ───────────────────────────────────────────────
#
# Hard rule 1: no buy/sell/target/accumulate/exit language anywhere in output
# or UI copy. Enforced here over every string the card carries, and in
# tests/test_radar.py over the page source and these modules.
#
# WORD BOUNDARIES, NOT SUBSTRINGS. "buy" as a substring matches "buyback",
# which is a legitimate filing category and a legitimate thing for a cited
# subject line to say; "sell" matches "seller" and "Sellers". The rule is about
# the engine telling a reader what to do, not about the alphabet.
BANNED_WORDS = (
    "buy", "buys", "sell", "sells", "target", "targets",
    "accumulate", "accumulating", "exit", "exits",
    "entry", "entries", "stop-loss", "stoploss",
    "book profit", "take profit", "add on dips", "hold",
    "long", "short", "bullish", "bearish", "recommend", "recommended",
    "overweight", "underweight",
)
_BANNED_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in BANNED_WORDS) + r")\b",
    re.IGNORECASE)

# WHAT IS EXEMPT, AND WHY EACH ONE. A cited filing's subject line is the
# exchange's words, not ours, and a card that refused to quote "Buyback of
# equity shares" would be unable to cite the single most price-relevant filing
# category there is. So cause_text is checked against a narrower rule: it may
# contain a banned word only as part of a filing category, and never as an
# imperative. Keys that hold OUR prose are checked in full.
_EXCHANGE_VOICE_KEYS = {"cause_text", "cause_url"}

# Filing vocabulary that legitimately contains a banned token.
_FILING_PHRASES = (
    "buyback", "buy-back", "buy back of", "share buyback",
    "sell-down", "secondary sell", "offer for sale",
    "target company",          # takeover-code language
    "long term", "long-term", "short term", "short-term",
    "shortfall", "exit offer", "exit option", "exit load",
)


def _strings(obj, path="") -> list:
    """Every string in a nested payload, with the key path that reached it."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_strings(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.extend(_strings(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        out.append((path, obj))
    return out


def banned_hits(text: str, exchange_voice: bool = False) -> list:
    """Banned words in a string. [] when clean."""
    hits = [m.group(1).lower() for m in _BANNED_RE.finditer(text or "")]
    if not hits or not exchange_voice:
        return hits
    low = (text or "").lower()
    # Drop a hit that is accounted for by a filing phrase present in the text.
    survivors = []
    for h in hits:
        if any(h in phrase and phrase in low for phrase in _FILING_PHRASES):
            continue
        survivors.append(h)
    return survivors


def build(event: dict, level_rows: list, cause: dict, outcome: dict | None,
          sessions_since: int, trade_date: date, cfg) -> dict:
    """The card payload. Pure: every input is already computed.

    Nothing in here reads bhavcopy, the network or the clock. That is what lets
    a card be rebuilt for a past trade_date and come out identical.
    """
    by_type = {r["level_type"]: r for r in (level_rows or [])}

    def lv(name):
        r = by_type.get(name)
        if not r:
            return None
        return {"price": r["price"], "dist_pct": r.get("dist_pct"),
                "ref_date": (r["ref_date"].isoformat()
                             if hasattr(r.get("ref_date"), "isoformat")
                             else r.get("ref_date"))}

    swings = {
        "highs": [lv(f"swing_high_{i}") for i in (1, 2, 3)],
        "lows":  [lv(f"swing_low_{i}") for i in (1, 2, 3)],
    }
    swings = {k: [x for x in v if x] for k, v in swings.items()}

    rsi_key = next((k for k in by_type if k.startswith("rsi_")), None)

    payload = {
        "schema": 1,
        "symbol": event["symbol"],
        "sector": event.get("sector"),
        "event_date": event["event_date"].isoformat()
            if hasattr(event["event_date"], "isoformat") else event["event_date"],
        "trade_date": trade_date.isoformat(),
        "trigger_type": event["trigger_type"],
        "direction": event.get("direction"),
        "move_pct": event.get("move_pct"),
        "vol_multiple": event.get("vol_multiple"),
        "severity": event.get("severity"),
        "heat": event.get("heat"),
        # THE HEADER CARRIES RANK / HEAT / SESSIONS, NOT A DAY COUNTER. A "day
        # 3" label implies a fixed lifetime, which is exactly the model the Hot
        # 10 replaces.
        "sessions_since": int(sessions_since),
        "rank": event.get("rank"),

        # ── 01 ────────────────────────────────────────────────────
        "q01": {
            "question": Q01,
            "what": {
                "move_pct": event.get("move_pct"),
                "direction": event.get("direction"),
                "vol_multiple": event.get("vol_multiple"),
                "close": event.get("close"),
                "pre_event_close": (lv("pre_event_close") or {}).get("price"),
                "event_high": (lv("event_high") or {}).get("price"),
                "event_low": (lv("event_low") or {}).get("price"),
                "event_vwap": (lv("event_vwap") or {}).get("price"),
                "delivery_pct": event.get("delivery_pct"),
            },
            "why": {
                "cause_text": cause.get("cause_text") or NO_CAUSE_TEXT,
                "cause_url": cause.get("cause_url"),
                "cause_published_at": (
                    cause["cause_published_at"].isoformat()
                    if cause.get("cause_published_at") else None),
                "sourced": bool(cause.get("cause_sourced")),
            },
            # ON THE CARD'S FACE, per the brief -- a required field, not a
            # footnote, and validation fails without it.
            "delivery_note": DELIVERY_DISCLAIMER,
        },

        # ── 02 ────────────────────────────────────────────────────
        "q02": {
            "question": Q02,
            "levels": {
                "w52_high": lv("w52_high"), "w52_low": lv("w52_low"),
                "dma_50": lv("dma_50"), "dma_200": lv("dma_200"),
                "ipo_price": lv("ipo_price"),
                "pre_event_close": lv("pre_event_close"),
                "event_vwap": lv("event_vwap"),
            },
            "swings": swings,
            "rsi": ({"period": cfg.rsi_period,
                     "value": by_type[rsi_key]["price"]} if rsi_key else None),
            "outcome": outcome or {},
            # §08. Below base_rate_min_n comparable events there is no base
            # rate, and the card says that rather than quoting a percentage
            # from four observations.
            "base_rate": {
                "available": False,
                "min_n": int(cfg.base_rate_min_n),
                # WORDED AROUND THE BANNED LIST ON PURPOSE. The first draft
                # said the library was still "accumulating" and validate()
                # refused the card -- correctly, since "accumulate" is on the
                # list. The fix is the prose, never the rule.
                "note": (f"A base rate needs at least {cfg.base_rate_min_n} "
                         f"comparable past events. The library is still "
                         f"filling; no frequency is shown until it has them."),
            },
        },

        # ── 03 ───────────────────────────────────────────────────
        # A DECLARED GAP. Named, with what it needs, because an empty section
        # reads as a rendering fault and a generic paragraph would imply an
        # answer R1 does not have.
        "q03": {
            "question": Q03,
            "available": False,
            "note": ("This depends on your own position size and holding "
                     "period, which TSL Flash does not know. Not computed in "
                     "this release."),
        },

        "provenance": {
            "price_source": "NSE bhavcopy",
            "cause_source": ("NSE corporate announcements"
                             if cause.get("cause_sourced") else None),
            "cause_checked": bool(cause.get("checked")),
            "levels_computed_by": "radar/levels.py (pure functions)",
            "model_used": None,
        },
    }
    # Levels that could not be computed are dropped, not carried as nulls --
    # fail closed, docs/TSL_FLASH.md §10.5.
    payload["q02"]["levels"] = {k: v for k, v in payload["q02"]["levels"].items()
                                if v is not None}
    return payload


def validate(payload: dict) -> tuple:
    """(status, notes). 'passed' or 'failed' plus what broke.

    FOUR CHECKS, EACH A HARD RULE FROM THE BRIEF:
      1. no banned action language in our own prose
      2. no cause text without a cited document
      3. the delivery-%% disclaimer is present
      4. no level type whose name implies an action
    """
    notes = []

    # 1 — banned language.
    for path, text in _strings(payload):
        exchange = any(path.endswith(k) or f".{k}" in path
                       for k in _EXCHANGE_VOICE_KEYS)
        hits = banned_hits(text, exchange_voice=exchange)
        if hits:
            notes.append(f"banned language at {path}: "
                         f"{sorted(set(hits))} in {text[:80]!r}")

    # 2 — a cause must cite a document. THE CENTRAL RULE: a plausible story
    # with no filing behind it is the thing this product must never ship.
    why = (payload.get("q01") or {}).get("why") or {}
    text = (why.get("cause_text") or "").strip()
    if why.get("sourced"):
        if not text:
            notes.append("cause marked sourced but the text is empty")
        if not why.get("cause_url"):
            notes.append("cause marked sourced but carries no document URL")
    else:
        if text != NO_CAUSE_TEXT:
            notes.append(f"unsourced cause must be the fixed sentence "
                         f"verbatim, got {text[:90]!r}")

    # 3 — the delivery disclaimer, on the card's face.
    if (payload.get("q01") or {}).get("delivery_note") != DELIVERY_DISCLAIMER:
        notes.append("the delivery-% disclaimer is missing or altered")

    # 4 — no action-shaped level names.
    for name in ((payload.get("q02") or {}).get("levels") or {}):
        if banned_hits(name):
            notes.append(f"level type {name!r} implies an action")

    status = "failed" if notes else "passed"
    if notes:
        log.error(f"{payload.get('symbol')}: card FAILED validation — "
                  + "; ".join(notes[:4]))
    return status, ("; ".join(notes) if notes else None)
