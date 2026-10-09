"""
Bhavcopy access. The only module in Radar that reads the disk.

EVERY NUMBER RADAR SHOWS COMES THROUGH HERE, by hard rule. There is no second
price source, no news-site scrape, no API quote. If a figure cannot be built
from these files it does not get shown -- see fail-closed, docs/TSL_FLASH.md
§10.5.

TWO LAYOUTS, AND BOTH ARE LIVE. The per-stock parquets written by
engine/01b_download_bhavcopy.py carry open/high/low/close/volume/delivery, and
that covers almost everything. What they do NOT carry is session turnover, and
turnover is what VWAP is made of. So event_vwap reads the RAW bhavcopy CSV for
that one date:

    legacy layout   AVG_PRICE          NSE publishes the VWAP directly
    2026 layout     TtlTrfVal / TtlTradgVol

Either way it is NSE's own figure. The (H+L+C)/3 proxy is deliberately not used
anywhere: it is a different quantity that resembles a VWAP closely enough to be
mistaken for one, which is worse than not having it.
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path
from datetime import date, datetime, timedelta

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("RADAR-BARS")

STOCKS_DIR = ROOT / "data/processed/stocks"
RAW_DIR = ROOT / "data/raw/bhavcopy"

# engine/01b_download_bhavcopy.py's START_DATE. A series whose first row is at
# or near this date begins at the DOWNLOAD FLOOR, not at the stock's listing --
# which is the whole reason ipo_price is usually dropped. Imported as a literal
# rather than from 01b, whose module name starts with a digit and cannot be
# imported by name.
DOWNLOAD_FLOOR = date(2023, 1, 1)
# A few sessions of slack: the floor is a Sunday in some years and the first
# available session lands a day or two later.
FLOOR_SLACK_DAYS = 10

_cache: dict = {}


def _calendar():
    try:
        from engine.trading_calendar import is_trading_day
        return is_trading_day
    except Exception as e:                                   # pragma: no cover
        log.error(f"trading calendar unavailable ({e}) — session arithmetic "
                  f"would count weekends; refusing to guess")
        raise


def load_symbol(symbol: str) -> pd.DataFrame | None:
    """The per-stock daily frame, date-sorted, or None.

    None rather than an empty frame: "no data for this symbol" and "a symbol
    that traded nothing" are different, and an empty frame makes the second
    look like the first to every caller downstream.
    """
    if symbol in _cache:
        return _cache[symbol]
    p = STOCKS_DIR / f"{symbol}.parquet"
    if not p.exists():
        _cache[symbol] = None
        return None
    try:
        df = pd.read_parquet(p)
    except Exception as e:
        log.warning(f"{symbol}: parquet unreadable ({e})")
        _cache[symbol] = None
        return None
    if df.empty or "date" not in df.columns:
        _cache[symbol] = None
        return None
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df = df.sort_values("date").reset_index(drop=True)
    _cache[symbol] = df
    return df


def clear_cache() -> None:
    _cache.clear()


def frame_through(symbol: str, through: date) -> pd.DataFrame | None:
    """History up to and including `through`.

    THE REPLAY DEPENDS ON THIS BEING A HARD CUT. Scoring 2026-09-24 must see
    nothing after 2026-09-24 or every level is computed with knowledge the
    session did not have, and a backtest built that way reports the future.
    """
    df = load_symbol(symbol)
    if df is None:
        return None
    out = df[df["date"] <= through]
    return out.reset_index(drop=True) if len(out) else None


def session_row(symbol: str, on: date) -> pd.Series | None:
    """That symbol's row for exactly that session, or None if it did not trade."""
    df = load_symbol(symbol)
    if df is None:
        return None
    hit = df[df["date"] == on]
    return hit.iloc[0] if len(hit) else None


def sessions_between(start: date, end: date) -> int:
    """NSE trading sessions after `start` up to and including `end`.

    SESSIONS, NOT CALENDAR DAYS. A card detected on a Friday is one session old
    on Monday; a day counter would call it three, and the heat decay would have
    aged it by three half-lives of a fifth each. The page and the arithmetic
    both read this function so they cannot disagree.
    """
    if end <= start:
        return 0
    is_td = _calendar()
    n, cur = 0, start + timedelta(days=1)
    while cur <= end:
        if is_td(cur):
            n += 1
        cur += timedelta(days=1)
    return n


