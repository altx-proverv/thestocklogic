"""
THE STOCK LOGIC — Market flash
==============================
One two-to-three sentence desk note per session, written at the end of the EOD
chain and stored in `market_flash` for signals.html to serve.

    python3 -m engine.market_flash              # generate and store
    python3 -m engine.market_flash --dry-run    # print both paths, store nothing

WHAT THIS IS NOT. It is not an explanation of how ATLAS works -- that is what it
replaces. It describes the day's conditions in the voice of a desk note, and it
never advises.

TWO PATHS, BOTH PERMANENT
-------------------------
  llm       Haiku writes it from a block of pre-formatted figures, and it is
            stored only if it passes validation.
  template  A deterministic line from the same figures. This is what runs when
            the API is unreachable, the key is absent, or validation rejects the
            generated text. It is infrastructure, not a placeholder -- the page
            must always have a flash and must never show yesterday's prose.

WHY A VALIDATOR AND NOT JUST A PROMPT
-------------------------------------
Two rules matter: never invent a number, and never recommend. A prompt cannot
enforce either -- it can only ask. So the generated text is checked before it is
stored and discarded if it fails:

  numbers   every numeric token in the output must appear in the input set. One
            unaccounted figure and the text is dropped. This is what turns "we
            asked it not to" into "it cannot ship if it did".
  advice    a deny-list over the lowercased text. Deliberately over-broad: a
            false rejection costs a template line, a false accept is a
            compliance problem.
  shape     at most 3 sentences, at most 60 words, single line, no markdown.

The inputs are stored with the text so any published figure can be checked
against the snapshot that produced it months later.

MODEL
-----
Haiku 4.5. The task is assembling a sentence from numbers that are already
computed and already rounded -- there is no reasoning in it, so a larger model
buys nothing measurable here and costs five times more.

INPUTS
------
Everything comes from tables the chain has already written. A field that cannot
be read is OMITTED from the prompt entirely rather than passed as null or zero: a
null in a prompt is an invitation to invent, and a zero is a lie.
"""

import os
import sys
import re
import argparse
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from atlas.config import SUPABASE_URL, SUPABASE_KEY          # noqa: E402

log = logging.getLogger("FLASH")
IST = timezone(timedelta(hours=5, minutes=30))

MODEL = "claude-haiku-4-5-20251001"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_TOKENS = 200
TIMEOUT = 30

# Over-broad on purpose. Anything that could read as telling the reader what to
# do, what to expect, or what is worth doing. A false positive costs the template
# line; a false negative is published advice.
ADVICE_MARKERS = (
    "should", "consider", "recommend", "advise", "advice", "suggest",
    "opportunity", "opportunities", "we expect", "expect to", "likely to",
    "look to", "look for", "watch for", "keep an eye", "worth watching",
    "worth a look", "set up for", "poised", "primed", "due for",
    "buy ", "sell ", "go long", "go short", "take profit", "book profit",
    "entry here", "target of", "stop at", "risk-reward", "risk/reward",
    "favour", "favor", "prefer", "attractive", "cheap", "expensive",
    "oversold bounce", "due a bounce", "tomorrow will", "next session will",
)

SYSTEM = """You write a two-to-three sentence market flash for an Indian equity \
screener, in the voice of a trading desk note at the close. Describe conditions. \
Never advise.

RULES
1. Use ONLY the numbers in the DATA block. Never calculate a new figure, never \
round differently, never add a number that is not there. If something is absent \
from DATA, do not mention it.
2. No recommendation, in any form. Not "consider", "watch for", "look to", \
"bias towards" addressed to the reader, no risk/reward framing, no expectation \
about tomorrow. Do not tell the reader what to do or what will happen.
3. Describing what ATLAS is configured to do is allowed and is not advice: \
"ATLAS is watching 158 setups and following breadth" is a fact about the engine. \
"Short bias is warranted" is advice. Keep to the first.
4. Lead with breadth or the index, then the sector spread, then ATLAS's state. \
Two or three sentences, 45 words or fewer.
5. Plain declaratives. No hedging ("appears to", "seems"), no hype ("plunged", \
"collapsed"), no questions, no exclamation marks, no emoji, no headline. Name \
sectors as they appear in DATA.
6. Output the flash text only. No preamble, no quotes around it, no markdown."""


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


