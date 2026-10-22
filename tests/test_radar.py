#!/usr/bin/env python3
"""
TSL FLASH — RADAR R1
====================
What this file guards, and why each one is a test rather than a comment:

  1. DISPLAY IS NOT MEASUREMENT. A displaced event keeps getting outcome rows.
     The brief asks for this test by name, and it is the one the base-rate
     library depends on: a displacement model that stopped measuring would
     starve §08 while looking like it worked.
  2. THE HOT 10 IS EXACTLY TEN, and the eleventh detection displaces the
     lowest heat -- including the case where the newcomer displaces itself.
  3. NO ACTION LANGUAGE anywhere -- in the card payload, in the page, or in
     radar/'s own output strings. Enforced over the files, not by review.
  4. NO CAUSE WITHOUT A CITED DOCUMENT, and the fixed sentence verbatim when
     there is none.
  5. THE LEVELS ARE PURE and fail closed: a short history yields fewer rows,
     never a mislabelled proxy.
  6. CONFIG IS COMPLETE and matches the migration's seed.
  7. RADAR TOUCHES NOTHING IT MUST NOT -- no atlas_* table, no atlas/ import,
     no signals_* write.

Offline: no network, no database. Detection runs against the real parquets on
disk, which is the point -- these are the same files the nightly run reads.

    python3 tests/test_radar.py
"""

import os
import re
import sys
import json
import logging
from pathlib import Path
from datetime import date, timedelta

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.CRITICAL)

from radar import bars, levels, detect, heat, cards, outcomes, documents  # noqa: E402
from radar.config import load as load_config, DEFAULTS                   # noqa: E402

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<62}{str(got):<10}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def section(t):
    print(f"\n{'=' * 78}\n{t}\n{'-' * 78}")


CFG = load_config(use_table=False)


def _event(symbol, ed, sev, status="live"):
    """A synthetic event row shaped like one radar_events holds."""
    return {"id": abs(hash((symbol, ed))) % 10_000_000, "symbol": symbol,
            "event_date": ed, "sector": "TEST", "trigger_type": "shock_1d",
            "move_pct": -10.0, "vol_multiple": sev * 3.0, "direction": "down",
            "severity": sev, "heat": sev, "status": status,
            "move_threshold": 8.0, "vol_threshold": 3.0}


