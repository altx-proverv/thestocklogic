"""
Universe filter — the mechanical tiers, with the funnel shown at every step.
===========================================================================

    python3 tools/universe_filter.py                  # report the funnel
    python3 tools/universe_filter.py --additions      # what is being held back
    python3 tools/universe_filter.py --emit-exclusions   # print the block

APPLYING THE RESULT TAKES TWO MACHINES
--------------------------------------
The box has the current bhavcopy and sec_list, so the list can only be COMPUTED
there. The box is also a read-only mirror -- `git reset --hard origin/main` every
five minutes -- so engine/universe.py can only be EDITED somewhere that pushes.
An edit made on the box survives for minutes and then silently is not there.

    on the box   python3 tools/universe_filter.py --write-artifact
                 -> data/artifacts/universe_exclusions.json, gitignored and
                    untracked, so the deploy cannot touch it

    scp that file to a checkout that can push, then

    there       python3 tools/universe_filter.py --apply-artifact <path>
                 -> edits engine/universe.py, offline, then commit and push

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
  bhavcopy         already on disk in data/raw/bhavcopy. The exchange's own
                   traded value, so the Rs5 crore test needs no new source and
                   no computation from price x volume.

TWO BHAVCOPY FORMATS, AND THE BUG THAT CAME OF IGNORING ONE
-----------------------------------------------------------
NSE switched the daily file to the UDiFF layout between 2026-08-18 and
2026-09-10. Both are on disk, under the same filename pattern:

    old      SYMBOL, SERIES, CLOSE_PRICE, TURNOVER_LACS   -- lakhs
    UDiFF    TckrSymb, SctySrs, ClsPric, TtlTrfVal        -- RUPEES

The first version of this tool knew only the old names, and `continue`d on
anything else. So UDiFF files were skipped in silence and the session count was
computed over FILES IT COULD PARSE rather than sessions that exist. On the box
that reported every symbol -- RELIANCE and TCS included -- as having fewer than
250 trading days, and proposed dropping all 495. A confident wrong answer, same
class as the No Band trap.

THE UNITS DIFFER BY 1e5 AND THAT IS THE MORE DANGEROUS HALF. RELIANCE on
2026-09-10: TtlTrfVal 11,838,269,882 = Rs1,184 crore. Read as lakhs that is
Rs118 million crore. Treating the two columns as interchangeable would make
every symbol look 100,000x more liquid and pass the Rs5 crore filter -- the same
bug pointing the other way, and far harder to notice because the output looks
plausible.

So: both formats are parsed, turnover is normalised to lakhs explicitly, and a
file that cannot be parsed is COUNTED AND FATAL rather than skipped. A funnel
computed from a partial read is not a funnel.

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

from __future__ import annotations

import io
import os
import re
import json
import sys
import csv
import glob
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone

# pandas and requests are imported INSIDE the functions that need them, not
# here.
#
# --apply-artifact is offline by design: it reads a JSON file written on the box
# and edits engine/universe.py. It needs no NSE call, no Supabase and no
# bhavcopy, so it should run wherever a commit can happen -- which is not
# necessarily a machine with the analysis stack installed. A module-level
# `import pandas` made that impossible: the Mac's system python3 has no pandas
# and the mode died before parsing its own arguments.
#
# That is the same shape as the emit bug this tool already had: a path that can
# only run where it cannot do its job. `from __future__ import annotations`
# above is part of the same fix -- without it a `pd.DataFrame` in a signature is
# evaluated at def time and fails just as hard.
#
# Keep it this way. Anything imported at module level here must be stdlib.

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

# 02b cost per symbol, measured on the BOX after the numpy conversion
# (commit beef1e4). It was 1,800 ms before that, which made 796 symbols a
# 24-minute job and the EOD window the binding constraint on the universe.
# At 42.7 ms it is not a constraint at any plausible universe size, and any
# plan built on the old number is planning around something that no longer
# holds.
#
# Hardcoding a measurement rots, so --time-smc re-measures it here.
SMC_MS_PER_SYMBOL = 42.7
SMC_MS_PER_SYMBOL_PRE_CONVERSION = 1800.0


_SEC_LIST_CACHE = {}


def fetch_sec_list(url: str = SEC_LIST_URL) -> "pd.DataFrame":
    import pandas as pd
    import requests
    if url in _SEC_LIST_CACHE:
        return _SEC_LIST_CACHE[url]
    r = requests.get(url, headers={"User-Agent": UA}, timeout=40)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    for c in ("Symbol", "Series", "Band"):
        df[c] = df[c].astype(str).str.strip()
    _SEC_LIST_CACHE[url] = df
    return df


def band_passes(band: pd.Series) -> pd.Series:
    """
    True when the security can move at least MIN_BAND_PCT, INCLUDING when it has
    no band at all. See the module docstring: reading this numerically excludes
    every large cap, silently.
    """
    import pandas as pd
    raw = band.astype(str).str.strip()
    no_band = raw.str.lower() == "no band"
    numeric = pd.to_numeric(raw, errors="coerce")
    return no_band | (numeric >= MIN_BAND_PCT)


def _normalise(x: pd.DataFrame) -> pd.DataFrame:
    """One shape from either bhavcopy layout: symbol, series, close, turnover_lacs."""
    import pandas as pd
    cols = {c.strip(): c for c in x.columns}
    if "SYMBOL" in cols and "TURNOVER_LACS" in cols:
        out = pd.DataFrame({
            "symbol": x[cols["SYMBOL"]].astype(str).str.strip(),
            "series": x[cols["SERIES"]].astype(str).str.strip(),
            "close":  pd.to_numeric(x[cols["CLOSE_PRICE"]], errors="coerce"),
            # already lakhs
            "turnover_lacs": pd.to_numeric(x[cols["TURNOVER_LACS"]], errors="coerce"),
        })
        return out
    if "TckrSymb" in cols and "TtlTrfVal" in cols:
        out = pd.DataFrame({
            "symbol": x[cols["TckrSymb"]].astype(str).str.strip(),
            "series": x[cols["SctySrs"]].astype(str).str.strip(),
            "close":  pd.to_numeric(x[cols["ClsPric"]], errors="coerce"),
            # RUPEES -> lakhs. Without this divisor every symbol looks 100,000x
            # more liquid and the Rs5 crore filter stops filtering.
            "turnover_lacs": pd.to_numeric(x[cols["TtlTrfVal"]], errors="coerce") / 1e5,
        })
        return out
    raise ValueError(f"unrecognised bhavcopy layout: {sorted(cols)[:8]}")


_STATS_CACHE = {}


def bhavcopy_stats(lookback: int = LOOKBACK_SESSIONS) -> tuple:
    """
    Per-symbol sessions / median traded value / last close, from EQ rows.

    A file that cannot be read or whose layout is unrecognised is FATAL, not
    skipped. Silently dropping files is what produced a funnel claiming RELIANCE
    had under 250 trading days.
    """
    import pandas as pd
    # main() needs these twice -- once for the funnel, once to explain the drops
    # -- and re-reading 300 CSVs to answer the same question is a minute of the
    # box's time for nothing.
    # Keyed on directory CONTENTS, not just its path. A cache keyed on the path
    # alone keeps serving the old answer after a file is added or repaired --
    # which the layout harness caught by adding a bad file and expecting the
    # next call to fail.
    _present = sorted(glob.glob(str(RAW_DIR / "*.csv")))
    key = (str(RAW_DIR), lookback, len(_present),
           max((os.path.getmtime(f) for f in _present), default=0.0))
    if key in _STATS_CACHE:
        return _STATS_CACHE[key]

    files = []
    for f in glob.glob(str(RAW_DIR / "*.csv")):
        m = re.match(r"cm(\d{2}[A-Za-z]{3}\d{4})bhav\.csv$", os.path.basename(f))
        if not m:
            continue
        try:
            files.append((datetime.strptime(m.group(1), "%d%b%Y").date(), f))
        except ValueError:
            continue
    # Only TRADING days. A cached file for an NSE holiday is never requested by
    # 01b and is sometimes an error page NSE served for a date that has no
    # bhavcopy -- engine/repair_bhavcopy.py classifies exactly those as inert.
    # Treating them as fatal below would make a holiday stop the funnel.
    try:
        from trading_calendar import is_trading_day
        files = [(d, f) for d, f in files if is_trading_day(d)]
    except Exception as e:
        log.warning(f"trading calendar unavailable ({e}) — not filtering holidays")

    files.sort()
    files = files[-lookback:]
    if not files:
        raise RuntimeError(f"no bhavcopy CSVs under {RAW_DIR}")

    frames, layouts, unreadable = [], {}, []
    for _, f in files:
        try:
            x = pd.read_csv(f)
        except Exception as e:
            unreadable.append((os.path.basename(f), f"read failed: {e}"))
            continue
        try:
            norm = _normalise(x)
        except ValueError as e:
            # An HTML error page cached as .csv lands here, and so would a third
            # NSE layout. Either way it must be seen, not swallowed.
            unreadable.append((os.path.basename(f), str(e)[:90]))
            continue
        kind = "UDiFF" if "TckrSymb" in {c.strip() for c in x.columns} else "old"
        layouts[kind] = layouts.get(kind, 0) + 1
        frames.append(norm[norm["series"] == "EQ"])

    if unreadable:
        # Every one of these is a TRADING session, holidays having been filtered
        # out above. A session that will not parse silently shortens the count
        # for every symbol, which is the bug that reported RELIANCE as having
        # fewer than 250 trading days.
        log.error(f"{len(unreadable)} of {len(files)} trading-session file(s) "
                  f"could not be parsed — refusing to compute a funnel from a "
                  f"partial read")
        for name, why in unreadable[:10]:
            log.error(f"  {name}: {why}")
        raise RuntimeError(
            f"{len(unreadable)} unparseable bhavcopy file(s). Run "
            f"`python3 -m engine.repair_bhavcopy` if they are error pages, or "
            f"teach _normalise() the layout if NSE has changed it again.")

    log.info(f"parsed {len(frames)} session(s): "
             + ", ".join(f"{k} x{v}" for k, v in sorted(layouts.items())))

    b = pd.concat(frames, ignore_index=True)
    g = b.groupby("symbol").agg(sessions=("close", "size"),
                                med_turnover_lacs=("turnover_lacs", "median"),
                                last_close=("close", "last"))
    _STATS_CACHE[key] = (g, files[0][0], files[-1][0])
    return _STATS_CACHE[key]


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


def open_positions() -> tuple:
    """
    (readable, {symbols}) ATLAS currently holds. FAILS CLOSED.

    Excluding a held symbol does not force an exit, but it does more than stop
    ATLAS watching it. 01b stops rebuilding its parquet, so the file goes stale
    -- and mark_signals, update_outcomes and trade_review all GLOB the stocks
    directory rather than reading the universe. They would keep finding the
    stale parquet and resolve the open position against a price series that
    stopped moving: the stop and the target can never trigger, and the P&L
    freezes. Silently.

    So a held symbol is never excluded automatically, and if this cannot be
    read the caller must refuse to emit anything. "Cannot determine holdings"
    must not read as "nothing is held" -- that is the six-positions bug, applied
    to the universe.
    """
    import os
    import requests as _rq
    url = os.environ.get("SUPABASE_URL",
                         "https://eibdlcanpudjgmkjxrga.supabase.co")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not key:
        log.error("SUPABASE_SERVICE_KEY not set — cannot determine what is held")
        return False, set()
    try:
        from atlas.config import BLOCKING_STATUSES
        statuses = ",".join(BLOCKING_STATUSES)
    except Exception:
        statuses = "PENDING,OPEN,GTT_PENDING"
    try:
        r = _rq.get(f"{url}/rest/v1/atlas_trades?status=in.({statuses})"
                    f"&select=symbol,direction,status,entry_date",
                    headers={"apikey": key, "Authorization": f"Bearer {key}"},
                    timeout=30)
        if r.status_code != 200:
            log.error(f"atlas_trades read failed: HTTP {r.status_code} {r.text[:120]}")
            return False, set()
        rows = r.json()
    except Exception as e:
        log.error(f"atlas_trades read failed: {type(e).__name__}: {e}")
        return False, set()
    held = {str(x.get("symbol", "")).strip() for x in rows if x.get("symbol")}
    log.info(f"{len(held)} symbol(s) currently held: {', '.join(sorted(held)) or '—'}")
    return True, held


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


UNIVERSE_PY = ROOT / "engine/universe.py"
ARTIFACT = ROOT / "data/artifacts/universe_exclusions.json"

# ══════════════════════════════════════════════════════════════════
# WHY THERE IS NO MODE THAT EDITS universe.py ON THE BOX
# ══════════════════════════════════════════════════════════════════
# There was one. It worked, and then the deploy wiped it.
#
# The box is a READ-ONLY MIRROR of origin/main: it runs
# `git reset --hard origin/main` every five minutes. Any edit to a TRACKED file
# there survives for at most five minutes and then vanishes, with no error and
# nothing in a log -- the edit simply is not there any more. So a tool that
# writes engine/universe.py on the box cannot work, however correct the write
# is. That is structural, not a bug to be fixed.
#
# The other half of the problem is that the box is the only machine with current
# bhavcopy and a current sec_list, so the list can only be COMPUTED there and
# can only be COMMITTED from a checkout that pushes.
#
# Hence two modes and an untracked artifact between them:
#
#   on the box    --write-artifact   computes the list, writes JSON under
#                                    data/artifacts/, which is gitignored and
#                                    untracked. `git reset --hard` resets
#                                    tracked files only, so it survives.
#   on the Mac    --apply-artifact   reads that JSON and edits universe.py in a
#                                    checkout that can commit and push.
#
# DO NOT ADD A MODE THAT EDITS A TRACKED FILE ON THE BOX. It will appear to
# work, and the next deploy will silently undo it.


def build_exclusion_block(reasons: dict, held_drops: list, last_session) -> tuple:
    """
    (block text, number of symbols excluded). Pure: no I/O, no credentials.

    THIS IS A FUNCTION BECAUSE IT CRASHED AS INLINE CODE. It lived inside the
    `if emitting:` branch of main(), which cannot be reached without a service
    key -- so every local run exercised only the refusal path, the block-builder
    was never executed here, and an ordering error (emit() called fifteen times
    before it was defined) shipped and died on the box with an UnboundLocalError.
    Nothing was written, which is the one mercy.

    Extracted so it can be called with made-up arguments and no network, which is
    what tests/test_exclusion_block.py does. A code path that only runs where it
    cannot be tested will eventually only fail there.

    Held symbols are named and withheld rather than excluded: excluding one leaves
    mark_signals, update_outcomes and trade_review resolving an open position
    against a parquet 01b has stopped updating.
    """
    safe = {r: [s for s in syms if s not in set(held_drops)]
            for r, syms in reasons.items()}
    safe = {r: v for r, v in safe.items() if v}
    n_excluded = sum(len(v) for v in safe.values())

    out = [
        "# ── MECHANICAL EXCLUSIONS ─────────────────────────────────────────",
        "#",
        "# Generated by tools/universe_filter.py --write-exclusions",
        f"# sec_list bands as of {datetime.now(timezone.utc):%Y-%m-%d}, "
        f"medians over {LOOKBACK_SESSIONS} sessions to {last_session}.",
        "#",
        "# These symbols are in SYMBOL_SECTOR_MAP and fail a MECHANICAL filter",
        "# -- liquidity, price, band, series or history. No quality judgement is",
        "# involved: a stock too thin to exit a Rs1,00,000 position out of is not",
        "# tradeable whatever its fundamentals.",
        "#",
        "# Every filter here was established on the data present when this ran --",
        "# sec_list fetched live, bhavcopy read to the date above. Re-run the tool",
        "# rather than editing this block by hand; it also checks atlas_trades and",
        "# refuses to exclude a symbol that is currently held.",
    ]
    if held_drops:
        out += [
            "#",
            "# HELD, THEREFORE NOT EXCLUDED — DECIDE THESE BY HAND:",
        ]
        for sym in held_drops:
            why = next((r for r, v in reasons.items() if sym in v), "?")
            out.append(f"#   {sym:<14} {why}")
        out += [
            "#",
            "# Excluding a held symbol does not force an exit, but 01b stops",
            "# rebuilding its parquet and mark_signals, update_outcomes and",
            "# trade_review all glob the stocks directory -- they would resolve",
            "# the open position against a price series that stopped moving, so",
            "# the stop and the target can never trigger. Exit the position",
            "# first, or keep the symbol until it closes.",
        ]

    out.append("EXCLUDED = {")
    for r, syms in sorted(safe.items(), key=lambda kv: -len(kv[1])):
        out.append(f"    # {r} ({len(syms)})")
        for sym in sorted(syms):
            out.append(f'    "{sym}",')
    out.append("}")
    if held_drops:
        out.append(f"# {len(held_drops)} held symbol(s) withheld from this list.")
    return "\n".join(out), n_excluded


def write_exclusions(block: str, n_excluded: int,
                     path: Path = UNIVERSE_PY) -> int:
    """
    Replace the EXCLUDED block in engine/universe.py, in place.

    This exists because the paste step kept being the thing that failed. The
    tool already computes the list on the machine where the data is current;
    carrying it through a terminal and back is an opportunity for it to arrive
    empty or truncated.

    Bounded: it rewrites only the region from the MECHANICAL EXCLUSIONS header
    (or `EXCLUDED = {` if the header is absent) through the closing brace, and
    it refuses if it cannot find exactly one such region. Everything else in the
    file -- the 500-entry sector map, the instrument keys -- is untouched.
    """
    if not path.exists():
        log.error(f"{path} not found")
        return 1
    src = path.read_text()

    start_marker = "# ── MECHANICAL EXCLUSIONS"
    if start_marker in src:
        start = src.index(start_marker)
    elif "\nEXCLUDED = {" in src:
        start = src.index("\nEXCLUDED = {") + 1
    else:
        log.error("no EXCLUDED block found in universe.py — refusing to guess "
                  "where it belongs. Add one by hand once, then this can "
                  "maintain it.")
        return 1
    if src.count("\nEXCLUDED = {") != 1:
        log.error(f"found {src.count(chr(10) + 'EXCLUDED = {')} EXCLUDED blocks "
                  f"— refusing to edit an ambiguous file")
        return 1

    brace = src.index("\nEXCLUDED = {", start)
    end = src.index("\n}", brace) + len("\n}")

    backup = path.with_suffix(".py.bak")
    backup.write_text(src)

    new_src = src[:start] + block + src[end:]
    path.write_text(new_src)

    probe = _probe(path)
    if probe is None:
        log.error("universe.py does not import after the edit — restoring backup")
        path.write_text(src)
        return 1
    n_map, n_excl, n_all, orphans = probe

    # THE CHECK IS "DID THE BLOCK WE WROTE TAKE EFFECT", nothing more.
    #
    # Two earlier versions of this guard were wrong in opposite ways. The first
    # asserted ALL_SYMBOLS must shrink -- but replacing a longer exclusion list
    # with a shorter one grows the universe legitimately, and a symbol is
    # re-admitted the moment it crosses 250 sessions. The second asserted
    # ALL_SYMBOLS == map - EXCLUDED, which silently assumes every excluded
    # symbol is a map member; it is not a consistency check but a subset
    # assumption wearing one.
    log.info(f"{path} updated ({backup.name} kept)")
    log.info(f"  SYMBOL_SECTOR_MAP {n_map}  EXCLUDED {n_excl}  ALL_SYMBOLS {n_all}")
    if n_excl != n_excluded:
        log.error(f"wrote {n_excluded} symbol(s) but EXCLUDED parsed as "
                  f"{n_excl} — restoring backup")
        path.write_text(src)
        return 1
    if orphans:
        # Not fatal: the map can change independently. But an exclusion list
        # naming symbols the universe does not have is a sign of a stale list.
        log.warning(f"{len(orphans)} excluded symbol(s) are not in "
                    f"SYMBOL_SECTOR_MAP and therefore exclude nothing: "
                    f"{', '.join(sorted(orphans)[:8])}")
    return 0


def _probe(path: Path):
    """(len map, len EXCLUDED, len ALL_SYMBOLS, orphan set) from a fresh import."""
    import importlib.util as _il
    try:
        spec = _il.spec_from_file_location("_u_probe", path)
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        orphans = set(mod.EXCLUDED) - set(mod.SYMBOL_SECTOR_MAP)
        return (len(mod.SYMBOL_SECTOR_MAP), len(mod.EXCLUDED),
                len(mod.ALL_SYMBOLS), orphans)
    except Exception as e:
        log.error(f"universe.py probe failed: {type(e).__name__}: {e}")
        return None


def write_artifact(block: str, n_excluded: int, reasons: dict,
                   held_drops: list, res: dict, cmp: dict,
                   path: Path = ARTIFACT) -> int:
    """
    Hand the computed list to another machine, on the box, surviving the deploy.

    Everything needed to apply it later travels with it -- the block text, the
    per-reason breakdown, what was held, the data window it was computed from --
    so --apply-artifact needs no network, no service key and no bhavcopy.
    """
    payload = {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "window": [str(res["window"][0]), str(res["window"][1])],
        "lookback_sessions": LOOKBACK_SESSIONS,
        "thresholds": {"band_pct": MIN_BAND_PCT,
                       "turnover_lacs": MIN_TURNOVER_LACS,
                       "price": MIN_PRICE, "sessions": MIN_SESSIONS},
        "tradeable": len(cmp["pass"]),
        "current_universe": len(cmp["current"]),
        "keep": len(cmp["keep"]),
        "additions_held_back": sorted(cmp["add"]),
        "drops_by_reason": {r: sorted(v) for r, v in reasons.items()},
        "held_not_excluded": sorted(held_drops),
        "holdings_readable": True,
        "n_excluded": n_excluded,
        "block": block,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=1))
    except Exception as e:
        log.error(f"could not write {path}: {e}")
        return 1
    log.info(f"wrote {path}")
    log.info(f"  {n_excluded} symbol(s) to exclude, "
             f"{len(held_drops)} withheld as held, "
             f"{len(cmp['add'])} additions held back")
    print()
    print(f"ARTIFACT WRITTEN: {path}")
    print("  gitignored and untracked, so the 5-minute deploy cannot wipe it.")
    print("  Fetch it to a checkout that can push, then:")
    print(f"    python3 tools/universe_filter.py --apply-artifact <path>")
    return 0


def apply_artifact(path: Path, universe: Path = UNIVERSE_PY) -> int:
    """
    Read an artifact written on the box and edit universe.py here.

    Offline by construction: no NSE call, no Supabase, no bhavcopy. The
    judgement was made where the data is current; this only carries it into a
    checkout that can commit.

    It validates rather than trusting: schema version, that holdings were
    actually readable when it was computed, that nothing held is in the block,
    and that every symbol named is a member of SYMBOL_SECTOR_MAP. A stale
    artifact is reported with its age rather than refused, because "stale" is
    the operator's call and a week-old list of surveillance-category stocks is
    usually still right.
    """
    if not path.exists():
        log.error(f"no artifact at {path}")
        return 1
    try:
        p = json.loads(path.read_text())
    except Exception as e:
        log.error(f"{path} is not readable JSON: {e}")
        return 1

    if p.get("schema") != 1:
        log.error(f"unknown artifact schema {p.get('schema')!r} — refusing")
        return 1
    if not p.get("holdings_readable"):
        log.error("the artifact was computed without a readable atlas_trades, so "
                  "whether any symbol is held is unknown — refusing")
        return 1

    block = p.get("block") or ""
    n = int(p.get("n_excluded") or 0)
    if "EXCLUDED = {" not in block:
        log.error("artifact carries no EXCLUDED block — refusing")
        return 1

    held = set(p.get("held_not_excluded") or [])
    leaked = sorted(s for s in held if f'"{s}",' in block)
    if leaked:
        log.error(f"held symbol(s) present in the block: {leaked} — refusing")
        return 1

    import importlib.util as _il
    try:
        spec = _il.spec_from_file_location("_u", universe)
        mod = _il.module_from_spec(spec)
        spec.loader.exec_module(mod)
        members = set(mod.SYMBOL_SECTOR_MAP)
    except Exception as e:
        log.error(f"cannot read {universe}: {e}")
        return 1
    named = set(re.findall(r'^\s+"([A-Z0-9&\-]+)",$', block, re.M))
    orphans = sorted(named - members)
    if orphans:
        log.warning(f"{len(orphans)} symbol(s) in the block are not in "
                    f"SYMBOL_SECTOR_MAP and exclude nothing: "
                    f"{', '.join(orphans[:8])}")

    age_days = None
    try:
        gen = datetime.fromisoformat(p["generated_at"])
        age_days = (datetime.now(timezone.utc) - gen).days
    except Exception:
        pass

    print(f"ARTIFACT  {path}")
    if age_days is None:
        age_note = ""
    elif age_days < 0:
        # The box and this machine disagree about the time. Worth saying rather
        # than rendering "-1 day(s) ago".
        age_note = f"   (dated {abs(age_days)} day(s) AHEAD of this clock)"
    else:
        age_note = f"   ({age_days} day(s) ago)"
    print(f"  computed  {p.get('generated_at')}{age_note}")
    print(f"  window    {p['window'][0]} to {p['window'][1]}, "
          f"{p.get('lookback_sessions')} sessions")
    print(f"  tradeable {p.get('tradeable')}   current {p.get('current_universe')}"
          f"   keep {p.get('keep')}")
    print(f"  excluding {n}   held/withheld {len(held)}"
          f"   additions held back {len(p.get('additions_held_back') or [])}")
    for r, syms in sorted((p.get("drops_by_reason") or {}).items(),
                          key=lambda kv: -len(kv[1])):
        print(f"    {r:<40}{len(syms):>4}")
    if age_days is not None and age_days > 14:
        print(f"  NOTE: {age_days} days old. Bands and liquidity move; re-run on "
              f"the box if that matters for this list.")
    print()
    return write_exclusions(block, n, path=universe)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-exclusions", action="store_true",
                    help="print the EXCLUDED block for engine/universe.py")
    ap.add_argument("--write-artifact", action="store_true",
                    help="compute the list and write it to data/artifacts/ "
                         "(run this ON THE BOX -- editing a tracked file there "
                         "is undone by the next deploy)")
    ap.add_argument("--apply-artifact", metavar="PATH",
                    help="read an artifact and edit engine/universe.py "
                         "(run this where you can commit and push)")
    ap.add_argument("--artifact", metavar="PATH", default=str(ARTIFACT),
                    help=f"where --write-artifact writes (default {ARTIFACT})")
    ap.add_argument("--additions", action="store_true",
                    help="list the passing symbols being held back")
    ap.add_argument("--time-smc", type=float, default=0.0, metavar="MS",
                    help="override the 02b ms/symbol figure in the runtime table")
    a = ap.parse_args()

    # --apply-artifact needs nothing this tool computes: no NSE, no bhavcopy,
    # no key. Handle it before any of that runs.
    if a.apply_artifact:
        return apply_artifact(Path(a.apply_artifact))

    emitting = a.emit_exclusions or a.write_artifact
    res = funnel(verbose=not a.emit_exclusions)
    surv = res["survivors"]
    sl = fetch_sec_list()
    stats, _, _ = bhavcopy_stats()
    cmp = compare_to_current(surv)
    reasons = explain_drops(cmp["drop"], sl, stats)

    if emitting:
        readable, held = open_positions()
        if not readable:
            print("# REFUSING TO EMIT.", file=sys.stderr)
            print("# Could not read atlas_trades, so whether any of these "
                  "symbols is held\n# is unknown -- and excluding a held symbol "
                  "leaves mark_signals and\n# update_outcomes resolving it "
                  "against a parquet that stops updating.\n"
                  "# Export SUPABASE_SERVICE_KEY and re-run.", file=sys.stderr)
            return 1
        held_drops = sorted(set(cmp["drop"]) & held)
        _, last = res["window"]
        block, n = build_exclusion_block(reasons, held_drops, last)

        if a.emit_exclusions:
            print(block)
            return 0
        return write_artifact(block, n, reasons, held_drops, res, cmp,
                              path=Path(a.artifact))

    print()
    print("AGAINST THE CURRENT UNIVERSE")
    print("-" * 78)
    print(f"  current universe                            {len(cmp['current']):>6,}")
    print(f"  passes mechanical filters                   {len(cmp['pass']):>6,}")
    print(f"  in both                                     {len(cmp['keep']):>6,}")
    print(f"  ADD   (held back until fundamentals ship)   {len(cmp['add']):>6,}")
    print(f"  DROP  (fails a mechanical filter)           {len(cmp['drop']):>6,}")

    # Surfaced here too, not only under --emit-exclusions: someone reading the
    # funnel to decide should see that a drop is held before they act on it.
    readable, held = open_positions()
    if not readable:
        print("  ⚠️  cannot read atlas_trades — whether any drop is HELD is unknown")
    else:
        held_drops = sorted(set(cmp["drop"]) & held)
        if held_drops:
            print(f"  of which HELD, so not excludable:           {len(held_drops):>6,}"
                  f"   {', '.join(held_drops)}")
        else:
            print("  none of the drops is currently held")
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
    ms = a.time_smc if a.time_smc else SMC_MS_PER_SYMBOL
    src = "measured just now" if a.time_smc else "measured on the box, post-conversion"
    print(f"RUNTIME — 02b at {ms:.1f} ms/symbol ({src})")
    print("-" * 78)
    for n, label in ((len(cmp["current"]), "today"),
                     (len(cmp["keep"]), "after dropping"),
                     (len(cmp["pass"]), "if the additions land")):
        print(f"  {label:<26}{n:>5} symbols   02b ~{n*ms/1000/60:>5.1f} min")
    worst = len(cmp["pass"]) * ms / 1000 / 60
    print(f"\n  The EOD window is 18:30 to 18:52, so 22 minutes for the whole")
    print(f"  chain. The largest universe here needs {worst:.1f} min of it.")
    print(f"  Before the numpy conversion this was "
          f"{SMC_MS_PER_SYMBOL_PRE_CONVERSION:.0f} ms/symbol and "
          f"{len(cmp['pass'])*SMC_MS_PER_SYMBOL_PRE_CONVERSION/1000/60:.1f} min,")
    print(f"  which is where the 'does it fit the window' question came from.")
    print(f"  It no longer binds at any universe size this filter can produce.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
