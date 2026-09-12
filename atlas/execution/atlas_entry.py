"""
ATLAS Entry Logic -- Accumulation Architecture (Phase: TRAINING)
=================================================================
Agent IDENTIFIES and ENTERS. No SL, no target, no exit orders. Exits manual.

GATE STACK
  0. Structural stop present      -- required for risk-based sizing
  1. Regime AND sentiment         -- both must hold or ATLAS stays in cash.
                                     REGIME:    close>200DMA and 50DMA>200DMA
                                     SENTIMENT: advances>declines today and
                                                Nifty>20DMA
                                     Blocked side is reported as
                                     SKIPPED_REGIME or SKIPPED_SENTIMENT.
  2. Opening range                -- SHORTS ONLY (intraday directional check)
  3. New entries today            -- MAX_TRADES_PER_DAY
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
two. The binding constraints are now MAX_TRADES_PER_DAY, one-position-per-symbol
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
    MAX_TRADES_PER_DAY, BLOCKING_STATUSES,
    ENFORCE_ENTRY_RANGE, OPENING_RANGE_GATE_APPLIES_TO,
    ALLOW_LONG_IN_BULLISH,
    ALLOW_SHORT_IN_BEARISH, REQUIRE_EXTREME_BEARISH_FOR_SHORTS,
    DEFAULT_ON_UNKNOWN_REGIME,
)
from atlas.risk.position_sizing import size_by_risk
from atlas.risk.kill_switch import check as kill_switch_check
from atlas.risk import breaker
from atlas.risk.funds import can_afford
from atlas.execution.broker import place_order, get_ltp

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
    adv, dec = ctx.get("advance_count"), ctx.get("decline_count")
    close, ma20 = ctx.get("nifty_close"), ctx.get("nifty_20dma")

    missing = [n for n, v in (("advance_count", adv), ("decline_count", dec),
                              ("nifty_close", close), ("nifty_20dma", ma20))
               if v is None]
    if missing:
        return False, (f"sentiment unevaluable from {ctx.get('source', '?')} "
                       f"-- missing {', '.join(missing)}")

    breadth_ok = adv > dec
    above_20   = close > ma20
    if not breadth_ok and not above_20:
        return False, (f"breadth negative ({adv:.0f} adv / {dec:.0f} dec) "
                       f"AND Nifty {close:.0f} below 20DMA {ma20:.0f}")
    if not breadth_ok:
        return False, f"breadth negative ({adv:.0f} adv / {dec:.0f} dec)"
    if not above_20:
        return False, f"Nifty {close:.0f} below 20DMA {ma20:.0f}"
    return True, (f"breadth {adv:.0f}/{dec:.0f}, Nifty {close:.0f} "
                  f"above 20DMA {ma20:.0f}")


def regime_allows_side(ctx: dict, direction: str) -> tuple:
    """
    ATLAS trades only when BOTH hold. -> (ok, reason, blocked_by)

        REGIME     close above 200DMA AND 50DMA above 200DMA
        SENTIMENT  advancing > declining today AND Nifty above its 20DMA

    Either fails, stay in cash.

    REGIME IS market_regime == "bull" BY DEFINITION. build_market._classify
    sets bull on exactly `above200 & stacked`, which is the same pair of
    conditions, so this reads the classification rather than recomputing it
    from the DMAs -- one definition, in the module that owns the series.

    SIDEWAYS NO LONGER QUALIFIES, and that is the substance of this change.
    Accumulation used to run in bull AND sideways on the argument that a quiet
    market is the setup rather than a reason to stand aside. The regime has
    been sideways throughout the live history, so on this rule ATLAS takes no
    trades over that period. Long stretches with no entries are the intended
    behaviour of this gate, not a fault in it.

    `blocked_by` is returned separately from the prose so the caller can put it
    in atlas_entry_log.status and the REGIME / SENTIMENT split stays countable
    rather than needing the reason text parsed.
    """
    d = direction.upper()
    regime = ctx.get("regime", "unknown")

    if regime == "unknown":
        return (False, f"regime unknown/stale ({ctx.get('source','?')}) "
                       f"-> {DEFAULT_ON_UNKNOWN_REGIME}", "REGIME")

    # ── SENTIMENT ────────────────────────────────────────────────
    # Checked for both sides and before the per-side logic: it is a statement
    # about whether the market is being bought today, and it does not become
    # truer for one direction than the other.
    sent_ok, sent_why = sentiment_ok(ctx)

    if d == "LONG":
        if regime != "bull":
            return (False, f"regime {regime} -- long needs bull "
                           f"(close>200DMA and 50DMA>200DMA)", "REGIME")
        if not ALLOW_LONG_IN_BULLISH:
            return False, "longs disabled", "CONFIG"
        if not sent_ok:
            return False, sent_why, "SENTIMENT"
        return True, f"bull regime; {sent_why}", None

    if d == "SHORT":
        # NOTE: with REGIME required to be bull, and extreme_bearish requiring
        # close < 200DMA - 3%, these two can never hold at once -- so a hedge
        # short is unreachable under this gate. That follows from "ATLAS trades
        # only when BOTH hold" applied to every entry, and is left as specified
        # rather than quietly carved out. Exempting shorts is a one-line change
        # here if that is not what was meant.
        if regime != "bull":
            return (False, f"regime {regime} -- entries require bull", "REGIME")
        if REQUIRE_EXTREME_BEARISH_FOR_SHORTS and not ctx.get("extreme_bearish"):
            return (False, "hedge shorts require extreme_bearish "
                           "(200DMA-3%, 50<200DMA, VIX>18)", "REGIME")
        if not ALLOW_SHORT_IN_BEARISH:
            return False, "shorts disabled", "CONFIG"
        if not sent_ok:
            return False, sent_why, "SENTIMENT"
        return True, f"extreme bearish; {sent_why}", None

    return False, f"unknown direction {d}", "CONFIG"


# ─────────────────────────────────────────────────────────────────────
# EXPOSURE
# ─────────────────────────────────────────────────────────────────────
# get_exposure() is gone. It summed entry_price*qty over our own atlas_trades
# rows to enforce MAX_CAPITAL_DEPLOYED -- a ledger that duplicated the broker's
# balance and could drift from it without anything reconciling the two. Both the
# ledger and the cap are removed; available funds come from kite.margins() at
# decision time via atlas/risk/funds.py, which also nets off resting GTTs.


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

    # GATE 1 -- regime AND sentiment. Both must hold or ATLAS stays in cash.
    #
    # The status carries the split so it is countable in atlas_entry_log
    # without parsing prose: SKIPPED_REGIME means the market structure is not
    # bull, SKIPPED_SENTIMENT means it is but today's participation is not.
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

    # GATE 3 -- new entries today. With the capital cap gone this and available
    # broker funds are the only things that stop further entries.
    todays = get_today_entry_count()
    if todays >= MAX_TRADES_PER_DAY:
        return {"status": "SKIPPED_LIMIT",
                "reason": f"entries today {todays}/{MAX_TRADES_PER_DAY}"}

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
    ltp = get_ltp(symbol) or entry_ref
    in_range, range_reason = check_entry_range(direction, ltp, entry_low, entry_high)
    if not in_range:
        return {"status": "SKIPPED_RANGE", "reason": range_reason}

    # GATE 5 -- sizing. Rs3,000 risk / Rs1,00,000 notional, qty a multiple of 5.
    sizing = size_by_risk(entry_price=ltp, stop_price=stop_price, direction=direction)
    if sizing.get("qty", 0) <= 0:
        return {"status": "REJECTED_SIZE", "reason": sizing.get("error", "zero qty")}

    # GATE 6 -- live broker funds, net of resting GTTs. FAIL CLOSED: can_afford
    # returns False both when funds are short and when they cannot be read.
    need = sizing["capital_required"]
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
    # The order IS placed -- that is the truth, so the status stays ENTERED.
    # `recorded` tells the caller whether the row was promoted out of PENDING.
    return {"status": "ENTERED", "recorded": recorded, **intent}


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
