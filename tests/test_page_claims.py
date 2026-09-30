#!/usr/bin/env python3
"""
OPERATOR-FACING CLAIMS MUST MATCH THE CODE
==========================================
Four claims outlived the rules they described, all by the same mechanism: every
page here is static, nothing serves config to the browser, so each figure was a
hand-maintained literal. signals.html carried a comment admitting it -- "if Rule
1 changes in config.py, change it here too. It read Rs5,000 until 28 Aug 2026 and
matched nothing." Knowing about the mechanism did not stop it producing three
more.

    Rs2,000 daily cap        zerodha_morning   — a cap that no longer exists
    Max Rs5,000 risk/trade   signals.html      — the figure was Rs3,000
    Rs1L flat capital basis  tsl-dashboard     — the view was rebuilt on real
                                                 notional in 20260821201337
    Targets at 2:1 and 3:1   signals.html      — zone_entry drops targets

This suite is the part that makes stamping worth doing: it fails the moment a
page and atlas/config.py disagree, so the fifth one cannot be committed quietly.

TWO KINDS OF CHECK, because they fail differently:
  NUMBERS   stamped spans, compared against the constants themselves
  CLAIMS    sentences that cannot be substituted -- a wrong sentence is not
            fixable by rewriting a number, so each is asserted against the
            behaviour it describes

    python3 tests/test_page_claims.py
"""

import re
import sys
import logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))
logging.basicConfig(level=logging.CRITICAL)

import importlib.util as il                                   # noqa: E402
_sp = il.spec_from_file_location("stampcfg", ROOT / "tools/stamp_config.py")
S = il.module_from_spec(_sp)
_sp.loader.exec_module(S)


def read(name):
    p = ROOT / name
    return p.read_text(encoding="utf-8") if p.exists() else ""


def visible(src: str) -> str:
    """
    `src` with HTML and JS block comments stripped.

    A claim is what the page SHOWS. The comments here deliberately quote the
    wording they replaced -- "it said 'each sized at the Rs1L notional cap'" --
    so a naive substring check would flag the explanation as the defect and
    pressure whoever hits it into deleting the history rather than the claim.
    """
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    return re.sub(r"/\*.*?\*/", "", src, flags=re.S)


