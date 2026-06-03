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

INITIAL_CAPITAL      = float(os.environ.get("ATLAS_CAPITAL", "100000"))
DAILY_LOSS_CAP_PCT   = 0.02
WEEKLY_DRAWDOWN_PCT  = 0.05
MAX_RISK_PER_TRADE   = 5000
MAX_OPEN_POSITIONS   = 3
MIN_CONVICTION_SCORE = 75
ELITE_CONVICTION     = 85

MAX_LIVE_SIGNALS     = 3
SIGNAL_DECAY_MINUTES = 30
MIN_RVOL             = 1.5
MIN_RR               = 2.0

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
    print(f"Capital:        INR {INITIAL_CAPITAL:,.0f}")
    print(f"Daily loss cap: INR {INITIAL_CAPITAL * DAILY_LOSS_CAP_PCT:,.0f}")
    print(f"Max risk/trade: INR {MAX_RISK_PER_TRADE:,.0f}")
    print(f"Min conviction: {MIN_CONVICTION_SCORE}/100")
    print(f"Config valid:   {validate()}")
