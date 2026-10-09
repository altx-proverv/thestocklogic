#!/usr/bin/env python3
"""
Radar replay — score a historical window from real bhavcopy, write nothing.

    python3 -m tools.radar_replay --from 2026-09-23 --to 2026-10-09
    python3 -m tools.radar_replay --from 2026-09-23 --to 2026-10-09 --expect POLICYBZR:2026-09-24

WHAT THIS IS FOR. The brief's verification: replay 23 Sep - 9 Oct 2026 and
confirm POLICYBZR produces an event card on the correct day, that the scanner's
top-10 lists match an independent recomputation, and that an 11th detection
displaces the lowest-heat card while that card keeps getting outcome rows.

NO DATABASE, NO NETWORK. Config comes from shipped defaults, not radar_config,
so the replay is reproducible from the code alone rather than from whatever the
operator last typed into the table. Filings are skipped, so every cause renders
as the no-disclosed-trigger sentence -- which is the honest rendering when NSE
was not asked.

THE HARD CUT MATTERS. Each session is scored against bars.frame_through(symbol,
session), so nothing after that session is visible. A replay that let a level be
computed with later data would report the future and call it a backtest.
"""

from __future__ import annotations

import sys
import json
import logging
import argparse
from pathlib import Path
from datetime import date

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd                                            # noqa: E402

from radar import bars, detect, heat, levels, cards, outcomes   # noqa: E402
from radar.documents import NO_CAUSE_TEXT                       # noqa: E402
from radar.config import load as load_config                    # noqa: E402

log = logging.getLogger("RADAR-REPLAY")

PASS, FAIL = [], []


def check(label: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(label)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"\n         {detail}" if detail else ""))
    return ok


def head(t: str) -> None:
    print(f"\n{'=' * 78}\n{t}\n{'=' * 78}")


# ── THE INDEPENDENT RECOMPUTATION ─────────────────────────────────
#
# DELIBERATELY NOT A CALL INTO radar/. It re-derives the scanner from the
# parquets with its own pandas, its own 52-week window and its own ranking, so
# agreeing with radar/detect.py is evidence rather than a tautology. A check
# that imported scan_extremes would pass even if scan_extremes were wrong.

def independent_scanner(on: date, symbols: list, sector_map: dict,
                        vol_mult: float, top_n: int, vol_window: int) -> dict:
    rows = {"high": [], "low": []}
    for sym in symbols:
        p = ROOT / "data/processed/stocks" / f"{sym}.parquet"
        if not p.exists():
            continue
        try:
            df = pd.read_parquet(p)
        except Exception:
            continue
        if df.empty or "date" not in df.columns:
            continue
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.sort_values("date").reset_index(drop=True)
        df = df[df["date"] <= on].reset_index(drop=True)
        if len(df) < vol_window + 1 or df["date"].iloc[-1] != on:
            continue

        prior = df.iloc[:-1]
        cur = df.iloc[-1]
        # 20-day mean volume through the PRIOR session.
        avg = float(prior["volume"].astype(float).iloc[-vol_window:].mean())
        if avg <= 0:
            continue
        mult = float(cur["volume"]) / avg
        if mult < vol_mult:
            continue
        w = prior.iloc[-252:]
        if not len(w):
            continue
        try:
            dp = None if pd.isna(cur.get("delivery_pct")) else round(float(cur["delivery_pct"]), 2)
        except (TypeError, ValueError):
            dp = None
        if float(cur["high"]) >= float(w["high"].astype(float).max()):
            ext = round(float(df["high"].astype(float).iloc[-252:].max()), 2)
            rows["high"].append({
                "symbol": sym, "vol_multiple": round(mult, 2),
                "close": round(float(cur["close"]), 2), "extreme_price": ext,
                "pct_from_extreme": round((round(float(cur["close"]), 2) - ext) / ext * 100, 2),
                "delivery_pct": dp, "sector": sector_map.get(sym)})
        if float(cur["low"]) <= float(w["low"].astype(float).min()):
            ext = round(float(df["low"].astype(float).iloc[-252:].min()), 2)
            rows["low"].append({
                "symbol": sym, "vol_multiple": round(mult, 2),
                "close": round(float(cur["close"]), 2), "extreme_price": ext,
                "pct_from_extreme": round((round(float(cur["close"]), 2) - ext) / ext * 100, 2),
                "delivery_pct": dp, "sector": sector_map.get(sym)})
    for side in ("high", "low"):
        rows[side].sort(key=lambda r: (-r["vol_multiple"], r["symbol"]))
        rows[side] = rows[side][:top_n]
    return rows