def main() -> int:
    section("1 — THE HOT 10 IS EXACTLY TEN, AND THE ELEVENTH DISPLACES")
    as_of = date(2026, 10, 7)
    # Eleven events, all on the same session, so decay is identical and the
    # ordering is severity alone.
    ev = [_event(f"SYM{i:02d}", as_of, float(11 - i)) for i in range(11)]
    r = heat.rank_hot(ev, as_of, CFG)
    check("eleven detections -> live set size", len(r["live"]), 10)
    check("  and one displaced", len(r["displaced"]), 1)
    check("  the displaced one is the LOWEST heat",
          r["displaced"][0]["symbol"], "SYM10",
          "severity 1.0, the minimum of the eleven")
    check("  the live set is sorted by heat, descending",
          [x["symbol"] for x in r["live"]] ==
          sorted([x["symbol"] for x in r["live"]],
                 key=lambda s: -next(e["severity"] for e in ev if e["symbol"] == s)),
          True)

    # A WEAK NEWCOMER DISPLACES ITSELF. docs/TSL_FLASH.md §06 rule 1.
    strong = [_event(f"HOT{i:02d}", as_of, 50.0 - i) for i in range(10)]
    weak = _event("WEAK", as_of, 0.4)
    r2 = heat.rank_hot(strong + [weak], as_of, CFG)
    check("a newcomer below the tenth displaces ITSELF",
          [x["symbol"] for x in r2["displaced"]], ["WEAK"],
          "recorded and measured, never shown")
    check("  and the live set is untouched", len(r2["live"]), 10)

    # DISPLACEMENT IS TERMINAL FOR DISPLAY. §06 rule 2.
    old = _event("OLDBIG", as_of, 99.0, status="displaced")
    r3 = heat.rank_hot([old] + strong[:3], as_of, CFG)
    check("an already-displaced event does NOT re-enter on heat alone",
          [x["symbol"] for x in r3["live"]],
          ["HOT00", "HOT01", "HOT02"],
          "severity 99 would otherwise rank first; re-entry would make the "
          "list churn on arithmetic rather than on news")
    check("  it stays in the displaced set",
          "OLDBIG" in [x["symbol"] for x in r3["displaced"]], True)

    section("2 — DISPLAY IS NOT MEASUREMENT (the brief asks for this by name)")
    # A REAL EVENT, REAL BHAVCOPY, DISPLACED, AND STILL MEASURED.
    # POLICYBZR fell 36% on 2026-09-24 and by 2026-10-07 its heat had decayed
    # below ten fresher detections -- the replay shows it displaced at rank 10
    # and then out. It must still carry forward returns.
    ed = date(2026, 9, 24)
    pol = detect.detect_symbol("POLICYBZR", ed, CFG)
    check("POLICYBZR detected on 2026-09-24", bool(pol), True,
          f"{pol['trigger_type']} {pol['move_pct']}% on "
          f"{pol['vol_multiple']}x" if pol else "no trigger fired")

    # Force it into the displaced set with ten hotter events, then measure.
    hotter = [_event(f"HOT{i:02d}", as_of, 50.0 - i) for i in range(10)]
    pol_row = dict(pol)
    pol_row.update({"id": 999, "severity": heat.severity(pol), "status": "live",
                    "sector": "FINANCE"})
    ranked = heat.rank_hot(hotter + [pol_row], as_of, CFG)
    pol_after = next(x for x in ranked["displaced"] if x["symbol"] == "POLICYBZR")
    check("  it is displaced by ten hotter cards",
          pol_after["status"], "displaced")

    # THE SWEEP TAKES EVERY OPEN EVENT AND FILTERS NOTHING BY STATUS.
    measured = outcomes.sweep(ranked["live"] + ranked["displaced"], CFG,
                              as_of=as_of)
    pol_oc = next((m for m in measured if m["symbol"] == "POLICYBZR"), None)
    check("  a DISPLACED event still produces an outcome record",
          bool(pol_oc), True)
    if pol_oc:
        oc = pol_oc["outcome"]
        check("    with a realised 5-session return",
              "ret_5d" in oc, True, f"ret_5d={oc.get('ret_5d')}%")
        check("    and a max drawdown",
              "max_drawdown_pct" in oc, True,
              f"max_drawdown_pct={oc.get('max_drawdown_pct')}%")
        check("    measured from the EVENT close, not the pre-event close",
              oc["base_close"], 1207.2,
              "1886.30 was the pre-event close; measuring from it would put "
              "the -36% shock inside the recovery number")
        check("    and the window is still open at 8 sessions",
              oc["window_complete"], False,
              f"{oc['sessions_elapsed']} of "
              f"{CFG.outcome_window_trading_days} sessions")

    # AND THE SWEEP MUST NOT BE FILTERABLE BY STATUS. If someone later passes
    # only the live set, this is the test that notices.
    src = (ROOT / "radar" / "outcomes.py").read_text()
    check("  outcomes.py never branches on status",
          "status" in src and re.search(r'status.*==.*["\']live', src) is not None,
          False,
          "a sweep that could tell live from displaced would eventually be "
          "optimised to skip the ones nobody is looking at")
    run_src = (ROOT / "radar" / "run_detection.py").read_text()
    check("  the runner sweeps open_now, not ranked['live']",
          "outcomes.sweep(open_now" in run_src, True)

    section("3 — NO ACTION LANGUAGE, ANYWHERE")
    # The card payload, built from real data.
    df = bars.frame_through("POLICYBZR", ed)
    lv = levels.compute_all(df, CFG, bars.session_vwap("POLICYBZR", ed))
    cause_none = {"cause_text": documents.NO_CAUSE_TEXT, "cause_url": None,
                  "cause_published_at": None, "cause_sourced": False,
                  "checked": True, "documents": []}
    payload = cards.build({**pol_row, "close": 1207.2, "delivery_pct": 59.32,
                           "rank": 1},
                          lv, cause_none, pol_oc["outcome"] if pol_oc else None,
                          8, as_of, CFG)
    status, notes = cards.validate(payload)
    check("a real card payload passes validation", status, "passed", str(notes))

    # THE PAGE'S VISIBLE COPY. Scanned as text with the markup, scripts and
    # comments stripped, because a banned word inside a CSS class name or a
    # code comment is not something a reader sees -- and flagging those would
    # make the check so noisy it would get switched off.
    page = (ROOT / "flash.html").read_text()
    visible = re.sub(r"<script.*?</script>", " ", page, flags=re.S)
    visible = re.sub(r"<style.*?</style>", " ", visible, flags=re.S)
    visible = re.sub(r"<!--.*?-->", " ", visible, flags=re.S)
    visible = re.sub(r"<[^>]+>", " ", visible)
    # WHITESPACE COLLAPSED. Rendered copy wraps wherever the source wraps, so a
    # substring test against the raw text fails on a sentence that is on screen
    # and merely split across two lines of HTML -- which is what happened the
    # first time this ran.
    visible_flat = " ".join(visible.split())
    hits = cards.banned_hits(visible)
    check("flash.html's visible copy carries no action language",
          sorted(set(hits)), [], f"scanned {len(visible)} chars")

    # The engine's own user-facing strings. Module docstrings and comments are
    # excluded for the same reason, and because this very file has to be able
    # to name the words it bans.
    for mod in ("cards.py", "detect.py", "levels.py", "documents.py",
                "heat.py", "outcomes.py"):
        text = (ROOT / "radar" / mod).read_text()
        strings = re.findall(r'"([^"\\\n]{12,})"', text) \
            + re.findall(r"'([^'\\\n]{12,})'", text)
        bad = []
        for s in strings:
            if s.strip().startswith(("http", "radar_", "data/", "NSE_", "%")):
                continue
            h = cards.banned_hits(s)
            if h:
                bad.append((s[:50], sorted(set(h))))
        # cards.py legitimately contains the banned list itself, as literals.
        expected = mod == "cards.py"
        check(f"radar/{mod} string literals are clean",
              bool(bad) and not expected, False,
              str(bad[:2]) if bad and not expected else
              ("the banned list itself" if expected and bad else ""))

    section("4 — NO CAUSE WITHOUT A CITED DOCUMENT")
    check("the no-cause sentence is the brief's wording, verbatim",
          documents.NO_CAUSE_TEXT,
          "No disclosed trigger found. Exchange clarification pending / not sought.")
    check("  it is a constant, not an f-string",
          "NO_CAUSE_TEXT = (" in (ROOT / "radar" / "documents.py").read_text(),
          True, "nothing can interpolate a number into it")

    bad = json.loads(json.dumps(payload))
    bad["q01"]["why"]["cause_text"] = "Fell on weak results and profit booking"
    check("a story with no document is REFUSED",
          cards.validate(bad)[0], "failed",
          "this is the single rule the product cannot ship without")
    bad2 = json.loads(json.dumps(payload))
    bad2["q01"]["why"] = {"cause_text": "Board approved a scheme of arrangement",
                          "cause_url": None, "sourced": True,
                          "cause_published_at": None}
    check("a sourced cause with no URL is REFUSED",
          cards.validate(bad2)[0], "failed")
    good = json.loads(json.dumps(payload))
    good["q01"]["why"] = {"cause_text": "Buyback of equity shares — outcome",
                          "cause_url": "https://nsearchives.nseindia.com/x.pdf",
                          "sourced": True, "cause_published_at": None}
    check("a cited filing quoting 'buyback' is ALLOWED",
          cards.validate(good)[0], "passed",
          "the exchange's words, not ours; refusing them would make the most "
          "price-relevant filing category uncitable")

    check("the delivery disclaimer is on the card's face",
          payload["q01"]["delivery_note"], documents.DELIVERY_DISCLAIMER)
    bad3 = json.loads(json.dumps(payload))
    bad3["q01"]["delivery_note"] = "High delivery points to strong hands"
    check("  altering it REFUSES the card", cards.validate(bad3)[0], "failed",
          "never infer a buyer from delivery %")
    check("  and it appears in the page's own visible copy too",
          documents.DELIVERY_DISCLAIMER.split(".")[0] in visible_flat, True,
          "two layers: the payload carries it and validation enforces that; "
          "the page carries it so a malformed payload cannot drop it silently")
    # THE TWO COPIES MUST BE IDENTICAL. A page-side constant that drifted from
    # radar/documents.py would show one sentence and validate another.
    m = re.search(r"var DELIVERY_NOTE='([^']+)'", page)
    check("  the page constant matches radar/documents.py exactly",
          m.group(1) if m else None, documents.DELIVERY_DISCLAIMER)

    section("5 — THE THREE QUESTIONS ARE THE STRUCTURE")
    check("01 wording", payload["q01"]["question"], "What happened, and why?")
    check("02 wording", payload["q02"]["question"],
          "Will it recover, or fall further?")
    check("03 wording", payload["q03"]["question"],
          "How would my capital behave?")
    check("03 is a DECLARED gap, not an empty box",
          bool(payload["q03"].get("note")) and payload["q03"]["available"] is False,
          True)
    for q in ("01", "02", "03"):
        check(f"  the page renders the number {q}",
              f'q-num">{q}<' in page, True)
    # THE HEADER CARRIES RANK/HEAT/SESSIONS, NOT A DAY COUNTER.
    check("the card header has sessions_since, not a day counter",
          "sessions_since" in payload and "day" not in str(payload.get("rank")),
          True)
    check("  and the page labels it 'Sessions since'",
          "Sessions since" in page, True)
    check("  with no 'Day N' label anywhere in the page",
          bool(re.search(r"\bDay\s*\d", page)), False)

    section("6 — LEVELS ARE PURE AND FAIL CLOSED")
    check("POLICYBZR ipo_price is dropped",
          any(r["level_type"] == "ipo_price" for r in lv), False,
          f"series starts {df['date'].iloc[0]}, the download floor is "
          f"{bars.DOWNLOAD_FLOOR}")
    check("  w52_low was set on the event day",
          next(r["price"] for r in lv if r["level_type"] == "w52_low"), 1207.2)
    check("  event_vwap came from bhavcopy, not a (H+L+C)/3 proxy",
          next(r["price"] for r in lv if r["level_type"] == "event_vwap"),
          1283.32, "AVG_PRICE in the legacy layout is NSE's own figure")
    check("  three swing highs and three swing lows",
          (sum(1 for r in lv if r["level_type"].startswith("swing_high")),
           sum(1 for r in lv if r["level_type"].startswith("swing_low"))),
          (3, 3))
    check("  rsi_14 is present and in range",
          0 <= next(r["price"] for r in lv if r["level_type"] == "rsi_14") <= 100,
          True,
          f"rsi={next(r['price'] for r in lv if r['level_type'] == 'rsi_14')}")

    # A SHORT HISTORY YIELDS FEWER ROWS, NOT A MISLABELLED PROXY.
    short = df.iloc[-60:].reset_index(drop=True)
    lv_short = levels.compute_all(short, CFG, None)
    types = {r["level_type"] for r in lv_short}
    check("60 sessions of history -> no dma_200", "dma_200" in types, False,
          "a 200-DMA from 60 sessions is not a 200-DMA")
    check("  but dma_50 is still computed", "dma_50" in types, True)
    check("  and no level carries a None price",
          any(r["price"] is None for r in lv_short), False)

    # CORPORATE ACTIONS DROP A LEVEL RATHER THAN SCALING IT. INDIAGLYCO's first
    # generated card said "52-week high, +218.53% from here" -- a pre-split
    # 1219.00 against a post-split close of 382.70. Found while populating the
    # page for real, which is the only reason it was found: no synthetic
    # fixture had a split in it.
    #
    # REPOINTED 2026-10-22, BECAUSE THIS SECTION ENCODED AN ACCIDENT. It
    # asserted the ex-date was 2026-09-17 and that "bhavcopy's PREV_CLOSE was
    # adjusted to 266.20 against a prior close of 1111.70". Both were wrong:
    #
    #   the ex-date is 2026-09-02      close 1111.70 -> open 225.00
    #   PREV_CLOSE was never adjusted  it read 1111.70 on the ex-date, the same
    #                                  as the prior close, and 266.20 on 17 Sep
    #                                  is simply 16 Sep's true close
    #
    # The 1111.70-vs-266.20 comparison only existed because eleven sessions
    # (02-16 Sep, while the stock sat in the BE series) were missing from the
    # parquet until INGEST_SERIES accepted BE. The old detector was reading a
    # hole in the record as an adjustment, and filling the hole silently
    # un-detected the only split it had ever caught -- which is what put a
    # pre-split high_52w back into daily_closes for 31 symbols.
    #
    # The measurement across the universe: 31 split/bonus events in the last
    # 252 sessions, NSE adjusted prev_close on 0 of them. So the assertion is
    # now on the real ex-date and the real mechanism -- the gap in the OPENING
    # print, which trading cannot produce because the price band would have
    # stopped it.
    ig = bars.frame_through("INDIAGLYCO", date(2026, 10, 7))
    acts = bars.corporate_actions(ig)
    check("INDIAGLYCO's split IS detected", bool(acts), True,
          "the whole point: the old detector returned [] on complete data")
    # THE DATE IS DERIVED FROM THE FRAME, NOT HARDCODED. data/processed is
    # gitignored, so the archive differs between the box (which has
    # INDIAGLYCO's 11 BE-series sessions) and a development machine (which may
    # not). Hardcoding 2026-09-02 asserts the archive's completeness, not the
    # detector's correctness -- and the previous version of this test hardcoded
    # a date that was an artifact of exactly that incompleteness. So: the
    # detected date must be the session whose OPEN gapped beyond the band.
    _c = ig["close"].astype(float).to_numpy()
    _o = ig["open"].astype(float).to_numpy()
    _band = bars.CORP_ACTION_GAP_PCT / 100.0
    gapped = [ig["date"].iloc[i] for i in range(1, len(ig))
              if _c[i - 1] and _o[i]
              and abs(_o[i] / _c[i - 1] - 1.0) > _band
              and abs(_c[i] / _c[i - 1] - 1.0) > _band]
    check("  and dated on the session whose OPEN gapped", sorted(acts),
          sorted(gapped), "mechanism, not a remembered date")
    # Strict where the data supports strictness: the real ex-date is 2026-09-02
    # and 17 Sep is an ordinary session. Only assertable when the archive
    # actually holds the sessions between them.
    _have = set(ig["date"])
    if date(2026, 9, 2) in _have and date(2026, 9, 16) in _have:
        check("  the real ex-date is 2026-09-02", date(2026, 9, 2) in acts, True,
              "close 1111.70 -> open 225.00")
        check("  and 2026-09-17 is NOT an action", date(2026, 9, 17) in acts,
              False, "its prev_close 266.20 IS 16 Sep's close")
    else:
        print("  (archive lacks the 02-16 Sep BE sessions on this machine — "
              "the exact-date assertions are skipped; the mechanism one above "
              "is not)")
    check("  the feed-continuity test is a separate, named thing",
          callable(getattr(bars, "feed_discontinuities", None)), True)
    # AND IT IS THE TEST THAT PROVES THE RENAME WAS RIGHT. Where the archive
    # holds the 02-16 Sep BE sessions the record is whole and the continuity
    # test is silent; where it does not, the test FIRES on the hole. Same
    # function, two archives, and in both cases it is reporting a gap in our
    # record rather than a corporate action -- which is the job its old name
    # hid.
    _fd = bars.feed_discontinuities(ig)
    if date(2026, 9, 2) in _have and date(2026, 9, 16) in _have:
        check("  the continuity test is silent on a whole record", _fd, [],
              "nothing missing between 01 Sep and 17 Sep")
    else:
        check("  the continuity test FIRES on the 11-session hole",
              bool(_fd), True,
              f"{[str(d) for d in _fd]} — a missing session, not a split")
    ig_types = {r["level_type"] for r in levels.compute_all(ig, CFG, None)}
    for dropped in ("w52_high", "w52_low", "dma_50", "dma_200", "rsi_14"):
        check(f"  {dropped} is dropped across the split",
              dropped in ig_types, False)
    for kept in ("event_high", "event_low", "pre_event_close"):
        check(f"  {kept} survives (single-session, unaffected)",
              kept in ig_types, True)
    # PER-LOOKBACK, not all-or-nothing: V2RETAIL's action is 2026-03-25 (the old
    # detector said 03-26, one session late, because it was reading the stale
    # prev_close on T+1 rather than the gap on T).
    v2 = bars.frame_through("V2RETAIL", date(2026, 10, 7))
    check("V2RETAIL's action is dated on the ex-date, not the day after",
          (date(2026, 3, 25) in bars.corporate_actions(v2),
           date(2026, 3, 26) in bars.corporate_actions(v2)), (True, False))
    v2_types = {r["level_type"] for r in levels.compute_all(v2, CFG, None)}
    check("a stock can lose its 200-DMA and keep its 50",
          ("dma_200" in v2_types, "dma_50" in v2_types), (False, True),
          "V2RETAIL, action 2026-03-25")
    check("  and the scanner will not call a split a new 52-week low",
          levels.is_new_52w_extreme(ig, "low"), False)
    # POLICYBZR IS THE CRASH FIXTURE, and the one that proves the detector
    # separates a corporate action from a bad day. It fell 36.0% on 2026-09-24
    # -- further than several real splits -- but it OPENED at -10.0% and fell
    # during the session, so the gap is not in the first print and the basis
    # never changed. Its levels must all survive.
    pb = bars.frame_through("POLICYBZR", date(2026, 10, 7))
    check("POLICYBZR's -36% crash is NOT a corporate action",
          bars.corporate_actions(pb), [],
          "opened -10.0%, closed -36.0%: an intraday fall, not a new basis")
    check("  so its 252-session window stays comparable",
          bars.action_in_last(pb, 252), False)
    check("a clean series keeps every level",
          len([r for r in lv if r["level_type"] in
               ("w52_high", "w52_low", "dma_50", "dma_200")]), 4,
          "POLICYBZR: no corporate action in the window")

    # NO ACTION-SHAPED LEVEL NAMES, now or later.
    all_types = {r["level_type"] for r in lv}
    check("no level type implies an action",
          [t for t in all_types if cards.banned_hits(t)], [],
          sorted(all_types))

    # The distance sign has to mean something.
    w52h = next(r for r in lv if r["level_type"] == "w52_high")
    check("a level above the price reads POSITIVE",
          w52h["dist_pct"] > 0, True,
          f"52w high {w52h['price']} is {w52h['dist_pct']:+}% from 1207.20")

    section("7 — DETECTION AND THE SCANNER")
    check("the 1-day volume average EXCLUDES the event day",
          round(float(detect.detect_symbol("POLICYBZR", ed, CFG)["vol_multiple"]), 2),
          20.41,
          "including it reads 10.44x -- a 20x day lifts its own mean by ~95%")
    # THE TRIGGER ORDER IS LOAD-BEARING: circuit is the most specific.
    check("circuit is checked before the shock triggers",
          detect.TRIGGER_ORDER[0].__name__, "check_circuit")
    check("  a session with high != low is never a circuit",
          detect.check_circuit(df, CFG), None,
          "POLICYBZR ranged 1697.70-1207.20; a band touch is not derivable "
          "from EOD data and is not guessed at")
    # The scanner's extreme test must exclude today, or it returns everything.
    check("the 52-week test excludes the session being scored",
          levels.is_new_52w_extreme(df.iloc[:-1], "low"), False,
          "POLICYBZR's low was set ON 09-24; the prior window must not "
          "contain it, or x >= max(..., x) is true for every row")
    check("  and is True on the session itself",
          levels.is_new_52w_extreme(df, "low"), True)
    # SESSIONS, NOT CALENDAR DAYS.
    check("Friday -> Monday is ONE session",
          bars.sessions_between(date(2026, 10, 2), date(2026, 10, 5)), 1,
          "2026-10-02 was Gandhi Jayanti; calendar days would say three")
    check("  and heat decays on sessions",
          heat.decay(8.0, bars.sessions_between(date(2026, 10, 2),
                                                date(2026, 10, 5)), 5.0),
          round(8.0 * 0.5 ** (1 / 5), 4))

    section("8 — CONFIG MATCHES THE MIGRATION'S SEED")
    mig = (ROOT / "migrations" / "20261010_radar_r1.sql").read_text()
    seeded = set(re.findall(r"^\s*\('([a-z_0-9]+)',\s*'", mig, re.M))
    check("every default is seeded by the migration",
          sorted(set(DEFAULTS) - seeded), [])
    check("every seeded key is one the code reads",
          sorted(seeded - set(DEFAULTS)), [])
    check("  19 keys in total", len(DEFAULTS), 19,
          "12 from the brief, 7 added and marked as such")
    for k in ("hot_cards_n", "heat_half_life_sessions", "shock_1d_pct",
              "shock_5d_pct", "outcome_window_trading_days"):
        check(f"  {k}", CFG[k], DEFAULTS[k])
    # A BAD VALUE KEEPS THE DEFAULT; it never becomes zero, which would turn a
    # threshold into "everything qualifies".
    from radar.config import _coerce
    check("an unparseable threshold keeps the default",
          _coerce("shock_1d_pct", "eight"), 8.0)
    check("  and an unparseable count does too",
          _coerce("hot_cards_n", ""), 10)

    section("9 — THE VIEW'S JSON PATHS EXIST IN THE REAL PAYLOAD")
    # THIS IS THE DRIFT THIS REPO HAS HAD FOUR TIMES: a page or a view naming
    # something the writer does not produce. The first draft of v_radar_hot10
    # read payload->>'cause_text', which exists nowhere in the payload -- every
    # Hot 10 row would have rendered a null cause and the page would have shown
    # ten "no disclosed trigger found" lines on a night when half had a filing.
    # So: parse the view's JSON paths out of the migration and walk each one
    # through a payload that was actually built.
    mig_sql_raw = (ROOT / "migrations" / "20261010_radar_r1.sql").read_text()
    # COMMENTS STRIPPED FIRST. The comment above that view EXPLAINS the bug by
    # quoting the wrong path, so a check that reads comments finds the defect it
    # is meant to prevent and fails forever. This project has made that mistake
    # eight times in other checks; executable_source() in tests/_srcutil.py
    # exists for the same reason.
    mig_sql = re.sub(r"--[^\n]*", "", mig_sql_raw)
    paths = []
    for m in re.finditer(r"payload((?:\s*->>?\s*'[a-z_0-9]+')+)", mig_sql):
        keys = re.findall(r"'([a-z_0-9]+)'", m.group(1))
        if keys:
            paths.append(keys)
    check("the migration names at least one payload path", bool(paths), True,
          f"{len(paths)} path(s): "
          + ", ".join(".".join(k) for k in paths))
    missing = []
    for keys in paths:
        node = payload
        for k in keys:
            if isinstance(node, dict) and k in node:
                node = node[k]
            else:
                missing.append(".".join(keys))
                break
    check("every payload path the views read EXISTS in a built payload",
          missing, [],
          "walked against the POLICYBZR payload built above")

    # And the page must only read columns the views actually expose.
    hot_cols = set()
    mv = re.search(r"CREATE VIEW public\.v_radar_hot10.*?;", mig_sql, re.S)
    if mv:
        for m in re.finditer(r"AS\s+([a-z_0-9]+)\s*[,\n]", mv.group(0)):
            hot_cols.add(m.group(1))
        for m in re.finditer(r"^\s+e\.([a-z_0-9]+),", mv.group(0), re.M):
            hot_cols.add(m.group(1))
    # Members of the fetch Response, not of a view row. sbGet() names its
    # response `r` too, so without this the check reports r.ok and r.json as
    # missing columns -- a false positive that would get the check switched off.
    RESPONSE_MEMBERS = {"ok", "json", "text", "status", "rows", "headers"}
    page_reads = set(re.findall(r"\br\.([a-z_0-9]+)\b", page)) - RESPONSE_MEMBERS
    # Columns the scanner view supplies, read in the same renderer namespace.
    scanner_cols = {"trade_date", "symbol", "side", "sector", "close",
                    "extreme_price", "pct_from_extreme", "vol_multiple",
                    "volume", "delivery_pct", "filing_recent", "promoted",
                    "rank_in_side"}
    unknown = sorted(page_reads - hot_cols - scanner_cols - {"payload"})
    check("every column flash.html reads is exposed by a view",
          unknown, [],
          f"hot10 exposes {len(hot_cols)} column(s)")

    section("10 — RADAR TOUCHES NOTHING IT MUST NOT")
    radar_src = {p.name: p.read_text() for p in (ROOT / "radar").glob("*.py")}
    all_src = "\n".join(radar_src.values())
    check("no radar module writes an atlas_* table",
          bool(re.search(r"atlas_[a-z_]+", all_src)), False)
    check("no radar module writes a signals_* table",
          bool(re.search(r"signals_[a-z_]+", all_src)), False)
    # The ONE allowed atlas import is the Telegram sender in the runner's
    # alert path, which is shared infrastructure and writes nothing.
    atlas_imports = re.findall(r"from atlas[.\w]* import (\w+)", all_src)
    check("the only atlas import is the Telegram sender",
          sorted(set(atlas_imports)), ["send"],
          "shared alerting; it reads and writes no ATLAS state")
    check("nothing references the halt file", "halt" in all_src.lower(), False)
    check("every table named is radar_*",
          sorted({t for t in re.findall(r"rest/v1/(\w+)", all_src)}
                 - {"radar_events", "radar_documents", "radar_card_versions",
                    "radar_levels", "radar_scanner_daily", "radar_config"}),
          [])
    # And the page must read the VIEWS, not the base tables.
    page_tables = set(re.findall(r"rest/v1/(\w+)", page))
    check("flash.html reads only the three narrow views",
          sorted(page_tables),
          ["v_radar_card_latest", "v_radar_hot10", "v_radar_scanner_today"])

    section("11 — THE PAGE'S COLOUR CONTRACT")
    # SIGNAL GREEN IS RESERVED. It may be declared as a token but must not be
    # used as a value anywhere a datum is rendered.
    green_uses = [m for m in re.findall(r"[^-\w](#00FF88|#00ff88)", page)]
    check("#00FF88 appears only as a token declaration",
          len(green_uses), 1,
          "reserved for the logo and primary CTAs")
    check("  declared as --signal-green", "--signal-green:#00FF88" in page, True)
    check("  and never referenced by a rule",
          "var(--signal-green)" in page, False,
          "R1 has no primary CTA on this page; the token exists so the one "
          "legitimate use is not a literal somebody copies into a cell")
    # Red/green must always sit beside a sign.
    check("the move formatter always emits a sign",
          "(n>0?'+':'')" in page, True,
          "so colour is redundant rather than load-bearing")
    check("the page is responsive at phone width",
          page.count("@media(max-width:") >= 4, True,
          f"{page.count('@media(max-width:')} breakpoints")
    check("wide content scrolls inside its own container",
          page.count("scroll-x") >= 3, True)

    print(f"\n{'-' * 78}")
    print("RADAR R1:", "correct" if OK else "*** DEFECTIVE ***")
    print('-' * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
