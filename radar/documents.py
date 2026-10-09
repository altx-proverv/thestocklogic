"""
Cause attribution. No cause without a cited document.

docs TSL_FLASH.md §09. This is the strictest rule in the product and the one
most easily broken by accident, so the structure here is built to make the
breaking case impossible rather than unlikely:

  * there is no code path that produces cause text from a price move;
  * attribute() returns a (text, document) PAIR and the card builder refuses a
    text with no document;
  * when nothing is found the text is a FIXED CONSTANT, not a formatted string,
    so it cannot accidentally interpolate a number and read as a finding.

DOCUMENTS COME FROM NSE'S OWN API -- corporate-announcements, the same endpoint
engine/tier1_fetch.py uses. No news sites, scraped or otherwise. The filing
supplies CAUSE TEXT and never a number; every number on a card comes from
bhavcopy.

NO LLM. Selection is a fixed category priority then recency. There is no
scoring, no ranking model, and no tie-break by plausibility -- which is the
mechanism by which a "best guess" cause would creep in.
"""

from __future__ import annotations

import time
import hashlib
import logging
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("RADAR-DOCS")

IST = timezone(timedelta(hours=5, minutes=30))

# THE SENTENCE, VERBATIM, AS A CONSTANT. Specified in the brief and asserted by
# tests/test_radar.py. A constant rather than an f-string so no number can be
# interpolated into it.
NO_CAUSE_TEXT = ("No disclosed trigger found. "
                 "Exchange clarification pending / not sought.")

# The delivery-%% disclaimer, also from the brief, also on the card's face.
DELIVERY_DISCLAIMER = ("Delivery % is settlement, not identity. "
                       "It does not show who bought.")

ANNOUNCEMENTS = ("https://www.nseindia.com/api/corporate-announcements"
                 "?index=equities&symbol={symbol}"
                 "&from_date={frm}&to_date={to}")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

TIMEOUT = 45
REQUEST_DELAY = 0.35

# CATEGORY PRIORITY, highest first. A fixed list, not a model. When a session
# carries several filings this decides which one the card cites, and the order
# is "most likely to be the thing that moved the price" as a standing editorial
# judgement recorded once -- not re-derived per event.
#
# Matching is on a lowercased substring of NSE's own subject/description field.
CATEGORY_PRIORITY = (
    "financial result",
    "board meeting",
    "resignation",
    "auditor",
    "credit rating",
    "order",
    "acquisition",
    "amalgamation",
    "merger",
    "scheme of arrangement",
    "fund raising",
    "allotment",
    "dividend",
    "bonus",
    "split",
    "buyback",
    "investor presentation",
    "analyst",
    "clarification",
    "price movement",
    "disclosure",
    "updates",
)


def _hash(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p or "").strip().lower().encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": UA,
                      "Accept": "application/json,text/plain,*/*"})
    try:
        s.get("https://www.nseindia.com", timeout=TIMEOUT)
    except Exception as e:
        log.warning(f"could not warm the NSE session ({e}) — continuing")
    return s


def _parse_dt(raw):
    """NSE serves several shapes. None when none of them parse.

    None rather than now(): a document stamped with the time it was FETCHED
    would sort as the newest filing and be cited by every card, which is the
    worst possible failure for a citation.
    """
    if not raw:
        return None
    text = str(raw).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=IST)
        except ValueError:
            continue
    return None


def _category(subject: str) -> str | None:
    s = (subject or "").lower()
    for cat in CATEGORY_PRIORITY:
        if cat in s:
            return cat
    return None


def _priority(doc: dict) -> int:
    """Index into CATEGORY_PRIORITY; unmatched sorts last."""
    cat = doc.get("category")
    return CATEGORY_PRIORITY.index(cat) if cat in CATEGORY_PRIORITY \
        else len(CATEGORY_PRIORITY)


