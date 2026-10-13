"""
ATLAS — Agentic Trading & Lifecycle Automation System
Central configuration. All modules import from here.
"""
import os
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# IST, because every date decision in this system is an IST date. The box runs
# Etc/UTC, so date.today() here is the UTC date -- which is the previous day
# between 00:00 and 05:30 IST. The go-live switch below turns on real money by
# date, so it reads the right one.
_IST = timezone(timedelta(hours=5, minutes=30))


def _today_ist() -> date:
    return datetime.now(_IST).date()


ROOT         = Path(__file__).parent.parent
ENGINE_DIR   = ROOT / "engine"
ATLAS_DIR    = ROOT / "atlas"
DATA_DIR     = ROOT / "data"
REPORTS_DIR  = ROOT / "reports"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

UPSTOX_API_KEY    = os.environ.get("UPSTOX_API_KEY", "")
UPSTOX_API_SECRET = os.environ.get("UPSTOX_API_SECRET", "")
UPSTOX_MOBILE     = os.environ.get("UPSTOX_MOBILE", "")
UPSTOX_PIN        = os.environ.get("UPSTOX_PIN", "")
UPSTOX_TOTP       = os.environ.get("UPSTOX_TOTP_SECRET", "")

ZERODHA_API_KEY    = os.environ.get("ZERODHA_API_KEY", "")
ZERODHA_API_SECRET = os.environ.get("ZERODHA_API_SECRET", "")
ZERODHA_USER_ID    = os.environ.get("ZERODHA_USER_ID", "")
ZERODHA_PASSWORD   = os.environ.get("ZERODHA_PASSWORD", "")
ZERODHA_TOTP       = os.environ.get("ZERODHA_TOTP_SECRET", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ═══════════════════════════════════════════════════════════════════
# CAPITAL
# ═══════════════════════════════════════════════════════════════════
# ATLAS does not track capital. There is no allocated pool, no deployed-capital
# ledger, no capital fence, no loss caps and no stored balance.
#
# Those were a second copy of a number the broker already knows, and the copies
# disagreed: atlas_state.capital said Rs1,50,000, this file said Rs3,00,000,
# atlas.html said Rs3,00,000, and the kill switch derived its daily loss cap
# from whichever it happened to read -- enforcing Rs4,500 against a documented
# Rs9,000. Removing the ledger removes the class of bug.
#
# Available funds are read LIVE from kite.margins() at decision time, minus the
# notional of any resting ATLAS GTT triggers. See atlas/risk/funds.py, which
# fails closed: unreadable funds block the trade.
#
# The operator manages the money pool and all exits manually.

# Statuses that represent a LIVE commitment. GTT_PENDING counts: the cash is
# spoken for the moment the trigger rests at the broker, before it fills.
OPEN_STATUSES        = ("OPEN", "GTT_PENDING")

# What BLOCKS a new entry, which is a wider set than what the book HOLDS.
#
# PENDING is written before the order is placed and cleared once the outcome is
# known, so it means "an order for this symbol may exist at the broker right
# now". Gate 3b must treat that as a holding: the whole point of writing the row
# first is that a death between the insert and the fill still leaves something
# the next evaluation can see.
#
# Deliberately NOT folded into OPEN_STATUSES. That tuple answers "what is the
# book?" and feeds the daily report and the funds view, where a two-second
# PENDING row is noise. This one answers "may I enter?" and is used only by
# Gate 3b. The two questions differ and a single tuple for both would silently
# change the report.
BLOCKING_STATUSES    = ("PENDING",) + OPEN_STATUSES

# ACCUMULATION SCREEN -- institutional footprint is QUIET tape, not loud.
# MIN_RVOL = 1.5 previously demanded above-average volume, which is the
# opposite of what accumulation looks like.
MAX_RVOL_ACCUMULATION = 1.0      # relative volume at or below average
MIN_DELIVERY_PCT      = 50.0     # delivery-based buying, not churn
DISCOUNT_MIN_PCT      = 5.0      # at least 5% off the 52-week high
DISCOUNT_MAX_PCT      = 20.0     # but not a broken chart

# --- DEPRECATED. Retained as names so imports do not break. Not used to gate.
MIN_CONVICTION_SCORE = 0         # score is non-predictive; gate removed
ELITE_CONVICTION     = 0
MAX_LIVE_SIGNALS     = 0         # GTT rests at the broker; no live queue
SIGNAL_DECAY_MINUTES = 0         # GTT lifetime is broker-side
MIN_RVOL             = 0.0       # see MAX_RVOL_ACCUMULATION
MIN_RR               = 0.0       # undefined with open targets


# ═══════════════════════════════════════════════════════════════════
# TSL ATLAS TRADING RULES — the complete set. Nothing else gates a trade.
# Phase: TRAINING — agent ENTERS trades only. No auto SL/target/exit.
# ═══════════════════════════════════════════════════════════════════
#
#   1. Rs3,000 risk per trade -> quantity derived from (entry - stop)
#   2. Quantity a multiple of 5
#   3. Rs1,00,000 max notional per trade
#   4. Max 3 new trades per day
#   5. Trade if the broker has available funds; stop if not
#
# There is deliberately no position limit and no capital cap. Rule 5 is the
# binding constraint and it is answered live by the broker, not by a stored
# number -- see atlas/risk/funds.py.

# ══════════════════════════════════════════════════════════════════
# SIZING MODE — ATLAS IS A MEASURING INSTRUMENT, NOT A CAPITAL ALLOCATION
# ══════════════════════════════════════════════════════════════════
#
# Restarted live on 2026-10-06 at a fixed Rs10,000 notional per trade. The record
# stopped in September and the learning loop needs live outcomes; Rs10,000 is small
# enough that a bad month costs a few thousand and large enough that intraday costs
# stay proportionate -- at a 2% stop, costs are 5% of 1R on a short and 11% on a
# delivery long.
#
# RISK IS NO LONGER AN INPUT in this mode. Quantity comes from the notional and the
# risk is whatever the stop implies -- about Rs205 at the median against the old
# Rs3,000 budget. MAX_RISK_PER_TRADE is therefore NOT consulted when
# SIZING_MODE == "fixed_notional", and the sizer does not report a risk_budget,
# because a figure of 3,000 would be false and atlas_trades.risk_inr would inherit it.
SIZING_MODE = "fixed_notional"           # "fixed_notional" | "risk_budget"
FIXED_NOTIONAL_PER_TRADE = 10000.0       # Rs10,000, the TARGET not a ceiling

# Rule 1 — INR at risk if the structural stop is hit. The sizing rule for
# SIZING_MODE == "risk_budget" only; quantity is DERIVED from this and the stop
# distance. Kept because real sizing returns, and because backtest/config.py still
# sizes this way and its basis must not change silently.
MAX_RISK_PER_TRADE = 3000.0

# Rule 2 — quantity must be a multiple of this.
#
# WAS 5, AND THAT WAS A HOUSE RULE, NOT A MARKET CONSTRAINT: Indian cash equity
# trades in single shares for both CNC and MIS. At a Rs10,000 notional a multiple of
# 5 requires 10000/price >= 5, i.e. price <= Rs2,000, which rejected 310 of 1,484
# published signals -- 21% -- for nothing. At 1 the rejection rate is 3.2% and the
# median position lands within Rs3 of target.
QUANTITY_MULTIPLE = 1

# Rule 3 — max notional exposure per trade
# Rule 3 — the per-trade notional ceiling, for SIZING_MODE == "risk_budget".
# Not consulted in fixed_notional mode, where FIXED_NOTIONAL_PER_TRADE is the
# target instead.
MAX_NOTIONAL_PER_TRADE = 100000.0        # ₹1,00,000

# Rule 4 — new entries per day. This is now a real limit rather than a
# secondary guard: with the capital cap gone, it and available broker funds are
# the only things that stop further entries.
# ── HOW FAR IS WORTH WATCHING: NOTHING, ANY MORE ──────────────────
#
# RETIRED with MAX_ENTRY_DIST_PCT on 2026-10-01. Both of these bounded the
# WATCHLIST -- how far from its zone a symbol could sit and still be worth a
# quote each cycle. CANDIDATE_MAX_ATR_MULTIPLE was 1.0 ATR and
# CANDIDATE_MAX_DIST_PCT was 3.0%, and both were measured and defensible for the
# question they answered.
#
# They answer a question that no longer exists. The watchlist is now every symbol
# carrying a valid unmitigated zone, because there is no entry-distance
# requirement to be "too far" from: a setup is taken when price reaches the band,
# and until then the cost of watching it is one quote inside a batched request
# that is already being made for every other symbol. Reach was a proxy for "could
# this fire today", and a bound on it only ever discarded setups that might.
#
# Kept as names at 0.0 so nothing that imports them breaks, and so a reader
# looking for the old behaviour finds this note rather than a missing symbol.
# 0.0 is the absence of a bound here, not a bound of zero -- engine/03b_score
# reads neither.
CANDIDATE_MAX_ATR_MULTIPLE = 0.0
CANDIDATE_MAX_DIST_PCT = 0.0

# ── THERE IS NO EXPOSURE CEILING ──────────────────────────────────
#
# No MAX_CONCURRENT_EXPOSURE, no MAX_CAPITAL_DEPLOYED, no maximum simultaneous
# capital deployed of any kind. BROKER FUNDS ARE THE ONLY BOUND: the operator
# keeps capital available, and ATLAS takes every setup that forms.
#
# A Rs4,00,000 rupee ceiling existed here for one commit. The reasoning for it
# was that capping CAPITAL rather than the per-day entry count bounds risk
# instead of opportunity -- true as far as it goes, but the level made it a cap
# on opportunity anyway. Peak deployed capital over the 20 sessions to
# 2026-09-30 was Rs13,81,517 and the AVERAGE was Rs7,09,526, so Rs4,00,000 sat
# BELOW the average the book had been running: about five concurrent longs. It
# would have started refusing ordinary sixth positions on week one.
#
# It was also the wrong place for the decision. A constant here is a second
# opinion about the operator's own balance -- held locally, unable to see
# deposits or withdrawals, and drifting from the truth the moment either
# happens. atlas/risk/funds.py reads kite.margins() at decision time, net of
# resting GTTs, and Gate 6 refuses what the account cannot pay for. That check
# cannot be stale.
#
# Per-trade bounds are unaffected and still apply to every entry:
# MAX_RISK_PER_TRADE and MAX_NOTIONAL_PER_TRADE. What is gone is the aggregate.

# RETIRED. Gate 3 is gone: there is no per-day entry count. A count bounded the
# NUMBER of positions while saying nothing about their size, and each is already
# bounded to MAX_RISK_PER_TRADE and MAX_NOTIONAL_PER_TRADE. What stops the next
# entry is the absence of funds to pay for it, read live from the broker at Gate 6.
#
# Kept at 0 rather than deleted because several readers imported it, and a name
# that disappears takes its history with it. 0 is not a limit of zero -- nothing
# reads it as one; it is the absence of a limit, and any code that starts
# comparing against it again will block everything immediately and loudly rather
# than quietly reinstating a cap.
MAX_TRADES_PER_DAY = 0

# STOP-DISTANCE BAND -- a cap only, as of 2026-10-01. Shared by two modules, so
# it belongs here rather than in each: engine/zone_entry.py applies it when
# PUBLISHING and atlas/risk/position_sizing.py again at ENTRY. They held separate
# copies, drifted to 7.0 and 6.0, and the gap was live.
#
# THE MINIMUM IS 0.5, down from 1.5. The 1.5 had no recorded derivation -- the
# audit found it sharing a comment block with the measured 7.0 and borrowing its
# authority -- and it cost about 12% of setups on both sides.
#
# A floor is still wanted for a reason the old number did not state: below about
# half a percent the stop is inside the spread and the slippage, so it is not a
# structural level, it is noise, and a "stop" there is a coin toss on the tick.
#
# THE SIZING HALF OF THAT ARGUMENT DOES NOT WORK, AND THE ARITHMETIC IS WORTH
# HAVING HERE. Risk-based sizing binds only while the stop is wider than
# MAX_RISK_PER_TRADE / MAX_NOTIONAL_PER_TRADE = Rs3,000 / Rs1,00,000 = 3.00%.
# Below 3% the notional cap is the smaller of the two quantities, so the NOTIONAL
# cap sizes the trade and actual risk falls below Rs3,000. The measured median
# published stop is 2.08%, so that is already true of most trades -- it is not a
# consequence of lowering this floor from 1.5 to 0.5, and no floor short of 3.0
# would change it.
#
# So 0.5 is justified by the spread, not by sizing. What the arithmetic actually
# says is that MAX_RISK_PER_TRADE stopped being the binding risk control some
# time ago, and MAX_NOTIONAL_PER_TRADE has been doing that job on any stop under
# 3%. That is a separate decision from this one and is left alone.
#
# THE CAP IS 5.0, from 7.0. Measured, and it is a real reduction: on one batch of
# 100 symbols the 5-7% band held 461 long and 161 short setups, so this is not a
# trim at the edge. The reason is the R geometry -- 3R arrives before the stop
# 30-37% of the time from stops under 4%, and 10-18% from stops over 4%, against
# a 25% breakeven. Above roughly 4-5% a 3R target loses by construction, so a
# wider stop is not a worse trade, it is a different and unprofitable one.
#
# UNITS: PERCENT, not a fraction. zone_entry compares in percent directly.
# position_sizing compares against abs(entry-stop)/entry, a fraction, and divides
# by 100 once at import. Feeding 5.0 to a fraction comparison is a 500% ceiling
# that rejects nothing; feeding 0.05 to a percent one rejects everything. Neither
# fails loudly, so keep the unit explicit at every use.
MIN_STOP_PCT = 0.5
MAX_STOP_PCT = 5.0

# Rules 1, 2 — agent must NOT place SL or target orders
# ── EXIT MANAGEMENT. THESE NOW ACTUALLY GATE SOMETHING ────────────
#
# All three were decorative: nothing in the tree read any of them, so the
# "scalable seam" was a comment. atlas/execution/exits.py reads them now, which
# means ENABLE_EXIT_MANAGEMENT is the single switch that turns autonomy on.
#
# TRUE since 2026-09-30. ATLAS now places its own stop on every fill and exits at
# market if it cannot. LIVE_TRADING_ENABLED is also True, so this is real money
# with no human in the loop.
#
# Turned on by operator instruction. The two supervised trades I had asked for --
# one long, one short, watched -- had NOT happened when this was flipped, and no
# part of this path has ever reached a real broker: kiteconnect is absent in
# development, so every order, GTT, margin lookup and cancellation in the tests
# went to an injected fake. In particular kite.order_margins' response shape is
# coded to the documentation and has never been observed.
#
# Set False to hand exits back to a human. That is not a degraded mode -- it is
# what ATLAS did until today -- and it is the fastest way to stop the agent
# managing positions without stopping it trading.
ENABLE_EXIT_MANAGEMENT = True

# A stop is NOT optional once exit management is on. Kept True to say so: with
# ENABLE_EXIT_MANAGEMENT True a position that cannot be given a stop is exited at
# market, so there is no state in which ATLAS holds a position by choice without
# one. Setting this False would not disable stops, it would just be a lie.
ALLOW_AUTOMATED_STOP_LOSS = True

# THE TARGET IS OPTIONAL, AND THIS IS THE KNOB FOR IT.
#   True  -> a 2R target leg is placed alongside the stop.
#   False -> stop only. Winners are held and trailed, which is what
#            engine/zone_entry.py describes ("target = NONE. Winners are held and
#            trailed") and what the mandate of long-term wealth building implies.
# A fixed 2R exit caps every winner at 2R, so this is a strategy choice rather
# than a safety one, and it is reversible without touching code.
#
# FALSE, decided. "Automatic targets" meant "do not make me manage exits by hand",
# not "exit at 2R" -- and stop-only with trailing does the first without giving up
# the second. It also restores what zone_entry already documented: target = NONE,
# winners are held and trailed. A single resting stop has the side benefit of
# having no sibling to orphan.
# TRUE, 2026-10-12. The protection mechanism is now ONE two-leg OCO GTT --
# stop leg and target leg, either cancelling the other at the broker -- which
# is what the operator already places by hand and trusts. An OCO needs both
# legs by construction, so this is no longer a free strategy choice: with it
# False there is no second leg and no OCO, just a single resting stop.
#
# What is given up is explicit: a 2R target caps every winner at 2R, against
# the "winners are held and trailed" line in engine/zone_entry.py. That
# tension is real and is the operator's to resolve -- see PARTIAL BOOKING in
# docs/ATLAS_LONG_ONLY.md, which is flagged and NOT built in this pass.
ALLOW_AUTOMATED_TARGET = True

# ENTRY ORDER TYPE. LIMIT at the live price, not MARKET.
#
# A MARKET entry was the last market-type order left in the path. Kite refuses
# an API MARKET order that omits market_protection, and that single rule took
# out the stop AND its fallback on 7 October; the entry survived only because
# broker.place_order happened to set market_protection=3. A LIMIT has no such
# rule and no slippage surprise.
#
# THE TRADE-OFF IS A MISSED FILL, NOT A BAD ONE. A LIMIT at the last traded
# price does not fill if the book moves away, so enter_trade cancels it and
# records a clean no-entry rather than chasing. Not entering is a cost this
# strategy can carry; an unbounded fill price is not.
ENTRY_ORDER_TYPE = "LIMIT"

# How long to wait for the LIMIT to fill before cancelling it. Short, because
# an unfilled resting entry is a position that may appear after the engine has
# stopped looking.
ENTRY_LIMIT_FILL_SECONDS = 20

# ══════════════════════════════════════════════════════════════════
# ENTRY PROXIMITY — 1% OF THE ZONE'S entry_ref
# ══════════════════════════════════════════════════════════════════
#
# A TRADE IS TAKEN ONLY WHEN LAST PRICE IS WITHIN THIS OF entry_ref. Outside
# it the setup is SKIPPED -- not queued, not widened, not retried at a worse
# price. Operator rule, 2026-10-13.
#
# WHAT THIS IS NOT. MAX_ZONE_DIST_PCT = 15.0 in engine/active_zones.py is a
# SCREENING filter: it decides how far from price a zone may sit and still be
# published at all. It has never been what admits a live entry, and it is left
# alone -- the screening stage is a separate job and other things read that
# frame.
#
# WHAT ADMITTED A LIVE ENTRY BEFORE THIS. atlas_entry.check_entry_range, which
# tests LTP inside [entry_low, entry_high] plus a mitigation and side-of-stop
# invariant. That band is structural and stays: it is what stops a knife-catch
# below a demand zone. But a band can be wide, and "inside a wide band" is not
# the same as "at the level" -- this adds the distance test the band cannot
# express.
#
# MEASURED FROM entry_ref, NOT FROM THE BAND EDGE. entry_ref is the level the
# setup was published against and the price sizing was computed from; the band
# is tolerance around it. Measuring from the nearer edge would make a wide band
# admit a far price, which is the thing being closed.
ENTRY_PROXIMITY_PCT = 1.0

# ── THE ENTRY-SIDE SENTIMENT GATE IS A LIVE READ ──────────────────
#
# Gate 1 used to read market.parquet: yesterday's closing breadth and a 20-day
# average. A daily aggregate deciding an intraday action, and the file ran a
# session behind as well, so the gate never once evaluated on current data.
#
# It now reads the NIFTY near-month future and requires its last price to be
# above (or below) all three of its previous close, today's open and today's
# session VWAP. See atlas/execution/live_sentiment.py for why the futures
# contract rather than the index: the index has no traded VWAP in this feed at
# all -- volume is None on the quote and 0 on every candle.
#
# THE STALENESS LIMIT IS NOT DAMPING. It decides when a quote is too old to be
# called live, which is the difference between a quiet market and a feed that
# has stopped. 120s against a 60s cycle allows one missed tick before the gate
# refuses rather than guesses. No smoothing, hysteresis or confirmation delay is
# applied to the verdict itself -- the operator asked to see the raw flip count
# first and to choose any damping themselves.
LIVE_SENTIMENT_MAX_AGE_SECONDS = 120.0

# ══════════════════════════════════════════════════════════════════
# PROTECTION: DAY 0 IS AN ORDER, DAY 1 ONWARDS IS A GTT
# ══════════════════════════════════════════════════════════════════
#
# THE SEAM THIS DESIGN USES. A GTT checks the DEMAT account; shares bought
# today reach demat at T+1. A regular SL order checks the ORDER BOOK, which
# knows about today's buy -- Zerodha shows delivery shares under Positions on
# the purchase day precisely so they can be traded before settlement.
#
# VERIFIED AGAINST THE LIVE BROKER RULES, 2026-10-13, before any of this was
# written:
#
#   "Shares you purchase for delivery using the CNC product type appear under
#    positions on the same day... Showing delivery shares in positions on the
#    purchase day allows you to participate in intraday trading."
#
#   "If your SL is triggered the same day, your trade will be considered
#    intraday... however, you'll have to place a fresh SL order every day
#    since all pending orders are cancelled at the end of the day."
#
#   End-of-session cancel, equity: 4:00 PM. "You can use AMO to place orders
#    beyond market hours or GTT if you want the order to stay active for 365
#    days."
#
# So: a day-0 SL order is accepted and works, and it dies at the close. The GTT
# is placed BEFORE that close and takes over from the next open, with no window
# between them.
#
# WHY NOT PLACE THE GTT NEXT MORNING. The position would be naked overnight,
# which is exactly when gaps happen. A GTT placed near the close cannot fire
# until the next session anyway, by which time the shares are credited -- so
# placing it on day 0 costs nothing and closes the gap.
DAY0_STOP_ORDER_TYPE = "SL"          # regular stop-loss LIMIT, not a GTT

# When the pre-close job runs and converts day-0 SL orders into resting GTTs.
# 15:20 IST: after the 15:12 CAS cutoff and the 15:15 close of continuous
# trading for F&O-segment stocks, before the 15:30 close and well before the
# 16:00 order-book flush. Late enough that a GTT placed now cannot fire today.
PRECLOSE_GTT_TIME = "15:20"

# ── THE 50/50 SPLIT ───────────────────────────────────────────────
#
# An OCO forces a target to be named, which fights "winners are held and
# trailed" (engine/zone_entry.py). The operator's rule resolves it by doing
# both on the same position:
#
#   half the quantity   OCO GTT, stop + target     booked at 2R
#   half the quantity   single-leg GTT, stop only  trailed, runs
#
# Zerodha's cap is 500 pending GTTs per client, so two per position is
# irrelevant -- 250 concurrent positions before it binds.
GTT_SPLIT_OCO_FRACTION = 0.5

# WHICH HALF GETS THE ODD SHARE. The trailed half, because that is the half
# whose whole purpose is to run -- and because at qty=1 the alternative is a
# position with a target and no trailing leg, which is the opposite of the
# intent. At qty=1 there is no OCO leg at all and the single stop-only GTT
# carries the position; that is a stated outcome, not an edge case.
GTT_SPLIT_ODD_SHARE_TO = "trailed"

# ── TRAILING: STOPS MOVE UP ONLY ──────────────────────────────────
#
# ABSOLUTE. A stop must never be moved down or widened, for any reason, under
# any condition. exits.tighten_only() enforces it by REFUSING a loosening
# update rather than by nobody writing one -- see the comment there for why a
# structural guard and not a convention.
#
#   price up 3% from entry  ->  stop to the entry price (breakeven)
#   thereafter              ->  trail to the most recent swing low as it forms
#   price through the stop  ->  the stop fires. The trade was wrong.
TRAIL_BREAKEVEN_AT_PCT = 0.03

# Run ONCE after the close, updating the resting GTTs for the next session. No
# intraday churn: a stop that moves on every tick is a stop that gets hit by
# noise, and every modification is an API call that can fail halfway.
TRAIL_RUN_ONCE_AFTER_CLOSE = True

# Fractal width for the swing low the trail follows. Same convention as
# radar/levels.py: a 5-bar fractal confirms two sessions after it forms, and
# that lag is real rather than a defect -- a swing low declared on the day it
# printed is just today's low.
TRAIL_SWING_FRACTAL_BARS = 5

# ══════════════════════════════════════════════════════════════════
# THE HALT FILE — ONE CONSTANT, AND IT DEFAULTS TO THE REAL LOCATION
# ══════════════════════════════════════════════════════════════════
#
# THIS FIXES A HALT THAT FAILED OPEN. breaker.py resolved it as
#
#     Path(os.environ.get("ATLAS_HALT_FILE", "~/.atlas/halt"))
#
# while the real file is /var/lib/atlas/halt. The halt therefore worked only
# where that variable happened to be set, and it was set in exactly ONE of the
# eight places that run ATLAS code:
#
#     SET      atlas-market-hours.service
#     MISSING  atlas-engine-report.service
#     MISSING  atlas-mis-squareoff.service
#     MISSING  5 cron jobs -- zerodha_morning, daily_report, bot_listener,
#              trade_outcome_checker, decay
#
# Every one of those resolved the halt to ~/.atlas/halt, which does not exist,
# read "not halted", and would have traded. Including zerodha_morning, the job
# that sends the 08:30 message now asked to REPORT the halt state -- it would
# have reported the wrong file and the wrong answer.
#
# THE DEFAULT IS THE REAL PATH. An unset environment is now the correct
# configuration rather than the broken one, so losing the variable is no longer
# a failure mode. The env var remains readable ONLY so the verification
# harness and the test suite can point at a scratch file without touching the
# live sentinel -- and when it points anywhere else, halt_file_state() says so
# loudly and startup logs it. A silent override is the thing being removed; an
# override that announces itself is not.
HALT_FILE_CANONICAL = "/var/lib/atlas/halt"
HALT_FILE = Path(os.environ.get("ATLAS_HALT_FILE") or HALT_FILE_CANONICAL)


def halt_file_state() -> dict:
    """Where the halt file is, whether it is the canonical path, and its state.

    -> {"path", "canonical", "is_canonical", "exists", "halted", "reason",
        "readable", "summary"}

    ONE FUNCTION FOR EVERY REPORTER, so the startup log, the 08:30 Telegram,
    /status and the verification harness cannot disagree about which file they
    are talking about. They disagreed before: the harness printed
    /var/lib/atlas/halt from a hardcoded help string while the engine it was
    checking read ~/.atlas/halt.

    UNREADABLE COUNTS AS HALTED. Not knowing whether the operator stopped us is
    not permission to trade.
    """
    exists = halted = False
    reason = ""
    readable = True
    try:
        exists = HALT_FILE.exists()
        if exists:
            halted = True
            reason = HALT_FILE.read_text().strip()[:320]
    except Exception as e:
        readable = False
        halted = True
        reason = f"halt file unreadable: {e}"

    is_canon = str(HALT_FILE) == HALT_FILE_CANONICAL
    bits = [f"halt file {HALT_FILE}"]
    if not is_canon:
        bits.append(f"NOT THE CANONICAL PATH ({HALT_FILE_CANONICAL}) — "
                    f"ATLAS_HALT_FILE is overriding it")
    bits.append("HALTED" if halted else "not halted")
    if reason:
        bits.append(reason)
    return {"path": str(HALT_FILE), "canonical": HALT_FILE_CANONICAL,
            "is_canonical": is_canon, "exists": exists, "halted": halted,
            "reason": reason, "readable": readable,
            "summary": " · ".join(bits)}


def log_halt_state(logger=None) -> dict:
    """Log the resolved path and the halt state. Called on every startup.

    AT ERROR LEVEL WHEN THE PATH IS NOT CANONICAL, because that is the
    condition under which the halt silently does nothing, and it has to be
    impossible to miss in a journal.
    """
    st = halt_file_state()
    lg = logger or logging.getLogger("ATLAS-HALT")
    if not st["is_canonical"]:
        lg.error(f"HALT FILE IS NOT THE CANONICAL PATH — {st['summary']}")
    elif st["halted"]:
        lg.warning(f"ATLAS IS HALTED — {st['summary']}")
    else:
        lg.info(f"halt check — {st['summary']}")
    return st


# ── MASTER LIVE-TRADING GATE ──────────────────────────────────────
#
# Shadow first, then live on a date fixed in advance:
#
#   2026-09-14, -15, -16   SHADOW. Proves the breaker, the duplicate guard and
#                          the notification path against real market conditions.
#                          It is NOT a strategy validation -- three sessions
#                          cannot say anything about a strategy, and the regime
#                          gate will very likely hold everything in cash anyway.
#   2026-09-17 onward      LIVE.
#
# THE SCHEDULE IS THE TRIGGER, WHICH IS A REAL CHANGE. Nothing asks for
# confirmation on the first live morning: the engine comes up and places real
# orders because the date says so. ATLAS_LIVE below is the brake.
#
# ATLAS_LIVE, when set, overrides the date in either direction:
#   ATLAS_LIVE=false   forces SHADOW however late it is -- the emergency brake,
#                      and it needs no commit, just a restart
#   ATLAS_LIVE=true    forces LIVE before the date, for a deliberate early test
#   unset              the date decides
#
# Read once at import. The trading window never crosses midnight, so a running
# session keeps the value it started with, which is what you want -- a service
# should not change mode underneath itself.
GO_LIVE_DATE = date(2026, 9, 17)


def _resolve_live() -> bool:
    forced = os.environ.get("ATLAS_LIVE", "").strip().lower()
    if forced in ("false", "0", "no", "off"):
        return False
    if forced in ("true", "1", "yes", "on"):
        return True
    return _today_ist() >= GO_LIVE_DATE


LIVE_TRADING_ENABLED = _resolve_live()

# Rules 8, 9, 10, 11 — regime → side hierarchy
ALLOW_LONG_IN_BULLISH   = True
ALLOW_SHORT_IN_BULLISH  = False
ALLOW_LONG_IN_BEARISH   = False
ALLOW_SHORT_IN_BEARISH  = True

# Accumulation runs in SIDEWAYS as well as BULL. A quiet, directionless market
# is when institutions accumulate and retail stops watching -- it is the setup,
# not a reason to stay in cash. Only a genuine bear (200DMA -3%) blocks longs.
# REMOVED: ALLOW_LONG_IN_SIDEWAYS.
#
# Accumulation used to run in bull AND sideways, on the argument that a quiet
# market is the setup rather than a reason to stand aside. The entry gate now
# requires a bull regime (close>200DMA and 50DMA>200DMA) AND positive sentiment
# (advances>declines and Nifty>20DMA), so sideways does not qualify and a flag
# permitting it would have no effect. Left as a note rather than deleted
# silently, because its absence is the change.
#
# The regime has been sideways throughout the live history: on this rule ATLAS
# takes zero trades over that period. That is the intent, not a regression.
ALLOW_SHORT_IN_SIDEWAYS = False

# WHICH SIDES MAY BE OPENED AT ALL. The direction for a given day comes from
# atlas_entry.allowed_side (the regime x sentiment matrix); these two are the
# master switches above it, so a side can be taken off the table without
# reasoning about market state.
#
# SHORTS ARE OFF. 2026-10-12, operator decision, and it is the whole point of
# this pass.
#
# THE RULE: trade only when regime AND sentiment are both bullish, otherwise
# cash. Strict, both must hold. That is LONGS ONLY, and the short path should
# never have existed under it.
#
# EVERY FAILURE OF 7 OCTOBER CAME OUT OF THE HEDGE-SHORT PATH, as a chain of
# forced consequences rather than a bug:
#
#     shorts    ->  forced MIS (rule 10, no overnight short)
#     MIS       ->  no GTT (GTT is CNC-only)
#     no GTT    ->  forced SL-M as the stop
#     SL-M      ->  Zerodha refuses it via API without market_protection
#     rejected  ->  fallback was a MARKET order, refused by the SAME rule
#
# Removing the first link removes the class. Longs are CNC, CNC takes a GTT,
# and a GTT is the mechanism the operator already uses by hand and trusts.
#
# CHECKED FIRST AND UNCONDITIONALLY, ahead of any market reading, and now also
# enforced at the bottom of the stack in broker.place_order -- which refuses a
# SHORT entry whatever the caller believes. Two independent guards, same
# principle as the exit fallback: a config flag alone is a convention, and a
# convention is what a future edit breaks silently.
ALLOW_LONG_ENTRIES  = True
ALLOW_SHORT_ENTRIES = False

# LONGS ARE CNC. Stated here rather than inferred from position_sizing, which
# is where the product is actually chosen, so the pair is readable together
# with SHORT_PRODUCT_TYPE above it.
LONG_PRODUCT_TYPE = "CNC"

# STRICT AND-GATE. When True, allowed_side returns LONG only for bull regime
# AND bullish sentiment, and cash for everything else -- including
# sideways+bullish, which the matrix used to take as a long.
#
# THE CODE AND THE CONFIG COMMENTS DISAGREED ABOUT THIS, and the comment above
# ALLOW_SHORT_IN_SIDEWAYS was already the correct description: "The entry gate
# now requires a bull regime ... AND positive sentiment ..., so sideways does
# not qualify." allowed_side did not implement that -- it followed sentiment in
# a sideways regime. The comment was right and the code was wrong; this makes
# them agree.
STRICT_BULL_AND_BULLISH = True

# SUPERSEDED BY THE MATRIX, and set False rather than deleted so its absence is
# the visible change. Shorts used to require extreme_bearish -- close < 200DMA-3%,
# 50DMA < 200DMA, VIX > 18 -- which made them a rare hedge. The direction is now
# decided by regime x sentiment in atlas_entry.allowed_side, where a bear regime
# with bearish breadth is sufficient. Leaving this True would have kept shorts
# unreachable while the config claimed they were enabled.
#
# extreme_bearish is still computed by build_market and still worth having: it is
# a much stronger condition than "bear regime", and it is the natural knob if
# shorts turn out to need one.
REQUIRE_EXTREME_BEARISH_FOR_SHORTS = False

# The Nifty opening-range gate is an INTRADAY directional check. It applies to
# hedge shorts only. Applied to accumulation longs it blocked entries on flat
# days -- precisely the days the strategy targets.
OPENING_RANGE_GATE_APPLIES_TO = ("SHORT",)
SHORT_PRODUCT_TYPE      = "MIS"          # rule 10 — shorts intraday only
ALLOW_OVERNIGHT_SHORT   = False
DEFAULT_ON_UNKNOWN_REGIME = "CASH"       # unknown/stale regime → no trade

# Rule 16 — short margin. A FALLBACK ONLY, and it is now genuinely the fallback:
# broker.order_margin() asks Kite what the order actually requires and Gate 6
# checks that, not this.
#
# It mattered because can_afford() gated on this number while nothing capped the
# number of trades, so an underestimate opens more shorts than the funds support:
# at 20% a short consumes a fifth of a long's capital, and five shorts fit where
# one long did. If the real requirement is 30% the fifth short is unfunded.
#
# Still here because the broker call can fail, and a sizing path that raises when
# the margin API is unreachable would refuse every trade for a reason unrelated to
# the trade. When it is used, it is logged as an estimate.
SHORT_MARGIN_PCT_ESTIMATE = 0.20         # fallback; the broker's number wins

# Entry-range gate — enter ONLY if live price is within the signal's
# [entry_low, entry_high] band. Applies to LONG and SHORT. No chasing.
ENFORCE_ENTRY_RANGE = True

# Rule 5 — funds safety buffer (brokerage/taxes/slippage on the way in),
# applied to the trade's requirement before it is compared against live broker
# funds. See atlas/risk/funds.can_afford().
FUNDS_SAFETY_BUFFER_PCT = 0.02           # 2% buffer; configurable

# ══════════════════════════════════════════════════════════════════
# MIS SQUARE-OFF TIMING
# ══════════════════════════════════════════════════════════════════
#
# VERIFIED AGAINST ZERODHA'S CURRENT PUBLISHED TIMINGS ON 2026-10-09, not from
# memory. Three segment cutoffs, effective 2026-08-03:
#
#   Equity, stock in CAS       15:12   SEBI's Closing Auction Session: continuous
#                                      trading for F&O-segment stocks ends 15:15,
#                                      and Zerodha squares MIS off by 15:12
#   Equity, stock not in CAS   15:25
#   Equity / index derivatives 15:26
#
# AND THE CUTOFF IS NOT ONLY A SQUARE-OFF TIME -- IT IS ALSO AN ORDER CUTOFF.
# Zerodha rejects MIS orders placed after the square-off time for that segment.
# On 2026-10-07 the 15:15 timer tried to close a BHARTIARTL MIS short and got
# "Intraday orders (MIS) are allowed only till 3:12 PM". The timer was three
# minutes past the cutoff, so that square-off had never been able to work.
#
# PHASE 1 OF CAS COVERS EVERY F&O-SEGMENT STOCK, which is most of what ATLAS
# trades, so 15:12 is the cutoff that binds. ONE TIME FOR ALL SYMBOLS, taken from
# the EARLIEST cutoff: a per-symbol time would need a live CAS membership list,
# and being early on a non-CAS stock costs nothing while being late on a CAS one
# costs the position.
MIS_BROKER_CUTOFFS = {
    "equity_cas":           "15:12",
    "equity_non_cas":       "15:25",
    "equity_derivatives":   "15:26",
    "index_derivatives":    "15:26",
}
MIS_BROKER_CUTOFF = min(MIS_BROKER_CUTOFFS.values())   # "15:12" -- the binding one

# MARGIN, not a guess. 15:15 failed by three minutes; twelve gives the engine
# room for a retry, a slow quote and a rejected-then-repriced limit order and
# still be flat before the broker takes over. The broker's own square-off is a
# BACKSTOP, not the plan: if ATLAS is working, the broker never has to act.
MIS_EXIT_MARGIN_MIN = 12
MIS_EXIT_HOUR, MIS_EXIT_MIN = divmod(
    int(MIS_BROKER_CUTOFF.split(":")[0]) * 60
    + int(MIS_BROKER_CUTOFF.split(":")[1]) - MIS_EXIT_MARGIN_MIN, 60)
MIS_EXIT_TIME = f"{MIS_EXIT_HOUR:02d}:{MIS_EXIT_MIN:02d}"        # "15:00"

if (MIS_EXIT_HOUR, MIS_EXIT_MIN) >= tuple(
        int(x) for x in MIS_BROKER_CUTOFF.split(":")):
    # Import-time, because the only other place this is discoverable is a
    # rejected order on a live position.
    raise AssertionError(
        f"MIS_EXIT_TIME {MIS_EXIT_TIME} is not before the broker cutoff "
        f"{MIS_BROKER_CUTOFF} -- this is the 2026-10-07 failure exactly")

SESSION_PRE_MARKET  = (9,  0,  9, 15)
SESSION_OPENING     = (9, 15,  9, 45)
SESSION_MORNING     = (9, 45, 11, 30)
SESSION_MIDDAY      = (11,30, 13, 30)
SESSION_AFTERNOON   = (13,30, 14, 30)
SESSION_POWER_HOUR  = (14,30, 15, 15)
SESSION_CLOSING     = (15,15, 15, 30)

# Operator-facing mode vocabulary. TWO MODES, because only two mean anything.
#
# PAUSED halts entries via the kill switch. NORMAL is the absence of that. There
# is nothing else to express: position size comes solely from the Rs3,000 risk
# budget and the stop distance, and the only trade limits are
# MAX_TRADES_PER_DAY and live broker funds.
#
# CAUTIOUS, AGGRESSIVE and DEFENSIVE were REMOVED. They once carried
# size_pct / min_conviction / max_trades; those levers went when capital
# tracking did, and the modes stayed on as labels that changed nothing. Four
# labels that do nothing are worse than two that mean something -- an operator
# who sets AGGRESSIVE and sees it confirmed reasonably believes the agent will
# behave differently, and it will not.
#
# A mode value from before this change (an atlas_state row still reading
# AGGRESSIVE) is normalised to NORMAL on read, so no interface reports a mode
# that no longer exists. See directives.get_agent_state.
AGENT_MODES = ("NORMAL", "PAUSED")
HALT_MODES  = ("PAUSED",)          # modes in which no new entry may be taken
DEFAULT_AGENT_MODE = "NORMAL"
VERSION = "1.0.0"
SYSTEM  = "ATLAS"

def validate():
    errors = []
    if not SUPABASE_KEY: errors.append("SUPABASE_SERVICE_KEY not set")
    if not UPSTOX_API_KEY: errors.append("UPSTOX_API_KEY not set")
    if errors:
        for e in errors: print(f"[CONFIG ERROR] {e}")
        return False
    return True

if __name__ == "__main__":
    print(f"ATLAS v{VERSION}")
    print("Trading rules:")
    print(f"  Risk per trade    INR {MAX_RISK_PER_TRADE:,.0f}")
    print(f"  Max notional      INR {MAX_NOTIONAL_PER_TRADE:,.0f}")
    print(f"  Qty multiple      {QUANTITY_MULTIPLE}")
    print(f"  Stop band         {MIN_STOP_PCT}% - {MAX_STOP_PCT}%")
    print(f"  Max trades/day    {MAX_TRADES_PER_DAY}")
    print(f"  Funds buffer      {FUNDS_SAFETY_BUFFER_PCT*100:.0f}%")
    print(f"  Live trading      {LIVE_TRADING_ENABLED}")
    print("Capital:            not tracked — read live from the broker")
    print(f"Config valid:       {validate()}")
