"""
ATLAS Entry Logic -- Accumulation Architecture (Phase: TRAINING)
=================================================================
Agent IDENTIFIES and ENTERS. No SL, no target, no exit orders. Exits manual.

GATE STACK
  0. Structural stop present      -- required for risk-based sizing
  0b. Fundamentals (Tier 1 only)  -- deterioration veto, from a cache, no
                                     network. Tier 2 is measured and recorded
                                     on the verdict but NEVER blocks.
                                     BLOCKED_NO_FUNDAMENTALS = the cache is
                                     missing or stale, so nothing was assessed.
                                     SKIPPED_FUNDAMENTALS  = assessed, vetoed.
                                     The split matters: a cache that has not
                                     been built yet must not read as a market
                                     that has turned.
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
    BLOCKING_STATUSES, MAX_CONCURRENT_POSITIONS, MAX_RISK_PER_TRADE,
    ENFORCE_ENTRY_RANGE, OPENING_RANGE_GATE_APPLIES_TO,
    ALLOW_SHORT_ENTRIES, ALLOW_LONG_ENTRIES,
    DEFAULT_ON_UNKNOWN_REGIME,
)
from atlas.risk.position_sizing import size_by_risk
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.risk import breaker
from atlas.risk.funds import can_afford
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

        advancing > declining on the day   AND   Nifty above its 20DMA

    Both are same-day facts about participation, which is what separates a
    market that is merely ABOVE its long averages from one that is being bought
    today. The regime half is structural and slow; this half is not, and a bull
    structure on a day of negative breadth is the case this exists to stop.

    Missing inputs are NOT a pass. build_market writes nifty_20dma as NaN for
    the first 20 rows of any series, and the sector_heatmap fallback cannot
    supply these at all, so "unknown" has to mean no -- otherwise the gate opens
    precisely when it knows least.
    """
    state, reason = sentiment_state(ctx)
    return state == "bullish", reason


def sentiment_state(ctx: dict) -> tuple:
    """
    (state, reason) where state is bullish / bearish / mixed / unknown.

    THREE-VALUED, because the direction matrix now needs a bearish reading and
    not merely the absence of a bullish one. Both legs must agree:

        bullish   advances > declines  AND  Nifty above its 20DMA
        bearish   declines > advances  AND  Nifty below its 20DMA
        mixed     the two legs disagree -- breadth says one thing and the
                  index the other. Not a direction; ATLAS stays in cash.
        unknown   an input is missing. Never a direction either.

    "mixed" is deliberately NOT folded into bearish. Under the old boolean,
    anything that was not bullish read as "no long", which was safe because the
    only consequence was inaction. Now the same reading would be a licence to
    SHORT, and "breadth is positive but the index is below its 20DMA" is not a
    bearish market -- it is an unclear one.
    """
    adv, dec = ctx.get("advance_count"), ctx.get("decline_count")
    close, ma20 = ctx.get("nifty_close"), ctx.get("nifty_20dma")

    missing = [n for n, v in (("advance_count", adv), ("decline_count", dec),
                              ("nifty_close", close), ("nifty_20dma", ma20))
               if v is None]
    if missing:
        return "unknown", (f"sentiment unevaluable from {ctx.get('source', '?')} "
                           f"-- missing {', '.join(missing)}")

    breadth_up = adv > dec
    above_20   = close > ma20
    legs = (f"breadth {adv:.0f}/{dec:.0f}, Nifty {close:.0f} "
            f"{'above' if above_20 else 'below'} 20DMA {ma20:.0f}")
    if breadth_up and above_20:
        return "bullish", legs
    if (not breadth_up) and (not above_20):
        return "bearish", legs
    return "mixed", (f"{legs} -- breadth and index disagree")