def replay(frm: date, to: date, expect: list, cfg) -> dict:
    """Score every available session in the window. -> state after the window."""
    symbols, sector_map = detect.universe(cfg)
    available = [d for d in bars.available_sessions() if frm <= d <= to]

    requested = bars.trading_days(frm, to)
    missing = [d for d in requested if d not in set(available)]
    head(f"REPLAY {frm} .. {to}")
    print(f"  universe            : {len(symbols)} symbols")
    print(f"  NSE sessions in range: {len(requested)}")
    print(f"  bhavcopy on disk     : {len(available)}  "
          f"({available[0]} .. {available[-1]})" if available else "  none")
    if missing:
        # NAMED, NOT SKIPPED. A session whose bhavcopy is absent is a hole in
        # the replay and the reader has to know which days were not scored --
        # otherwise "no event on 10-08" reads as a quiet market.
        print(f"  NOT SCORED (no file) : {', '.join(str(d) for d in missing)}")

    # Carried state, exactly as the nightly run carries it in radar_events.
    events: dict = {}          # (symbol, event_date) -> row
    daily_scan: dict = {}
    per_session = []

    for i, on in enumerate(available, start=1):
        hits = detect.detect_events(on, cfg, symbols, sector_map)

        # Dedup against the open episodes, same rule as run_detection.dedup.
        latest_by_symbol = {}
        for (sym, ed), row in events.items():
            if row["status"] == "closed":
                continue
            cur = latest_by_symbol.get(sym)
            if cur is None or ed > cur:
                latest_by_symbol[sym] = ed

        for h in hits:
            prev = latest_by_symbol.get(h["symbol"])
            sev = heat.severity(h)
            if prev is not None and \
                    0 <= bars.sessions_between(prev, h["event_date"]) <= cfg.event_dedup_sessions:
                row = events[(h["symbol"], prev)]
                if sev > float(row["severity"]):
                    row.update({"severity": sev, "move_pct": h["move_pct"],
                                "vol_multiple": h["vol_multiple"],
                                "trigger_type": h["trigger_type"]})
                continue
            events[(h["symbol"], h["event_date"])] = {
                "symbol": h["symbol"], "event_date": h["event_date"],
                "sector": h.get("sector"), "trigger_type": h["trigger_type"],
                "move_pct": h["move_pct"], "vol_multiple": h["vol_multiple"],
                "direction": h["direction"], "severity": sev,
                "heat": sev, "status": "live",
                "move_threshold": h["move_threshold"],
                "vol_threshold": h["vol_threshold"],
            }

        open_rows = [r for r in events.values() if r["status"] != "closed"]
        ranked = heat.rank_hot(open_rows, on, cfg)
        for r in ranked["live"] + ranked["displaced"]:
            tgt = events[(r["symbol"], r["event_date"])]
            tgt.update({"heat": r["heat"], "severity": r["severity"],
                        "status": r["status"],
                        "sessions_since": r["sessions_since"]})
        for rank, r in enumerate(ranked["live"], start=1):
            events[(r["symbol"], r["event_date"])]["rank"] = rank

        # Outcomes for EVERY open event -- live and displaced alike.
        measured = outcomes.sweep(open_rows, cfg, as_of=on)
        for m in measured:
            key = (m["symbol"], m["event_date"])
            if key in events:
                events[key]["outcome"] = m["outcome"]
                if m["window_complete"]:
                    events[key]["status"] = "closed"

        daily_scan[on] = detect.scan_extremes(on, cfg, symbols, sector_map)
        per_session.append({"date": on, "hits": len(hits),
                            "live": len(ranked["live"]),
                            "displaced": len(ranked["displaced"])})

    print(f"\n  {'session':<13}{'triggers':>9}{'live':>7}{'displaced':>11}")
    for s in per_session:
        print(f"  {str(s['date']):<13}{s['hits']:>9}{s['live']:>7}{s['displaced']:>11}")

    return {"events": events, "scan": daily_scan, "sessions": available,
            "symbols": symbols, "sector_map": sector_map,
            "per_session": per_session, "missing": missing}


