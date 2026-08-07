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
INITIAL_CAPITAL      = float(os.environ.get("ATLAS_CAPITAL", "150000"))  # INR 1.5L
CAPITAL_PER_TRADE    = 50000.0   # Fixed INR 50K per CNC trade
DAILY_LOSS_CAP_PCT   = 0.02      # 2% of allocated capital = INR 3,000
WEEKLY_DRAWDOWN_PCT  = 0.05      # 5% of allocated capital = INR 7,500
MAX_RISK_PER_TRADE   = 3000      # Max SL loss per trade INR 5,000
MAX_OPEN_POSITIONS   = 3
MIN_CONVICTION_SCORE = 82
ELITE_CONVICTION     = 85

MAX_LIVE_SIGNALS     = 3
SIGNAL_DECAY_MINUTES = 30
MIN_RVOL             = 1.5
MIN_RR               = 2.0


# ═══════════════════════════════════════════════════════════════════
# TSL ATLAS TRADING RULES — single source of truth (added 6 Jul 2026)
# Phase: TRAINING — agent ENTERS trades only. No auto SL/target/exit.
# ═══════════════════════════════════════════════════════════════════

# Rule 3 — max notional exposure per trade
MAX_NOTIONAL_PER_TRADE = 100000.0        # ₹1,00,000

# Rule 4 — quantity must be a multiple of this
QUANTITY_MULTIPLE = 5

# Rule 6 — hard daily trade cap
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

AGENT_MODES = {
    "AGGRESSIVE": {"size_pct": 1.0, "min_conviction": 75, "max_trades": 3},
    "NORMAL":     {"size_pct": 0.7, "min_conviction": 78, "max_trades": 3},
    "CAUTIOUS":   {"size_pct": 0.5, "min_conviction": 82, "max_trades": 2},
    "DEFENSIVE":  {"size_pct": 0.3, "min_conviction": 87, "max_trades": 1},
    "PAUSED":     {"size_pct": 0.0, "min_conviction": 100,"max_trades": 0},
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