# ══════════════════════════════════════════════════════════════════
# INPUTS
# ══════════════════════════════════════════════════════════════════

def _get(path: str):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=_headers(),
                     timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"{path} -> HTTP {r.status_code} {r.text[:160]}")
    return r.json() or []


def gather() -> dict:
    """Every figure the flash may quote, pre-rounded to how it must appear.

    PRE-ROUNDED DELIBERATELY. The model is given strings that are already in
    their final form, so there is no arithmetic for it to do and no second
    rounding to disagree with the validator. "28.7" is both the prompt input and
    the only acceptable output token.

    A field that cannot be read is absent from the dict, not null. The prompt is
    built from what is present, so an unreadable input makes the flash shorter
    rather than wrong.
    """
    out = {}

    sectors = _get("sector_heatmap?order=signal_date.desc,rank.asc&limit=40")
    if not sectors:
        raise RuntimeError("sector_heatmap is empty — the chain has not run")
    day = sectors[0]["signal_date"]
    rows = [s for s in sectors if s["signal_date"] == day]
    out["session_date"] = day

    adv = sum(int(s.get("advancing") or 0) for s in rows)
    dec = sum(int(s.get("declining") or 0) for s in rows)
    if adv + dec:
        out["advancing"] = str(adv)
        out["declining"] = str(dec)
        out["stocks"] = str(adv + dec)
        out["breadth_pct"] = f"{adv / (adv + dec) * 100:.1f}"

    regime = str(rows[0].get("market_direction") or "").lower()
    if regime:
        out["regime"] = regime

    ranked = [s for s in rows if s.get("ret_1d") is not None]
    if ranked:
        ranked.sort(key=lambda s: float(s["ret_1d"]), reverse=True)
        out["best"] = [(s["sector"], f"{float(s['ret_1d']):+.2f}")
                       for s in ranked[:3]]
        out["worst"] = [(s["sector"], f"{float(s['ret_1d']):+.2f}")
                        for s in reversed(ranked[-3:])]
        out["sectors_total"] = str(len(ranked))
        out["sectors_green"] = str(sum(1 for s in ranked
                                       if float(s["ret_1d"]) > 0))

    # Watchlist and actionable counts, from the batch just published.
    try:
        pub = _get(f"signals?signal_date=eq.{day}"
                   f"&select=publication_kind&limit=2000")
        if pub:
            out["watching"] = str(len(pub))
            out["actionable"] = str(sum(
                1 for p in pub
                if str(p.get("publication_kind", "signal")) == "signal"))
    except Exception as e:
        log.warning(f"watchlist counts unavailable, omitting: {e}")

    try:
        today = datetime.now(IST).date().isoformat()
        ent = _get(f"atlas_entry_log?run_date=eq.{today}"
                   f"&status=eq.ENTERED&select=symbol")
        out["entries_today"] = str(len(ent))
    except Exception as e:
        log.warning(f"entry count unavailable, omitting: {e}")

    try:
        mm = _get("market_marks?order=mark_date.desc&limit=1")
        # ONLY IF IT IS TODAY'S. market_marks is written irregularly -- it had a
        # six-day gap in September -- and a stale index move presented as today's
        # is exactly the kind of invented fact the validator cannot catch,
        # because the number IS in the inputs. So the staleness check happens
        # here, where the date is visible.
        if mm and str(mm[0].get("mark_date")) == str(day)[:10] \
                and mm[0].get("nifty_move_pct") is not None:
            out["nifty_move_pct"] = f"{float(mm[0]['nifty_move_pct']):+.2f}"
            out["nifty_close"] = f"{float(mm[0]['nifty_close']):,.0f}"
        else:
            log.info("no Nifty mark for this session — omitting the index")
    except Exception as e:
        log.warning(f"nifty mark unavailable, omitting: {e}")

    return out