def main() -> int:
    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  {label:<56}{'ok' if cond else '** FAILS **'}  {detail}")

    print("=" * 78)
    print("STAMPED NUMBERS MATCH atlas/config.py")
    print("-" * 78)
    vals = S.values()
    bad = S.drifted(vals)
    check("no stamped span disagrees with the config", not bad,
          "" if not bad else f"{len(bad)} drifted")
    for name, key, found, expected in bad:
        print(f"      {name}: {key}  page={found!r} config={expected!r}")
    total = sum(len(S.scan(ROOT / n, vals)) for n in S.PAGES)
    check("the pages actually carry stamped spans", total > 0, f"{total} span(s)")
    # the stamper must be idempotent, or --write in a deploy would churn the tree
    check("stamping is idempotent (no drift right after a check)",
          not S.drifted(vals))

    print()
    print("CLAIMS THAT CANNOT BE STAMPED, ASSERTED AGAINST BEHAVIOUR")
    print("-" * 78)
    signals = visible(read("signals.html"))
    dash    = visible(read("tsl-dashboard.html"))
    index   = visible(read("index.html"))
    wait    = visible(read("waitlist.html"))

    # 1. TARGETS. zone_entry explicitly drops target_1/target_2/rr_1/rr_2 as
    #    trade levels: "target = NONE. Winners are held and trailed." Any page
    #    promising a 2:1/3:1 target is describing a strategy that was retired.
    ze = read("engine/zone_entry.py") or (ROOT / "engine/zone_entry.py").read_text()
    check("zone_entry still drops targets (the premise holds)",
          'for dead in ("target_1", "target_2", "rr_1", "rr_2", "sl_pct")' in ze)
    check("signals.html does not promise 2:1 / 3:1 targets",
          "Targets at 2:1" not in signals and "targets at 2:1" not in signals)
    check("signals.html does not say 'Hold until target'",
          "Hold until target" not in signals)
    # 2. THE INVENTED RATIO. (s.rr_1||2) printed "2:1 -> 3:1" even when the
    #    fields were absent -- a number produced by a fallback, shown as if it
    #    were the trade's own.
    check("no (s.rr_1||2) fallback fabricating a ratio",
          "s.rr_1||2" not in signals.replace(" ", ""))
    check("an absent rr renders as an open target instead",
          "open target" in signals)
    # 3. THE CAPITAL BASIS. v_capital_window was rebuilt on real notional.
    mig = read("migrations/20260821201337_aggregates_real_notional_peak_and_avg.sql")
    check("the capital view is built on real notional (premise holds)",
          "DROP VIEW IF EXISTS public.v_capital_window" in mig)
    check("the dashboard no longer claims a flat Rs1L basis",
          "1L notional cap" not in dash and "Rs1L notional cap" not in dash)
    # 4. THE UNIVERSE. Mechanically filtered NSE names, not a published index.
    from engine.universe import ALL_SYMBOLS
    for page, name in ((index, "index.html"), (wait, "waitlist.html")):
        check(f"{name} does not claim 'Nifty 500'",
              "Nifty 500" not in page and "NIFTY 500" not in page)
    check("the stamped universe count is the real one",
          vals["universe_count"] == str(len(ALL_SYMBOLS)),
          f"{len(ALL_SYMBOLS)} symbols")

    print()
    print("THE MODES ARE THE TWO THAT MEAN SOMETHING")
    print("-" * 78)
    from atlas.config import AGENT_MODES, HALT_MODES, DEFAULT_AGENT_MODE
    from atlas.reporting.directives import normalise_mode, handle_directive
    check("AGENT_MODES is exactly NORMAL and PAUSED",
          set(AGENT_MODES) == {"NORMAL", "PAUSED"}, str(AGENT_MODES))
    check("PAUSED is still the only halting mode",
          tuple(HALT_MODES) == ("PAUSED",))
    # A retired mode left in atlas_state must not surface as if it still existed.
    norm_ok = all(normalise_mode(g) == DEFAULT_AGENT_MODE
                  for g in ("AGGRESSIVE", "CAUTIOUS", "DEFENSIVE", "garbage",
                            "", None))
    check("a retired mode in the DB normalises to NORMAL", norm_ok)
    check("PAUSED survives normalisation", normalise_mode("PAUSED") == "PAUSED")
    # and the retired commands say so rather than falling through to "unknown"
    for cmd in ("/aggressive", "/cautious", "/defensive"):
        r = handle_directive(cmd)
        good = "NO LONGER EXISTS" in r
        ok &= good
        print(f"  {cmd + ' is answered, not ignored':<56}"
              f"{'ok' if good else '** FAILS **'}")

    print()
    print("A STALE BATCH SAYS SO, RATHER THAN POSING AS TODAY'S")
    print("-" * 78)
    # sessionDate is derived from batchDate, so a stale batch labelled itself for
    # whatever session followed it: with nothing published since 25 Sep the page
    # read "TODAY'S TRADES - Mon, 28 Sep" on the 28th and again on the 29th.
    check("the page computes an expected session of its own",
          "expectedSession" in signals)
    check("  and compares the batch against it", "isStale" in signals)
    check("  distinguishing 'not published yet' from 'published nothing'",
          "19*60" in signals.replace(" ", ""),
          "the 18:35 chain, so no alarm between 15:15 and the evening run")
    check("the stale state names the session with no signals",
          "NOTHING PUBLISHED FOR" in signals)
    check("  and labels the old cards as the last batch",
          "LAST PUBLISHED BATCH" in signals)
    check("  carrying the regime when one is known",
          "market_direction" in signals and "regime " in signals)
    # the scenarios run against the PAGE'S OWN functions, not a copy
    import shutil, subprocess
    if shutil.which("node"):
        r = subprocess.run(["node", str(ROOT / "tests/signals_batch_state.js")],
                           capture_output=True, text=True)
        check("node scenario suite passes", r.returncode == 0,
              (r.stdout.strip().splitlines() or [""])[-1])
        if r.returncode != 0:
            print(r.stdout[-900:])
    else:
        # The AWS box has no node. Say so rather than reporting a pass.
        print(f"  {'node scenario suite':<56}SKIPPED  node not installed")

    print()
    print("THE REGIME IS CONTEXT, NOT A FILTER ON WHAT IS SHOWN")
    print("-" * 78)
    check("the page marks counter-trend signals", "counterTrend" in signals)
    check("  de-emphasising rather than hiding them",
          "sc-counter" in signals and "opacity" in signals)
    check("  and labelling them on the card", "COUNTER-TREND" in signals)
    check("a regime banner frames every signal list", "regimeBanner" in signals)
    check("  naming what ATLAS will and will not act on",
          "ATLAS" in signals and "not instructions" in signals)
    # A short on the screener is information. The page must not read as a
    # short instruction, and the entry path must be unable to take one --
    # that half is proved in tests/test_regime_gate.py.
    # SHORTS ARE LIVE NOW. This asserted they were disabled, which was true while
    # the mandate was long-only. What must still hold is that the PAGE does not
    # describe a policy the agent no longer follows -- the banner used to say
    # "ATLAS itself takes neither", which became false the moment shorts shipped.
    from atlas.config import ALLOW_SHORT_ENTRIES, ALLOW_LONG_ENTRIES
    check("both sides are enabled in config",
          ALLOW_SHORT_ENTRIES and ALLOW_LONG_ENTRIES, True)
    check("the page no longer claims ATLAS takes neither side",
          "takes neither" not in signals, True)
    check("  and says which side it opens in a bearish regime",
          "opens SHORTS here" in signals, True)
    check("  and that a disagreement means cash",
          "disagree" in signals, True)
    # ONE side at a time, from the matrix, whatever the market
    from atlas.execution.atlas_entry import allowed_side, regime_allows_side
    both = []
    for reg in ("bull", "bear", "sideways", "unknown"):
        for a, d, c_, m_ in ((1200, 800, 25000, 24500), (800, 1200, 24000, 24500),
                             (1200, 800, 24000, 24500)):
            cx = {"regime": reg, "advance_count": a, "decline_count": d,
                  "nifty_close": c_, "nifty_20dma": m_, "source": "market.parquet"}
            if regime_allows_side(cx, "LONG")[0] and regime_allows_side(cx, "SHORT")[0]:
                both.append(reg)
    check("no market state permits both sides at once", not both, str(both))

    print()
    print("THE WATCHLIST IS PRESENTED AS A WATCHLIST, NOT AS CALLS")
    print("-" * 78)
    # ~86 candidates a session against ~3 signals. Merged into one list the page
    # stops saying "here are today's signals" and starts offering 89 rows to act on.
    check("the page fetches candidates separately",
          "fetchWatchlist" in signals)
    check("  the signal fetch excludes them",
          "publication_kind=eq.signal" in signals)
    check("  and the watchlist fetch asks only for them",
          "publication_kind=eq.candidate" in signals)
    check("rendered in its own section, below the signals",
          "buildWatchlist" in signals)
    # a table, not cards: the visual grammar says "different thing" before the words
    check("  as a table, not cards", "wl-tbl" in signals)
    check("  collapsed by default", 'id="wlBody" style="display:none"' in signals,
          "an open 90-row table IS the page")
    check("  and it says plainly they are not calls",
          "not calls" in signals)
    check("  and that they are outside the accuracy record",
          "accuracy record" in signals)
    # no score, grade or target column -- the fields that make a row look like a
    # recommendation, which a candidate has not earned
    wl = signals[signals.index("function buildWatchlist"):
                 signals.index("function buildSection")]
    for field in ("score", "grade", "target"):
        absent = field not in wl.lower().replace("wl-", "")
        ok &= absent
        print(f"  {'no ' + field + ' column on a candidate row':<52}"
              f"{'ok' if absent else '** PRESENTS AS A CALL **'}")
    check("distance to zone IS shown", "TO ZONE" in wl,
          "the only column that says whether a row is plausibly live")

    print()
    print("THE DEPLOY CAN REACH THE RUNNING LISTENER")
    print("-" * 78)
    wd = read("scripts/bot_watchdog.sh")
    check("the watchdog compares the deployed SHA", "rev-parse HEAD" in wd)
    check("  and restarts when it moves", "kill -TERM" in wd)
    check("  recording the SHA only after a successful spawn",
          "FAILED to start" in wd)
    bl = read("atlas/reporting/bot_listener.py")
    check("the listener persists its Telegram offset", "save_offset" in bl)
    check("  before handling the update, not after",
          bl.index("save_offset(offset)") < bl.index("process_update(update)"))

    print("-" * 78)
    print("PAGE CLAIMS:", "correct" if ok else "*** DEFECTIVE ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
