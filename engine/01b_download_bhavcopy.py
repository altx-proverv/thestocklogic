"""
THE STOCK LOGIC — Stage 1b: Bhavcopy Download via jugaad-data
=============================================================
Uses jugaad-data which handles NSE session/cookie management.
Downloads real Bhavcopy CSVs, parses them, builds per-stock parquets.

Run from thestocklogic/ folder:
    python3 engine/01b_download_bhavcopy.py

Takes ~30-60 mins for 2 years of data.
Safe to stop and restart — skips already downloaded days.
"""

import os
import sys
import time
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm

try:
    from jugaad_data.nse import bhavcopy_save
except ImportError:
    print("Run: pip install jugaad-data")
    sys.exit(1)

# ── LOGGING ───────────────────────────────────────────────────────
Path("reports").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("reports/01b_download.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────
START_DATE    = date(2023, 1, 1)

# IST, because the box runs UTC and every NSE time below is an IST one.
IST = timezone(timedelta(hours=5, minutes=30))

# NSE publishes the day's bhavcopy in the evening, well after the 15:30 close.
# Asked for it earlier, the archive answers with an ERROR PAGE, and bhavcopy_save
# writes that HTML to disk under a .csv name. 18:00 IST is conservative: the EOD
# chain runs at 18:35, and a file that is a few hours late is a gap the next run
# fills, while a cached error page is permanent.
BHAVCOPY_READY_IST_HOUR = 18
RAW_DIR       = Path("data/raw/bhavcopy")
PROCESSED_DIR = Path("data/processed")
STOCKS_DIR    = Path("data/processed/stocks")
DELAY         = 0.3   # seconds between requests

import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from universe import ALL_SYMBOLS as _ALL_SYMS, SYMBOL_SECTOR_MAP
NIFTY100_SYMBOLS = set(_ALL_SYMS)

# Holiday calendar comes from engine/trading_calendar.py. This module used to
# keep its own set, which stopped at 2025-05-01 and had drifted apart from that
# one (this list had 2025-02-26, the other had 2025-01-26). Both are merged
# there now; every NSE holiday after May 2025 was previously treated as a
# trading day here, producing a failed download and a warning for each.
try:
    from engine.trading_calendar import is_trading_day
except ModuleNotFoundError:
    from trading_calendar import is_trading_day


# ── HELPERS ───────────────────────────────────────────────────────

def last_requestable_session(now: datetime = None) -> date:
    """
    The newest session whose bhavcopy can exist yet.

    THE BUG THIS CLOSES. END_DATE was date.today(), so any run requested today's
    file whatever the time. A 09:30 IST run on 2026-09-28 asked for a session that
    had not happened, NSE served an error page, and it was cached under
    cm28Sep2026bhav.csv -- permanently, because download_all treats existence as
    success. One mid-session run poisons that date for good.

    Before the publish hour, today is simply not asked for. The next evening run
    picks it up, which is the run that was always going to produce the signals.
    """
    now = now or datetime.now(IST)
    d = now.date()
    if is_trading_day(d) and now.hour < BHAVCOPY_READY_IST_HOUR:
        d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def get_trading_days(start: date, end: date) -> list:
    days, cur = [], start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def csv_filename(d: date) -> str:
    """jugaad-data saves files as cmDDMMMYYYYbhav.csv"""
    return f"cm{d.strftime('%d%b%Y')}bhav.csv"


def parse_bhavcopy_csv(csv_path: Path, d: date) -> pd.DataFrame:
    """
    Parses Bhavcopy CSV — handles both old and new NSE formats.
    Old format (pre-2026): SYMBOL, SERIES, OPEN_PRICE, CLOSE_PRICE, DELIV_PER etc.
    New format (2026+):    TckrSymb, SctySrs, OpnPric, ClsPric (no delivery %)
    """
    df = pd.read_csv(csv_path)
    df.columns = [c.strip() for c in df.columns]

    # Detect format
    is_new_format = "TckrSymb" in df.columns

    if is_new_format:
        # New 2026+ NSE format
        series_col = "SctySrs"
        if series_col in df.columns:
            df = df[df[series_col].str.strip() == "EQ"].copy()
        if len(df) == 0:
            return pd.DataFrame()
        col_map = {
            "TckrSymb":       "symbol",
            "OpnPric":        "open",
            "HghPric":        "high",
            "LwPric":         "low",
            "ClsPric":        "close",
            "PrvsClsgPric":   "prev_close",
            "TtlTradgVol":    "volume",
            "TtlNbOfTxsExctd":"trades",
        }
        df = df.rename(columns=col_map)
        # No delivery % in new format
        df["delivery_qty"] = np.nan
        df["delivery_pct"] = np.nan

    else:
        # Old format (pre-2026)
        if "SERIES" in df.columns:
            df = df[df["SERIES"].str.strip() == "EQ"].copy()
        if len(df) == 0:
            return pd.DataFrame()
        col_map = {
            "SYMBOL":       "symbol",
            "PREV_CLOSE":   "prev_close",
            "OPEN_PRICE":   "open",
            "HIGH_PRICE":   "high",
            "LOW_PRICE":    "low",
            "CLOSE_PRICE":  "close",
            "TTL_TRD_QNTY": "volume",
            "NO_OF_TRADES": "trades",
            "DELIV_QTY":    "delivery_qty",
            "DELIV_PER":    "delivery_pct",
        }
        df = df.rename(columns=col_map)

    # Add date
    df["date"] = pd.Timestamp(d)

    # Clean numeric columns
    num_cols = ["open","high","low","close","prev_close",
                "volume","trades","delivery_qty","delivery_pct"]
    for col in num_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "delivery_pct" in df.columns:
        df.loc[df["delivery_pct"] > 100, "delivery_pct"] = np.nan
        df.loc[df["delivery_pct"] < 0,   "delivery_pct"] = np.nan

    df = df[df["close"] > 0].copy()

    keep = ["date","symbol","open","high","low","close",
            "volume","delivery_qty","delivery_pct","trades","prev_close"]
    available = [c for c in keep if c in df.columns]
    return df[available].reset_index(drop=True)


# ── STEP 1: DOWNLOAD ──────────────────────────────────────────────

def download_all(trading_days: list) -> dict:
    """
    Downloads all Bhavcopy CSVs. Returns {date: True/False}.

    EXISTENCE IS NOT SUCCESS, which is what made a single bad fetch permanent.
    Two separate guards, because they fail at different moments:

      AT WRITE TIME   a payload that is not a bhavcopy is deleted immediately and
                      counted as failed. bhavcopy_save writes whatever NSE
                      returns, so an error page lands as a .csv and every later
                      run skips it. Refusing to cache it is the only point at
                      which that is cheap -- and unlinking something this
                      function just wrote is not touching history.

      AT SKIP TIME    an ALREADY cached bad file is reported, loudly, and counted
                      as failed rather than success. It is NOT deleted here:
                      repair_bhavcopy owns that on purpose, because deleting and
                      re-downloading is not something a nightly job should do on
                      its own initiative. What this must not do is keep calling it
                      a success, which is how two real sessions went missing from
                      every parquet in the universe without anyone noticing.
    """
    try:
        from engine.repair_bhavcopy import is_bad
    except ModuleNotFoundError:
        from repair_bhavcopy import is_bad

    results = {}
    missing, poisoned = [], []

    for d in tqdm(trading_days, desc="Downloading Bhavcopy"):
        fname = csv_filename(d)
        fpath = RAW_DIR / fname

        if fpath.exists():
            why = is_bad(fpath)
            if why:
                results[d] = False
                poisoned.append((d, why))
                log.error(f"CACHED FILE IS NOT A BHAVCOPY: {fname} — {why}. "
                          f"Not counted as downloaded.")
            else:
                results[d] = True
            continue

        try:
            bhavcopy_save(d, str(RAW_DIR))
            if not fpath.exists():
                results[d] = False
                missing.append(d)
                log.warning(f"MISSING after download: {d}")
            else:
                why = is_bad(fpath)
                if why:
                    # Never cache junk. This is the fix for the whole class: the
                    # file is gone before any later run can mistake it for data.
                    fpath.unlink()
                    results[d] = False
                    missing.append(d)
                    log.warning(f"REJECTED {d}: NSE did not return a bhavcopy "
                                f"({why}) — discarded, not cached")
                else:
                    results[d] = True
        except Exception as e:
            results[d] = False
            missing.append(d)
            log.warning(f"FAILED {d}: {e}")

        time.sleep(DELAY)

    success = sum(results.values())
    log.info(f"Downloaded: {success}/{len(trading_days)} days")
    if missing:
        log.warning(f"Missing {len(missing)} days: {missing[:5]}{'...' if len(missing)>5 else ''}")
    if poisoned:
        log.error(f"{len(poisoned)} CACHED FILE(S) ARE NOT BHAVCOPIES and every "
                  f"session they cover is absent from every parquet:")
        for d, why in poisoned[:10]:
            log.error(f"    {d}  {why}")
        log.error("    Fix: python3 -m engine.repair_bhavcopy --fix")
    return results


# ── STEP 1b: DOWNLOAD DELIVERY DATA ─────────────────────────────

def download_delivery_data(d: date) -> dict:
    """
    Download sec_bhavdata_full file which contains DELIV_PER.
    Returns {symbol: delivery_pct} for the given date.
    URL: https://archives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv
    """
    import requests as _req
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{d.strftime('%d%m%Y')}.csv"
    try:
        r = _req.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            log.warning(f"Delivery data not available for {d}: {r.status_code}")
            return {}
        import io
        df = pd.read_csv(io.StringIO(r.text))
        df.columns = [c.strip() for c in df.columns]
        # Filter EQ series only
        if "SERIES" in df.columns:
            df = df[df["SERIES"].str.strip() == "EQ"].copy()
        if "SYMBOL" not in df.columns or "DELIV_PER" not in df.columns:
            return {}
        df["SYMBOL"] = df["SYMBOL"].str.strip()
        delivery_map = dict(zip(df["SYMBOL"], pd.to_numeric(df["DELIV_PER"], errors="coerce")))
        log.info(f"Delivery data loaded for {d}: {len(delivery_map)} stocks")
        return delivery_map
    except Exception as e:
        log.warning(f"Delivery data download failed for {d}: {e}")
        return {}


# ── STEP 2: BUILD PARQUETS ────────────────────────────────────────

# Columns whose value on a SETTLED bar must never change. date and symbol are
# the key, not values, and are excluded.
VALUE_COLS = ("open", "high", "low", "close", "prev_close",
              "volume", "delivery_qty", "delivery_pct", "trades")

# A settled bar is immutable by default.
#
# THIS MODULE HAS NO MERGE, AND THAT IS THE PROBLEM
# -------------------------------------------------
# build_parquets never read the file it was about to write. It re-parsed every
# cached CSV from START_DATE forward, concatenated, and overwrote each symbol's
# parquet whole. So there was no merge to contain a bug -- and equally, nothing
# that could notice a bar changing. Every run silently re-derived all history
# from whatever the CSVs happened to say at that moment, and any difference
# became the new truth with no record that it had ever been otherwise.
#
# That is why a nine-paise move on one field is hard to attribute after the
# fact: the evidence of the previous value was overwritten by the thing that
# changed it. It also means the answer to "does this recur" was yes by
# construction, independent of what caused any single instance.
#
# Now: overlapping dates are taken from the EXISTING file, new dates are
# appended, and any disagreement is reported per field. Set
# TSL_ACCEPT_REVISIONS=1 to take the incoming values instead -- for a genuine
# NSE correction, which does happen and should then be applied deliberately and
# logged, not absorbed silently.
ACCEPT_REVISIONS = _os.environ.get("TSL_ACCEPT_REVISIONS", "") == "1"


def _diff_settled(old: pd.DataFrame, new: pd.DataFrame) -> list:
    """
    Fields that changed on a date present in BOTH frames.

    Returns [(date, column, old_value, new_value)]. NaN equals NaN: delivery_pct
    is absent for most history and a NaN-to-NaN comparison is not a revision.
    """
    if old is None or old.empty or new is None or new.empty:
        return []
    o = old.set_index("date")
    n = new.set_index("date")
    shared = o.index.intersection(n.index)
    if not len(shared):
        return []

    out = []
    for c in VALUE_COLS:
        if c not in o.columns or c not in n.columns:
            continue
        ov, nv = o.loc[shared, c], n.loc[shared, c]
        both_nan = ov.isna() & nv.isna()
        differs = ~both_nan & ~np.isclose(
            pd.to_numeric(ov, errors="coerce").astype(float),
            pd.to_numeric(nv, errors="coerce").astype(float),
            rtol=0, atol=1e-9, equal_nan=True)
        for d in shared[differs]:
            out.append((pd.Timestamp(d).date().isoformat(), c,
                        ov.loc[d], nv.loc[d]))
    return out


def _reconcile(new: pd.DataFrame, out_path: Path, symbol: str) -> tuple:
    """
    (frame_to_write, conflicts). Settled bars win over incoming ones.

    Also guards TRUNCATION, which is the same failure in a louder form: if a
    CSV goes missing from data/raw the rebuild simply omits those dates, and
    the old code wrote the shorter frame over the longer one. Dates present in
    the existing file and absent from the incoming one are carried forward.
    """
    if not out_path.exists():
        return new, []

    try:
        old = pd.read_parquet(out_path)
        old["date"] = pd.to_datetime(old["date"])
    except Exception as e:
        log.warning(f"{symbol}: existing parquet unreadable ({e}) — writing fresh")
        return new, []

    conflicts = _diff_settled(old, new)

    if ACCEPT_REVISIONS:
        keep_old = old[~old["date"].isin(set(new["date"]))]
        merged = pd.concat([new, keep_old], ignore_index=True)
    else:
        # Settled bars come from the existing file; only genuinely new dates
        # are taken from the incoming frame.
        add = new[~new["date"].isin(set(old["date"]))]
        merged = pd.concat([old, add], ignore_index=True)

    merged = (merged.sort_values("date")
                    .drop_duplicates("date")
                    .reset_index(drop=True))
    return merged, conflicts


def build_parquets(trading_days: list):
    """Loads all CSVs, filters to Nifty 100, saves per-stock parquets."""
    log.info("Loading and parsing all Bhavcopy CSVs...")

    all_dfs = []
    for d in tqdm(trading_days, desc="Parsing CSVs"):
        fpath = RAW_DIR / csv_filename(d)
        if not fpath.exists():
            continue
        try:
            df = parse_bhavcopy_csv(fpath, d)
            if len(df) > 0:
                all_dfs.append(df)
        except Exception as e:
            log.warning(f"Parse error {d}: {e}")

    if not all_dfs:
        log.error("No data parsed. Check download step.")
        return

    combined = pd.concat(all_dfs, ignore_index=True)
    log.info(f"Total rows: {len(combined):,} across {combined['symbol'].nunique()} symbols")

    # Build per-stock parquets
    # Download delivery data for most recent trading day
    latest_day = max(trading_days) if trading_days else None
    delivery_map = {}
    if latest_day:
        delivery_map = download_delivery_data(latest_day)
        log.info(f"Delivery map loaded: {len(delivery_map)} stocks")

    # Snapshot BEFORE the first write, not after the last. The guard below
    # keeps settled bars, but a guard only covers the writes it runs against
    # -- an accepted revision, a manual delete, a bug elsewhere all land on
    # the only copy there is. This is the copy that is not the only one.
    try:
        from engine.stocks_snapshot import take as take_snapshot
    except ModuleNotFoundError:
        from stocks_snapshot import take as take_snapshot
    try:
        take_snapshot(label="pre-01b rebuild")
    except Exception as e:
        log.error(f"SNAPSHOT FAILED ({e}) — continuing, but today's parquets "
                  f"are not recoverable if this run goes wrong")

    log.info("Building per-stock parquets...")
    ok, skipped, misdated = 0, 0, 0
    all_conflicts = {}

    for symbol in tqdm(sorted(NIFTY100_SYMBOLS), desc="Building stocks"):
        df = combined[combined["symbol"] == symbol].copy()

        if len(df) < 50:
            log.warning(f"{symbol}: only {len(df)} rows — skipping")
            skipped += 1
            continue

        df = df.sort_values("date").drop_duplicates("date").reset_index(drop=True)

        # Delivery % is fetched for latest_day only, so it may only be written
        # onto latest_day's bar. This wrote to df.index[-1] unconditionally: a
        # symbol that did not trade on latest_day has an OLDER bar in that
        # position, and that settled bar was given another session's delivery
        # figure -- silently, on every run, for as long as the symbol stayed
        # untraded. A mutation of history with no record that it happened.
        if delivery_map and symbol in delivery_map and latest_day is not None:
            del_pct = delivery_map[symbol]
            if not pd.isna(del_pct):
                if df["date"].iloc[-1] == pd.Timestamp(latest_day):
                    df.loc[df.index[-1], "delivery_pct"] = float(del_pct)
                else:
                    misdated += 1

        out = STOCKS_DIR / f"{symbol}.parquet"
        df, conflicts = _reconcile(df, out, symbol)
        if conflicts:
            all_conflicts[symbol] = conflicts
        df.to_parquet(out, index=False)
        ok += 1

    log.info(f"Parquets saved: {ok} OK, {skipped} skipped")
    if misdated:
        log.info(f"Delivery %% withheld from {misdated} symbol(s) whose last bar "
                 f"predates {latest_day} — they did not trade that session")

    # A settled bar changed. Loud by design: this is the class of fault that
    # invalidates every backtest run against the affected symbol, and it used
    # to be invisible.
    if all_conflicts:
        n = sum(len(v) for v in all_conflicts.values())
        verb = "APPLIED" if ACCEPT_REVISIONS else "REJECTED"

        # A rejected revision that is only logged is forgotten by the next
        # log rotation. The dates matter beyond this run: they are the bars
        # upstream now disagrees with, and every backtest still reads them.
        try:
            from engine.data_manifest import record_revisions
        except ModuleNotFoundError:
            from data_manifest import record_revisions
        try:
            record_revisions(all_conflicts, accepted=ACCEPT_REVISIONS)
        except Exception as e:
            log.error(f"could not write revisions ledger: {e}")

        log.error(f"{'='*66}")
        log.error(f"HISTORY CHANGED — {n} field(s) across {len(all_conflicts)} "
                  f"symbol(s). Incoming values {verb}.")
        if not ACCEPT_REVISIONS:
            log.error("The existing bars were kept. Re-run with "
                      "TSL_ACCEPT_REVISIONS=1 to take the new values.")
        for sym, rows in sorted(all_conflicts.items())[:20]:
            for d, c, ov, nv in rows[:6]:
                log.error(f"  {sym:<14} {d}  {c:<13} {ov!r} -> {nv!r}")
        if len(all_conflicts) > 20:
            log.error(f"  ... and {len(all_conflicts)-20} more symbols")
        log.error(f"{'='*66}")


# ── STEP 3: VALIDATE ──────────────────────────────────────────────

def validate():
    """Quick spot check on the output."""
    files = list(STOCKS_DIR.glob("*.parquet"))
    log.info(f"\n{'='*50}")
    log.info(f"VALIDATION")
    log.info(f"{'='*50}")
    log.info(f"Stock parquets: {len(files)}")

    for sym in ["RELIANCE", "TCS", "SBIN"]:
        p = STOCKS_DIR / f"{sym}.parquet"
        if p.exists():
            df = pd.read_parquet(p)
            del_cov = df["delivery_pct"].notna().mean() if "delivery_pct" in df.columns else 0
            log.info(
                f"{sym}: {len(df)} rows | "
                f"₹{df['close'].min():.0f}–₹{df['close'].max():.0f} | "
                f"vol avg {df['volume'].mean():,.0f} | "
                f"delivery coverage {del_cov:.0%}"
            )
        else:
            log.warning(f"{sym}: parquet NOT found")

    if len(files) >= 80:
        log.info("\nSTATUS: PASS — Ready for Stage 2 (indicator engine)")
    else:
        log.info(f"\nSTATUS: PARTIAL — Only {len(files)} stocks. Check missing symbols.")
    log.info(f"{'='*50}")


# ── MAIN ──────────────────────────────────────────────────────────

def main():
    log.info("THE STOCK LOGIC — Stage 1b: Bhavcopy Download")
    end = last_requestable_session()
    now_ist = datetime.now(IST)
    log.info(f"Range: {START_DATE} to {end}  "
             f"(now {now_ist:%Y-%m-%d %H:%M} IST)")
    if end != now_ist.date():
        log.info(f"today ({now_ist.date()}) is not requested: "
                 + ("its session has not closed and the bhavcopy is published "
                    f"after {BHAVCOPY_READY_IST_HOUR}:00 IST"
                    if is_trading_day(now_ist.date())
                    else "not an NSE trading day"))

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)

    trading_days = get_trading_days(START_DATE, end)
    log.info(f"Trading days: {len(trading_days)}")

    # Step 1: Download
    log.info("\n── Step 1: Downloading ──")
    download_all(trading_days)

    # Step 2: Build parquets
    log.info("\n── Step 2: Building parquets ──")
    build_parquets(trading_days)

    # Step 3: Validate
    log.info("\n── Step 3: Validating ──")
    validate()

    # Step 4: Re-fingerprint. Written AFTER the build, so the manifest always
    # describes the state a subsequent run will be checked against. The
    # immutability guard in _reconcile is what prevents drift; this is the
    # independent record that it held, and the thing a future run compares to
    # instead of finding out from a parity test weeks later.
    log.info("\n── Step 4: Manifest ──")
    try:
        from engine.data_manifest import write as write_manifest
    except ModuleNotFoundError:
        from data_manifest import write as write_manifest
    write_manifest()

    log.info("\nDone. Next: python3 engine/02_indicators.py")


if __name__ == "__main__":
    os.chdir(Path(__file__).parent.parent)
    main()
