"""
MERIDIAN config.

ITS OWN, NOT atlas.config. The same environment variables, read independently,
so a change on the ATLAS side cannot reach this vertical and a reader of either
file is not misled about what the other does. The duplication is the point.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

SUPABASE_URL = os.environ.get("SUPABASE_URL",
                              "https://eibdlcanpudjgmkjxrga.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# READ ONLY. The equity price feed owns this file and refreshes it; meridian must
# never write it. A bad rewrite here is an outage over there.
UPSTOX_TOKEN_FILE = DATA_DIR / "upstox_token.json"

UPSTOX_BASE = "https://api.upstox.com/v2"
# Static asset, not a rate-limited API. Refreshed daily around 06:00 IST, with
# expired contracts and delisted names dropped from the next BOD file -- so it is
# the exchange's own list one step removed, and it is why the F&O universe is
# never hardcoded here.
UPSTOX_INSTRUMENTS_NSE = ("https://assets.upstox.com/market-quote/instruments/"
                          "exchange/NSE.json.gz")

# ── THE FOUR INDICES ──────────────────────────────────────────────
# Keyed by Upstox instrument_key, which is what /option/chain wants.
#
# EXPIRY KEYWORD PER UNDERLYING, and this is load-bearing. NIFTY is the only NSE
# index that still has weekly contracts; BANKNIFTY, FINNIFTY and MIDCPNIFTY went
# monthly-only when weeklies were rationalised, and every stock has always been
# monthly, last Tuesday. Asking for current_week on a monthly-only underlying
# either errors or quietly returns the monthly chain -- and a silently wrong
# expiry would poison a year of history before anyone looked.
INDICES = {
    "NIFTY":      {"key": "NSE_INDEX|Nifty 50",          "expiry": "current_week"},
    "BANKNIFTY":  {"key": "NSE_INDEX|Nifty Bank",        "expiry": "current_month"},
    "FINNIFTY":   {"key": "NSE_INDEX|Nifty Fin Service", "expiry": "current_month"},
    "MIDCPNIFTY": {"key": "NSE_INDEX|NIFTY MID SELECT",  "expiry": "current_month"},
}
STOCK_EXPIRY_KEYWORD = "current_month"

INDIA_VIX_KEY = "NSE_INDEX|India VIX"

# ── RATE LIMITING ─────────────────────────────────────────────────
# Upstox Standard APIs: 50/sec, 500/min, 2000/30min, per API per user.
# ~212 underlyings at one call each is 43% of the per-minute budget, so the
# per-MINUTE limit is the binding one and the pacing below targets it rather than
# the per-second headline. 5/sec = 300/min leaves 40% spare for a retry pass.
REQ_PER_SEC = 5.0
MAX_RETRIES = 3
RETRY_BACKOFF_S = (2, 8, 20)
HTTP_TIMEOUT = 20

# ── WHAT IS RECORDED ──────────────────────────────────────────────
# ATM +/- 10 strikes. The full chain is 2-3 million rows a year against a
# database holding ~20k; this band keeps the smile and the skew, which is what
# strike detail is for.
STRIKE_BAND = 10

# ── FAILING LOUDLY ────────────────────────────────────────────────
# Below this share of expected underlyings the run is PARTIAL and alerts. A
# recorder that quietly writes 12 rows instead of 212 is the failure mode this
# number exists for.
MIN_WRITE_RATIO = 0.90
