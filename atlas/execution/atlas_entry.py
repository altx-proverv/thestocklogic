"""
ATLAS Entry Logic -- Accumulation Architecture (Phase: TRAINING)
=================================================================
Agent IDENTIFIES and ENTERS. No SL, no target, no exit orders. Exits manual.

GATE STACK
  0. Structural stop present      -- required for risk-based sizing
  0b. (removed)                   -- the Tier 1 fundamentals veto was dropped
                                     on 2026-10-01. The cache and crons remain,
                                     so verdicts are still recorded and the
                                     counterfactual stays answerable; nothing
                                     blocks on them.
  1. Regime AND sentiment         -- both must hold or ATLAS stays in cash.
                                     REGIME:    close>200DMA and 50DMA>200DMA
                                     SENTIMENT: advances>declines today and
                                                Nifty>20DMA
                                     Blocked side is reported as
                                     SKIPPED_REGIME or SKIPPED_SENTIMENT.
  2. Opening range                -- SHORTS ONLY (intraday directional check)
  3. (removed -- there is no per-day entry count. Size is bounded per trade
      by Rs3,000 risk / Rs1,00,000 notional, and total exposure by live broker
      funds at Gate 6. Nothing caps simultaneous capital deployed.)
  3b. Already holding symbol      -- hard skip. No scale-in: the risk and
                                     notional caps are per-entry, so a second
                                     fill behind the same stop doubles both.
  4. Entry range                  -- inside zone -> MARKET order now.
                                     Outside -> skip. No resting orders.
  5. Sizing                       -- Rs3k risk / Rs1L notional dual cap
  6. Live broker funds            -- kite.margins() less resting GTT notional
  7. Kill switch                  -- operator halt + funds backstop

WHAT CHANGED FROM THE INTRADAY VERSION
--------------------------------------
OPENING-RANGE GATE now applies to SHORTS only. It asks whether Nifty broke its
first 15 minutes -- meaningful for an intraday hedge, noise for a multi-year
hold. Applied to longs it returned WAIT on flat days, which is exactly when
institutions accumulate and retail stops watching.

CAPITAL IS NO LONGER TRACKED. The open-position limit and the deployed-capital
cap are both gone, along with the atlas_trades-derived exposure ledger that fed
them -- it was a second copy of the broker's balance and nothing reconciled the
two. The binding constraints are now one-position-per-symbol
(Gate 3b), and whether the broker actually has the cash, read live at decision
time. See atlas/risk/funds.py.

REGIME now comes from data/processed/market.parquet (bull / sideways / bear,
200 DMA with a 3% neutral band) rather than sector_heatmap's
bullish/bearish/mixed vocabulary. build_market.py writes it nightly in the EOD
chain, ahead of 02b. Falls back to sector_heatmap if the parquet is absent or
STALE; if both sources are stale the regime is 'unknown', which means no trade.

SIDEWAYS NO LONGER QUALIFIES. Accumulation ran in bull AND sideways on the
argument that a quiet market is the setup rather than a reason to stand aside.
Gate 1 now requires bull AND positive same-day sentiment. The regime has been
sideways throughout the live history, so on this rule ATLAS takes zero trades
over that period -- long stretches in cash are what this gate is for, not a
fault in it.

A consequence worth knowing: with REGIME pinned to bull and extreme_bearish
requiring close < 200DMA - 3%, the two cannot hold together, so a hedge SHORT
is unreachable while this gate stands. That follows from applying "both must
hold" to every entry and is left explicit in regime_allows_side rather than
carved out.

The sector_heatmap fallback can no longer permit anything either: it carries a
direction string with no Nifty series and no breadth, so SENTIMENT is
unevaluable from it and unevaluable means no. It is kept so the log can say why
rather than going quiet.
"""
import sys, requests, logging
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from atlas.config import (
    SUPABASE_URL, SUPABASE_KEY, LIVE_TRADING_ENABLED,
    BLOCKING_STATUSES,
    ENFORCE_ENTRY_RANGE, OPENING_RANGE_GATE_APPLIES_TO, FUNDS_SAFETY_BUFFER_PCT,
    ENTRY_PROXIMITY_PCT,
    ALLOW_SHORT_ENTRIES, ALLOW_LONG_ENTRIES, STRICT_BULL_AND_BULLISH,
    DEFAULT_ON_UNKNOWN_REGIME,
)
from atlas.risk.position_sizing import size_for_trade
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.risk import breaker
from atlas.risk.funds import available_funds, FundsUnavailable
from atlas.execution.broker import place_order, get_ltp, order_margin

log = logging.getLogger("ATLAS-ENTRY")
IST = timezone(timedelta(hours=5, minutes=30))

MARKET_FILE = Path(__file__).parent.parent.parent / "data" / "processed" / "market.parquet"
# OPEN_STATUSES now lives in atlas/config.py -- kill_switch reads the same
# tuple. It was defined only here, and kill_switch counted status=eq.OPEN
# alone, so the two disagreed about whether a resting GTT holds capital.

# Refuse a regime older than this. market.parquet is rebuilt nightly by
# build_market.py (EOD chain, ahead of 02b); a gap this wide means that job
# stopped running. Reading iloc[-1] unconditionally meant a months-old regime
# could gate live entries with no signal that anything was wrong. Sized to
# survive a long weekend plus an NSE holiday, and matched to
# market_open.MAX_BATCH_AGE_DAYS so the two staleness guards agree.
MAX_REGIME_AGE_DAYS = 5


def _headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


# ─────────────────────────────────────────────────────────────────────
# MARKET CONTEXT
# ─────────────────────────────────────────────────────────────────────

def _stale(as_of: str, label: str) -> bool:
    """True if `as_of` (YYYY-MM-DD) is older than MAX_REGIME_AGE_DAYS.

    An unparseable date is treated as STALE. This gate is fail-closed by
    design: a regime we cannot date is a regime we cannot trust, and
    regime_allows_side() turns 'unknown' into no-trade.
    """
    if not as_of:
        log.error(f"{label}: no date on regime row -- treating as stale")
        return True
    try:
        age = (datetime.now(IST).date() - date.fromisoformat(str(as_of)[:10])).days
    except Exception as e:
        log.error(f"{label}: unparseable regime date {as_of!r} ({e}) -- treating as stale")
        return True
    if age > MAX_REGIME_AGE_DAYS:
        log.error(f"STALE REGIME -- {label} is {age} days old ({as_of}); "
                  f"max {MAX_REGIME_AGE_DAYS}. Is build_market.py still running?")
        return True
    log.info(f"{label} regime {as_of} -- {age} day(s) old")
    return False


def get_market_context() -> dict:
    """Regime from market.parquet. Falls back to sector_heatmap if the parquet
    is absent OR stale; returns 'unknown' (-> no trade) if both are stale."""
    try:
        import pandas as pd
        if MARKET_FILE.exists():
            d = pd.read_parquet(MARKET_FILE).sort_values("date")
            if len(d):
                last = d.iloc[-1]
                as_of = str(last.get("date", ""))[:10]
                # Do NOT trust iloc[-1] on date alone -- build_market.py is the
                # only writer, and if it stops the last row sits there forever.
                if not _stale(as_of, "market.parquet"):
                    def _num(col):
                        v = last.get(col)
                        try:
                            f = float(v)
                        except (TypeError, ValueError):
                            return None
                        return None if f != f else f          # NaN -> None
                    return {
                        "regime":             str(last.get("market_regime", "unknown")),
                        "allow_accumulation": bool(last.get("allow_accumulation", False)),
                        "extreme_bearish":    bool(last.get("extreme_bearish", False)),
                        # SENTIMENT primitives. Carried as raw facts so the gate
                        # can name which one failed rather than reporting a
                        # precomputed boolean whose reason is already lost.
                        "nifty_close":        _num("nifty_close"),
                        "nifty_20dma":        _num("nifty_20dma"),
                        "advance_count":      _num("advance_count"),
                        "decline_count":      _num("decline_count"),
                        "as_of":              as_of,
                        "source":             "market.parquet",
                    }
    except Exception as e:
        log.warning(f"market.parquet read failed: {e}")

    # Fallback -- sector_heatmap uses bullish/bearish/mixed
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/sector_heatmap?select=market_direction,signal_date"
            f"&order=signal_date.desc&limit=1", headers=_headers(), timeout=10)
        if r.status_code == 200 and r.json():
            row = r.json()[0]
            as_of = row.get("signal_date", "")
            # The fallback needs the same guard: it orders by signal_date desc
            # with no lower bound, so a dead 07_sector_momentum yields an
            # arbitrarily old row that still looks like a live answer.
            if not _stale(as_of, "sector_heatmap"):
                d = (row.get("market_direction") or "unknown").lower()
                mapped = {"bullish": "bull", "bearish": "bear",
                          "mixed": "sideways"}.get(d, "unknown")
                return {
                    "regime":             mapped,
                    "allow_accumulation": mapped in ("bull", "sideways"),
                    # Fallback cannot establish extreme_bearish -- it needs the
                    # 200DMA and VIX. Deny shorts rather than guess.
                    "extreme_bearish":    False,
                    # NOR CAN IT ESTABLISH SENTIMENT. sector_heatmap carries a
                    # direction string and nothing else -- no Nifty series for a
                    # 20DMA, no advance/decline counts. Left as None, which the
                    # gate reports as unevaluable and refuses on. Since the two
                    # conditions are ANDed, this source can no longer permit an
                    # entry at all; it survives so the log can say WHY rather
                    # than going silent when market.parquet is stale.
                    "nifty_close":        None,
                    "nifty_20dma":        None,
                    "advance_count":      None,
                    "decline_count":      None,
                    "as_of":              as_of,
                    "source":             "sector_heatmap (fallback)",
                }
    except Exception as e:
        log.warning(f"regime fallback failed: {e}")

    log.error("No usable regime source -- both market.parquet and sector_heatmap "
              "are absent or stale. Refusing to trade.")
    return {"regime": "unknown", "allow_accumulation": False,
            "extreme_bearish": False, "nifty_close": None, "nifty_20dma": None,
            "advance_count": None, "decline_count": None,
            "as_of": "", "source": "none"}


def sentiment_ok(ctx: dict) -> tuple:
    """
    The SENTIMENT half of the entry gate. -> (ok, reason)

    Now a LIVE read of the NIFTY near-month future against its previous close,
    today's open and today's session VWAP -- see sentiment_state. This used to
    be "advancing > declining AND Nifty above its 20DMA" from market.parquet,
    which was a daily aggregate deciding an intraday action.

    Missing inputs are NOT a pass, unchanged and for the same reason: unknown
    has to mean no, or the gate opens precisely when it knows least.
    """
    state, reason, _code = sentiment_state(ctx)
    return state == "bullish", reason


