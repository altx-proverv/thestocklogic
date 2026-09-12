"""
Universe filter — the mechanical tiers, with the funnel shown at every step.
===========================================================================

    python3 tools/universe_filter.py                  # report the funnel
    python3 tools/universe_filter.py --emit-exclusions
    python3 tools/universe_filter.py --additions      # what is being held back

WHAT THIS DECIDES, AND WHAT IT DOES NOT
---------------------------------------
Six mechanical filters, in order. They are necessary but not sufficient: a stock
that passes them all is TRADEABLE, not QUALITY. The fundamentals tiers decide
the second question and are not built yet, which is why --emit-exclusions only
ever REMOVES symbols from the current universe. The 345 symbols that pass these
filters and are not already in it stay out until Tier 1 and Tier 2 are live --
adding 345 unscreened names is the risk the fundamentals layer exists to stop.

Removing is safe on its own: the symbols it removes fail liquidity, price or
history, and no quality judgement is involved in saying a stock trades too
thinly to exit a Rs1,00,000 position out of.

SOURCES, BOTH OFFICIAL, NEITHER SCRAPED
---------------------------------------
  sec_list.csv     nsearchives.nseindia.com/content/equities/sec_list.csv
                   Symbol, Series, Security Name, Band, Remarks. The only
                   source for the circuit band.
  bhavcopy         already on disk in data/raw/bhavcopy. TURNOVER_LACS is the
                   exchange's own traded value, so the Rs5 crore test needs no
                   new source and no computation from price x volume.

"No Band" MEANS NO PRICE BAND, AND MUST PASS
--------------------------------------------
Band is not a number. Its values are 2, 5, 10, 20, 40 and the literal string
"No Band" -- which means the security has NO price band at all, the most
permissive case there is. RELIANCE, TCS, SBIN, HDFCBANK, INFY and every other
large cap carry it: 540 of 3,538 rows, 540 of them EQ.

A numeric read turns "No Band" into NaN, and `NaN >= 10` is False, so the naive
version of this filter silently excludes the entire large-cap universe while
looking like it is doing the right thing. The first draft of this funnel
reported 564 survivors and claimed 212 current symbols were failing on band.
The correct answer is 796.
"""

import io
import os
import re
import sys
import csv
import glob
import argparse
import logging
from pathlib import Path
from datetime import datetime

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

log = logging.getLogger("universe-filter")

SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
RAW_DIR = ROOT / "data/raw/bhavcopy"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# The thresholds. Starting points, stated so they can be tested -- the 8% entry
# distance and the 82-score gate were both set by reasoning and both wrong.
MIN_BAND_PCT      = 10      # below this a stock cannot be exited during a move
MIN_TURNOVER_LACS = 500     # Rs5 crore; a Rs1L position is then under 2% of volume
MIN_PRICE         = 50.0
MIN_SESSIONS      = 250
LOOKBACK_SESSIONS = 300     # window the medians are taken over


def fetch_sec_list(url: str = SEC_LIST_URL) -> pd.DataFrame:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    for c in ("Symbol", "Series", "Band"):
        df[c] = df[c].astype(str).str.strip()
    return df


def band_passes(band: pd.Series) -> pd.Series:
    """
    True when the security can move at least MIN_BAND_PCT, INCLUDING when it has
    no band at all. See the module docstring: reading this numerically excludes
    every large cap, silently.
    """
    raw = band.astype(str).str.strip()
    no_band = raw.str.lower() == "no band"
    numeric = pd.to_numeric(raw, errors="coerce")
    return no_band | (numeric >= MIN_BAND_PCT)