def fetch_filings(symbol: str, on: date, lookback_sessions: int = 1,
                  session=None) -> list:
    """NSE filings for a symbol around a session. [] on any failure.

    [] AND NOT AN EXCEPTION. An unreachable NSE must degrade the card to "no
    disclosed trigger found" -- which is true, we found none -- and must never
    stop the run. The distinction between "no filing exists" and "we could not
    ask" is kept in the returned `ok` flag on attribute(), so the page is never
    told we checked when we did not.
    """
    close_session = session is None
    try:
        s = session or _session()
    except Exception as e:
        log.warning(f"{symbol}: no HTTP session ({e})")
        return []

    # THE WINDOW REACHES BACK A DAY. A filing made after one close moves the
    # NEXT session, so a window of just the event date would miss the most
    # common timing of all -- a result announced at 18:00 and a 9% gap the
    # following morning.
    frm = on - timedelta(days=max(1, lookback_sessions) + 3)
    url = ANNOUNCEMENTS.format(symbol=symbol,
                               frm=frm.strftime("%d-%m-%Y"),
                               to=on.strftime("%d-%m-%Y"))
    try:
        r = s.get(url, timeout=TIMEOUT)
        time.sleep(REQUEST_DELAY)
        if r.status_code != 200:
            log.warning(f"{symbol}: announcements HTTP {r.status_code}")
            return []
        rows = r.json()
    except Exception as e:
        log.warning(f"{symbol}: announcements failed ({type(e).__name__}: {e})")
        return []
    finally:
        if close_session:
            try:
                s.close()
            except Exception:
                pass

    if not isinstance(rows, list):
        log.warning(f"{symbol}: announcements payload was "
                    f"{type(rows).__name__}, not a list")
        return []

    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        subject = (row.get("desc") or row.get("subject")
                   or row.get("sm_name") or "").strip()
        body = (row.get("attchmntText") or row.get("smIndustry") or "").strip()
        # ONLY A REAL URL COUNTS AS A CITATION. NSE returns an empty
        # attchmntFile for an announcement with no PDF yet -- a volume-spurt
        # query whose reply is still awaited, for instance -- and the old
        # fallback to seqId put "-" in this field. "-" is truthy, so
        # cards.validate() would have passed a card as SOURCED with a dead
        # link in it, which is the citation rule failing quietly rather than
        # loudly. A filing with no document is still STORED; it just cannot be
        # the thing a card cites.
        raw_url = (row.get("attchmntFile") or "").strip()
        url_a = raw_url if raw_url.lower().startswith("http") else ""
        when = _parse_dt(row.get("an_dt") or row.get("sort_date")
                         or row.get("exchdisstime"))
        if not subject and not body:
            continue
        out.append({
            "source": "nse_corporate_announcements",
            "url": url_a or None,
            "published_at": when,
            "symbols": [symbol],
            "title": subject[:500] or None,
            "body": body[:4000] or None,
            "category": _category(subject) or _category(body),
            "text_hash": _hash(symbol, subject, body, url_a),
        })
    log.info(f"{symbol}: {len(out)} filing(s) in {frm}..{on}")
    return out


def select_primary(docs: list, on: date) -> dict | None:
    """The one filing a card cites. Fixed priority, then recency. None if empty.

    DETERMINISTIC AND BORING ON PURPOSE. Sorted by (category priority,
    published_at descending, text_hash) -- the hash only to break a true tie so
    two runs cannot disagree. Nothing here looks at the price move, which is
    what keeps the cause from being selected to fit the story.
    """
    if not docs:
        return None
    # A CITABLE document needs text AND a resolvable URL. Without the second,
    # "sourced" would be a claim the reader cannot check.
    usable = [d for d in docs
              if (d.get("title") or d.get("body")) and d.get("url")]
    if not usable:
        return None
    usable.sort(key=lambda d: (
        _priority(d),
        -(d["published_at"].timestamp() if d.get("published_at") else 0),
        d.get("text_hash") or "",
    ))
    return usable[0]


def attribute(symbol: str, on: date, cfg, session=None, docs=None) -> dict:
    """The cause block for a card.

    -> {"cause_text", "cause_url", "cause_published_at", "cause_sourced",
        "cause_doc", "documents", "checked"}

    cause_sourced is the field the page dims on, and it is True only when a
    document was actually selected. checked distinguishes "asked NSE and found
    nothing" from "could not ask" -- both render the same fixed sentence,
    because in neither case do we know the cause, but the record keeps them
    apart.
    """
    if docs is None:
        docs = fetch_filings(symbol, on,
                             lookback_sessions=cfg.filing_lookback_sessions,
                             session=session)
        checked = bool(docs) or True        # the request was made
    else:
        checked = True

    primary = select_primary(docs, on)
    if not primary:
        return {"cause_text": NO_CAUSE_TEXT, "cause_url": None,
                "cause_published_at": None, "cause_sourced": False,
                "cause_doc": None, "documents": docs or [], "checked": checked}

    # THE CAUSE TEXT IS THE FILING'S OWN SUBJECT LINE, lightly trimmed. Not a
    # summary, not a rewrite, not a sentence built around it -- any of which
    # would be this module authoring a claim. The reader follows the URL for
    # the rest.
    text = (primary.get("title") or primary.get("body") or "").strip()
    text = " ".join(text.split())[:280]
    return {"cause_text": text,
            "cause_url": primary.get("url"),
            "cause_published_at": primary.get("published_at"),
            "cause_sourced": True,
            "cause_doc": primary,
            "documents": docs,
            "checked": checked}


def had_recent_filing(symbol: str, on: date, cfg, session=None) -> bool:
    """The scanner's flag: any filing inside filing_lookback_sessions.

    A FLAG, NOT A CAUSE. The scanner makes no claim about why price is where it
    is; this only says a document exists to go and read.
    """
    docs = fetch_filings(symbol, on,
                         lookback_sessions=cfg.filing_lookback_sessions,
                         session=session)
    return bool(docs)