def sentiment_state(ctx: dict) -> tuple:
    """
    (state, reason) where state is bullish / bearish / mixed / unknown.

    A LIVE READ AS OF 2026-10-14, operator-approved. This read market.parquet
    -- yesterday's closing advance/decline counts and a 20-day moving average
    -- to decide whether today's entries were permitted. A daily aggregate
    deciding an intraday action; and because build_market has been running a
    session behind (the file held 2026-10-09 while the 2026-10-13 session
    traded) the gate never once evaluated on current data.

    THE RULE NOW. At the moment a candidate is evaluated, the NIFTY near-month
    future is read live and its last price compared against three references:

        bullish   above previous close AND above today's open AND above VWAP
        bearish   below all three
        mixed     anything else -- not a direction; ATLAS stays in cash
        unknown   the read failed or was incomplete. Never a direction.

    All three must agree, deliberately: one number crossing zero would flip the
    gate all day.

    WHY THE FUTURE AND NOT THE INDEX, and what the VWAP leg measures, is in
    atlas/execution/live_sentiment.py. The short version: this feed has no
    traded VWAP for the index -- volume is None on the quote and 0 on every one
    of the 375 daily candles -- and the near-month future has a real
    exchange-computed one. All three legs are taken on that one instrument so
    the futures basis cancels instead of sitting inside a single leg.

    THERE IS NO FALLBACK TO market.parquet. A failed read returns "unknown"
    carrying LIVE_SENTIMENT_UNAVAILABLE, and unknown has never been a side. The
    defect being fixed is a gate that failed quietly, so this one says so: the
    reason code is distinct from SENTIMENT, which means "the market is readable
    and permits no side" and calls for waiting rather than investigating.

    ctx IS NO LONGER THE SENTIMENT SOURCE. It is still passed and still carries
    the nightly regime, which allowed_side reports (and does not apply). The
    nightly regime filter in engine/03b_score is untouched and remains the
    regime authority.
    """
    # THE INSTANTANEOUS READ, AT THE MOMENT THE CANDIDATE IS EVALUATED.
    # 2026-10-15, operator decision: back to what 8172f33 shipped.
    #
    # A 24-of-30 rolling vote sat here for one session. It required a side to
    # hold 24 of the last 30 cycles before permitting anything, which also made
    # entries impossible before about 09:50. The 2026-10-14 session settled it:
    # 623 of that day's 2,233 skips were the window's own two codes, and the raw
    # read was never once bullish, so the warm-up blocked nothing the read would
    # not have blocked anyway -- while it would have blocked the first thirty
    # cycles of any day that did open bullish. There is no warm-up and no
    # minimum cycle count now; ATLAS can enter from 09:20.
    #
    # THE STAND-DOWN IS CHECKED FIRST and is the one thing that outlives a
    # single read: it blocks entries for the rest of the session once something
    # fires it. THE MARKET RANGE BREAKER FIRES IT, live as of 2026-10-16 --
    # range_breaker.market_check() calls SD.stand_down() when a one-minute
    # close breaks the held side of the opening range. This comment said
    # "NOTHING FIRES IT TODAY" for as long as BREAKER_OBSERVE_ONLY was True.
    #
    # IT IS EXEMPT FROM "ATLAS MUST NOT SWITCH ITSELF OFF" by Hemal's ruling:
    # the reversal breakers are pre-authorised to pause trading. Everything
    # else that used to stand the day down now only alerts.
    from atlas.execution import sentiment_window as SW
    sd = SW.state()
    if sd["stood_down"]:
        return "unknown", SW.line(sd), sd["reason_code"]

    from atlas.execution import live_sentiment as LS
    s = LS.read()
    if not s["ok"]:
        # THE CODE TRAVELS AS A VALUE, not as a prefix on a sentence. It lands
        # in atlas_entry_log.blocked_by, so a session spent sitting out because
        # the feed was down is countable separately from one spent waiting for
        # a direction.
        return "unknown", s["detail"], s["reason_code"]
    return s["verdict"], s["detail"], None


def allowed_side(ctx: dict) -> tuple:
    """
    (side, reason, blocked_by) -- the ONE direction ATLAS may open now, or None.

    LONGS ARE PERMITTED BY SENTIMENT ALONE. 2026-10-13, operator decision.

        sentiment   ->  side
        bullish         LONG
        bearish         cash
        mixed           cash
        unknown         cash

    REGIME IS NOT CONSULTED FOR A LONG, and that is the change. It was, and it
    was DOUBLE-COUNTING: the nightly scoring stage already applies a regime
    filter -- bear_regime_no_reversal disqualified 42,187 longs on 7 October --
    so a signal reaching this function has already passed a regime test on the
    full universe with the pipeline's own data.

    AND THE COPY APPLIED HERE IS THE WEAKER ONE. A gate applied a second time
    to a population the nightly stage has already filtered, on data this
    function does not own, cannot add safety -- it can only subtract trades,
    which is what it did: one tradeable state out of twenty.

    A FIGURE THAT USED TO SIT HERE IS GONE. This said the entry-side gate
    "reads 361 symbols of 746, with breadth that has been inverted". Measured
    against the live data on 2026-10-13 it does not reproduce: market.parquet's
    breadth denominator is 705, the contributing-symbol count has been 742-746
    all quarter and 705-706 since, and no session in the whole history has a
    count within 15 of 361. The double-counting argument above stands on its
    own; an unreproducible number does not strengthen it.

    NOT THE SAME AS REMOVING THE REGIME TEST. The nightly filter is untouched
    and is still the regime authority. What is removed is the second, weaker
    application of it at entry time. The regime gate's data source is a
    separate job and is deliberately not touched here either.

    SHORTS ARE STILL DISABLED AND THE PATH IS STILL PRESENT. A bearish reading
    returns cash rather than naming a SHORT, because ALLOW_SHORT_ENTRIES is
    False -- but the short branch below is kept, not deleted, so re-enabling
    is a config change and not a rewrite.

    ONE SIDE AT A TIME, never both. This function returns a single direction,
    so a long and a short cannot be opened on the same evaluation cycle.
    Positions already held are NOT affected by a change here -- see the note in
    enter_trade. This decides only what may be OPENED.
    """
    sent, sreason, scode = sentiment_state(ctx)

    if sent == "unknown":
        # AN UNREADABLE INPUT IS NEVER A SIDE. Unchanged, and the one place
        # this function still fails closed on data quality.
        #
        # THE BUCKET IS THE REASON CODE, not a generic SENTIMENT. "the live
        # feed would not answer" and "the market permits no side" need
        # different actions -- investigate versus wait -- and were previously
        # the same row in any count over blocked_by.
        return (None, f"{scode}: {sreason}", scode or "SENTIMENT")
    if sent == "mixed":
        return None, f"sentiment mixed: {sreason}", "SENTIMENT"

    if sent == "bullish":
        if not ALLOW_LONG_ENTRIES:
            return (None, "long entries are disabled (ALLOW_LONG_ENTRIES)",
                    "CONFIG")
        # REGIME IS REPORTED, NOT APPLIED. Worth carrying in the reason so
        # atlas_entry_log records what the market looked like when the trade
        # was taken -- a later question about whether regime should come back
        # needs that history, and it cannot be reconstructed afterwards.
        reg = str(ctx.get("regime", "unknown")).lower()
        return ("LONG",
                f"bullish sentiment; {sreason} [regime {reg}, reported not "
                f"applied -- the nightly filter owns regime]", None)

    # ── BEARISH ───────────────────────────────────────────────────
    #
    # The side this would name is a SHORT, and shorts are disabled. Kept as a
    # branch rather than collapsed into the cash return above, so the reason
    # the engine sat out is the real one and re-enabling shorts is a config
    # change.
    if not ALLOW_SHORT_ENTRIES:
        return (None, f"bearish sentiment would be a SHORT; ATLAS is "
                      f"long-only ({sreason})", "LONG_ONLY")
    return "SHORT", f"bearish sentiment; {sreason}", None


def regime_allows_side(ctx: dict, direction: str) -> tuple:
    """
    (ok, reason, blocked_by) for ONE direction. Thin wrapper over allowed_side so
    the matrix lives in exactly one place.
    """
    d = (direction or "").upper()
    if d not in ("LONG", "SHORT"):
        return False, f"unknown direction {d}", "CONFIG"

    if d == "SHORT" and not ALLOW_SHORT_ENTRIES:
        # LONG_ONLY, not CONFIG. "CONFIG" named a switch without naming the
        # policy, and it shared its label with the unknown-direction case
        # above. LONG_ONLY is the same label allowed_side uses for a state
        # whose side would have been a short, so a count over
        # atlas_entry_log.blocked_by sums the long-only policy into one bucket
        # however the rejection was reached.
        return (False, "short entries are disabled — ATLAS is long-only "
                       "(ALLOW_SHORT_ENTRIES)", "LONG_ONLY")
    if d == "LONG" and not ALLOW_LONG_ENTRIES:
        return (False, "long entries are disabled (ALLOW_LONG_ENTRIES)", "CONFIG")

    side, reason, blocked = allowed_side(ctx)
    if side is None:
        return False, reason, blocked
    if side != d:
        return (False, f"{reason} -- {side} is the permitted side today, "
                       f"not {d}", "REGIME")
    return True, reason, None


# ─────────────────────────────────────────────────────────────────────
# EXPOSURE -- THERE IS NO LOCAL MEASURE OF IT, BY DESIGN
# ─────────────────────────────────────────────────────────────────────
# Two functions have stood here and both are gone: get_exposure(), which summed
# entry_price*qty to enforce MAX_CAPITAL_DEPLOYED, and get_open_exposure(),
# which did the same for MAX_CONCURRENT_EXPOSURE. Each was a ledger duplicating
# the broker's balance with nothing reconciling the two, and each existed only
# to serve a cap that no longer exists.
#
# Available funds come from kite.margins() at decision time via
# atlas/risk/funds.py, which also nets off resting GTTs. That is the only
# exposure bound: broker funds. The operator keeps capital available and ATLAS
# takes every setup that forms.


def get_today_entry_count() -> int:
    """New LIVE entries recorded today."""
    today = datetime.now(IST).date().isoformat()
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?entry_date=eq.{today}&agent_mode=eq.LIVE&select=id",
            headers=_headers(), timeout=10)
        return len(r.json()) if r.status_code == 200 else 0
    except Exception:
        return 0