def allowed_side(ctx: dict) -> tuple:
    """
    (side, reason, blocked_by) -- the ONE direction ATLAS may open right now, or
    None for cash. side is "LONG", "SHORT" or None.

    THE MATRIX, and it is a matrix rather than a pair of independent tests
    because regime and sentiment can disagree and the disagreement is itself an
    answer:

        regime    sentiment   ->  side
        bull      bullish         LONG
        bear      bearish         SHORT
        sideways  bullish         LONG     follow sentiment
        sideways  bearish         SHORT    follow sentiment
        bull      bearish         cash     structure and participation conflict
        bear      bullish         cash     same, other way round
        any       mixed           cash     breadth and index disagree
        unknown   any             cash     DEFAULT_ON_UNKNOWN_REGIME
        any       unknown         cash     an unreadable input is never a side

    ONE SIDE AT A TIME, never both. The matrix returns a single direction, so a
    long and a short can never be opened on the same evaluation cycle. Positions
    already held are NOT affected by a change here -- see the note in
    enter_trade. This function only decides what may be OPENED.

    Replaces the previous shape, where LONG required bull-and-bullish and SHORT
    was unreachable by construction. Shorts are now reachable, which is why
    ALLOW_SHORT_ENTRIES and the extreme-bearish requirement had to be revisited
    rather than left as flags that happened to block.
    """
    regime = str(ctx.get("regime", "unknown")).lower()
    if regime not in ("bull", "bear", "sideways"):
        return (None, f"regime {regime}/stale ({ctx.get('source','?')}) "
                      f"-> {DEFAULT_ON_UNKNOWN_REGIME}", "REGIME")

    sent, sreason = sentiment_state(ctx)
    if sent == "unknown":
        return None, sreason, "SENTIMENT"
    if sent == "mixed":
        return None, f"sentiment mixed: {sreason}", "SENTIMENT"

    if regime == "bull" and sent == "bullish":
        return "LONG", f"bull regime; {sreason}", None
    if regime == "bear" and sent == "bearish":
        return "SHORT", f"bear regime; {sreason}", None
    if regime == "sideways":
        side = "LONG" if sent == "bullish" else "SHORT"
        return side, f"sideways regime following {sent} sentiment; {sreason}", None

    # bull+bearish or bear+bullish: structure and participation point opposite
    # ways. Neither side is taken -- this is the case the AND-gate existed for.
    #
    # blocked_by is CONFLICT, not REGIME or SENTIMENT. Attributing a disagreement
    # to either half would be arbitrary and would destroy the thing blocked_by
    # exists for: counting WHY ATLAS sat out, from atlas_entry_log, without
    # parsing prose. "The trend is there and nobody is buying it" is a distinct
    # market state from "there is no trend", and over a long flat stretch the
    # split between them is the interesting number.
    return (None, f"{regime} regime against {sent} sentiment -- no side "
                  f"({sreason})", "CONFLICT")


def regime_allows_side(ctx: dict, direction: str) -> tuple:
    """
    (ok, reason, blocked_by) for ONE direction. Thin wrapper over allowed_side so
    the matrix lives in exactly one place.
    """
    d = (direction or "").upper()
    if d not in ("LONG", "SHORT"):
        return False, f"unknown direction {d}", "CONFIG"

    if d == "SHORT" and not ALLOW_SHORT_ENTRIES:
        return (False, "short entries are disabled (ALLOW_SHORT_ENTRIES)",
                "CONFIG")
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
# EXPOSURE
# ─────────────────────────────────────────────────────────────────────
# get_exposure() is gone. It summed entry_price*qty over our own atlas_trades
# rows to enforce MAX_CAPITAL_DEPLOYED -- a ledger that duplicated the broker's
# balance and could drift from it without anything reconciling the two. Both the
# ledger and the cap are removed; available funds come from kite.margins() at
# decision time via atlas/risk/funds.py, which also nets off resting GTTs.


def get_open_position_count() -> tuple:
    """
    (readable, count) of positions occupying a slot right now.

    BLOCKING_STATUSES, MAX_CONCURRENT_POSITIONS, MAX_RISK_PER_TRADE, not just OPEN: a PENDING row is a fill we could not confirm,
    so it may be a live position. A position that MIGHT exist occupies a slot --
    assuming otherwise is how a ceiling gets exceeded by exactly the positions
    nobody is sure about.

    FAILS CLOSED. An unreadable ledger returns readable=False and the caller
    refuses the entry. Returning 0 would read as "nothing is open", which is the
    one answer that turns the ceiling off at the moment it is least safe to.
    """
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/atlas_trades"
            f"?status=in.({','.join(BLOCKING_STATUSES)})&select=id",
            headers={**_headers(), "Prefer": "count=exact"}, timeout=15)
        if r.status_code != 200:
            log.error(f"open position count failed: HTTP {r.status_code}")
            return False, 0
        return True, len(r.json() or [])
    except Exception as e:
        log.error(f"open position count failed: {e}")
        return False, 0


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
    the caller must block on that. This is deliberately stricter than Gate 3
    above, which returns 0 on failure: an unreadable book there costs at most
    one extra trade against the daily cap, whereas here it costs a second
    position stacked behind a stop already sized for one.
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