# ══════════════════════════════════════════════════════════════════
# THE PROMPT, AND THE SET OF LEGAL NUMBERS
# ══════════════════════════════════════════════════════════════════

def build_prompt(d: dict) -> str:
    lines = [f"DATA for {str(d['session_date'])[:10]}"]
    if "regime" in d:
        lines.append(f"regime: {d['regime']}")
    if "breadth_pct" in d:
        lines.append(f"breadth: {d['breadth_pct']}% advancing "
                     f"({d['advancing']} advancing, {d['declining']} declining, "
                     f"{d['stocks']} stocks)")
    if "nifty_move_pct" in d:
        lines.append(f"nifty: {d['nifty_move_pct']}% at {d['nifty_close']}")
    if "sectors_green" in d:
        lines.append(f"sectors green: {d['sectors_green']} of {d['sectors_total']}")
    if "best" in d:
        lines.append("best sectors: " +
                     ", ".join(f"{s} {v}%" for s, v in d["best"]))
        lines.append("worst sectors: " +
                     ", ".join(f"{s} {v}%" for s, v in d["worst"]))
    if "watching" in d:
        lines.append(f"atlas watching: {d['watching']} setups")
        lines.append(f"atlas actionable: {d['actionable']} of those")
    if "entries_today" in d:
        lines.append(f"atlas entries today: {d['entries_today']}")
    return "\n".join(lines)


_NUM = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?")


def _norm(tok: str) -> str:
    """A number as a comparable string: no commas, no sign, no trailing zeros."""
    t = tok.replace(",", "").lstrip("+-")
    if "." in t:
        t = t.rstrip("0").rstrip(".")
    return t or "0"


def legal_numbers(d: dict) -> set:
    """Every numeric token the flash is allowed to contain.

    Built from the same dict the prompt is built from, so the two cannot drift.
    Derived forms are admitted ONLY where they are the same quantity written
    differently -- an integer percentage of a one-decimal one, for instance --
    because a desk note saying "29%" for 28.7 is rounding, not invention.
    """
    legal = set()
    for v in d.values():
        if isinstance(v, str):
            for m in _NUM.finditer(v):
                legal.add(_norm(m.group()))
        elif isinstance(v, list):
            for _, num in v:
                legal.add(_norm(num))
    # rounding of a one-decimal figure to a whole number, both directions
    for t in list(legal):
        try:
            f = float(t)
        except ValueError:
            continue
        legal.add(_norm(f"{round(f):d}"))
        legal.add(_norm(f"{f:.1f}"))
    return legal


# ══════════════════════════════════════════════════════════════════
# VALIDATION
# ══════════════════════════════════════════════════════════════════

def validate(text: str, d: dict) -> tuple:
    """(ok, reason). Reason names the rule that failed, for the stored row."""
    t = (text or "").strip()
    if not t:
        return False, "empty"
    if "\n" in t:
        return False, "multi-line"
    if any(c in t for c in "*_#`>|"):
        return False, "markdown"
    if "?" in t or "!" in t:
        return False, "question or exclamation"

    words = len(t.split())
    if words > 60:
        return False, f"too long ({words} words)"
    sentences = [x for x in re.split(r"(?<=[.])\s+", t) if x.strip()]
    if len(sentences) > 3:
        return False, f"too many sentences ({len(sentences)})"

    low = t.lower()
    for m in ADVICE_MARKERS:
        if m in low:
            return False, f"advisory language: {m.strip()!r}"

    legal = legal_numbers(d)
    for mt in _NUM.finditer(t):
        tok = _norm(mt.group())
        if tok not in legal:
            return False, f"number not in inputs: {mt.group()!r}"
    return True, ""


# ══════════════════════════════════════════════════════════════════
# THE TWO PATHS
# ══════════════════════════════════════════════════════════════════

