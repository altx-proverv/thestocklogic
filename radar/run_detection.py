"""
TSL FLASH — Radar nightly detection.

    python3 -m radar.run_detection                   # score the newest session
    python3 -m radar.run_detection --date 2026-09-24 # score one session
    python3 -m radar.run_detection --dry-run         # compute, write nothing
    python3 -m radar.run_detection --no-filings      # skip NSE, cause unsourced

RUNS AFTER THE SIGNALS CHAIN, AS ITS OWN CRON ENTRY -- deliberately NOT joined
to the && chain. docs/TSL_FLASH.md §13: a failure here must never stop signals
publishing, and joined with && a crash in a new module would take out the whole
screener for the night. That is a failure mode the chain has already had twice,
from modules far better tested than this one.

ORDER OF OPERATIONS, and each step is where it is for a reason:

  1. config          logged in full, with provenance, so an edit that did not
                     take effect is visible rather than inferred
  2. detect          Mode A over the universe for the session
  3. dedup           an episode already seen inside event_dedup_sessions
                     updates its event instead of creating a second one
  4. scanner         Mode B, and promote any row that also met a trigger
  5. upsert events   on (symbol, event_date), so a re-run is idempotent
  6. re-rank         heat recomputed for EVERY open event, Hot 10 settled
  7. outcomes        measured for EVERY open event -- live and displaced alike
  8. cards           built, validated, stored per trade_date
  9. completion      read back what was written; exit non-zero and Telegram
                     on zero
"""

from __future__ import annotations

import sys
import logging
import argparse
from pathlib import Path
from datetime import date, datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from radar import bars, detect, heat, levels, cards, outcomes, documents  # noqa: E402
from radar.config import load as load_config                              # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
log = logging.getLogger("RADAR-RUN")


def _alert(kind: str, text: str) -> None:
    """Telegram, with the same shape the signals chain uses.

    An undelivered alert is logged as undelivered. A scheduled job whose
    failure notice also fails silently is two problems wearing one coat.
    """
    log.error(f"[{kind}] {text}")
    try:
        from atlas.reporting.telegram import send
        if not send(f"<b>TSL FLASH — {kind}</b>\n{text}"):
            log.error(f"ALERT NOT DELIVERED [{kind}] — Telegram failed or is "
                      f"unconfigured. This alert exists only in this log.")
    except Exception as e:
        log.error(f"ALERT NOT DELIVERED [{kind}] ({e})")


def _chain_failed(reason: str) -> None:
    """Radar produced nothing. Say so loudly and exit non-zero.

    Same shape as engine/06_push_supabase._chain_failed. A scheduled job that
    fails quietly is indistinguishable from one that succeeded with nothing to
    do -- which is how the signals chain was dead for three sessions before
    anyone noticed.
    """
    _alert("RADAR DETECTION FAILED", reason)
    sys.exit(1)


def newest_session() -> date | None:
    """The newest session actually on disk.

    FROM THE DATA, NOT THE CALENDAR. The calendar says the exchange was open;
    this says bhavcopy was downloaded. Scoring a session whose file is missing
    would produce a day of silent zeros -- every symbol "did not trade" -- and
    report success.
    """
    sessions = bars.available_sessions()
    return sessions[-1] if sessions else None


def dedup(events: list, prior: list, cfg) -> tuple:
    """Split fresh detections from re-triggers of a live episode.

    -> (new_events, refresh[(prior_row, hit)])

    docs/TSL_FLASH.md §03. POLICYBZR fell 36% on 2026-09-24 and then satisfied
    shock_5d on 09-25, 09-28, 09-29 and 09-30 as the five-day window dragged the
    crash behind it. Without this, one episode takes five of the ten cards and
    the Hot 10 describes a single stock.
    """
    by_symbol = {}
    for p in prior:
        ed = p["event_date"]
        if isinstance(ed, str):
            ed = date.fromisoformat(ed)
        cur = by_symbol.get(p["symbol"])
        if cur is None or ed > cur[0]:
            by_symbol[p["symbol"]] = (ed, p)

    fresh, refresh = [], []
    for e in events:
        hit = by_symbol.get(e["symbol"])
        if not hit:
            fresh.append(e)
            continue
        prior_date, prior_row = hit
        gap = bars.sessions_between(prior_date, e["event_date"])
        if 0 <= gap <= int(cfg.event_dedup_sessions):
            refresh.append((prior_row, e))
        else:
            fresh.append(e)
    if refresh:
        log.info(f"dedup: {len(refresh)} re-trigger(s) folded into a live "
                 f"episode: "
                 + ", ".join(f"{p['symbol']}({p['event_date']})"
                             for p, _ in refresh))
    return fresh, refresh