def bhavcopy_stats(lookback: int = LOOKBACK_SESSIONS) -> pd.DataFrame:
    """Per-symbol sessions / median traded value / last close, from EQ rows."""
    files = []
    for f in glob.glob(str(RAW_DIR / "*.csv")):
        m = re.match(r"cm(\d{2}[A-Za-z]{3}\d{4})bhav\.csv$", os.path.basename(f))
        if not m:
            continue
        try:
            files.append((datetime.strptime(m.group(1), "%d%b%Y").date(), f))
        except ValueError:
            continue
    files.sort()
    files = files[-lookback:]
    if not files:
        raise RuntimeError(f"no bhavcopy CSVs under {RAW_DIR}")

    keep = ("SYMBOL", "SERIES", "CLOSE_PRICE", "TURNOVER_LACS")
    frames = []
    for _, f in files:
        try:
            x = pd.read_csv(f, usecols=lambda c: c.strip() in keep)
        except Exception:
            continue                       # HTML error pages and the like
        x.columns = [c.strip() for c in x.columns]
        if "SERIES" not in x.columns or "TURNOVER_LACS" not in x.columns:
            continue
        x = x[x["SERIES"].astype(str).str.strip() == "EQ"]
        frames.append(x[["SYMBOL", "CLOSE_PRICE", "TURNOVER_LACS"]])

    b = pd.concat(frames, ignore_index=True)
    b["SYMBOL"] = b["SYMBOL"].astype(str).str.strip()
    g = b.groupby("SYMBOL").agg(sessions=("CLOSE_PRICE", "size"),
                                med_turnover_lacs=("TURNOVER_LACS", "median"),
                                last_close=("CLOSE_PRICE", "last"))
    return g, files[0][0], files[-1][0]


def funnel(verbose: bool = True) -> dict:
    sl = fetch_sec_list()
    stats, first_day, last_day = bhavcopy_stats()

    steps = []
    def step(label, frame, note=""):
        prev = steps[-1][1] if steps else None
        steps.append((label, len(frame), note))
        if verbose:
            drop = "" if prev is None else f"  (-{prev - len(frame):,})"
            print(f"  {label:<44}{len(frame):>6,}{drop}  {note}")
        return frame

    if verbose:
        print(f"THE FUNNEL — bands as of today, medians over "
              f"{LOOKBACK_SESSIONS} sessions to {last_day}")
        print("-" * 78)

    s = step("NSE listed securities (sec_list.csv)", sl)
    s = step("EQ series only", sl[sl["Series"] == "EQ"],
             "BE/BZ surveillance, SM/ST SME, GS/GB gilts")
    s = step(f"circuit band >= {MIN_BAND_PCT}% or No Band", s[band_passes(s["Band"])],
             "No Band = unrestricted, must pass")
    j = s.set_index("Symbol").join(stats, how="inner")
    j = step("has traded history in the window", j)
    j = step(f">= {MIN_SESSIONS} trading days", j[j["sessions"] >= MIN_SESSIONS])
    j = step(f"price >= Rs{MIN_PRICE:.0f}", j[j["last_close"] >= MIN_PRICE])
    j = step(f"median daily value >= Rs{MIN_TURNOVER_LACS/100:.0f} crore",
             j[j["med_turnover_lacs"] >= MIN_TURNOVER_LACS],
             "a Rs1L position stays under 2% of volume")

    if verbose:
        print("-" * 78)
        print(f"  {'TRADEABLE (mechanical filters only)':<44}{len(j):>6,}")
        print("  Not yet QUALITY -- the fundamentals tiers are a separate gate.")
    return {"survivors": j, "window": (first_day, last_day), "steps": steps}


def compare_to_current(survivors: pd.DataFrame) -> dict:
    import importlib.util
    spec = importlib.util.spec_from_file_location("u", ROOT / "engine/universe.py")
    u = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(u)
    cur = set(u.ALL_SYMBOLS)
    new = set(survivors.index)
    return {"current": cur, "pass": new,
            "keep": sorted(cur & new), "add": sorted(new - cur),
            "drop": sorted(cur - new)}