def trading_days(start: date, end: date) -> list:
    is_td = _calendar()
    out, cur = [], start
    while cur <= end:
        if is_td(cur):
            out.append(cur)
        cur += timedelta(days=1)
    return out


def available_sessions(symbol: str = "RELIANCE") -> list:
    """Sessions actually present on disk, from a liquid reference symbol.

    WHY A REFERENCE SYMBOL AND NOT THE CALENDAR. The calendar says which days
    the exchange was open; this says which days were DOWNLOADED. The replay
    needs the second -- asking it to score a session whose bhavcopy is missing
    would produce a day of silent zeros, and 2026-10-08 and 10-09 are exactly
    that case on this machine.
    """
    df = load_symbol(symbol)
    return list(df["date"]) if df is not None else []


# ── RAW CSV, FOR TURNOVER ONLY ────────────────────────────────────

def _raw_path(on: date) -> Path:
    """jugaad-data's filename: cmDDMonYYYYbhav.csv."""
    return RAW_DIR / f"cm{on.strftime('%d%b%Y')}bhav.csv"


def session_vwap(symbol: str, on: date) -> float | None:
    """NSE's own session VWAP for one symbol-day, or None.

    None on anything unexpected -- file absent, symbol absent, zero volume,
    neither layout recognised. The caller DROPS the level rather than
    substituting a proxy; see docs/TSL_FLASH.md §10.5.
    """
    p = _raw_path(on)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
    except Exception as e:
        log.warning(f"{symbol}: raw bhavcopy {p.name} unreadable ({e})")
        return None
    df.columns = [c.strip() for c in df.columns]

    if "SYMBOL" in df.columns:
        # Legacy layout. Values carry leading spaces in this file -- the
        # header does too, which is why both are stripped. A filter written
        # without the strip returns an empty frame and looks like a symbol
        # that did not trade.
        sym_col, ser_col = "SYMBOL", "SERIES"
        series_ok = ("EQ", "BE")
        for c in (sym_col, ser_col):
            df[c] = df[c].astype(str).str.strip()
        hit = df[(df[sym_col] == symbol) & (df[ser_col].isin(series_ok))]
        if not len(hit):
            return None
        row = hit.iloc[0]
        # AVG_PRICE is the published VWAP. Prefer it over recomputing from
        # turnover: it is what NSE itself calls the day's average price.
        if "AVG_PRICE" in df.columns:
            try:
                v = float(row["AVG_PRICE"])
                if v > 0:
                    return round(v, 2)
            except (TypeError, ValueError):
                pass
        try:
            # TURNOVER_LACS is in hundred-thousands of rupees.
            turn = float(row["TURNOVER_LACS"]) * 1e5
            qty = float(row["TTL_TRD_QNTY"])
            return round(turn / qty, 2) if qty > 0 else None
        except (TypeError, ValueError, KeyError, ZeroDivisionError):
            return None

    if "TckrSymb" in df.columns:
        # 2026 layout. No published average price; turnover is exact.
        for c in ("TckrSymb", "SctySrs"):
            df[c] = df[c].astype(str).str.strip()
        hit = df[(df["TckrSymb"] == symbol) & (df["SctySrs"].isin(("EQ", "BE")))]
        if not len(hit):
            return None
        row = hit.iloc[0]
        try:
            turn = float(row["TtlTrfVal"])
            qty = float(row["TtlTradgVol"])
            return round(turn / qty, 2) if qty > 0 else None
        except (TypeError, ValueError, ZeroDivisionError):
            return None

    log.warning(f"{p.name}: neither bhavcopy layout recognised "
                f"(columns: {list(df.columns)[:6]}…)")
    return None


# ── DERIVED SERIES THE TRIGGERS NEED ──────────────────────────────

def vol_average(df: pd.DataFrame, window: int, through_prior: bool = True) -> float | None:
    """Mean volume over `window` sessions.

    through_prior EXCLUDES THE LAST ROW, and that is the whole point. A 20x
    volume day raises its own 20-session mean by about 95%, so including it
    turns a 20x reading into roughly 10x -- the threshold would then mean
    something other than what the config says. POLICYBZR on 2026-09-24 reads
    20.41x excluding the day and 10.4x including it.
    """
    vols = df["volume"].astype(float)
    if through_prior:
        vols = vols.iloc[:-1]
    if len(vols) < window:
        return None
    m = float(vols.iloc[-window:].mean())
    return m if m > 0 else None