def check_entry_range(direction: str, ltp: float, lo_in: float, hi_in: float) -> tuple:
    """Enter ONLY if LTP is inside the zone band."""
    if not ENFORCE_ENTRY_RANGE:
        return True, "range check off"
    if not lo_in or not hi_in or lo_in <= 0 or hi_in <= 0:
        return False, "no entry band defined"
    lo, hi = min(lo_in, hi_in), max(lo_in, hi_in)
    if lo <= ltp <= hi:
        return True, f"LTP Rs{ltp:.1f} inside zone Rs{lo:.1f}-Rs{hi:.1f}"
    return False, f"LTP Rs{ltp:.1f} outside zone Rs{lo:.1f}-Rs{hi:.1f}"


# ─────────────────────────────────────────────────────────────────────
# ENTRY
# ─────────────────────────────────────────────────────────────────────

def enter_trade(signal: dict) -> dict:
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
    if halted:
        return {"status": "BLOCKED_HALTED", "reason": why}

    # GATE 0 -- structural stop mandatory
    if stop_price <= 0:
        return {"status": "REJECTED_NO_STOP",
                "reason": "no structural stop -- cannot size by risk"}

    # GATE 0b -- FUNDAMENTALS. Tier 1 only; Tier 2 measures and never blocks.
    #
    # Cache-only, no network, so it is free and sits ahead of every gate that
    # costs a request. Entry-only by construction: enter_trade is never on an
    # exit path, so a name that deteriorates while held is a stop-loss question,
    # not this gate's.
    #
    # TWO DISTINCT STATUSES, DELIBERATELY. gate() fails closed, so before the
    # cache is built it blocks EVERYTHING -- correct, and indistinguishable from
    # a deteriorated universe unless it says so. BLOCKED_NO_FUNDAMENTALS means
    # the cache is missing, unreadable or stale and NOTHING has been assessed;
    # SKIPPED_FUNDAMENTALS means this symbol was assessed and Tier 1 vetoed it.
    # Folded into one status, a missing file would read as the market turning.
    try:
        from engine.fundamentals import (load_cache_cached, cache_health,
                                         gate as fundamentals_gate)
        fcache = load_cache_cached()
        cache_ok, cache_why = cache_health(fcache)
        if not cache_ok:
            return {"status": "BLOCKED_NO_FUNDAMENTALS", "reason": cache_why,
                    "infrastructure": True}
        f_ok, f_why = fundamentals_gate(symbol, cache=fcache)
        if not f_ok:
            return {"status": "SKIPPED_FUNDAMENTALS", "reason": f_why}
    except Exception as e:
        # An exception here is not permission to trade. The layer exists to stop
        # names a technical stop cannot protect against, and a broken gate that
        # defaults to open is worse than no gate, because it looks like one.
        log.error(f"fundamentals gate raised for {symbol}: {e}")
        return {"status": "BLOCKED_NO_FUNDAMENTALS",
                "reason": f"fundamentals gate unavailable ({e}) — failing closed",
                "infrastructure": True}

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

    # GATE 3 -- CONCURRENT EXPOSURE. Not a per-day count; a ceiling on what can
    # be open at once. See MAX_CONCURRENT_POSITIONS: the number is the measured
    # peak of the book plus headroom, so it does not bind on normal behaviour and
    # does bind on a watchlist 45x wider raising the entry rate.
    #
    # Fails closed: an unreadable ledger refuses the entry rather than treating
    # "cannot count" as "nothing is open".
    slots_ok, open_now = get_open_position_count()
    if not slots_ok:
        return {"status": "BLOCKED_NO_LEDGER",
                "reason": "cannot count open positions — refusing to add to an "
                          "exposure I cannot measure"}
    if open_now >= MAX_CONCURRENT_POSITIONS:
        return {"status": "SKIPPED_EXPOSURE",
                "reason": (f"{open_now}/{MAX_CONCURRENT_POSITIONS} positions "
                           f"already open (~Rs{open_now * MAX_RISK_PER_TRADE:,.0f} "
                           f"of risk at stop)")}

    # THE PER-DAY COUNT IS STILL GONE. A count of entries TODAY bounded the
    # number of positions while saying nothing about their size, and refused the
    # fourth good setup for arithmetic.
    #
    # A count was never a risk control -- it bounded the NUMBER of positions while
    # saying nothing about their size, and each one is already bounded to Rs3,000
    # of risk and Rs1,00,000 of notional by the sizing rule. Three entries of
    # Rs3k risk and thirty are different exposures, but the thing that should stop
    # the thirtieth is the absence of funds to pay for it, which Gate 6 reads live
    # from the broker net of resting GTTs.
    #
    # THE CONSEQUENCE, STATED: broker funds are now the ONLY bound on total
    # exposure. There is no maximum simultaneous capital deployed --
    # MAX_CAPITAL_DEPLOYED and get_exposure() were removed when capital tracking
    # went. get_today_entry_count() is kept and still logged for the daily report,
    # because how many entries a day produced is worth knowing even when nothing
    # limits it.

    # GATE 3b -- already holding this symbol. HARD SKIP, never a scale-in.
    #
    # A zone that survives two sessions re-publishes, so the same symbol
    # reappears in consecutive batches -- DELHIVERY was in both the 20 Aug and
    # 21 Aug batches with an identical setup and an identical stop, and nothing
    # below would have noticed. Gate 3 counts TODAY's entries only, so a
    # position opened any earlier day was invisible; kill_switch dropped its
    # max-open-positions check; and market_open.dedupe() dedupes within a batch,
    # not against the book.
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
    in_range, range_reason = check_entry_range(direction, ltp, entry_low, entry_high)
    if not in_range:
        return {"status": "SKIPPED_RANGE", "reason": range_reason}

    # GATE 5 -- sizing. Rs3,000 risk / Rs1,00,000 notional, qty a multiple of 5.
    sizing = size_by_risk(entry_price=ltp, stop_price=stop_price, direction=direction)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    # GATE 6 -- live broker funds, net of resting GTTs. FAIL CLOSED: can_afford
    # returns False both when funds are short and when they cannot be read.
    # THE BROKER'S MARGIN, NOT OUR ESTIMATE. sizing["capital_required"] is
    # notional for a CNC long and notional x SHORT_MARGIN_PCT_ESTIMATE (0.20) for
    # an MIS short. With no cap on the number of trades, that 0.20 is the only
    # thing deciding how many shorts fit: five of them occupy one long's capital
    # at 20%, and if the true requirement is 30% the fifth is unfunded. So ask.
    #
    # A failed lookup falls back to the estimate and says so. Refusing every trade
    # because the margin endpoint is unreachable would be a different failure, not
    # a safer one -- and can_afford still fails closed on unreadable FUNDS, which
    # is the check that actually protects the account.
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

    funds_ok, funds_reason, funds_detail = can_afford(need)
    if not funds_ok:
        status = ("BLOCKED_NO_FUNDS_DATA"
                  if funds_detail.get("data_available") is False else "SKIPPED_FUNDS")
        return {"status": status, "reason": funds_reason}

    # GATE 7 -- kill switch
    signal["capital_required"] = need
    if not kill_switch_check(signal):
        return {"status": "BLOCKED_KILLSWITCH", "reason": "kill switch active"}

    intent = _build_intent(signal, symbol, direction, ltp, sizing, ctx)

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
    order = place_order(symbol=symbol, direction=direction, qty=sizing["qty"],
                        order_type="MARKET", tag=f"ATLAS:{row_id}",
                        product=sizing["product"])

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
    # BLOCKS here. A MARKET order fills in well under a second; the wait is
    # bounded and its timeout is treated as "a position may exist", never as
    # "no position".
    # ENABLE_EXIT_MANAGEMENT IS THE SWITCH THAT MAKES ATLAS AUTONOMOUS.
    #
    # False is the behaviour ATLAS has always had: it opens a position and leaves
    # it to a human. That is not a degraded mode, it is the status quo, and it is
    # the default so that deploying this code does not silently start managing
    # money. True is the deliberate act.
    from atlas.config import ENABLE_EXIT_MANAGEMENT
    if not ENABLE_EXIT_MANAGEMENT:
        log.warning(f"{symbol}: ENTERED with NO automatic stop — "
                    f"ENABLE_EXIT_MANAGEMENT is False, so this position is "
                    f"managed by hand")
        return {"status": "ENTERED", "recorded": recorded,
                "exit_managed": False, **intent}

    from atlas.execution import exits as X

    fill = X.await_fill(intent["order_id"])
    intent["fill"] = fill.get("outcome")

    if fill["outcome"] == "REJECTED":
        # Nothing was bought or sold. Release rather than leave a phantom row.
        _release_intent(row_id, f"order rejected: {fill.get('reason')}")
        breaker.record_order_reject(symbol, str(fill.get("reason")))
        return {"status": "ORDER_FAILED", "reason": fill.get("reason")}

    if fill["outcome"] == "INDETERMINATE":
        # The order may still fill. Nothing may be assumed about the position, so
        # no stop can be sized and none is placed -- the row stays PENDING and
        # reconcile owns it. This is the one path that can leave an unprotected
        # position, and it does so because acting on an unknown quantity is worse.
        log.error(f"{symbol}: fill INDETERMINATE ({fill.get('reason')}) — trade "
                  f"{row_id} left PENDING, NOT protected. reconcile owns it.")
        _mark_indeterminate(row_id, f"fill unconfirmed: {fill.get('reason')}")
        _alert("FILL UNCONFIRMED",
               f"{symbol} {direction}: {fill.get('reason')}. No stop placed. "
               f"Trade {row_id} is PENDING and may be a live position.")
        return {"status": "FILL_UNCONFIRMED", "trade_id": row_id,
                "reason": fill.get("reason"), **intent}

    filled_qty = int(fill["filled_qty"])
    avg_price  = float(fill["avg_price"])
    intent["fill_price"] = round(avg_price, 2)
    intent["filled_qty"] = filled_qty

    prot = X.protect(symbol=symbol, direction=direction, qty=filled_qty,
                     product=sizing["product"], fill_price=avg_price,
                     stop_price=sizing["stop_price"])
    intent["exit_legs"] = prot.get("legs") or {}
    intent["exit_mechanism"] = prot.get("mechanism", "")
    intent["target_price"] = prot.get("target")

    if not prot.get("ok"):
        # THE STOP COULD NOT BE PLACED. Exit at market, now. An unprotected open
        # position is an unbounded loss in a system nobody is watching; the bad
        # exit costs a known amount once. Not a retry -- retrying is how a minute
        # becomes an afternoon.
        ex = X.emergency_exit(symbol, direction, filled_qty, sizing["product"],
                              why=f"stop placement failed: {prot.get('reason')}")
        if ex.get("ok"):
            _alert("EXITED UNPROTECTED POSITION",
                   f"{symbol} {direction} {filled_qty}: stop could not be placed "
                   f"({prot.get('reason')}), exited at market.")
            return {"status": "EXITED_NO_STOP", "trade_id": row_id,
                    "reason": prot.get("reason"), **intent}
        # The exit failed too. Open, unprotected, and the broker is refusing
        # orders: halt so nothing else is opened into the same condition.
        breaker.halt("OPEN_UNPROTECTED",
                     f"{symbol}: open with no stop — placement failed "
                     f"({prot.get('reason')}) AND the market exit failed "
                     f"({ex.get('reason')})")
        _alert("OPEN AND UNPROTECTED",
               f"{symbol} {direction} {filled_qty} is OPEN with no stop and the "
               f"market exit failed. {ex.get('reason')}. Entries halted.")
        return {"status": "OPEN_UNPROTECTED", "trade_id": row_id,
                "reason": ex.get("reason"), **intent}

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
    """PENDING -> OPEN, once the order is confirmed placed."""
    ok = _patch_trade(row_id, {
        "status": "OPEN",
        "order_id": intent.get("order_id"),
        "entry_price": intent.get("entry_price"),
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