def explain_drops(drop: list, sl: pd.DataFrame, stats: pd.DataFrame) -> dict:
    eq = set(sl[sl["Series"] == "EQ"]["Symbol"])
    bandok = set(sl[(sl["Series"] == "EQ") & band_passes(sl["Band"])]["Symbol"])
    out = {}
    for s in drop:
        if s not in eq:
            r = "not EQ series / not listed under this symbol"
        elif s not in bandok:
            r = f"circuit band < {MIN_BAND_PCT}%"
        elif s not in stats.index:
            r = "no traded history in the window"
        elif stats.loc[s, "sessions"] < MIN_SESSIONS:
            r = f"< {MIN_SESSIONS} sessions"
        elif stats.loc[s, "last_close"] < MIN_PRICE:
            r = f"price < Rs{MIN_PRICE:.0f}"
        else:
            r = f"median value < Rs{MIN_TURNOVER_LACS/100:.0f}cr"
        out.setdefault(r, []).append(s)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-exclusions", action="store_true",
                    help="print the EXCLUDED block for engine/universe.py")
    ap.add_argument("--additions", action="store_true",
                    help="list the passing symbols being held back")
    a = ap.parse_args()

    res = funnel(verbose=not a.emit_exclusions)
    surv = res["survivors"]
    sl = fetch_sec_list()
    stats, _, _ = bhavcopy_stats()
    cmp = compare_to_current(surv)
    reasons = explain_drops(cmp["drop"], sl, stats)

    if a.emit_exclusions:
        first, last = res["window"]
        print("# Generated by tools/universe_filter.py")
        print(f"# sec_list bands as of {datetime.utcnow():%Y-%m-%d}, medians over "
              f"{LOOKBACK_SESSIONS} sessions to {last}.")
        print("#")
        print("# These symbols are in SYMBOL_SECTOR_MAP and fail a MECHANICAL")
        print("# filter -- liquidity, price, band, series or history. No quality")
        print("# judgement is involved: a stock too thin to exit a Rs1,00,000")
        print("# position out of is not tradeable whatever its fundamentals.")
        print("EXCLUDED = {")
        for r, syms in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            print(f"    # {r} ({len(syms)})")
            for s in syms:
                print(f'    "{s}",')
        print("}")
        return 0

    print()
    print("AGAINST THE CURRENT UNIVERSE")
    print("-" * 78)
    print(f"  current universe                            {len(cmp['current']):>6,}")
    print(f"  passes mechanical filters                   {len(cmp['pass']):>6,}")
    print(f"  in both                                     {len(cmp['keep']):>6,}")
    print(f"  ADD   (held back until fundamentals ship)   {len(cmp['add']):>6,}")
    print(f"  DROP  (fails a mechanical filter)           {len(cmp['drop']):>6,}")
    print()
    print("why the drops fail:")
    for r, syms in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
        print(f"  {r:<44}{len(syms):>4}   {', '.join(syms[:5])}"
              f"{' ...' if len(syms) > 5 else ''}")

    if a.additions:
        print()
        print(f"HELD BACK — {len(cmp['add'])} symbols pass mechanically and are")
        print("NOT being added, pending Tier 1 and Tier 2. Most liquid first:")
        add = surv.loc[cmp["add"]].sort_values("med_turnover_lacs", ascending=False)
        for s, row in add.head(25).iterrows():
            print(f"  {s:<14} Rs{row['med_turnover_lacs']/100:>7.1f} cr/day"
                  f"   close Rs{row['last_close']:>9,.1f}")
        if len(add) > 25:
            print(f"  ... and {len(add)-25} more")

    print()
    print("RUNTIME IMPACT (02b measured at ~1,800 ms/symbol on the box)")
    print("-" * 78)
    for n, label in ((len(cmp["current"]), "today"),
                     (len(cmp["keep"]), "after dropping"),
                     (len(cmp["pass"]), "if the additions land")):
        print(f"  {label:<26}{n:>5} symbols   02b ~{n*1.8/60:>4.1f} min")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