def run(on: date | None = None, dry: bool = False, use_table: bool = True,
        fetch_filings: bool = True) -> int:
    cfg = load_config(use_table=use_table)
    log.info(cfg.log_table())

    on = on or newest_session()
    if on is None:
        _chain_failed("no bhavcopy on disk at all — data/processed/stocks is "
                      "empty or unreadable. Radar scored nothing.")
    log.info(f"scoring session {on}")

    if on not in set(bars.available_sessions()):
        _chain_failed(f"no bhavcopy for {on}. The signals chain downloads it; "
                      f"Radar does not, and will not score a session it cannot "
                      f"see rather than report a day of zeros.")

    symbols, sector_map = detect.universe(cfg)
    log.info(f"universe: {len(symbols)} symbols")

    # ── 2/3. detect, then dedup against what is already open ──────
    hits = detect.detect_events(on, cfg, symbols, sector_map)

    prior = []
    if not dry:
        from radar import store
        prior = store.open_events()
    fresh, refresh = dedup(hits, prior, cfg)

    # ── 4. the extremes scanner ───────────────────────────────────
    scan = detect.scan_extremes(on, cfg, symbols, sector_map)
    triggered = {h["symbol"] for h in hits}
    scan_rows = []
    for side in ("high", "low"):
        for r in scan[side]:
            r["promoted"] = r["symbol"] in triggered
            scan_rows.append(r)

    # The filing FLAG on scanner rows. One NSE session shared across the whole
    # sweep; skipped entirely when filings are off.
    if fetch_filings and scan_rows:
        try:
            sess = documents._session()
            for r in scan_rows:
                try:
                    r["filing_recent"] = documents.had_recent_filing(
                        r["symbol"], on, cfg, session=sess)
                except Exception as e:
                    log.warning(f"{r['symbol']}: filing flag failed ({e})")
                    r["filing_recent"] = False
        except Exception as e:
            log.warning(f"NSE session unavailable ({e}) — filing flags are "
                        f"all False and the page should be read that way")

    if dry:
        _report_dry(on, hits, fresh, refresh, scan, cfg)
        return 0

    from radar import store

    # ── 5. upsert events ──────────────────────────────────────────
    to_write = []
    for e in fresh:
        row = dict(e)
        row["severity"] = heat.severity(e)
        row["heat"] = row["severity"]          # as-of its own day; re-ranked below
        row["status"] = "live"
        to_write.append(row)
    for prior_row, e in refresh:
        # A RE-TRIGGER RAISES HEAT, NEVER LOWERS IT. The episode's severity is
        # the most extreme thing that happened in it; a milder fifth day must
        # not demote the crash that started it.
        new_sev = heat.severity(e)
        old_sev = float(prior_row.get("severity") or 0)
        if new_sev > old_sev:
            to_write.append({**prior_row, "severity": new_sev,
                             "move_pct": e.get("move_pct"),
                             "vol_multiple": e.get("vol_multiple"),
                             "trigger_type": e["trigger_type"],
                             "event_date": prior_row["event_date"],
                             "symbol": prior_row["symbol"]})

    try:
        stored = store.upsert_events(to_write)
        log.info(f"upserted {len(stored)} event row(s)")
    except store.StoreUnavailable as e:
        _chain_failed(f"could not write radar_events: {e}")

    # ── 6. re-rank EVERY open event ───────────────────────────────
    try:
        open_now = store.open_events()
    except store.StoreUnavailable as e:
        _chain_failed(f"could not read radar_events back: {e}")

    for r in open_now:
        if isinstance(r["event_date"], str):
            r["event_date"] = date.fromisoformat(r["event_date"])
    ranked = heat.rank_hot(open_now, on, cfg)
    for i, r in enumerate(ranked["live"], start=1):
        r["rank"] = i

    for r in ranked["live"] + ranked["displaced"]:
        store.update_event(r["id"], {"heat": r["heat"],
                                     "severity": r["severity"],
                                     "status": r["status"]})
    log.info(f"Hot {len(ranked['live'])} settled; "
             f"{len(ranked['displaced'])} displaced")

    # ── 7. outcomes for EVERY open event, live and displaced ──────
    #
    # docs/TSL_FLASH.md §07. The list passed here is open_now, which is
    # status != closed -- it is NOT ranked["live"]. A displaced event keeps
    # recording forward returns; the base-rate library depends on it.
    measured = outcomes.sweep(open_now, cfg, as_of=on)
    n_closed = 0
    for m in measured:
        fields = {"outcome": m["outcome"]}
        if m["window_complete"]:
            fields["status"] = "closed"
            n_closed += 1
        store.update_event(m["id"], fields)
    log.info(f"outcomes written for {len(measured)} event(s); "
             f"{n_closed} window(s) closed")

    # ── 8. cards for the live set ─────────────────────────────────
    n_cards, n_failed, n_sourced = 0, 0, 0
    sess = None
    if fetch_filings:
        try:
            sess = documents._session()
        except Exception as e:
            log.warning(f"NSE session unavailable ({e}) — every card will "
                        f"carry the no-disclosed-trigger sentence")

    for r in ranked["live"]:
        ed = r["event_date"]
        df = bars.frame_through(r["symbol"], ed)
        if df is None:
            log.warning(f"{r['symbol']}: no frame for {ed}, card skipped")
            continue
        vwap = bars.session_vwap(r["symbol"], ed)
        level_rows = levels.compute_all(df, cfg, vwap)
        try:
            store.upsert_levels(r["id"], level_rows)
        except store.StoreUnavailable as e:
            log.error(f"{r['symbol']}: levels not written ({e})")

        if fetch_filings:
            cause = documents.attribute(r["symbol"], ed, cfg, session=sess)
            if cause.get("documents"):
                try:
                    store.upsert_documents(cause["documents"])
                except store.StoreUnavailable as e:
                    log.error(f"{r['symbol']}: documents not written ({e})")
        else:
            cause = {"cause_text": documents.NO_CAUSE_TEXT, "cause_url": None,
                     "cause_published_at": None, "cause_sourced": False,
                     "checked": False, "documents": []}
        n_sourced += 1 if cause.get("cause_sourced") else 0

        srow = bars.session_row(r["symbol"], ed)
        ev = dict(r)
        if srow is not None:
            try:
                import pandas as pd
                dp = srow.get("delivery_pct")
                ev["delivery_pct"] = (None if dp is None or pd.isna(dp)
                                      else round(float(dp), 2))
                ev["close"] = round(float(srow["close"]), 2)
            except Exception:
                pass

        oc = next((m["outcome"] for m in measured if m["id"] == r["id"]), None)
        payload = cards.build(ev, level_rows, cause, oc,
                              r["sessions_since"], on, cfg)
        status, notes = cards.validate(payload)
        if status == "failed":
            n_failed += 1
        try:
            store.upsert_card(r["id"], on, payload, status, notes)
            n_cards += 1
        except store.StoreUnavailable as e:
            log.error(f"{r['symbol']}: card not written ({e})")

    # ── scanner rows ──────────────────────────────────────────────
    try:
        n_scan = store.upsert_scanner(scan_rows)
        log.info(f"scanner: {n_scan} row(s) written")
    except store.StoreUnavailable as e:
        log.error(f"scanner rows not written ({e})")
        n_scan = 0

    # ── 9. COMPLETION CHECK — read back, do not trust the POST ────
    #
    # requests.post does not raise on 4xx and PostgREST discards an entire
    # insert that names an unknown column. A 200 from the write is not evidence
    # that rows are there; the only acceptable proof is a SELECT.
    iso = on.isoformat()
    n_live = store.count("radar_events", "?status=eq.live&select=id")
    n_cards_live = store.count("radar_card_versions",
                               f"?trade_date=eq.{iso}&validation_status=eq.passed"
                               f"&select=id")
    n_scan_live = store.count("radar_scanner_daily",
                              f"?trade_date=eq.{iso}&select=id")

    log.info(f"COMPLETION CHECK: {n_live} live event(s), "
             f"{n_cards_live} passed card(s) for {iso}, "
             f"{n_scan_live} scanner row(s) for {iso}")

    if n_live < 0 or n_cards_live < 0 or n_scan_live < 0:
        _chain_failed(f"wrote Radar output for {iso}, then could not read it "
                      f"back (events={n_live}, cards={n_cards_live}, "
                      f"scanner={n_scan_live}).")
    if n_live == 0 and n_scan_live == 0:
        # BOTH ZERO IS A FAILURE. Either alone can legitimately be zero -- a
        # quiet session produces no events, and a session where nothing printed
        # a 52-week extreme on volume produces no scanner rows. Both zero on a
        # 706-symbol universe means the write was rejected or the universe did
        # not resolve, not that the market stood still.
        _chain_failed(f"zero live events AND zero scanner rows for {iso} "
                      f"across {len(symbols)} symbols. Detection found "
                      f"{len(hits)} trigger(s) and the scanner found "
                      f"{len(scan_rows)} row(s) before the write, so the write "
                      f"was rejected.")
    if n_failed:
        _alert("RADAR CARD VALIDATION",
               f"{n_failed} of {n_cards} card(s) for {iso} FAILED validation "
               f"and will not be served. Check radar_card_versions."
               f"validation_notes.")

    log.info(f"done: {len(ranked['live'])} live, "
             f"{len(ranked['displaced'])} displaced, {n_cards} card(s) "
             f"({n_sourced} with a cited document), {n_scan} scanner row(s)")
    return 0


