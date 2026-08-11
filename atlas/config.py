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

# Capital configuration
INITIAL_CAPITAL      = float(os.environ.get("ATLAS_CAPITAL", "300000"))  # INR 3L

# Risk architecture: Rs3,000 risked per trade, Rs1,00,000 max notional.
# Quantity is DERIVED from stop distance -- see risk/position_sizing.py.
MAX_RISK_PER_TRADE   = 3000.0    # INR at risk if the structural stop is hit
MAX_CAPITAL_DEPLOYED = 300000.0  # THE binding constraint. 3 x Rs1L.
MAX_OPEN_POSITIONS   = 6         # secondary bound; capital usually binds first

# Statuses that represent a LIVE capital commitment. GTT_PENDING counts: the
# capital is committed the moment the trigger rests at the broker, before it
# fills. kill_switch and atlas_entry MUST agree on this -- they were diverging,
# with atlas_entry counting both and kill_switch counting only OPEN, so the
# position limit undercounted by every resting GTT.
OPEN_STATUSES        = ("OPEN", "GTT_PENDING")

# Absolute, not percentage. 3 trades x Rs3k = Rs9,000 worst case in one day.
# The old 2%-of-1.5L rule produced Rs3,000 -- one stop-out halted the system.
DAILY_LOSS_CAP_INR   = 9000.0
WEEKLY_DRAWDOWN_INR  = 22500.0
# Retained for any legacy caller; derived from the absolute values above.
DAILY_LOSS_CAP_PCT   = DAILY_LOSS_CAP_INR / INITIAL_CAPITAL
WEEKLY_DRAWDOWN_PCT  = WEEKLY_DRAWDOWN_INR / INITIAL_CAPITAL

# ACCUMULATION SCREEN -- institutional footprint is QUIET tape, not loud.
# MIN_RVOL = 1.5 previously demanded above-average volume, which is the
# opposite of what accumulation looks like.
MAX_RVOL_ACCUMULATION = 1.0      # relative volume at or below average
MIN_DELIVERY_PCT      = 50.0     # delivery-based buying, not churn
DISCOUNT_MIN_PCT      = 5.0      # at least 5% off the 52-week high
DISCOUNT_MAX_PCT      = 20.0     # but not a broken chart

# --- DEPRECATED. Retained as names so imports do not break. Not used to gate.
CAPITAL_PER_TRADE    = 0.0       # superseded by the Rs3k/Rs1L dual cap
MIN_CONVICTION_SCORE = 0         # score is non-predictive; gate removed
ELITE_CONVICTION     = 0
MAX_LIVE_SIGNALS     = 0         # GTT rests at the broker; no live queue
SIGNAL_DECAY_MINUTES = 0         # GTT lifetime is broker-side
MIN_RVOL             = 0.0       # see MAX_RVOL_ACCUMULATION
MIN_RR               = 0.0       # undefined with open targets


# ═══════════════════════════════════════════════════════════════════
# TSL ATLAS TRADING RULES — single source of truth (added 6 Jul 2026)
# Phase: TRAINING — agent ENTERS trades only. No auto SL/target/exit.
# ═══════════════════════════════════════════════════════════════════

# Rule 3 — max notional exposure per trade
MAX_NOTIONAL_PER_TRADE = 100000.0        # ₹1,00,000

# Rule 4 — quantity must be a multiple of this
QUANTITY_MULTIPLE = 5

# Rule 6 — new entries per day. NOTE: for accumulation this is rarely the
# binding limit. Longs are held indefinitely, so capital does not recycle --
# MAX_CAPITAL_DEPLOYED is what actually stops further entries.
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

# Funds safety buffer (fees/taxes/slippage) — applied before funds check.
FUNDS_SAFETY_BUFFER_PCT = 0.02           # 2% buffer; configurable

SESSION_PRE_MARKET  = (9,  0,  9, 15)
SESSION_OPENING     = (9, 15,  9, 45)
SESSION_MORNING     = (9, 45, 11, 30)
SESSION_MIDDAY      = (11,30, 13, 30)
SESSION_AFTERNOON   = (13,30, 14, 30)
SESSION_POWER_HOUR  = (14,30, 15, 15)
SESSION_CLOSING     = (15,15, 15, 30)

# max_trades here is read by kill_switch CHECK 4 as the OPEN POSITION limit,
# so it must track MAX_OPEN_POSITIONS, not the daily entry count.
# min_conviction is retained at 0 -- the conviction gates were removed from
# kill_switch; leaving the key avoids KeyError in any legacy reader.
AGENT_MODES = {
    "AGGRESSIVE": {"size_pct": 1.0, "min_conviction": 0, "max_trades": MAX_OPEN_POSITIONS},
    "NORMAL":     {"size_pct": 1.0, "min_conviction": 0, "max_trades": MAX_OPEN_POSITIONS},
    "CAUTIOUS":   {"size_pct": 0.7, "min_conviction": 0, "max_trades": 3},
    "DEFENSIVE":  {"size_pct": 0.5, "min_conviction": 0, "max_trades": 2},
    "PAUSED":     {"size_pct": 0.0, "min_conviction": 0, "max_trades": 0},
}
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
    print(f"Capital:          INR {INITIAL_CAPITAL:,.0f}")
    print(f"Capital per trade:INR {CAPITAL_PER_TRADE:,.0f}")
    print(f"Daily loss cap:   INR {INITIAL_CAPITAL * DAILY_LOSS_CAP_PCT:,.0f}")
    print(f"Max risk/trade:   INR {MAX_RISK_PER_TRADE:,.0f}")
    print(f"Min conviction:   {MIN_CONVICTION_SCORE}/100")
    print(f"Config valid:     {validate()}")