def template(d: dict) -> str:
    """Deterministic, from the same inputs. Permanent, not a placeholder.

    Written to pass its own validator -- it quotes only input figures and makes no
    recommendation -- so the fallback cannot be the thing that fails validation.
    """
    parts = []
    if "breadth_pct" in d:
        parts.append(f"Breadth is {d['breadth_pct']}% advancing, "
                     f"{d['advancing']} up against {d['declining']} down.")
    elif "regime" in d:
        parts.append(f"The regime reads {d['regime']}.")
    if "worst" in d and "sectors_green" in d:
        w = d["worst"][0]
        tail_names = [s for s, _ in d["worst"][1:3]]
        rest = (" and ".join(tail_names) if len(tail_names) == 2
                else (tail_names[0] if tail_names else ""))
        parts.append(f"{w[0]} is weakest at {w[1]}%"
                     + (f", with {rest} next" if rest else "")
                     + f"; {d['sectors_green']} of {d['sectors_total']} "
                     f"sectors are green.")
    if "watching" in d:
        tail = (f" and has entered {d['entries_today']} today"
                if "entries_today" in d else "")
        parts.append(f"ATLAS is watching {d['watching']} setups, "
                     f"{d['actionable']} actionable,{tail}.".replace(",.", "."))
    return " ".join(parts) or "Conditions unavailable for this session."


def generate(d: dict) -> tuple:
    """(text, source, model, reject_reason). Never raises."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        log.info("ANTHROPIC_API_KEY not set — using the template")
        return template(d), "template", None, None
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": key, "anthropic-version": API_VERSION,
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": MAX_TOKENS,
                  "system": SYSTEM,
                  "messages": [{"role": "user", "content": build_prompt(d)}]},
            timeout=TIMEOUT)
        if r.status_code != 200:
            log.warning(f"flash API HTTP {r.status_code} {r.text[:160]} "
                        f"— using the template")
            return template(d), "template", None, None
        body = r.json()
        text = "".join(b.get("text", "") for b in body.get("content", [])).strip()
    except Exception as e:
        log.warning(f"flash API {type(e).__name__}: {e} — using the template")
        return template(d), "template", None, None

    ok, why = validate(text, d)
    if not ok:
        # LOUD. A validator that silently falls back cannot be tuned, and a run
        # of rejections is information about the prompt.
        log.warning(f"generated flash REJECTED ({why}): {text[:160]!r} "
                    f"— using the template")
        return template(d), "template", None, why
    return text, "llm", MODEL, None


# ══════════════════════════════════════════════════════════════════
# STORE
# ══════════════════════════════════════════════════════════════════

def store(d: dict, text: str, source: str, model, reject_reason) -> bool:
    rec = {"session_date": str(d["session_date"]), "flash_text": text,
           "source": source, "model": model,
           "inputs": {k: v for k, v in d.items()},
           "reject_reason": reject_reason,
           "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/market_flash?on_conflict=session_date",
            headers={**_headers(),
                     "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=rec, timeout=20)
        if r.status_code not in (200, 201, 204):
            log.error(f"flash not stored: HTTP {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.error(f"flash not stored: {type(e).__name__}: {e}")
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [FLASH] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompt and both paths; store nothing")
    a = ap.parse_args()

    try:
        d = gather()
    except Exception as e:
        log.error(f"inputs unavailable: {e}")
        return 1

    if a.dry_run:
        print("─── PROMPT ───")
        print(build_prompt(d))
        print("\n─── TEMPLATE ───")
        tpl = template(d)
        print(tpl)
        ok, why = validate(tpl, d)
        print(f"   template passes its own validator: {ok}"
              f"{'' if ok else '  ** ' + why + ' **'}")
        print("\n─── LEGAL NUMBERS ───")
        print(" ".join(sorted(legal_numbers(d), key=lambda x: (len(x), x))))
        text, source, model, why = generate(d)
        print(f"\n─── {source.upper()} ───")
        print(text)
        if why:
            print(f"   rejected because: {why}")
        return 0

    text, source, model, why = generate(d)
    log.info(f"[{source}] {text}")
    if not store(d, text, source, model, why):
        return 1
    log.info(f"stored for {str(d['session_date'])[:10]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
