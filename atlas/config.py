"""
ATLAS — Agentic Trading & Lifecycle Automation System
Central configuration. All modules import from here.
"""
import os
from pathlib import Path

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
MAX_TRADES_PER_DAY = 3

# Rules 1, 2 — agent must NOT place SL or target orders
ALLOW_AUTOMATED_STOP_LOSS = False
ALLOW_AUTOMATED_TARGET     = False

# Phase switch — exit management (SL/target/trailing/risk) OFF this phase.
# Scalable seam: flip True in a later phase to enable end-to-end management.
ENABLE_EXIT_MANAGEMENT = False

# Master live-trading gate — DEFAULT DENY. Must be deliberately enabled.
LIVE_TRADING_ENABLED = os.environ.get("ATLAS_LIVE", "false").lower() == "true"

# Rules 8, 9, 10, 11 — regime → side hierarchy
ALLOW_LONG_IN_BULLISH   = True
ALLOW_SHORT_IN_BULLISH  = False
ALLOW_LONG_IN_BEARISH   = False
ALLOW_SHORT_IN_BEARISH  = True

# Accumulation runs in SIDEWAYS as well as BULL. A quiet, directionless market
# is when institutions accumulate and retail stops watching -- it is the setup,
# not a reason to stay in cash. Only a genuine bear (200DMA -3%) blocks longs.
ALLOW_LONG_IN_SIDEWAYS  = True
ALLOW_SHORT_IN_SIDEWAYS = False

# Hedge shorts require the extreme_bearish flag from market.parquet:
# close < 200DMA-3% AND 50DMA < 200DMA AND VIX > 18. Deliberately rare.
REQUIRE_EXTREME_BEARISH_FOR_SHORTS = True

# The Nifty opening-range gate is an INTRADAY directional check. It applies to
# hedge shorts only. Applied to accumulation longs it blocked entries on flat
# days -- precisely the days the strategy targets.
OPENING_RANGE_GATE_APPLIES_TO = ("SHORT",)
SHORT_PRODUCT_TYPE      = "MIS"          # rule 10 — shorts intraday only
ALLOW_OVERNIGHT_SHORT   = False
DEFAULT_ON_UNKNOWN_REGIME = "CASH"       # unknown/stale regime → no trade

# Rule 16 — short margin. ~20% of notional for MIS intraday, but NEVER
# treat as guaranteed — always prefer broker's live margin check when available.
SHORT_MARGIN_PCT_ESTIMATE = 0.20         # estimate only; broker value wins

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

# Operator-facing mode vocabulary. Only PAUSED changes behaviour -- it halts
# entries via the kill switch. The others are labels the operator sets to record
# intent; they no longer carry size_pct / min_conviction / max_trades, because
# position size now comes solely from the Rs3,000 risk budget and the stop
# distance, and the only trade limits are MAX_TRADES_PER_DAY and live funds.
# Keeping fake per-mode multipliers would imply a sizing lever that no longer
# exists.
AGENT_MODES = ("NORMAL", "CAUTIOUS", "AGGRESSIVE", "DEFENSIVE", "PAUSED")
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
    print(f"  Max trades/day    {MAX_TRADES_PER_DAY}")
    print(f"  Funds buffer      {FUNDS_SAFETY_BUFFER_PCT*100:.0f}%")
    print(f"  Live trading      {LIVE_TRADING_ENABLED}")
    print("Capital:            not tracked — read live from the broker")
    print(f"Config valid:       {validate()}")