def _report_dry(on, hits, fresh, refresh, scan, cfg) -> None:
    print(f"\n── RADAR DRY RUN — {on} ──")
    print(f"\nMODE A — {len(hits)} trigger(s), {len(fresh)} new, "
          f"{len(refresh)} folded into a live episode")
    print(f"  {'symbol':<13}{'trigger':<11}{'move%':>9}{'volx':>9}{'sev':>8}  dir")
    for h in sorted(hits, key=lambda x: -heat.severity(x)):
        print(f"  {h['symbol']:<13}{h['trigger_type']:<11}"
              f"{h['move_pct']:>9.2f}{(h['vol_multiple'] or 0):>9.2f}"
              f"{heat.severity(h):>8.2f}  {h['direction']}")
    for side in ("high", "low"):
        print(f"\nMODE B — 52-week {side}s, top {cfg.scanner_top_n_each_side}")
        print(f"  {'#':>2} {'symbol':<13}{'close':>10}{'from ext%':>11}"
              f"{'volx':>8}{'deliv%':>8}  sector")
        for r in scan[side]:
            dp = f"{r['delivery_pct']:.1f}" if r.get("delivery_pct") is not None else "—"
            print(f"  {r['rank_in_side']:>2} {r['symbol']:<13}{r['close']:>10,.2f}"
                  f"{(r['pct_from_extreme'] or 0):>11.2f}{r['vol_multiple']:>8.2f}"
                  f"{dp:>8}  {r.get('sector')}")
    print("\nnothing was written (--dry-run)\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="session to score, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-config-table", action="store_true",
                    help="shipped defaults only; what the replay uses")
    ap.add_argument("--no-filings", action="store_true",
                    help="skip NSE; every cause renders unsourced")
    a = ap.parse_args()
    on = date.fromisoformat(a.date) if a.date else None
    return run(on=on, dry=a.dry_run, use_table=not a.no_config_table,
               fetch_filings=not a.no_filings)


if __name__ == "__main__":
    sys.exit(main())
