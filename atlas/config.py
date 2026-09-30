"""
ATLAS — Agentic Trading & Lifecycle Automation System
Central configuration. All modules import from here.
"""
import os
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

# Rule 1 — INR at risk if the structural stop is hit. Quantity is DERIVED from
# this and the stop distance; see risk/position_sizing.py.
MAX_RISK_PER_TRADE = 3000.0

# Rule 2 — quantity must be a multiple of this
QUANTITY_MULTIPLE = 5

# Rule 3 — max notional exposure per trade
MAX_NOTIONAL_PER_TRADE = 100000.0        # ₹1,00,000

# Rule 4 — new entries per day. This is now a real limit rather than a
# secondary guard: with the capital cap gone, it and available broker funds are
# the only things that stop further entries.
# ── HOW FAR IS WORTH WATCHING ──────────────────────────────────────
#
# A candidate is a signal valid in every way except that price was not at its zone
# at last night's close. The loop re-checks that distance live, so the question is
# which candidates could PLAUSIBLY close the gap during one session.
#
# MEASURED, over 754 sessions and 232 symbols: the median candidate sits 3.14% from
# its zone and the 90th percentile 9.85%, while ATR is 2.72% of price at the median.
# A candidate 9.85% away needs a four-ATR day in one direction; watching it costs a
# quote every cycle and it will never fire.
#
# So the cut is ATR-RELATIVE, not a fixed percentage. One ATR is roughly "a normal
# day's range", and it is PER SYMBOL, which a percentage cannot be: 2% is routine
# for a volatile name and impossible for a stable one. At 1.0 ATR the watchlist
# keeps 44.4% of candidates -- about 30 a session on 232 symbols, or 90 on 706 --
# and drops only the ones that could not be reached anyway.
#
# Raise it to widen the net at the cost of quotes and noise; nothing about the
# ENTRY standard changes either way, because near_zone still has to pass on the
# live price.
CANDIDATE_MAX_ATR_MULTIPLE = 1.0

# AND AN EXPLICIT ABSOLUTE CEILING, so the watchlist does not inherit its outer
# bound from a constant chosen for something else. active_zones.MAX_ZONE_DIST_PCT
# is 15% and exists to stop forward-filling a zone price has travelled far past;
# it was never a considered watchlist threshold, and a stock 12% from its zone is
# not a candidate.
#
# MEASURED on the ATR-filtered set, 754 sessions: p50 1.32%, p90 2.70%, MAX 8.88%.
# So 15% was already not binding on anything -- the ATR cut does the work -- and
# the honest numbers for the thresholds asked about are:
#
#     cap      kept/session (232 sym)   share    scaled to 706
#     1.5%              17.5            58.1%         53
#     2.0%              22.7            75.5%         69
#     3.0%              28.2            93.8%         86
#     5.0%              30.0            99.7%         91
#     8.0%              30.1           100.0%         92   <- a no-op
#
# 3.0% is the choice: it is the point where an absolute cap starts to do anything
# at all (trimming 6%), it sits just above the p90 of 2.70% so it keeps everything
# ordinary, and it removes the volatile tail -- the 8.88% outlier is a stock whose
# ATR is nearly 9%, where one ATR of movement is a different event from the one
# this watchlist is for. 5% and 8% would be decoration; 1.5% and 2% discard
# reachable setups to no purpose, since the ATR test has already established they
# are reachable.
CANDIDATE_MAX_DIST_PCT = 3.0

# ── CONCURRENT EXPOSURE CEILING, IN RUPEES ────────────────────────
#
# A DIFFERENT SHAPE FROM THE PER-DAY COUNT THAT WAS REMOVED. That one capped
# OPPORTUNITY: three entries a day whatever the book held, so a quiet week and a
# crowded one were treated alike and the fourth good setup was refused for
# arithmetic. This caps CAPITAL AT RISK OF BEING COMMITTED, which is the thing
# that compounds.
#
# IN RUPEES, NOT POSITIONS, and the difference is not cosmetic. A count treats a
# Rs20,000 short and a Rs1,00,000 long as the same unit of exposure when one ties
# up five times the cash. Capital is what the broker actually withholds and what
# runs out.
#
# WHAT Rs4,00,000 MEANS AGAINST THE MEASURED BOOK. Over the 20 sessions to
# 2026-09-30, peak deployed capital was Rs13,81,517 and the AVERAGE was
# Rs7,09,526. So this ceiling sits below the average the book has been running --
# it is roughly a third of the peak, and it WILL bind on ordinary behaviour, not
# only on the wider watchlist. At the measured Rs78,000 average notional it allows
# about five concurrent longs, or about twenty-five shorts, since MIS margin is a
# fifth of notional. Five longs is about Rs15,000 of risk if every stop fills.
#
# That is a deliberate reduction in activity, not just a guard rail. It is recorded
# here because a number chosen to "bound risk rather than opportunity" does both at
# this level, and whoever revisits it should know which of the two they are
# adjusting.
#
# Computed from qty x entry_price on rows in a BLOCKING status, with MIS shorts at
# SHORT_MARGIN_PCT_ESTIMATE, so no new column is needed. A PENDING row whose fill
# was never confirmed counts: a position that MIGHT exist has already committed the
# cash, and assuming otherwise is how a ceiling is exceeded by exactly the
# positions nobody is sure about.
MAX_CONCURRENT_EXPOSURE = 400000.0

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

# STOP-DISTANCE BAND — a quality filter on the structural stop. Not one of the
# numbered rules, but shared by two modules, so it belongs here rather than in
# each of them: engine/zone_entry.py applies it when PUBLISHING a signal and
# atlas/risk/position_sizing.py applies it again at ENTRY. They held separate
# copies, drifted to 7.0 and 6.0, and the gap was live — a signal with a 6-7%
# stop was published and then refused at entry by the stricter sizer.
#
# 7.0 is the measured value and the correct one. Live swing-low distances across
# 40 symbols cluster 2-9%; the 6.0 ceiling was set by reasoning rather than
# measurement and was rejecting genuine setups at 6.19-6.93%.
#
# UNITS: PERCENT, not a fraction. zone_entry compares in percent directly.
# position_sizing compares against abs(entry-stop)/entry, a fraction, and
# divides by 100 once at import. Feeding 7.0 to a fraction comparison is a 700%
# ceiling that rejects nothing; feeding 0.07 to a percent one rejects
# everything. Neither fails loudly, so keep the unit explicit at every use.
MIN_STOP_PCT = 1.5
MAX_STOP_PCT = 7.0

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
ALLOW_AUTOMATED_TARGET = False

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
# Shorts were False while the mandate was long-only. They are True now: ATLAS
# takes the side the matrix names, shorts included, MIS and intraday.
#
# ALLOW_SHORT_ENTRIES is still checked FIRST and unconditionally in the SHORT
# branch, ahead of any market reading. It was added because the previous
# guarantee was emergent -- SHORT required bull AND extreme_bearish, two
# individually satisfiable conditions that conflicted only because
# build_market._classify cannot emit them together. That is not a guarantee, it
# is an invariant in another module, and a sweep found the corner where it held.
# The switch stays for the same reason it was added: so the answer is local.
ALLOW_LONG_ENTRIES  = True
ALLOW_SHORT_ENTRIES = True

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
