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
# Names only. The instrument_key and the expiry date are both READ from the
# instrument master at run time -- "NSE_INDEX|NIFTY MID SELECT" is not a string
# worth retyping from memory, and the expiry is the thing a keyword got wrong.
#
# THE EXPIRY KEYWORDS ARE GONE. The recorder used current_week for NIFTY and
# current_month for everything else. On Friday 2026-10-02 NIFTY alone returned
# "chain carried no strikes": current_week resolves to the expiry inside the
# current CALENDAR week, which was Tuesday 2026-09-29 and had already expired.
# NIFTY weeklies expire Tuesday, so that keyword would have failed every
# Wednesday, Thursday and Friday -- three sessions in five, on the most important
# underlying in the recorder, in a series that cannot be back-filled.
#
# fno_universe.parse_master now reads each contract's real expiry and takes the
# nearest one strictly after today. That also removes the weekly/monthly split
# and any day-of-week arithmetic: NSE shifts expiries off Tuesday around
# holidays, and the master currently holds a Monday weekly and a Monday monthly
# that no weekday rule would have found.
INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")

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
