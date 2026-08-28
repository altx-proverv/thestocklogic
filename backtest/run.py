"""
BACKTEST — entry point
======================
Runs a config end to end and persists it. Every run is recorded, whatever it
returned: keeping only the runs that looked interesting is selection on noise.

    python3 -m backtest.run --baseline
    python3 -m backtest.run --verify-seam
"""

import sys
import time
import logging
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest import store, baseline, window as W
from backtest.config import Config, BASELINE

log = logging.getLogger("BACKTEST-RUN")


def verify_seam() -> bool:
    """
    Prove the seam holds before trusting anything to write through it.

    Not a unit test of a helper -- an assertion about the thing that keeps a
    backtest from corrupting the live record. It checks the property that
    matters: there is no way to reach a live table with a write, because no
    writer takes a table name.
    """
    import inspect
    ok = True

    print("SEAM VERIFICATION")
    print("-" * 62)

    # 1. No writer accepts a table name.
    for fn in (store.create_run, store.finish_run, store.write_signals):
        params = set(inspect.signature(fn).parameters)
        bad = params & {"table", "table_name", "path", "url", "target"}
        status = "ok" if not bad else f"FAIL takes {bad}"
        print(f"  {fn.__name__:<16} takes no table name          {status}")
        ok &= not bad

    # 2. Writer source mentions only backtest tables.
    for fn in (store.create_run, store.finish_run, store.write_signals):
        src = inspect.getsource(fn)
        hits = [t for t in store.LIVE_TABLES if f"/{t}" in src or f'"{t}"' in src]
        status = "ok" if not hits else f"FAIL references {hits}"
        print(f"  {fn.__name__:<16} names no live table          {status}")
        ok &= not hits

    # 3. The read path is GET-only.
    src = inspect.getsource(store.read_live_readonly)
    writes = [v for v in ("requests.post", "requests.patch", "requests.delete",
                          "requests.put") if v in src]
    status = "ok" if not writes else f"FAIL contains {writes}"
    print(f"  read_live_readonly  is GET-only              {status}")
    ok &= not writes

    # 4. It refuses an unknown table rather than passing it through.
    try:
        store.read_live_readonly("backtest_runs", "run_id", "run_id.asc")
        print("  read_live_readonly  rejects non-live tables  FAIL accepted one")
        ok = False
    except ValueError:
        print("  read_live_readonly  rejects non-live tables  ok")

    print("-" * 62)
    print("SEAM:", "HOLDS" if ok else "*** UNSOUND — do not run backtests ***")
    return ok


def verify_asof(symbols=None, dates=None) -> bool:
    """
    The as-of assertion. Separate from the seam and asserting a different
    property: generation for D must not read any bar after D.

    Destructive rather than declarative -- every bar after D is replaced with
    absurd prices and the candidate set must not move. It does not care how a
    leak would happen, only whether the answer changes.
    """
    from backtest import asof, replay, testb

    # Dates and symbols are chosen so candidates actually exist. An empty
    # candidate set compares equal to an empty candidate set, so a guard run on
    # quiet days passes for a leaky generator too and proves nothing. These are
    # taken from what was actually published, which guarantees the comparison
    # has something in it.
    if symbols is None or dates is None:
        live = testb.published_signals("2026-08-13", "2026-08-27")
        by_day = live.groupby("signal_date").size().sort_values(ascending=False)
        dates = dates or [d.date().isoformat() for d in by_day.head(3).index]
        symbols = symbols or sorted(
            live[live["signal_date"].isin(pd.to_datetime(dates))]["symbol"].unique())

    frames = replay.load_frames(symbols)
    market = replay.load_market()
    log.info(f"as-of guard over {len(frames)} symbols x {len(dates)} dates "
             f"(chosen for non-empty candidate sets)")

    result = asof.assert_asof(replay.generate_for_date, frames, market,
                              [pd.Timestamp(d) for d in dates],
                              label=f"({len(frames)} symbols)")
    asof.report(result)

    # A pass built entirely on empty comparisons is not a pass.
    substantive = [p for p in result["per_date"] if "identical" in str(p[2])]
    if result["passed"] and not substantive:
        print("\n  *** WEAK: every date compared empty against empty. "
              "This proves nothing — choose dates with candidates. ***")
        return False
    print(f"\n  {len(substantive)}/{len(result['per_date'])} dates carried "
          f"candidates, so the comparison was substantive on those.")
    return result["passed"]


def run_baseline(persist: bool = True) -> bool:
    """Test A, persisted per-signal."""
    cfg = BASELINE
    t0 = time.time()
    run_id = None

    if persist:
        run_id = store.create_run(
            config=cfg.to_dict(), config_hash=cfg.hash(), label=cfg.label,
            period_start=cfg.period_start, period_end=cfg.period_end,
            holdout_start=cfg.holdout_start, window_end=cfg.window_end)
        log.info(f"run {run_id} opened")

    try:
        result = baseline.run(cfg.window_end)
        baseline.report(result)

        if persist:
            pub = result["publications"]
            rows = []
            for r in pub.itertuples():
                rows.append({
                    "signal_date":      r.signal_date,
                    "symbol":           r.symbol,
                    "direction":        r.direction,
                    "setup_name":       getattr(r, "setup_name", None),
                    "resolution":       r.resolution if pd.notna(r.resolution) else None,
                    "resolved_pnl_pct": (float(r.resolved_pnl_pct)
                                         if pd.notna(r.resolved_pnl_pct) else None),
                    "resolved_on":      (r.resolved_on
                                         if pd.notna(r.resolved_on) else None),
                    "is_first_signal":  bool(r.is_first_signal),
                    "period":           ("holdout" if r.signal_date >= cfg.holdout_start
                                         else "tune"),
                })
            n = store.write_signals(run_id, rows)
            log.info(f"persisted {n} per-signal rows")
            store.finish_run(run_id, "PASS" if result["passed"] else "FAIL",
                             n_candidates=len(rows),
                             n_resolved=int(pub["resolution"].notna().sum()),
                             runtime_sec=round(time.time() - t0, 2),
                             notes="Test A — resolver parity")
        return result["passed"]

    except Exception as e:
        if persist and run_id:
            store.finish_run(run_id, "ERROR", runtime_sec=round(time.time() - t0, 2),
                             notes=f"{type(e).__name__}: {e}")
        raise


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="run Test A")
    ap.add_argument("--verify-seam", action="store_true")
    ap.add_argument("--verify-asof", action="store_true")
    ap.add_argument("--testb", action="store_true", help="run Test B (generator parity)")
    ap.add_argument("--no-persist", action="store_true")
    a = ap.parse_args()

    if a.verify_seam:
        return 0 if verify_seam() else 1

    if a.verify_asof:
        return 0 if verify_asof() else 1

    if a.testb:
        # The as-of guard gates Test B. A replay that can see past D would
        # "match" or "miss" for reasons that have nothing to do with the
        # generator, so parity would mean nothing.
        if not verify_asof():
            print("\nas-of guard failed — refusing to run Test B")
            return 1
        from backtest import testb
        print()
        r = testb.run()
        testb.report(r)
        return 0 if r["passed"] else 1

    if a.baseline:
        # The seam is checked before every persisting run, not once at build
        # time. It is the prerequisite; nothing else here is safe without it.
        if not a.no_persist and not verify_seam():
            return 1
        print()
        return 0 if run_baseline(persist=not a.no_persist) else 1

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
