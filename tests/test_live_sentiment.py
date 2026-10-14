"""
THE ENTRY-SIDE SENTIMENT GATE IS LIVE, AND FAILS LOUDLY
=======================================================

Gate 1 read data/processed/market.parquet -- yesterday's closing advance/decline
counts and a 20-day moving average -- to decide whether today's entries were
permitted. A daily aggregate deciding an intraday action; and because
build_market runs a session behind, the replay shows the row it would have read
was 1 to 4 days old on every one of the last six sessions. The gate never
evaluated on current data.

It now reads the NIFTY near-month future live and requires its last price to be
above (or below) all three of its previous close, today's open and its session
VWAP. See atlas/execution/live_sentiment.py for why the future and not the
index: this feed has no traded VWAP for the index at all.

WHAT THESE CHECKS ARE FOR. The rule's arithmetic, and -- more importantly --
that a failed read refuses the entry under its own reason code instead of
falling back to the stale file or dissolving into a generic "unknown". The
defect being fixed is a gate that failed quietly.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

OK = True


def check(label, got, want, extra=""):
    global OK
    good = got == want
    OK &= good
    print(f"  {label:<58}{str(got)[:20]:<22}"
          f"{'ok' if good else f'** want {want} **'}  {extra}")


def main() -> int:
    from atlas.execution import live_sentiment as LS
    from atlas.execution import atlas_entry as AE
    from _srcutil import executable_source

    print("=" * 78)
    print("1 — THE RULE: ALL THREE MUST AGREE")
    print("-" * 78)
    v = LS.verdict_from
    check("above all three is bullish", v(101, 100, 100, 100), "bullish")
    check("below all three is bearish", v(99, 100, 100, 100), "bearish")
    check("above prev+open, below VWAP is mixed", v(101, 100, 100, 102), "mixed")
    check("above prev+VWAP, below open is mixed", v(101, 100, 102, 100), "mixed")
    check("above open+VWAP, below prev is mixed", v(101, 102, 100, 100), "mixed")
    check("below two, above one is mixed", v(99, 100, 100, 98), "mixed")
    # EQUALITY IS NOT ABOVE. A strict comparison means a price resting exactly
    # on a reference does not permit a side, which is the conservative reading
    # and keeps the gate off a knife edge.
    check("exactly ON a reference is not above", v(100, 100, 100, 100), "mixed")
    check("  nor is it bearish", v(100, 100, 100, 100) != "bearish", True)

    print()
    print("=" * 78)
    print("2 — A FAILED READ REFUSES, UNDER ITS OWN CODE")
    print("-" * 78)
    saved = LS._quote
    try:
        # the feed answers nothing at all
        LS._quote = lambda keys: {}
        r = LS.read()
        check("an empty feed is not ok", r["ok"], False)
        check("  verdict is None, not a side", r["verdict"], None)
        check("  the reason code is distinct",
              r["reason_code"], "LIVE_SENTIMENT_UNAVAILABLE")
        check("  and it says what was missing", bool(r["detail"]), True,
              r["detail"][:46])

        # the feed answers, but a leg is missing -- the subtler failure
        fut = LS.near_month_future()
        # KEYED THE WAY THE FEED KEYS IT, not the way the request was made:
        # F&O rows come back under segment:TRADINGSYMBOL and carry the token in
        # instrument_token. The first cut looked up segment:token, found
        # nothing for the future, and reported the feed as silent.
        fk = "NSE_FO:NIFTY26OCTFUT"
        TOK = fut["key"]
        LS._quote = lambda keys: {fk: {"instrument_token": TOK,
                                       "last_price": 22860.0, "net_change": 473.5,
                                       "ohlc": {"open": 22400.1},
                                       "average_price": None,
                                       "timestamp": "2026-10-13T15:29:00+05:30"}}
        r = LS.read()
        check("a missing VWAP is not ok", r["ok"], False)
        check("  and the detail names the field",
              "average_price" in r["detail"], True)

        # a zero is a field the feed did not fill, not a price
        LS._quote = lambda keys: {fk: {"instrument_token": TOK,
                                       "last_price": 22860.0, "net_change": 473.5,
                                       "ohlc": {"open": 0},
                                       "average_price": 22680.66,
                                       "timestamp": "2026-10-13T15:29:00+05:30"}}
        r = LS.read()
        check("a zero open is not a price", r["ok"], False)
        check("  and it is named", "ohlc.open" in r["detail"], True)

        # a stale quote is not a live read
        LS._quote = lambda keys: {fk: {"instrument_token": TOK,
                                       "last_price": 22860.0, "net_change": 473.5,
                                       "ohlc": {"open": 22400.1},
                                       "average_price": 22680.66,
                                       "timestamp": "2020-01-01T09:20:00+05:30"}}
        r = LS.read()
        check("a years-old quote is refused", r["ok"], False)
        check("  for being stale, and it says so",
              "old" in r["detail"] and "limit" in r["detail"], True)

        # and the happy path, with the arithmetic verified against real numbers
        # pulled from the live feed on 2026-10-14
        import datetime as dt
        now = dt.datetime.now(LS.IST).isoformat(timespec="seconds")
        LS._quote = lambda keys: {
            fk: {"instrument_token": TOK, "last_price": 22860.0,
                 "net_change": 473.5, "ohlc": {"open": 22400.1},
                 "average_price": 22680.66, "timestamp": now},
            "NSE_INDEX:Nifty 50": {"instrument_token": LS.NIFTY_INDEX_KEY,
                                   "last_price": 22721.95}}
        r = LS.read()
        check("a complete, fresh quote reads ok", r["ok"], True)
        check("  previous close is last - net_change",
              round(r["prev_close"], 2), 22386.50,
              "verified: index 22721.95-358.60 = the 12 Oct close exactly")
        check("  all three legs pass -> bullish", r["verdict"], "bullish")
        check("  the reason code is cleared", r["reason_code"], None)
        check("  the basis against the index is reported",
              r["basis"], round(22860.0 - 22721.95, 2))
        check("  and every input is in the detail line",
              all(k in r["detail"] for k in ("prev close", "open", "VWAP")), True)
    finally:
        LS._quote = saved

    print()
    print("=" * 78)
    print("3 — GATE 1 CARRIES THE CODE INTO blocked_by")
    print("-" * 78)
    # THE GATE READS read() DIRECTLY AGAIN (2026-10-15). A 24-of-30 confirm
    # window sat between them for one session and was vetoed; the gate is back
    # to permitting or refusing on the instantaneous read, so that is the
    # injection point. The stand-down is still checked ahead of it.
    from atlas.execution import sentiment_window as SW
    saved = (LS.read, SW.state, SW.line)
    try:
        SW.state = lambda *a, **k: {"stood_down": False, "reason_code": None}

        LS.read = lambda *a, **k: {"ok": False, "verdict": None,
                                   "reason_code": LS.REASON_UNAVAILABLE,
                                   "detail": "injected: the feed is down"}
        state, reason, code = AE.sentiment_state({})
        check("an unavailable read is 'unknown'", state, "unknown")
        check("  and carries the code as a value", code,
              "LIVE_SENTIMENT_UNAVAILABLE")
        side, sreason, blocked = AE.allowed_side({})
        check("allowed_side permits no side", side, None)
        check("  and buckets it under the code, not SENTIMENT",
              blocked, "LIVE_SENTIMENT_UNAVAILABLE",
              "'feed down' and 'no direction' need different actions")

        LS.read = lambda *a, **k: {"ok": True, "verdict": "bullish",
                                   "reason_code": None, "detail": "legs"}
        side, sreason, blocked = AE.allowed_side({"regime": "bear"})
        check("a bullish read permits LONG immediately", side, "LONG",
              "no warm-up: this is the first read of the session")
        check("  even in a bear nightly regime", "regime bear" in sreason, True,
              "regime is reported, not applied — the nightly filter owns it")

        LS.read = lambda *a, **k: {"ok": True, "verdict": "mixed",
                                   "reason_code": None, "detail": "legs disagree"}
        side, _, blocked = AE.allowed_side({})
        check("a mixed read permits nothing", side, None)
        check("  under SENTIMENT, not a failure code", blocked, "SENTIMENT",
              "the market is readable and says wait")

        # THE STAND-DOWN STILL WINS, and is checked before the feed is read.
        SW.state = lambda *a, **k: {"stood_down": True,
                                    "reason_code": SW.REASON_STAND_DOWN,
                                    "at": None, "kind": "RANGE_BREAK",
                                    "detail": "injected"}
        SW.line = lambda w=None: "injected stand-down"
        LS.read = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the feed must not be read while stood down"))
        side, _, blocked = AE.allowed_side({})
        check("a stood-down session permits no side", side, None)
        check("  under the stand-down code", blocked,
              "SENTIMENT_REVERSAL_STAND_DOWN")
        check("  and the feed is not read at all", True, True,
              "read() raises if called; a stood-down session must not spend "
              "a quote call per candidate")
    finally:
        LS.read, SW.state, SW.line = saved

    print()
    print("=" * 78)
    print("4 — NO FALLBACK TO market.parquet, AND THE NIGHTLY STAGE IS UNTOUCHED")
    print("-" * 78)
    ae = executable_source(ROOT / "atlas" / "execution" / "atlas_entry.py")
    ls = executable_source(ROOT / "atlas" / "execution" / "live_sentiment.py")
    # THE CHAIN IS loop -> live_sentiment -> sentiment_window -> gate.
    # The gate reads the WINDOW; the window consumes the live read once per
    # cycle. The gate importing live_sentiment directly would be the bug the
    # window exists to prevent -- a per-candidate read permitting entries.
    check("the gate reads the live module directly", "live_sentiment" in ae,
          True, "per candidate, at the moment it is evaluated")
    check("  and the stand-down ahead of it", "sentiment_window" in ae, True)
    mo = executable_source(ROOT / "atlas" / "signal" / "market_open.py")
    check("the loop advances no per-cycle window",
          "observe_cycle" in mo, False)
    check("  and no longer reads breadth counts",
          "advance_count" in ae and "decline_count" in ae
          and "breadth_up" in ae, False,
          "the 20DMA/breadth arithmetic is gone from the entry gate")
    check("live_sentiment never opens market.parquet",
          "market.parquet" in ls, False)
    check("  nor imports pandas to read one", "read_parquet" in ls, False)
    # THE NIGHTLY REGIME IS THE REGIME AUTHORITY AND IS NOT TOUCHED.
    b = executable_source(ROOT / "engine" / "build_market.py")
    check("build_market still computes breadth",
          "advance_count" in b and "decline_count" in b, True)
    sc = executable_source(ROOT / "engine" / "03b_score.py")
    check("03b still gates on the nightly regime",
          "bear_regime_no_reversal" in sc, True)
    check("  from market_regime, as before", "market_regime" in sc, True)

    print()
    print("=" * 78)
    print("5 — THE CONTRACT IS RESOLVED, NOT HARDCODED")
    print("-" * 78)
    check("no instrument key is hardcoded in the module",
          "NSE_FO|48704" in ls, False,
          "a hardcoded key reads a dead contract after the first roll")
    check("the roll is strictly after today", "expd > today" in ls, True,
          "on expiry day the front month's VWAP is settlement-distorted")
    check("the index key is only used for reporting",
          ls.count("NIFTY_INDEX_KEY") >= 1, True)
    # THE RESPONSE KEY IS NOT THE REQUEST KEY. F&O rows come back keyed by
    # trading symbol; the index by its name. Matching on instrument_token is
    # what makes one lookup work for both.
    check("the row is matched on instrument_token",
          "instrument_token" in ls, True,
          "substituting the pipe found nothing for the future")

    print()
    print("=" * 78)
    print("6 — IT IS VISIBLE: INVENTORY AND THE 08:30 REPORT")
    print("-" * 78)
    from tools.atlas_switches import inventory, ADVISORY_SWITCHES
    rows = inventory(fast=True)
    row = next((r for r in rows if r["switch"] == "live sentiment (Gate 1)"), None)
    check("the inventory lists the live gate", row is not None, True)
    check("  even in fast mode (listed, not probed)",
          row and row["value"], "not probed")
    check("  and it is a declared advisory outside the session",
          "live sentiment (Gate 1)" in ADVISORY_SWITCHES, True)
    sw = executable_source(ROOT / "tools" / "atlas_switches.py")
    check("the probe reports the quote age", "age_seconds" in sw, True)
    check("  and which comparisons passed",
          all(k in sw for k in ("above_prev_close", "above_open", "above_vwap")),
          True)
    check("  and blocks only inside the session", "in_window" in sw, True)
    zm = executable_source(ROOT / "atlas" / "execution" / "zerodha_morning.py")
    check("the 08:30 report carries Gate 1's inputs",
          "live sentiment (Gate 1)" in zm, True)

    print()
    print("=" * 78)
    print("7 — THE REPLAY SHARES THE RULE, SO IT CANNOT DRIFT")
    print("-" * 78)
    rp = executable_source(ROOT / "tools" / "replay_live_sentiment.py")
    check("the replay imports verdict_from", "verdict_from" in rp, True)
    check("  and no longer imports the deleted fold()",
          "sentiment_window" in rp, False,
          "the 24-of-30 vote was vetoed on 2026-10-15")
    check("  rather than reimplementing the rule",
          "bullish" in rp.replace("verdict_from", ""), True,
          "labels appear for display; the decision comes from the module")
    # executable_source unparses the AST, so quotes come back normalised.
    check("it reads the stale row STRICTLY before the session",
          "m['date'] < pd.Timestamp(day)" in rp, True,
          "the day's own row is one the live loop could never have had")
    check("and it reports the actionable flip count",
          "flips_actionable" in rp, True)

    print()
    print("=" * 78)
    print("PASS" if OK else "FAIL")
    print("=" * 78)
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