def main() -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--expect", action="append", default=[],
                    help="SYMBOL:YYYY-MM-DD — assert a card on that session")
    a = ap.parse_args()

    cfg = load_config(use_table=False)
    frm, to = date.fromisoformat(a.frm), date.fromisoformat(a.to)
    expect = []
    for raw in a.expect:
        sym, _, d = raw.partition(":")
        expect.append((sym.strip().upper(), date.fromisoformat(d.strip())))

    state = replay(frm, to, expect, cfg)
    events, scan = state["events"], state["scan"]

    # ── 1. the expected event ─────────────────────────────────────
    head("1 — THE EXPECTED EVENT, ON THE CORRECT SESSION")
    for sym, want_date in expect:
        rows = {ed: r for (s, ed), r in events.items() if s == sym}
        print(f"\n  {sym}: {len(rows)} event(s) in the window")
        for ed in sorted(rows):
            r = rows[ed]
            print(f"    {ed}  {r['trigger_type']:<9} move {r['move_pct']:>7.2f}%  "
                  f"vol {(r['vol_multiple'] or 0):>6.2f}x  sev {r['severity']:>6.2f}  "
                  f"heat {r['heat']:>6.3f}  rank {r.get('rank', '—')}  {r['status']}")
        check(f"{sym} produced an event on {want_date}", want_date in rows,
              f"sessions with an event: {[str(d) for d in sorted(rows)]}")
        if want_date in rows:
            r = rows[want_date]
            # ONE EVENT, NOT FIVE. The 5-day trigger fires for days after a
            # crash as the window drags it along; the dedup rule is what keeps
            # that one episode to one card.
            check(f"{sym} produced exactly ONE event for the episode",
                  len(rows) == 1,
                  f"dedup window is {cfg.event_dedup_sessions} sessions; "
                  f"without it the 5-day trigger fires for days after the crash")

            # And the card must build and validate.
            df = bars.frame_through(sym, want_date)
            lv = levels.compute_all(df, cfg, bars.session_vwap(sym, want_date))
            srow = bars.session_row(sym, want_date)
            ev = dict(r)
            ev["close"] = round(float(srow["close"]), 2)
            try:
                ev["delivery_pct"] = round(float(srow["delivery_pct"]), 2)
            except (TypeError, ValueError):
                ev["delivery_pct"] = None
            cause = {"cause_text": NO_CAUSE_TEXT, "cause_url": None,
                     "cause_published_at": None, "cause_sourced": False,
                     "checked": False, "documents": []}
            payload = cards.build(ev, lv, cause, r.get("outcome"),
                                  r.get("sessions_since", 0),
                                  state["sessions"][-1], cfg)
            status, notes = cards.validate(payload)
            check(f"{sym}'s card passes validation", status == "passed",
                  str(notes))
            print(f"\n    card header: rank {payload['rank']}  "
                  f"heat {payload['heat']}  "
                  f"sessions_since {payload['sessions_since']}  "
                  f"(no day counter)")
            print(f"    q01 why    : sourced={payload['q01']['why']['sourced']}  "
                  f"{payload['q01']['why']['cause_text'][:60]}")
            print(f"    q02 levels : {len(payload['q02']['levels'])} computed, "
                  f"{len(payload['q02']['swings'].get('highs', []))} swing highs, "
                  f"{len(payload['q02']['swings'].get('lows', []))} swing lows")
            print(f"    q02 outcome: {json.dumps(payload['q02']['outcome'])}")
            check(f"{sym}'s card carries the delivery-% disclaimer",
                  bool(payload["q01"].get("delivery_note")),
                  payload["q01"].get("delivery_note", ""))
            check(f"{sym}'s card declares q03 as not computed",
                  payload["q03"]["available"] is False)
            check(f"{sym}'s ipo_price is correctly absent",
                  "ipo_price" not in payload["q02"]["levels"],
                  f"series starts {df['date'].iloc[0]}, the download floor is "
                  f"{bars.DOWNLOAD_FLOOR} — so the first row is not a listing")

    # ── 2. the scanner against an independent recomputation ───────
    head("2 — SCANNER TOP-10 vs AN INDEPENDENT RECOMPUTATION")
    print("\n  The recomputation below does not call radar/detect.py. It "
          "re-derives\n  the sweep from the parquets with its own 52-week "
          "window and ranking,\n  so agreement is evidence rather than a "
          "tautology.\n")
    mismatches = []
    for on in state["sessions"]:
        ind = independent_scanner(on, state["symbols"], state["sector_map"],
                                  cfg.scanner_vol_mult,
                                  cfg.scanner_top_n_each_side,
                                  cfg.vol_avg_window)
        for side in ("high", "low"):
            mine = [(r["symbol"], r["vol_multiple"]) for r in scan[on][side]]
            theirs = [(r["symbol"], r["vol_multiple"]) for r in ind[side]]
            if mine != theirs:
                mismatches.append((on, side, mine, theirs))
        print(f"  {on}  highs {len(scan[on]['high']):>2}/{len(ind['high']):<2} "
              f" lows {len(scan[on]['low']):>2}/{len(ind['low']):<2}"
              f"   {'match' if scan[on]['high'] and True else ''}"
              f"{'' if (on, 'high', 0, 0) else ''}"
              f"{'  ** MISMATCH **' if any(m[0] == on for m in mismatches) else '  ok'}")
    check("every session's top-10 lists match the recomputation, "
          "both sides, in order", not mismatches,
          "\n         ".join(f"{d} {s}: radar={m} independent={t}"
                             for d, s, m, t in mismatches[:4])
          or f"{len(state['sessions'])} session(s) compared")

    # also check the carried fields, not just the membership
    field_bad = []
    for on in state["sessions"]:
        ind = independent_scanner(on, state["symbols"], state["sector_map"],
                                  cfg.scanner_vol_mult,
                                  cfg.scanner_top_n_each_side,
                                  cfg.vol_avg_window)
        for side in ("high", "low"):
            for mine, theirs in zip(scan[on][side], ind[side]):
                for f in ("close", "extreme_price", "pct_from_extreme",
                          "delivery_pct", "sector"):
                    if mine.get(f) != theirs.get(f):
                        field_bad.append((on, side, mine["symbol"], f,
                                          mine.get(f), theirs.get(f)))
    check("every scanner row's carried fields match too "
          "(% from extreme, volume multiple, delivery %, sector)",
          not field_bad,
          "\n         ".join(f"{d} {s} {sym}.{f}: {a} vs {b}"
                             for d, s, sym, f, a, b in field_bad[:5])
          or "close, extreme_price, pct_from_extreme, delivery_pct, sector")

    # ── 3. displacement, and measurement surviving it ─────────────
    head("3 — DISPLACEMENT, AND OUTCOMES SURVIVING IT")
    final_live = sorted([r for r in events.values() if r["status"] == "live"],
                        key=lambda r: -r["heat"])
    displaced = sorted([r for r in events.values() if r["status"] == "displaced"],
                       key=lambda r: -r["heat"])
    print(f"\n  after the window: {len(final_live)} live, "
          f"{len(displaced)} displaced, "
          f"{sum(1 for r in events.values() if r['status'] == 'closed')} closed, "
          f"{len(events)} total detected")
    print(f"\n  HOT {len(final_live)} as of {state['sessions'][-1]}")
    print(f"  {'#':>2} {'symbol':<13}{'event':<13}{'sev':>7}{'heat':>8}"
          f"{'sess':>6}  trigger")
    for r in final_live:
        print(f"  {r.get('rank', '—'):>2} {r['symbol']:<13}"
              f"{str(r['event_date']):<13}{r['severity']:>7.2f}{r['heat']:>8.3f}"
              f"{r.get('sessions_since', 0):>6}  {r['trigger_type']}")
    check(f"the live set is capped at hot_cards_n={cfg.hot_cards_n}",
          len(final_live) <= cfg.hot_cards_n, f"{len(final_live)} live")

    if displaced:
        print(f"\n  DISPLACED ({len(displaced)}) — not shown, still measured")
        print(f"  {'symbol':<13}{'event':<13}{'heat':>8}  outcome")
        for r in displaced:
            oc = r.get("outcome") or {}
            keys = [k for k in ("ret_5d", "ret_20d", "ret_60d",
                                "max_drawdown_pct") if k in oc]
            print(f"  {r['symbol']:<13}{str(r['event_date']):<13}"
                  f"{r['heat']:>8.3f}  "
                  f"{{{', '.join(f'{k}={oc[k]}' for k in keys)}}}"
                  if keys else
                  f"  {r['symbol']:<13}{str(r['event_date']):<13}"
                  f"{r['heat']:>8.3f}  (window just opened)")
        with_outcome = [r for r in displaced if (r.get("outcome") or {})]
        check("EVERY displaced event still carries an outcome record",
              len(with_outcome) == len(displaced),
              f"{len(with_outcome)}/{len(displaced)} have one — "
              f"display is not measurement, docs/TSL_FLASH.md §07")
        measured_ret = [r for r in displaced
                        if any(k.startswith("ret_") for k in (r.get("outcome") or {}))]
        check("and at least one has a realised forward return",
              bool(measured_ret),
              f"{len(measured_ret)} displaced event(s) have a ret_* horizon "
              f"filled in")
    else:
        check("the window produced a displacement to test", False,
              f"only {len(events)} event(s) detected and hot_cards_n is "
              f"{cfg.hot_cards_n}; tests/test_radar.py covers displacement "
              f"with a constructed set")

    head("RESULT")
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print(f"    FAILED: {f}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