def get_open_position(symbol: str) -> tuple:
    """Committed position in `symbol`, if any. -> (readable, position|None).

    OPEN is a filled position; GTT_PENDING is a resting trigger that has not
    filled but has cash spoken for behind it. Both are commitments to the same
    symbol, so both block. CLOSED and CANCELLED do not, and SHADOW rows are
    never either status so a shadow run cannot block itself -- but a LIVE
    holding DOES block a shadow evaluation, which is correct: the shadow log is
    supposed to say what live would have done.

    FAILS CLOSED. `readable` is False when the book cannot be read at all, and
    the caller must block on that. It is the only book read that still gates an
    entry, now that the aggregate caps are gone, and it has to be: an unreadable
    answer here costs a second position stacked behind a stop already sized for
    one, which is one structural stop covering double the intended risk.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?symbol=eq.{symbol}&status=in.({','.join(BLOCKING_STATUSES)})"
            f"&select=id,direction,qty,entry_price,stop_price,entry_date,status"
            f"&order=entry_date.desc&limit=1",
            headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.error(f"open-position check failed for {symbol}: "
                      f"HTTP {r.status_code} {r.text[:120]}")
            return False, None
        rows = r.json()
        return True, (rows[0] if rows else None)
    except Exception as e:
        log.error(f"open-position check failed for {symbol}: "
                  f"{type(e).__name__}: {e}")
        return False, None


def check_entry_range(direction: str, ltp: float, lo_in: float, hi_in: float,
                     stop: float = 0.0, entry_ref: float = 0.0) -> tuple:
    """Enter ONLY if LTP is within ENTRY_PROXIMITY_PCT of entry_ref, inside the
    zone band, AND on the right side of it.

    THE SIDE-OF-PRICE INVARIANT, asserted here as well as in market_open.at_zone
    and engine/zone_entry. Three places is not redundancy by accident: zone_entry
    checks it against the batch CLOSE, at_zone against the LIVE quote when the
    loop selects a candidate, and this against the quote the order will actually
    be priced from. The defect fixed on 30 Sep was a zone on the wrong side of
    price reported as a stop-width problem, and with MAX_ENTRY_DIST_PCT gone
    there is no distance bound left to make a wrong-side zone harmless.

    The mitigation test is the load-bearing half. A demand band price has fallen
    THROUGH is not a discounted setup, it is a failed one, and buying inside the
    band on the way down is a knife-catch that the old 0.30% bound made
    impossible by accident rather than by rule.
    """
    if not ENFORCE_ENTRY_RANGE:
        return True, "range check off"
    if not lo_in or not hi_in or lo_in <= 0 or hi_in <= 0:
        return False, "no entry band defined"
    lo, hi = min(lo_in, hi_in), max(lo_in, hi_in)
    is_long = str(direction).upper() != "SHORT"

    # MITIGATED -- the zone is spent, whichever side of the band LTP now sits.
    # Checked BEFORE the band test, because a price below a demand band's low is
    # both "outside the band" and "the setup is dead", and only the second is
    # worth reading in a log.
    if is_long and ltp < lo:
        return False, (f"demand zone mitigated — LTP Rs{ltp:.1f} below band low "
                       f"Rs{lo:.1f}; the setup failed rather than cheapened")
    if (not is_long) and ltp > hi:
        return False, (f"supply zone mitigated — LTP Rs{ltp:.1f} above band high "
                       f"Rs{hi:.1f}; the setup failed rather than cheapened")

    # The stop must still be outside the zone on the correct side. size_by_risk
    # rejects a stop on the wrong side of ENTRY, but not one that price has
    # already passed, and an entry taken at a price beyond its own stop is a
    # position that is stopped out before it is filled.
    if stop > 0:
        if is_long and ltp <= stop:
            return False, (f"LTP Rs{ltp:.1f} at or below the stop Rs{stop:.1f} "
                           f"— already beyond the structural level")
        if (not is_long) and ltp >= stop:
            return False, (f"LTP Rs{ltp:.1f} at or above the stop Rs{stop:.1f} "
                           f"— already beyond the structural level")

    if not (lo <= ltp <= hi):
        return False, f"LTP Rs{ltp:.1f} not yet at zone Rs{lo:.1f}-Rs{hi:.1f}"

    # ── PROXIMITY: WITHIN 1% OF entry_ref ─────────────────────────
    #
    # THE BAND TEST ABOVE IS NOT A DISTANCE TEST. A band can be wide, and
    # "inside a wide band" is not "at the level" -- a 6% demand band admits a
    # price 5% from the reference the setup was published against and sizing was
    # computed from. This closes that.
    #
    # LAST, so the log reads in order of severity: a mitigated zone is a dead
    # setup, a price outside the band is not there yet, and a price inside the
    # band but beyond 1% is there-ish but not close enough. Three different
    # facts, and the third used to be invisible.
    #
    # SKIPPED, NOT QUEUED. No widening, no retry at a worse price. If the setup
    # comes to the level the next cycle takes it; if it does not, there was no
    # trade. MAX_ZONE_DIST_PCT (15%, engine/active_zones.py) remains the
    # SCREENING filter and is untouched -- it governs what gets published, not
    # what gets bought.
    if entry_ref and entry_ref > 0:
        dist = abs(ltp / entry_ref - 1.0) * 100.0
        # THE BOUNDARY IS INCLUSIVE, and the epsilon is what makes it so in
        # floating point. 1010/1000 - 1 is 0.010000000000000009, so a price
        # exactly 1% away compared greater-than and was SKIPPED -- "within
        # 1.0%" has to include 1.0%, and a gate that rejects its own stated
        # boundary is a gate nobody can reason about.
        if dist > ENTRY_PROXIMITY_PCT + 1e-9:
            return False, (f"LTP Rs{ltp:.1f} is {dist:.2f}% from entry_ref "
                           f"Rs{entry_ref:.1f}, outside the "
                           f"{ENTRY_PROXIMITY_PCT:.1f}% proximity gate — "
                           f"skipped, not queued")
        return True, (f"LTP Rs{ltp:.1f} inside zone Rs{lo:.1f}-Rs{hi:.1f} and "
                      f"{dist:.2f}% from entry_ref Rs{entry_ref:.1f}")

    # NO entry_ref MEANS NO PROXIMITY TEST, AND THAT IS A REFUSAL rather than a
    # pass. Admitting a trade because the reference price is missing would make
    # a data gap the widest gate in the system.
    return False, ("no entry_ref on the signal — cannot measure proximity, "
                   "and a missing reference is not permission")


# ─────────────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────────────

def _settle_unfilled_limit(order_id: str, symbol: str) -> dict:
    """A LIMIT that did not fill in the window: cancel it and find out what happened.

    -> {"outcome": "NO_FILL" | "FILLED" | "UNKNOWN",
        "filled_qty": int, "avg_price": float, "reason": str}

    WHY THIS EXISTS AT ALL. A MARKET order either fills or fails; a LIMIT has a
    third state -- resting, unfilled, still live -- and that state is the
    COMMON one when the book moves away from the last traded price. Treating it
    as INDETERMINATE, which is what await_fill alone would do, would alert the
    operator on an ordinary no-entry and leave a resting order that could fill
    minutes later with nothing watching for it.

    CANCEL FIRST, THEN READ. The order is cancelled before its status is
    trusted, because the alternative order -- read, then cancel -- has a window
    in which the read says "open", the cancel fails, and the order fills
    afterwards while the caller has already concluded there is no position.

    THE RACE IS REAL AND IS HANDLED: a cancel can lose to a fill that was
    already in flight. So the status is re-read AFTER cancelling and a fill is
    believed over the cancel. A partial fill is a position and is reported as
    FILLED with the quantity that actually filled -- never rounded up to the
    order quantity, which would size a stop for shares nobody owns.
    """
    from atlas.execution.broker import cancel_order, get_order_status

    cancelled = cancel_order(order_id)
    status = get_order_status(order_id) or {}
    st = str(status.get("status", "")).upper()
    try:
        filled = int(float(status.get("filled_quantity") or 0))
    except (TypeError, ValueError):
        filled = 0
    try:
        avg = float(status.get("average_price") or 0)
    except (TypeError, ValueError):
        avg = 0.0

    if filled > 0:
        # It filled, wholly or partly, despite the cancel. A position exists.
        log.warning(f"{symbol}: entry LIMIT {order_id} filled {filled} "
                    f"@ {avg} despite the cancel — protecting the position")
        return {"outcome": "FILLED", "filled_qty": filled, "avg_price": avg,
                "reason": f"filled {filled} @ {avg} during cancellation"}

    if st in ("CANCELLED", "REJECTED") or (cancelled and st in ("", "CANCELLED")):
        log.info(f"{symbol}: entry LIMIT {order_id} did not fill and was "
                 f"cancelled — no position, no entry")
        return {"outcome": "NO_FILL", "filled_qty": 0, "avg_price": 0.0,
                "reason": f"limit at {status.get('price')} did not fill; "
                          f"cancelled cleanly (status {st or 'gone'})"}

    # Cancel failed and nothing is confirmed filled. The order may still be
    # live. This is the genuine unknown, and it keeps the old handling: the row
    # stays PENDING and reconcile owns it.
    log.error(f"{symbol}: entry LIMIT {order_id} could not be settled "
              f"(cancel_ok={cancelled}, status={st or 'not in book'}) — "
              f"it may still be live")
    return {"outcome": "UNKNOWN", "filled_qty": 0, "avg_price": 0.0,
            "reason": f"cancel_ok={cancelled}, status={st or 'not in book'}"}


def enter_trade(signal: dict, dry_run: bool = False) -> dict:
    """Evaluate a candidate and, unless dry_run, enter it.

    dry_run=False is the only mode that can place an order. It is the DEFAULT,
    so a caller that forgets the argument gets the live path and the guards
    below -- rather than silently evaluating and reporting nothing happened.
    """
    symbol     = signal.get("symbol", "")
    direction  = signal.get("direction", "LONG").upper()
    entry_ref  = float(signal.get("entry_ref", signal.get("entry", 0)) or 0)
    entry_low  = float(signal.get("entry_low", 0) or 0)
    entry_high = float(signal.get("entry_high", 0) or 0)
    stop_price = float(signal.get("sl", 0) or 0)
    mode = "LIVE" if LIVE_TRADING_ENABLED else "SHADOW"

    log.info(f"[{mode}] Evaluating {symbol} {direction}")

    # GATE -1 -- self-halt. Ahead of everything because a halted ATLAS should
    # cost nothing per evaluation, and because the condition that halted it is
    # usually the condition that would make the gates below lie.
    #
    # Local sentinel only; the atlas_state side is Gate 7's kill switch, which
    # already fails closed. Reading it twice would double the request count for
    # no extra safety.
    halted, why = breaker.is_halted()
    if halted and not dry_run:
        return {"status": "BLOCKED_HALTED", "reason": why}
    if halted:
        # dry_run DELIBERATELY CONTINUES PAST THE HALT. Being halted is the
        # reason this evaluation is a dry run at all, so returning here would
        # make a halted ATLAS blind in exactly the way it was blind for 360
        # cycles on 8 Oct: it would learn nothing about what it would have done.
        # Nothing below can place an order -- dry_run returns before the write
        # phase, and place_order() refuses an ENTRY while halted regardless.
        log.info(f"{symbol}: halted ({why}) — evaluating anyway, placing nothing")

    # GATE -1b -- EXIT MANAGEMENT MUST BE ON TO TAKE AN ENTRY AT ALL.
    #
    # MOVED HERE FROM AFTER THE FILL, 2026-10-13. The check used to sit between
    # await_fill and protect_or_exit: with the flag False, ATLAS placed the
    # order, let it fill, logged a warning, and returned ENTERED with
    # exit_managed=False. A live position, no stop, no halt, and a status
    # indistinguishable from a protected entry to anything reading
    # atlas_entry_log.
    #
    # A FLAG THAT REMOVES ALL PROTECTION MUST NOT BE AN ENTRY PATH. The old
    # shape treated "leave it to a human" as a mode; it is not a mode the
    # engine can be in, because the engine does not know whether a human is
    # there. If exit management is off, the correct number of positions for
    # ATLAS to open is zero.
    #
    # BEFORE THE RESERVATION AND BEFORE THE ORDER. This is the cheapest check
    # in the function and it is a config read, so it costs nothing to put it
    # first -- and being first is the point: nothing downstream can have
    # happened by the time it refuses.
    #
    # ITS OWN REASON CODE. SKIPPED_NO_EXIT_MANAGEMENT, not folded into CONFIG,
    # so a session spent sitting out for this reason is countable in
    # atlas_entry_log rather than being guessed at from prose.
    from atlas.config import ENABLE_EXIT_MANAGEMENT
    if not ENABLE_EXIT_MANAGEMENT:
        log.warning(
            f"{symbol}: ENTRY REFUSED — entries are disabled because exit "
            f"management is off (ENABLE_EXIT_MANAGEMENT is False). ATLAS "
            f"would have no stop on this position, so it does not open one.")
        return {"status": "SKIPPED_NO_EXIT_MANAGEMENT",
                "reason": ("entries are disabled because exit management is "
                           "off (ENABLE_EXIT_MANAGEMENT is False) — ATLAS "
                           "will not open a position it cannot protect"),
                "blocked_by": "NO_EXIT_MANAGEMENT"}

    # GATE 0 -- structural stop mandatory
    if stop_price <= 0:
        return {"status": "REJECTED_NO_STOP",
                "reason": "no structural stop -- cannot size by risk"}

    # THERE IS NO FUNDAMENTALS GATE. Tier 1 -- pledge, auditor resignation,
    # promoter exit, rising leverage -- no longer vetoes an entry.
    #
    # DROPPED BY OPERATOR DECISION, and worth recording what is given up. The
    # veto was reachable and it did bite: 631 of 706 symbols passed, 72 were
    # vetoed, 3 unparseable. Those 72 are now tradeable. The layer existed for
    # the class of loss a technical stop cannot protect against -- a pledge
    # unwind or an auditor walking gaps through a stop rather than touching it --
    # and nothing replaces it.
    #
    # What is NOT given up: engine/fundamentals.py, its cache and its crons stay
    # in place and keep recording verdicts, so the question "would this have been
    # vetoed" remains answerable after the fact. That is the only reason removing
    # it is recoverable. If the veto is ever wanted back it is one gate here, not
    # a rebuild.
    #
    # The Tier 2 metrics were never a veto and are unaffected.

    # GATE 0b -- THIS SYMBOL BROKE ITS OWN RANGE TODAY.
    #
    # A local file read, so it sits up here with the cheap gates: a symbol
    # blocked by the stock breaker cannot be entered again today whatever the
    # rest of the stack says, and there is no sense spending a quote or a funds
    # call to rediscover that.
    #
    # IT DOES NOT AFFECT AN OPEN POSITION IN THAT SYMBOL. The stock breaker
    # blocks NEW entries; the stop-loss order owns the exit.
    from atlas.execution import range_breaker as RB
    _blocked, _bwhy = RB.is_blocked(symbol)
    if _blocked:
        return {"status": f"SKIPPED_{RB.REASON_SYMBOL_BREAK}",
                "reason": f"{symbol} broke its range today — {_bwhy}",
                "blocked_by": RB.REASON_SYMBOL_BREAK}

    # THE SAME SHAPE, FOR A DIFFERENT CAUSE. A symbol whose protection could
    # not be placed earlier today is blocked for the session -- the one thing
    # that used to stand the whole account down. Per symbol, per session, and
    # it does not touch an open position in that symbol: the block is about new
    # entries, and the stop owns the exit.
    from atlas.risk import symbol_block as SB
    _sb, _sb_reason, _sb_detail = SB.is_blocked(symbol)
    if _sb:
        return {"status": f"SKIPPED_{_sb_reason}",
                "reason": f"{symbol} is blocked for the session — {_sb_detail}",
                "blocked_by": _sb_reason}

    # GATE 1 -- regime AND sentiment. Both must hold or ATLAS stays in cash.
    #
    # The status carries the split so it is countable in atlas_entry_log
    # without parsing prose: SKIPPED_REGIME means the structure gives no side,
    # SKIPPED_SENTIMENT means participation is unclear or unreadable, and
    # SKIPPED_CONFLICT means the two point opposite ways.
    # Knowing which one holds the agent out, over a long flat stretch, is the
    # difference between "the trend is not there" and "the trend is there and
    # nobody is buying it".
    ctx = get_market_context()
    ok, reason, blocked_by = regime_allows_side(ctx, direction)
    if not ok:
        return {"status": f"SKIPPED_{blocked_by or 'REGIME'}", "reason": reason,
                "blocked_by": blocked_by,
                "regime": ctx.get("regime"), "regime_source": ctx.get("source")}

    # GATE 2 -- opening range. SHORTS ONLY.
    if direction in OPENING_RANGE_GATE_APPLIES_TO:
        try:
            from atlas.execution.index_state import get_market_direction
            mkt_dir = get_market_direction().get("direction", "WAIT")
        except Exception as e:
            log.warning(f"opening-range check failed: {e}")
            mkt_dir = "WAIT"
        if mkt_dir != "SHORT":
            return {"status": "SKIPPED_MARKET_WAIT",
                    "reason": f"opening range is {mkt_dir} -- hedge short needs SHORT"}

    # THERE IS NO GATE 3. Nothing here counts entries, positions, or rupees.
    #
    # BROKER FUNDS ARE THE ONLY BOUND ON TOTAL EXPOSURE. The operator keeps
    # capital available and ATLAS takes every setup that forms. There is no
    # maximum simultaneous capital deployed and no per-day entry count.
    #
    # Three limits have stood in this spot and all three are gone for the same
    # reason. MAX_TRADES_PER_DAY counted entries, which bounded the NUMBER of
    # positions while saying nothing about their size, and refused the fourth good
    # setup for arithmetic. MAX_CAPITAL_DEPLOYED, with get_exposure(), summed our
    # own rows into a local balance. MAX_CONCURRENT_EXPOSURE, with
    # get_open_exposure(), did the same in rupees at Rs4,00,000 -- below the
    # average this book had been running and about a third of its peak, so it
    # would have refused ordinary sixth positions. That is a cap on opportunity
    # wearing a risk control's clothes.
    #
    # What remains is the right shape: each entry is bounded individually by
    # MAX_RISK_PER_TRADE and MAX_NOTIONAL_PER_TRADE, and what stops the next one
    # is the absence of funds to pay for it, which Gate 6 reads live from
    # kite.margins() net of resting GTTs. Three entries of Rs3k risk and thirty
    # are genuinely different exposures -- but the broker already knows which one
    # the account can carry, and any local ceiling is a second opinion about the
    # operator's own balance, held in a constant, able to drift from the truth,
    # and wrong more often than the broker is.
    #
    # get_today_entry_count() is kept and still logged for the daily report:
    # how many entries a day produced is worth knowing even when nothing limits
    # it. If a limit is ever wanted again it belongs in the funds the account
    # holds, not here.

    # GATE 3b -- already holding this symbol. HARD SKIP, never a scale-in.
    #
    # A zone that survives two sessions re-publishes, so the same symbol
    # reappears in consecutive batches -- DELHIVERY was in both the 20 Aug and
    # 21 Aug batches with an identical setup and an identical stop, and nothing
    # below would have noticed. Nothing else looks: the per-day count that used
    # to sit above saw TODAY's entries only, so a position opened any earlier day
    # was invisible to it; kill_switch dropped its max-open-positions check; and
    # market_open.dedupe() dedupes within a batch, not against the book. This is
    # the only gate that reads the book by symbol.
    #
    # Skip rather than scale in because the caps are per-entry: RISK_PER_TRADE
    # and MAX_NOTIONAL would each be applied a second time, so a second fill
    # behind the SAME structural stop is one position at double the risk while
    # the ledger reads it as two independent trades that each respect the cap.
    # Making a scale-in safe means enforcing risk and notional across the
    # combined position, which is the BUY/ADD/SELL work parked for a later
    # phase. Until that exists a hard skip is the correct behaviour -- do not
    # relax this into an averaging-in rule without that accounting.
    #
    # Placed ahead of Gate 4 so a held symbol costs no LTP fetch.
    readable, held = get_open_position(symbol)
    if not readable:
        return {"status": "BLOCKED_NO_POSITION_DATA",
                "reason": "cannot read open positions -- refusing to risk a duplicate"}
    if held:
        return {"status": "SKIPPED_HOLDING",
                "reason": (f"already holding {held.get('qty')} {symbol} "
                           f"{held.get('direction')} @ Rs{held.get('entry_price')} "
                           f"from {held.get('entry_date')} "
                           f"(stop Rs{held.get('stop_price')}, {held.get('status')})"),
                "held_trade_id": held.get("id")}

    # GATE 4 -- entry range. Enter at MARKET or skip. No resting orders.
    #
    # A LONG whose zone sat below price used to rest a GTT and wait for a
    # retrace. That path is gone. Signals now only publish when price is within
    # MAX_ENTRY_DIST_PCT (0.30%) of the zone, i.e. already there, so waiting for
    # a retest is not the trade -- taking it now is. Resting triggers also
    # committed cash for days against a fill that mostly never came.
    # Price comes from the caller when it has one. The market-hours loop polls
    # every symbol from Upstox in a single batched request per cycle, so asking
    # Zerodha again here would be one broker call per candidate per cycle for a
    # number we already have. Zerodha stays the execution broker; Upstox is the
    # price feed. get_ltp remains the fallback for a one-shot caller.
    ltp = float(signal.get("ltp") or 0) or get_ltp(symbol) or entry_ref
    in_range, range_reason = check_entry_range(direction, ltp, entry_low,
                                              entry_high, stop_price,
                                              entry_ref=entry_ref)
    if not in_range:
        return {"status": "SKIPPED_RANGE", "reason": range_reason}

    # GATE 4b -- THE STOCK BREAKER, on this candidate, now.
    #
    # AFTER THE BAND AND PROXIMITY GATE, DELIBERATELY. This costs one candle
    # fetch per candidate and there is no batch endpoint, so it is spent only
    # on candidates that have already passed the 1% proximity test and could
    # actually be entered this cycle. A candidate sitting 4% from its zone
    # cannot be entered whatever its box says, and fetching its candles to
    # learn that would be ~6x the API traffic for no decision.
    #
    # ON A BREAK: this symbol is blocked for the rest of the session and the
    # operator is alerted. It NEVER exits anything -- the stop-loss order owns
    # that, and two systems racing to close one position is how that goes wrong.
    _sb = RB.stock_check(symbol, direction, last_price=ltp)
    if _sb["broke"]:
        return {"status": f"SKIPPED_{RB.REASON_SYMBOL_BREAK}",
                "reason": f"{symbol} {_sb['detail']}",
                "blocked_by": RB.REASON_SYMBOL_BREAK}
    if _sb["state"] == RB.REASON_CANNOT_EVALUATE:
        # FAIL OPEN, AND SAY SO. See the reasoning in range_breaker's
        # docstring: this breaker is one-way and the stop-loss is the real
        # protection, so a feed hiccup must not refuse the entry. Logged, not
        # silent, and surfaced in the inventory.
        log.warning(f"{symbol}: stock breaker could not evaluate — "
                    f"{_sb['detail'][:160]}; proceeding, the stop-loss is the "
                    f"protection")

    # GATE 5 -- sizing. Rs3,000 risk / Rs1,00,000 notional, qty a multiple of 5,
    # AND capped by what the account can actually pay for.
    #
    # THE SIZER'S FUNDS PATH WAS DEAD. size_by_risk() has taken an
    # `available_funds` argument since it was written -- it reduces qty to fit,
    # sets binding="funds", and says so in the log -- and NO caller has ever
    # passed it. The consequence was not a missed optimisation, it was Gate 6
    # refusing trades ATLAS had priced for itself: a CNC long blocks its FULL
    # notional, sizing ran to the Rs1,00,000 cap regardless of the balance, and
    # then the funds check declined it. The live record has this verbatim --
    # 2026-08-14, "insufficient funds: need Rs101,486 (Rs99,496 + 2% buffer),
    # available Rs38,395". Nothing was wrong with the trade. A Rs38,395 account
    # can carry it at 380 shares instead of 1,000.
    #
    # So the floor for taking ANY long was an idle Rs1,02,000, set by
    # MAX_NOTIONAL_PER_TRADE rather than by risk, while MAX_RISK_PER_TRADE says
    # the trade risks Rs3,000. Those two numbers were describing different
    # systems.
    #
    # READ ONCE, HERE, AND REUSE IT. Gate 6 below needs the same figure, so
    # taking it here costs nothing and removes a second kite.margins() +
    # get_gtts() round trip per candidate per cycle.
    #
    # FAILS CLOSED, unchanged: an unreadable balance blocks. Sizing against an
    # assumed balance is exactly the stored-capital ledger this module deleted.
    try:
        _funds = available_funds()
        spendable = float(_funds["available"]) / (1.0 + FUNDS_SAFETY_BUFFER_PCT)
    except FundsUnavailable as e:
        return {"status": "BLOCKED_NO_FUNDS_DATA",
                "reason": f"broker funds unreadable: {e}"}

    # size_for_trade dispatches on SIZING_MODE. In fixed_notional mode the quantity
    # comes from the Rs10,000 target and risk is whatever the stop implies -- there
    # is no risk budget to report and the result carries no risk_budget key.
    # ── THE STOP ATLAS WILL ACTUALLY USE ──────────────────────────
    #
    # The published stop is a proposal. On 2026-10-15 not one of 103 long
    # candidates had one inside [MIN_STOP_PCT, MAX_STOP_PCT] -- 86 too wide,
    # 17 too tight -- so the band, not publication_kind, was refusing them all.
    #
    # derive() returns the published stop unchanged when it fits, and otherwise
    # the NEAREST SMC level that does: demand OB low, then bull FVG low, then
    # the last swing low, all already computed by the nightly chain. Only when
    # no level fits does it clamp to the band edge, and it says so.
    #
    # BOTH STOPS ARE RECORDED. If the derived stops get hit far more often than
    # the published ones would have, that is the finding -- and it cannot be
    # recovered later from one column.
    from atlas.execution.derived_stop import derive as _derive_stop
    _ds = _derive_stop(symbol, float(ltp), float(stop_price), direction)
    if _ds["stop"] and _ds["source"] != "published":
        log.info(f"{symbol}: published stop {stop_price} is "
                 f"{(_ds['published_pct'] or 0):.2f}% from entry — using "
                 f"{_ds['stop']:.2f} from {_ds['source']} "
                 f"({_ds['stop_pct']:.2f}%). {_ds['why']}")
    published_stop = float(stop_price)
    if _ds["stop"]:
        stop_price = float(_ds["stop"])
    sizing = size_for_trade(entry_price=ltp, stop_price=stop_price,
                            direction=direction, available_funds=spendable)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}
    if sizing.get("binding_cap") == "funds":
        log.info(f"{symbol}: sized to the balance — {sizing['qty']} shares, "
                 f"Rs{sizing['capital_required']:,.0f} of Rs{spendable:,.0f} spendable "
                 f"(risk Rs{sizing['risk_actual']:,.0f})")
    if sizing.get("oversized"):
        # The share price exceeds the whole target, so one share was taken anyway.
        # Deliberate -- the instrument must not be blind to MRF or MARUTI because of
        # its own test size -- but the record has to say so.
        log.warning(f"{symbol}: OVERSIZED — one share at Rs{ltp:,.0f} is "
                    f"{sizing['notional_pct']:.0f}% of the Rs"
                    f"{sizing['target_notional']:,.0f} target, risk "
                    f"Rs{sizing['risk_actual']:,.0f}")

    # GATE 6 -- live broker funds, net of resting GTTs. FAILS CLOSED at Gate 5,
    # where the balance is read: unreadable means BLOCKED_NO_FUNDS_DATA before a
    # quantity exists. What is left here is the comparison.
    # THE BROKER'S MARGIN, NOT OUR ESTIMATE. sizing["capital_required"] is
    # notional for a CNC long and notional x SHORT_MARGIN_PCT_ESTIMATE (0.20) for
    # an MIS short. With no cap on the number of trades, that 0.20 is the only
    # thing deciding how many shorts fit: five of them occupy one long's capital
    # at 20%, and if the true requirement is 30% the fifth is unfunded. So ask.
    #
    # A failed lookup falls back to the estimate and says so. Refusing every trade
    # because the margin endpoint is unreachable would be a different failure, not
    # a safer one -- and can_afford still fails closed on unreadable FUNDS, which
    # is the check that actually protects the account. The kill switch at Gate 7
    # still calls can_afford() itself and remains the independent fail-closed
    # authority on the balance.
    need = sizing["capital_required"]
    marg = order_margin(symbol=symbol, direction=direction, qty=sizing["qty"],
                        product=sizing["product"], order_type="MARKET")
    if marg.get("ok"):
        broker_need = float(marg["total"])
        if broker_need > need:
            log.info(f"{symbol}: broker margin Rs{broker_need:,.0f} exceeds the "
                     f"estimate Rs{need:,.0f} — gating on the broker's number")
        need = max(need, broker_need)
        sizing["capital_required"] = need
        sizing["margin_source"] = "broker"
    else:
        log.warning(f"{symbol}: using ESTIMATED margin Rs{need:,.0f} "
                    f"({marg.get('reason')})")
        sizing["margin_source"] = "estimate"

    # THE SAME BALANCE THE SIZER WAS GIVEN, not a second read. can_afford() would
    # re-fetch kite.margins() and get_gtts() here, and acting on the FIRST of two
    # reads while the second decides is worse than one read used for both: sizing
    # and the check would be answering different questions about the same account.
    # Semantics are unchanged -- the buffer still applies, an unreadable balance
    # still blocked, above at Gate 5, before any sizing happened.
    need_buffered = need * (1.0 + FUNDS_SAFETY_BUFFER_PCT)
    if need_buffered > float(_funds["available"]):
        return {"status": "SKIPPED_FUNDS",
                "reason": (f"insufficient funds: need Rs{need_buffered:,.0f} "
                           f"(Rs{need:,.0f} + {FUNDS_SAFETY_BUFFER_PCT*100:.0f}% buffer), "
                           f"available Rs{_funds['available']:,.0f} (broker "
                           f"Rs{_funds['margin']:,.0f} less Rs{_funds['gtt_committed']:,.0f} "
                           f"resting GTTs)")}

    # GATE 7 -- kill switch
    signal["capital_required"] = need
    if not kill_switch_check(signal):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    intent = _build_intent(signal, symbol, direction, ltp, sizing, ctx)
    # BOTH STOPS AND THE KIND, onto the row. The columns were added by
    # migrations/atlas_trades_derived_stop.sql, applied 2026-10-16.
    # _reserve_intent still probes for each rather than assuming, so a database
    # restored from a backup taken before the migration costs four reporting
    # fields instead of every entry -- the same shape that kept publishing
    # working before signals.engine_sha existed.
    intent["published_stop"] = published_stop
    intent["stop_source"] = _ds["source"]
    intent["stop_pct_used"] = _ds["stop_pct"]
    intent["publication_kind"] = str(signal.get("publication_kind") or "")

    if dry_run:
        # EVERY GATE HAS RUN. The intent is built, which means sizing, the
        # quote, the margin and the kill switch all passed. We stop one step
        # before the reservation, so nothing is written to atlas_trades and
        # nothing reaches the broker -- the caller records the decision in
        # atlas_entry_log, which is the record we actually want from a halted
        # cycle.
        return {"status": "WOULD_ENTER", **intent}

    if not LIVE_TRADING_ENABLED:
        log.info(f"[SHADOW] WOULD ENTER {direction} {sizing['qty']} {symbol} @ Rs{ltp:.1f} "
                 f"| stop Rs{stop_price:.1f} ({sizing['stop_pct']}%) "
                 f"| risk Rs{sizing['risk_actual']:,.0f}")
        _log_intent(intent, shadow=True)
        return {"status": "SHADOW_INTENT", **intent}

    # ── TWO-PHASE WRITE ───────────────────────────────────────────
    #
    # This used to be place_order() then _log_intent(), so the position existed
    # before anything recorded it. A death in between left a holding at the
    # broker with no atlas_trades row -- and Gate 3b reads atlas_trades, so the
    # next evaluation could not see it. Once per day that was a bad morning;
    # at one cycle a minute it is a symbol re-entered until something stops it.
    #
    # Inverted, the row is committed BEFORE the order can exist, and PENDING is
    # in BLOCKING_STATUSES, so every possible death leaves a state the next
    # evaluation can see:
    #
    #   before reserve      nothing happened
    #   reserve, no order   PENDING row, no position -- over-blocks the symbol
    #                       until reconcile clears it. Wrong in the safe
    #                       direction.
    #   order, no complete  PENDING row + position -- blocked, correct
    #   complete            OPEN row + position
    #
    # There is no ordering that yields a position with no row. Even a lost
    # INSERT response resolves safely: we treat it as failed and place nothing,
    # leaving a stray PENDING row for reconcile.
    row_id, failed_code, failed_detail = _reserve_intent(intent)
    if row_id is None:
        breaker.record_ledger_write(False, failed_code, failed_detail,
                                    phase="reserve")
        return {"status": "BLOCKED_NO_LEDGER",
                "reason": f"could not reserve a trade row: {failed_detail}"}
    breaker.record_ledger_write(True, phase="reserve")
    intent["trade_id"] = row_id

    # atlas_trades.id is a bigint, so ATLAS:<id> fits Kite's 20-character tag
    # and reconcile can join broker orders to rows exactly rather than guessing
    # from symbol and timestamp.
    # ── A LIMIT AT THE LIVE PRICE, NOT MARKET ─────────────────────
    #
    # The MARKET entry was the last market-type order in the path. Kite refuses
    # an API MARKET order that omits market_protection -- the rule that took
    # out both the stop and its fallback on 7 October -- and the entry survived
    # only because broker.place_order happens to set market_protection=3. A
    # LIMIT is outside that rule entirely and prices itself.
    #
    # ltp IS THE PRICE READ AT GATE 5, a few hundred milliseconds ago, not a
    # fresh quote. Deliberate: re-reading here would mean sizing was done
    # against one price and the order placed at another, and the 2R target is
    # computed from the actual fill anyway.
    #
    # THE COST IS A MISSED FILL. A LIMIT at the last traded price does not fill
    # if the book moves away, and the settle step below cancels it and records a
    # clean no-entry rather than chasing. Not entering is a cost this strategy
    # can carry; an unbounded fill price is not.
    # THE ENTRY LIMIT IS ALSO A PRICE THE BROKER PARSES. round(ltp, 2) is not
    # a tick multiple on a 0.05, 0.10, 0.50, 1.00 or 5.00 instrument, and it
    # survived only because the LTP it copies is itself on-tick -- an accident,
    # not a guarantee, and the same accident that hid the stop's off-tick price
    # until 2026-10-15.
    #
    # AN ENTRY ROUNDS UP FOR A LONG (DOWN for a short): pay the tick and get
    # the fill. A protective price rounds to the safer side; an entry rounds to
    # the side that fills, because a LIMIT that rests behind the book is a
    # missed trade.
    from atlas.config import ENTRY_ORDER_TYPE
    from atlas.execution import exits as X
    try:
        entry_px = X.price_for(symbol, float(ltp), "entry", direction)
    except X.TickUnknown as e:
        # NOT A REFUSAL ABOUT TICK SIZE. ATLAS cannot compute a price the
        # broker will accept, so it cannot protect what it is about to open --
        # the same principle as refusing to enter when exit management is off.
        from atlas.execution.tick_map import REASON_NO_TICK
        # DETERMINATE: no order was sent, so the reservation is released the
        # same way a margin refusal releases it. Leaving it PENDING would block
        # Gate 3b on a symbol nothing was ever opened in.
        log.error(f"{symbol}: ENTRY REFUSED — {e}. No order was sent; "
                  f"releasing the reservation on trade {row_id}.")
        _release_intent(row_id, f"{REASON_NO_TICK}: {e}")
        return {"status": f"SKIPPED_{REASON_NO_TICK}", "trade_id": row_id,
                "reason": (f"{symbol}: no tick size is known, so no price can "
                           f"be computed that the broker will accept — "
                           f"refusing to open a position that cannot be "
                           f"protected"),
                "blocked_by": REASON_NO_TICK}
    order = place_order(symbol=symbol, direction=direction, qty=sizing["qty"],
                        order_type=ENTRY_ORDER_TYPE, price=entry_px,
                        tag=f"ATLAS:{row_id}", product=sizing["product"],
                        intent="ENTRY")
    intent["entry_order_type"] = ENTRY_ORDER_TYPE
    intent["entry_limit_price"] = entry_px

    if not order.get("success"):
        reason = order.get("reason", "order failed")
        # ONLY release the reservation when we KNOW no order exists. place_order
        # returns success=False for a margin refusal and for a socket timeout
        # alike; releasing on the latter would erase the only record of a live
        # position and hand the next cycle a clean slate to re-enter from.
        if order.get("determinate"):
            _release_intent(row_id, reason)
            breaker.record_order_reject(symbol, reason)
            return {"status": "ORDER_FAILED", "reason": reason}

        log.error(f"{symbol}: order outcome UNKNOWN ({order.get('error_type')}: "
                  f"{reason}) — leaving trade {row_id} PENDING for reconcile")
        _mark_indeterminate(row_id, reason)
        return {"status": "ORDER_INDETERMINATE", "trade_id": row_id,
                "reason": f"outcome unknown, left PENDING: {reason}"}

    breaker.record_order_ok()
    intent["order_id"] = order.get("order_id")
    recorded = _complete_intent(row_id, intent)

    # ── PROTECTION, IN THIS CYCLE, BEFORE RETURNING ───────────────
    #
    # Detecting the fill on the NEXT cycle would leave a minute of unprotected
    # exposure on every entry in a system nobody is watching, so await_fill
    # BLOCKS here. The wait is bounded and its timeout is treated as "a
    # position may exist", never as "no position".
    #
    # A LIMIT CHANGES WHAT A TIMEOUT MEANS. A MARKET order that has not filled
    # in 20s is a genuine unknown; a LIMIT that has not filled is usually just
    # resting away from the book, which is an ordinary outcome and not an
    # incident. _settle_unfilled_limit below cancels it and finds out which of
    # the two it was, so the common case stops being reported as INDETERMINATE.
    # ENABLE_EXIT_MANAGEMENT IS CHECKED AT GATE -1b, BEFORE ANY ORDER EXISTS.
    # It used to be checked HERE, after the fill, and returned ENTERED with no
    # stop attempted and a log warning. See the gate for why that is gone.
    from atlas.execution import exits as X

    fill = X.await_fill(intent["order_id"])
    intent["fill"] = fill.get("outcome")

    if fill["outcome"] == "REJECTED":
        # Nothing was bought or sold. Release rather than leave a phantom row.
        _release_intent(row_id, f"order rejected: {fill.get('reason')}")
        breaker.record_order_reject(symbol, str(fill.get("reason")))
        return {"status": "ORDER_FAILED", "reason": fill.get("reason")}

    if fill["outcome"] == "INDETERMINATE":
        # A RESTING LIMIT IS THE LIKELY CAUSE, NOT AN INCIDENT. Cancel it and
        # find out, instead of alerting on what is usually an ordinary no-entry.
        settled = _settle_unfilled_limit(intent["order_id"], symbol)

        if settled["outcome"] == "NO_FILL":
            # Nothing was bought. Release the reservation -- this is a clean
            # no-entry and the symbol must stay available tomorrow.
            _release_intent(row_id, settled["reason"])
            log.info(f"{symbol}: NO ENTRY — {settled['reason']}")
            return {"status": "NO_FILL_LIMIT", "reason": settled["reason"],
                    **intent}

        if settled["outcome"] == "FILLED":
            # It filled during the cancel. Fall through to protection with the
            # quantity that ACTUALLY filled.
            fill = {"outcome": "FILLED",
                    "filled_qty": settled["filled_qty"],
                    "avg_price": settled["avg_price"],
                    "reason": settled["reason"]}
            intent["fill"] = "FILLED"
        else:
            # The genuine unknown. Unchanged handling: nothing may be assumed
            # about the position, so no stop can be sized and none is placed --
            # the row stays PENDING and reconcile owns it. This is the one path
            # that can leave an unprotected position, and it does so because
            # acting on an unknown quantity is worse.
            log.error(f"{symbol}: fill INDETERMINATE and the entry LIMIT could "
                      f"not be settled ({settled['reason']}) — trade {row_id} "
                      f"left PENDING, NOT protected. reconcile owns it.")
            _mark_indeterminate(row_id, f"fill unconfirmed: {settled['reason']}")
            _alert("FILL UNCONFIRMED",
                   f"{symbol} {direction}: {settled['reason']}. No stop "
                   f"placed. Trade {row_id} is PENDING and may be a live "
                   f"position.")
            return {"status": "FILL_UNCONFIRMED", "trade_id": row_id,
                    "reason": settled["reason"], **intent}

    filled_qty = int(fill["filled_qty"])
    avg_price  = float(fill["avg_price"])
    intent["fill_price"] = round(avg_price, 2)
    intent["filled_qty"] = filled_qty

    # ── THE REAL FILL BECOMES THE RECORD ──────────────────────────
    #
    # entry_price USED TO BE THE PRE-ORDER LTP. _build_intent set it from the
    # quote taken before the order went out, and the post-fill patch wrote that
    # same value back, so the fill average reached no column at all. Row 22
    # (ATHERENERG) recorded 1456.40 against a broker average of 1453.70 -- and
    # every figure computed from the row, stop_pct_used and risk and R, was
    # measured from a price the trade never paid.
    #
    # THE STOP AND TARGET DO NOT MOVE. Hemal's ruling: they stay exactly where
    # ATLAS placed them. Entry variance is expected and the record should show
    # what was actually paid, not be rewritten so the arithmetic looks tidy. So
    # the fill replaces entry_price and the DERIVED figures are recomputed off
    # it; the stop and target prices themselves are untouched.
    intent["entry_price"] = round(avg_price, 2)
    intent["qty"] = filled_qty
    _stop_used = float(sizing["stop_price"])
    intent["stop_pct_used"] = (abs(avg_price - _stop_used) / avg_price * 100.0
                               if avg_price else None)
    intent["risk_actual"] = abs(avg_price - _stop_used) * filled_qty
    intent["r_multiple_basis"] = abs(avg_price - _stop_used)

    # THE PRIMARY AND ITS OUTCOME. protect_or_flag no longer exits on failure:
    # by ruling, a position that cannot be protected is KEPT and alerted on.
    pe = X.protect_or_flag(symbol=symbol, direction=direction, qty=filled_qty,
                           product=sizing["product"], fill_price=avg_price,
                           stop_price=_stop_used)
    prot = pe["prot"]
    intent["exit_legs"] = prot.get("legs") or {}
    intent["exit_mechanism"] = prot.get("mechanism", "")

    # THE TARGET IS RECORDED WHETHER OR NOT IT PLACED. target_price was None on
    # row 22 because the OCO failed before anything was written, so the record
    # could not say what the target had been. The INTENDED level is computed
    # here from the fill and the stop actually used, and whether it is LIVE is
    # recorded separately in target_confirmed -- two different facts that were
    # previously one missing column.
    _intended_target = prot.get("target")
    if _intended_target is None:
        try:
            _intended_target = X.target_price(avg_price, _stop_used, direction,
                                              symbol=symbol)
        except Exception as e:
            log.warning(f"{symbol}: could not compute the intended target "
                        f"({type(e).__name__}: {e}) — recording none")
            _intended_target = None
    intent["target_price"] = _intended_target
    intent["stop_confirmed"] = bool(prot.get("ok"))
    intent["target_confirmed"] = bool(prot.get("ok") and prot.get("target"))

    # ── THE STOP DID NOT PLACE: KEEP THE POSITION, SAY SO, LOUDLY ──
    #
    # APPROVED BY HEMAL ON 2026-10-16, overruling the recommendation that stood
    # here. This block used to do two things and now does neither: it closed
    # the position on a marketable LIMIT, and it set the account-wide halt.
    # What it does instead:
    #
    #   1. BLOCKS THIS SYMBOL for the session, never the account. One stock's
    #      tick size took 443 published rows off the table on 2026-10-16; the
    #      fault was one instrument's rounding and the blast radius was the
    #      whole universe.
    #   2. REGISTERS the position as unprotected, so the alert REPEATS every
    #      15 minutes until a stop is confirmed at the broker. ATHERENERG's
    #      single alert on 15 October went quiet and the position stayed naked
    #      for a session and a night.
    #   3. ALERTS IMMEDIATELY with everything needed to act by hand, including
    #      the running count that is the operator's signal to pause.
    #
    # It does NOT halt, does not exit, and does not decide anything on the
    # count's behalf. There is deliberately no threshold here.
    if pe["unprotected"]:
        from atlas.risk import symbol_block as SB
        from atlas.execution import protection as PR
        _why = prot.get("reason") or pe.get("reason") or "unknown"
        _wanted = pe.get("wanted_stop", _stop_used)

        SB.block(symbol, SB.REASON_PROTECTION_FAILED,
                 f"the day-0 stop could not be placed ({_why}). The position "
                 f"is OPEN and unprotected; a manual stop is needed.")

        _row = {"id": row_id, "symbol": symbol, "direction": direction,
                "qty": filled_qty, "entry_price": avg_price,
                "stop_price": _wanted}
        _entry = PR.register(_row, _why, wanted_stop=_wanted)
        _alert("OPEN WITHOUT PROTECTION — MANUAL STOP NEEDED",
               f"{symbol} {direction} {filled_qty} filled at "
               f"Rs{avg_price:,.2f}. The stop ATLAS wanted was "
               f"Rs{float(_wanted):,.2f} and it COULD NOT BE PLACED: {_why}. "
               f"The position is open and unprotected — place a stop by hand "
               f"now.\n\nunprotected position {_entry.get('ordinal')} of "
               f"{PR.seen_today()} today. No new entries in {symbol} today; "
               f"every other symbol is unaffected and ATLAS has NOT stood the "
               f"day down. This alert repeats every "
               f"{PR.UNPROTECTED_REPEAT_SECONDS // 60} minutes until a stop is "
               f"confirmed at the broker. Send /pause to stop entries.")

        _patch_trade(row_id, {"notes": (f"OPEN WITHOUT PROTECTION — wanted stop "
                                        f"Rs{float(_wanted)}; placement failed: "
                                        f"{str(_why)[:200]}")},
                     "record unprotected")
        return {"status": "ENTERED_UNPROTECTED", "trade_id": row_id,
                "recorded": recorded, "unprotected": True,
                "reason": _why, "halted": False, **intent}

    log.info(f"{symbol}: protected via {prot.get('mechanism')} — "
             f"stop {prot.get('stop')} target {prot.get('target')}")
    intent["exit_managed"] = True

    # PERSIST THE GTT TRIGGER ID. This is not bookkeeping: Kite's get_gtts()
    # OMITS the tag field entirely, so a GTT cannot be recognised as ours from the
    # broker side. atlas_trades.gtt_trigger_id is the only link, and
    # gtt.list_atlas_gtts() reads exactly that column -- an unrecorded trigger is
    # invisible to ATLAS while resting live at the broker, which is what happened
    # to GTT 331263278 on GRASIM. Reconciliation would then either ignore our own
    # orphan or, far worse, act on a human's GTT.
    #
    # Regular orders are different: their tag SURVIVES in the order book, so MIS
    # short legs are discoverable by tag and need no column.
    trig = (prot.get("legs") or {}).get("stop_trigger_id") \
        or (prot.get("legs") or {}).get("oco_trigger_id")
    if trig:
        if not _patch_trade(row_id, {"gtt_trigger_id": str(trig),
                                     "trigger_price": prot.get("stop")},
                            "record gtt"):
            _alert("GTT UNRECORDED",
                   f"{symbol}: stop GTT {trig} is live at the broker but could "
                   f"not be recorded on trade {row_id}. It is invisible to "
                   f"list_atlas_gtts and will not be reconciled. Record it by hand.")
    # The order IS placed -- that is the truth, so the status stays ENTERED.
    # `recorded` tells the caller whether the row was promoted out of PENDING.
    return {"status": "ENTERED", "recorded": recorded, **intent}


def _alert(kind: str, text: str) -> None:
    """
    Telegram, best effort, always logged.

    NOT market_open.alert: market_open imports enter_trade, so importing back
    would be circular. The throttling there is per-session state this module does
    not have -- and these alerts are per-position incidents rather than the
    repeating kind a cooldown exists for.
    """
    log.error(f"[{kind}] {text}")
    try:
        from atlas.reporting.telegram import send
        if not send(f"<b>ATLAS — {kind}</b>\n{text}"):
            log.error(f"ALERT NOT DELIVERED [{kind}] — Telegram send failed or is "
                      f"unconfigured. This alert exists only in this log.")
    except Exception as e:
        log.error(f"ALERT NOT DELIVERED [{kind}] ({e})")


def _build_intent(signal, symbol, direction, price, sizing, ctx) -> dict:
    return {
        "symbol": symbol, "direction": direction, "qty": sizing["qty"],
        "entry_price": round(price, 2), "stop_price": sizing["stop_price"],
        "stop_pct": sizing["stop_pct"], "risk_actual": sizing["risk_actual"],
        "notional": sizing["notional"], "product": sizing["product"],
        "binding_cap": sizing["binding_cap"], "regime": ctx.get("regime", ""),
        "capital_required": sizing["capital_required"],
        # The realised size, not the intended one, and which rule produced it.
        # When real sizing returns the record will hold both regimes and nothing
        # else would tell them apart -- the same gap the measurement-basis change
        # left in the outcome record.
        "notional_pct": sizing.get("notional_pct"),
        "target_notional": sizing.get("target_notional"),
        "oversized": sizing.get("oversized", False),
        "sizing_basis": sizing.get("sizing_basis"),
        "setup_name": signal.get("setup_name", ""), "session": signal.get("session", ""),
        "score": signal.get("score", 0), "grade": signal.get("grade", ""),
        "sector": signal.get("sector", ""), "zone_source": signal.get("zone_source", ""),
    }


# _place_gtt_entry() removed. ATLAS no longer rests GTT triggers: signals only
# publish when price is within 0.30% of the zone (engine/zone_entry.py
# MAX_ENTRY_DIST_PCT), so the entry is a MARKET order taken immediately rather
# than a trigger waiting for a retrace that mostly never arrived.
#
# gtt.place_zone_gtt() is removed with it. gtt.py keeps the read and cancel
# helpers -- funds.pending_gtt_commitment() must still see any GTT resting at
# the broker, including ones the operator placed by hand, because they commit
# cash regardless of origin.


# Does atlas_trades have `product` yet? Probed once per process rather than
# assumed, because the column arrives with a hand-applied migration and an INSERT
# carrying an unknown key is rejected whole -- which would turn a missing
# reporting column into a missing trade. Re-probed on a new process, so applying
# the migration takes effect at the next restart without a code change.
#
# Fails to False: not sending the column costs a row that reconcile fills in by
# direction. Sending one that does not exist costs the entry.


_TRADES_COLS = {}


def _trades_has(col: str, why: str = "") -> bool:
    """Does atlas_trades have this column? One probe per column per process.

    GENERALISED FROM _trades_has_product, which existed because PostgREST rejects
    the WHOLE insert for one unknown key -- so a column that has not been migrated
    yet must be omitted rather than allowed to take the trade down with it. The
    sizing restart adds eight more optional columns and eight copies of that
    function would have been the wrong answer.

    An absent column is reported ONCE, at warning, naming what is lost. Silently
    dropping a field is how atlas_live_zones went a month without a row.
    """
    if col not in _TRADES_COLS:
        try:
            r = requests.get(
                f"{SUPABASE_URL}/rest/v1/atlas_trades?select={col}&limit=1",
                headers=_headers(), timeout=10)
            _TRADES_COLS[col] = r.status_code in (200, 206)
            if not _TRADES_COLS[col]:
                log.warning(f"atlas_trades.{col} does not exist — reserving "
                            f"without it. {why}".rstrip())
        except Exception as e:
            log.warning(f"could not probe atlas_trades.{col} ({e}) — "
                        f"reserving without it")
            _TRADES_COLS[col] = False
    return _TRADES_COLS[col]


def _trades_has_product() -> bool:
    """Kept as a named call site: exits dispatch on this column, so its absence is
    a different kind of problem from a missing analytical field."""
    return _trades_has("product",
                       "Apply migrations/PENDING_atlas_trades_product.sql; "
                       "exits dispatch on this column.")


def _reserve_intent(intent: dict) -> tuple:
    """
    Commit a PENDING row BEFORE any order exists. -> (row_id, status_code, detail)

    row_id is None on failure, and then no order may be placed: the whole
    guarantee is that `place_order` is unreachable without a committed row.
    status_code is carried out so the breaker can tell a deterministic 4xx from
    a transient 5xx -- the difference between halting now and retrying.

    A LOST RESPONSE IS TREATED AS FAILURE. If the insert actually landed we
    leave a stray PENDING row, which over-blocks one symbol until reconcile
    clears it. The opposite mistake -- assuming it landed and placing an order
    against a row that does not exist -- is the one this function exists to
    make impossible.
    """
    rec = {
        "symbol": intent["symbol"], "direction": intent["direction"],
        "entry_price": intent["entry_price"], "qty": intent["qty"],
        "stop_price": intent.get("stop_price"),
        "status": "PENDING",
        "entry_date": datetime.now(IST).date().isoformat(),
        "agent_mode": "LIVE",
        "setup_name": intent.get("setup_name", ""),
        "session": intent.get("session", ""),
        "score": intent.get("score", 0),
        "grade": intent.get("grade", ""),
        "sector": intent.get("sector", ""),
        "zone_source": intent.get("zone_source", ""),
        # LOAD-BEARING FOR EXITS, and it was never written. exits.protect()
        # dispatches on product -- GTT legs for a CNC long, an SL (stop-loss
        # LIMIT) regular order for an MIS short -- and squareoff_mis() filters on
        # it for the MIS_EXIT_TIME timer. A row without it is a position ATLAS
        # cannot decide how to close.
        #
        # Sent conditionally: the column arrives with
        # PENDING_atlas_trades_product.sql, and until that is applied PostgREST
        # rejects the whole INSERT for one unknown key. A reserve that fails takes
        # the entry with it, so this must not be the thing that stops a trade.
        **({"product": intent["product"]} if _trades_has_product() else {}),
        # THE SIZING RECORD. Same conditional pattern and the same reason: one
        # unknown key would reject the insert and take the entry with it.
        **({"notional": intent.get("notional")}
           if _trades_has("notional", "realised position size is not recorded.")
           else {}),
        **({"notional_pct": intent.get("notional_pct")}
           if _trades_has("notional_pct", "size against target is not recorded.")
           else {}),
        **({"target_notional": intent.get("target_notional")}
           if _trades_has("target_notional") else {}),
        **({"oversized": intent.get("oversized", False)}
           if _trades_has("oversized",
                          "forced single-share trades are not flagged.") else {}),
        **({"sizing_basis": intent.get("sizing_basis")}
           if _trades_has("sizing_basis",
                          "the two sizing regimes will be indistinguishable.")
           else {}),
        **({"risk_inr": intent.get("risk_actual")}
           if _trades_has("risk_inr") else {}),
        # BOTH STOPS, WHERE THE USED ONE CAME FROM, AND THE KIND.
        # atlas_trades_derived_stop applied 2026-10-16. These were set on the
        # intent from the day derive() was wired in and never reached the row:
        # _reserve_intent builds its own `rec` and only sends keys it names, so
        # an intent field that is not listed here is silently dropped. Four
        # columns of the record that explains every candidate entry were
        # missing for exactly that reason.
        #
        # published_stop is the one that matters most. A derived stop that gets
        # hit is only interpretable against the published stop it replaced --
        # without both, the record cannot say whether deriving helped or hurt,
        # and that comparison is the whole reason candidates are enterable.
        **({"published_stop": intent.get("published_stop")}
           if _trades_has("published_stop",
                          "the stop ATLAS was handed is not recorded, so a "
                          "derived stop cannot be compared to it.") else {}),
        **({"stop_source": intent.get("stop_source")}
           if _trades_has("stop_source",
                          "a structural stop will be indistinguishable from a "
                          "clamp to the band edge.") else {}),
        **({"stop_pct_used": intent.get("stop_pct_used")}
           if _trades_has("stop_pct_used") else {}),
        **({"publication_kind": intent.get("publication_kind")}
           if _trades_has("publication_kind",
                          "signal and candidate entries will not be separable "
                          "in the record.") else {}),
        "notes": (f"RESERVED before order placement — stop Rs"
                  f"{intent.get('stop_price', 0)} | risk Rs"
                  f"{intent.get('risk_actual', 0):,.0f}"),
    }
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/atlas_trades",
                          headers=_headers(), json=rec, timeout=10)
    except Exception as e:
        log.error(f"RESERVE FAILED ({rec['symbol']}): {type(e).__name__}: {e}")
        return None, None, f"{type(e).__name__}: {e}"

    if r.status_code not in (200, 201):
        log.error(f"RESERVE REJECTED ({rec['symbol']}): HTTP {r.status_code} "
                  f"{r.text[:200]}")
        log.error(f"  payload: {rec}")
        return None, r.status_code, f"HTTP {r.status_code} {r.text[:150]}"

    try:
        row_id = r.json()[0]["id"]
    except Exception as e:
        # Written, but we cannot name it -- so we cannot join an order to it
        # and must not place one. Reconcile will find the orphan row.
        log.error(f"RESERVE returned no id ({rec['symbol']}): {e} {r.text[:200]}")
        return None, r.status_code, f"insert returned no id: {r.text[:120]}"

    log.info(f"reserved trade {row_id} for {rec['symbol']} {rec['direction']} "
             f"(PENDING — blocks Gate 3b until resolved)")
    return row_id, None, ""


def _patch_trade(row_id, patch: dict, what: str) -> bool:
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/atlas_trades?id=eq.{row_id}",
            headers=_headers(), json=patch, timeout=10)
    except Exception as e:
        log.error(f"{what} failed for trade {row_id}: {type(e).__name__}: {e}")
        return False
    if r.status_code not in (200, 204):
        log.error(f"{what} rejected for trade {row_id}: HTTP {r.status_code} "
                  f"{r.text[:200]}")
        return False
    return True


def _complete_intent(row_id, intent: dict) -> bool:
    """PENDING -> OPEN, once the order is confirmed placed.

    entry_price HERE IS THE FILL. enter_trade() overwrites
    intent["entry_price"] with the broker's average before this is called, so
    the row records what was actually paid rather than the quote the order was
    sized from. Everything derived from it -- stop_pct_used, risk_inr, the
    R basis -- is recomputed against the fill at the same moment and written
    below. The stop and target prices are NOT recomputed: by ruling they stay
    exactly where ATLAS placed them.
    """
    ok = _patch_trade(row_id, {
        "status": "OPEN",
        "order_id": intent.get("order_id"),
        "entry_price": intent.get("entry_price"),
        "qty": intent.get("qty"),
        # EACH CONDITIONAL, same reason as the reserve: PostgREST rejects the
        # whole PATCH for one unknown key, and a rejected completion leaves a
        # live position recorded as PENDING.
        **({"stop_pct_used": intent.get("stop_pct_used")}
           if _trades_has("stop_pct_used") else {}),
        **({"risk_inr": intent.get("risk_actual")}
           if _trades_has("risk_inr") else {}),
        **({"target_price": intent.get("target_price")}
           if _trades_has("target_price") else {}),
        **({"exit_legs": intent.get("exit_legs") or {}}
           if _trades_has("exit_legs") else {}),
        **({"exit_mechanism": intent.get("exit_mechanism")}
           if _trades_has("exit_mechanism") else {}),
        **({"stop_confirmed": intent.get("stop_confirmed")}
           if _trades_has("stop_confirmed",
                          "the record cannot say whether the stop is live at "
                          "the broker.") else {}),
        **({"target_confirmed": intent.get("target_confirmed")}
           if _trades_has("target_confirmed") else {}),
        "notes": (f"MANUAL RISK REQUIRED - place SL at Rs"
                  f"{intent.get('stop_price', 0)} | Order "
                  f"{intent.get('order_id', '')} | risk Rs"
                  f"{intent.get('risk_actual', 0):,.0f} | notional Rs"
                  f"{intent.get('notional', 0):,.0f}"),
    }, "complete")
    if not ok:
        # The order is live and the row still says PENDING. It keeps blocking
        # the symbol, so this is not a duplicate risk -- but the ledger is
        # knowingly wrong, and the breaker halts on it.
        _alert_unrecorded(
            {"symbol": intent.get("symbol"), "status": "PENDING",
             "qty": intent.get("qty"), "entry_price": intent.get("entry_price"),
             "stop_price": intent.get("stop_price"), "agent_mode": "LIVE"},
            f"order placed but trade {row_id} could not be promoted to OPEN",
            intent.get("order_id"))
        breaker.record_ledger_write(False, None, f"trade {row_id}",
                                    phase="complete")
    return ok


def _release_intent(row_id, reason: str) -> bool:
    """No order exists. Free the symbol rather than blocking it all session."""
    ok = _patch_trade(row_id, {
        "status": "CANCELLED",
        "notes": f"released — order refused before placement: {reason[:200]}",
    }, "release")
    if not ok:
        log.error(f"trade {row_id} could not be released and will keep "
                  f"blocking its symbol until reconcile runs")
    return ok


def _mark_indeterminate(row_id, reason: str) -> bool:
    """Order outcome unknown. Stay PENDING; only the broker can settle it."""
    return _patch_trade(row_id, {
        "notes": (f"ORDER OUTCOME UNKNOWN — left PENDING deliberately. "
                  f"reconcile must check the broker for tag ATLAS:{row_id}. "
                  f"{reason[:200]}"),
    }, "mark-indeterminate")


def _log_intent(intent: dict, shadow: bool, gtt: bool = False) -> bool:
    """Record the entry. Returns True if the row was actually written.

    status: SHADOW | GTT_PENDING (trigger resting) | OPEN (filled).
    """
    rec = {
        "symbol": intent["symbol"], "direction": intent["direction"],
        "entry_price": intent["entry_price"], "qty": intent["qty"],
        "stop_price": intent.get("stop_price"),
        "gtt_trigger_id": intent.get("gtt_trigger_id"),
        "trigger_price": intent.get("trigger_price"),
        "status": "SHADOW" if shadow else ("GTT_PENDING" if gtt else "OPEN"),
        "entry_date": datetime.now(IST).date().isoformat(),
        "agent_mode": "SHADOW" if shadow else "LIVE",
        "setup_name": intent.get("setup_name", ""),
        "session": intent.get("session", ""),
        "score": intent.get("score", 0),
        "grade": intent.get("grade", ""),
        "sector": intent.get("sector", ""),
        "zone_source": intent.get("zone_source", ""),
        "notes": (
            f"SHADOW -- no order placed | stop Rs{intent.get('stop_price',0)} "
            f"({intent.get('stop_pct',0)}%) | risk Rs{intent.get('risk_actual',0):,.0f} "
            f"| notional Rs{intent.get('notional',0):,.0f} | {intent.get('regime','')}"
            if shadow else
            (f"GTT RESTING at Rs{intent.get('trigger_price',0)} -- not filled. "
             f"On fill place SL at Rs{intent.get('stop_price',0)} "
             f"| trigger {intent.get('gtt_trigger_id','')} "
             f"| risk Rs{intent.get('risk_actual',0):,.0f}"
             if gtt else
             f"MANUAL RISK REQUIRED - place SL at Rs{intent.get('stop_price',0)} "
             f"| Order {intent.get('order_id','')} "
             f"| risk Rs{intent.get('risk_actual',0):,.0f} "
             f"| notional Rs{intent.get('notional',0):,.0f}")
        ),
    }
    # CHECK THE RESPONSE. requests.post does not raise on 4xx, and this used to
    # catch exceptions only -- so a PostgREST rejection was discarded in
    # silence. gtt_trigger_id and trigger_price did not exist as columns, so
    # every GTT entry was rejected with 400 PGRST204 and produced no row, no
    # warning and no traceback. GTT 331263278 (GRASIM, 2026-08-11) was really
    # placed and left unrecorded that way.
    #
    # A failure here is NOT cosmetic. By this point the order may already exist
    # at the broker, and an unrecorded order is invisible to the funds check,
    # the dashboard and the report -- so ATLAS would size its next entry as if
    # that cash were free. Shout, with enough detail to reconstruct the row by
    # hand.
    label = f"{rec['symbol']} {rec['status']}"
    try:
        r = requests.post(f"{SUPABASE_URL}/rest/v1/atlas_trades",
                          headers=_headers(), json=rec, timeout=10)
    except Exception as e:
        log.error(f"INTENT LOG FAILED ({label}): {type(e).__name__}: {e}")
        _alert_unrecorded(rec, f"{type(e).__name__}: {e}", intent.get("order_id"))
        return False

    if r.status_code not in (200, 201, 204):
        log.error(f"INTENT LOG REJECTED ({label}): HTTP {r.status_code} "
                  f"{r.text[:200]}")
        log.error(f"  payload: {rec}")
        _alert_unrecorded(rec, f"HTTP {r.status_code} {r.text[:150]}",
                          intent.get("order_id"))
        return False
    return True


def _alert_unrecorded(rec: dict, why: str, order_id=None):
    """Tell the operator a real order may exist with no database row.

    Only fires for LIVE records -- a SHADOW row failing to write costs nothing
    but a gap in the log.
    """
    if rec.get("agent_mode") != "LIVE":
        return
    try:
        from atlas.reporting.telegram import send
        send(
            "🚨 <b>ORDER NOT RECORDED</b>\n"
            f"<b>{rec.get('symbol')}</b> {rec.get('status')}\n"
            f"trigger id: {rec.get('gtt_trigger_id') or '—'}\n"
            f"order id:   {order_id or '—'}\n"
            f"qty {rec.get('qty')} @ Rs{rec.get('entry_price')} · "
            f"stop Rs{rec.get('stop_price')}\n\n"
            f"The order may be LIVE at the broker with no atlas_trades row.\n"
            f"Check Kite and add the row by hand.\n\n"
            f"<code>{why}</code>"
        )
    except Exception as e:                 # never let alerting mask the failure
        log.error(f"could not send unrecorded-order alert: {e}")
