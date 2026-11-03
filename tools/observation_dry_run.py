#!/usr/bin/env python3
"""What the T+3 scorer WOULD have done over the last N sessions. Writes nothing.

The ledger did not exist for those sessions, so this RECONSTRUCTS the
observations from what the publish tables hold today -- signals, live_signals,
orb_breaks, radar_events, heavy_quiet -- with the same from_rows() calls the
publishers now make, then runs score_batch() against the real calendar, the
real parquets, NSE's event calendar and whatever surveillance snapshots exist.

It is a reconstruction, and the report says what that costs: rows a publisher
later deleted or replaced are gone, and orb_breaks / heavy_quiet keep only a few
recent sessions.

Prints counts by session, type and skip reason. It does not print, compute or
return any distribution of outcomes: no hit rate exists until real scores land.

    python3 tools/observation_dry_run.py [--sessions 10] [--root /path/to/repo]
                                         [--extra-bars DIR]

--extra-bars: a directory of NSE sec_bhavdata_full CSVs, for a checkout whose
parquets are behind (the Mac's are not refreshed by cron). Each file's own
DATE1 is used, EQ and BE series only (01b's INGEST_SERIES), and a bar is
appended only AFTER the last bar the parquet already holds -- the parquet stays
the authority wherever it has data.
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def from_lines(path, lo, hi, obs):
    """Published rows from a verified line file. Every line's md5 is re-checked;
    one mismatch refuses the whole file rather than scoring a corrupted copy."""
    import hashlib
    src = {"signal": "engine/06_push_supabase.py", "candidate": "engine/06_push_supabase.py",
           "detection": "engine/rbe_engine.py", "btst": "engine/btst_engine.py",
           "orb_break": "engine/publish_orb.py", "radar_event": "radar/store.py",
           "heavy_quiet": "engine/rbe_engine.py"}
    out = []
    for raw in Path(path).read_text().splitlines():
        if not raw.strip():
            continue
        h, line = raw.split(" ", 1)
        if hashlib.md5(line.encode()).hexdigest() != h:
            raise SystemExit(f"md5 mismatch, refusing the file: {line[:60]}")
        otype, sess, d, items = line.split("|", 3)
        if not (lo <= sess <= hi):
            continue
        for it in items.split(","):
            sym, _, px = it.partition("@")
            row = {"symbol": sym, "session": sess,
                   "direction": None if d == "-" else d,
                   "price": float(px) if px else None}
            out += obs.from_rows(otype, [row], date_key="session",
                                 direction_key=None if d == "-" else "direction",
                                 price_key="price", source_file=src[otype],
                                 key_direction=(otype != "radar_event"))
    return out


def load_extra(directory):
    import pandas as pd
    frames = []
    for f in sorted(Path(directory).glob("*.csv")):
        df = pd.read_csv(f)
        df.columns = [c.strip() for c in df.columns]
        df = df[df["SERIES"].str.strip().isin(("EQ", "BE"))]
        frames.append(pd.DataFrame({
            "date": pd.to_datetime(df["DATE1"].str.strip(), format="%d-%b-%Y").dt.date,
            "symbol": df["SYMBOL"].str.strip(),
            "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"),
            "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
            "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"),
            "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
            "prev_close": pd.to_numeric(df["PREV_CLOSE"], errors="coerce"),
            "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce")}))
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=10)
    ap.add_argument("--end", default=None, help="last session, YYYY-MM-DD (default today, IST)")
    ap.add_argument("--root", default=str(HERE),
                    help="repo whose data/ and calendar cache to read")
    ap.add_argument("--extra-bars", default=None)
    ap.add_argument("--out", default=None, help="write every decision as JSON lines")
    ap.add_argument("--from-lines", default=None,
                    help="published rows as '<md5> type|session|DIR|SYM[@price],...' "
                         "lines, for a machine with no service key")
    a = ap.parse_args()
    root = Path(a.root)
    os.environ.setdefault("TSL_HOLIDAY_CACHE", str(root / "data/reference/nse_holidays.json"))
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(HERE))
    from engine import trading_calendar as tc
    from engine import observations as obs
    from engine import score_observations as so
    import requests

    url, key = obs._cfg()
    if not key and not a.from_lines:
        print("SUPABASE_SERVICE_KEY not set (or pass --from-lines)")
        return 1
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    today = (date.fromisoformat(a.end) if a.end else
             datetime.now(timezone(timedelta(hours=5, minutes=30))).date())
    sessions, d = [], today
    while len(sessions) < a.sessions:
        if tc.is_trading_day(d):
            sessions.append(d)
        d -= timedelta(days=1)
    sessions.sort()
    lo, hi = sessions[0].isoformat(), sessions[-1].isoformat()

    def get(table, params):
        out, off = [], 0
        while True:
            r = requests.get(f"{url}/rest/v1/{table}", params=params, timeout=60,
                             headers={**h, "Range-Unit": "items",
                                      "Range": f"{off}-{off + 999}"})
            r.raise_for_status()
            rows = r.json()
            out += rows
            if len(rows) < 1000:
                return out
            off += 1000

    built = []
    if a.from_lines:
        built = from_lines(a.from_lines, lo, hi, obs)
    for kind in (() if a.from_lines else ("signal", "candidate")):
        rows = get("signals", {"signal_date": f"gte.{lo}", "and": f"(signal_date.lte.{hi})",
                               "publication_kind": f"eq.{kind}", "select": "*"})
        built += obs.from_rows(kind, rows, date_key="signal_date",
                               source_file="engine/06_push_supabase.py")
    live = [] if a.from_lines else get("live_signals", {"signal_date": f"gte.{lo}", "and": f"(signal_date.lte.{hi})",
                                "select": "*"})
    built += obs.from_rows("detection", [r for r in live if r.get("session") != "btst"],
                           date_key="signal_date", price_key="entry",
                           source_file="engine/rbe_engine.py")
    built += obs.from_rows("btst", [r for r in live if r.get("session") == "btst"],
                           date_key="signal_date", source_file="engine/btst_engine.py")
    built += [] if a.from_lines else obs.from_rows("orb_break",
                           get("orb_breaks", {"session_date": f"gte.{lo}",
                                              "and": f"(session_date.lte.{hi})", "select": "*"}),
                           date_key="session_date", direction_key="side",
                           price_key="break_close", source_file="engine/publish_orb.py")
    built += [] if a.from_lines else obs.from_rows("radar_event",
                           get("radar_events", {"event_date": f"gte.{lo}",
                                                "and": f"(event_date.lte.{hi})",
                                                "select": "symbol,sector,trigger_type,"
                                                          "event_date,status,heat,severity,"
                                                          "move_pct,vol_multiple,direction"}),
                           date_key="event_date", key_direction=False,
                           source_file="radar/store.py")
    hq = [] if a.from_lines else get("heavy_quiet", {"session_date": f"gte.{lo}", "and": f"(session_date.lte.{hi})",
                             "select": "*", "order": "snapshot_at.asc"})
    built += obs.from_rows("heavy_quiet", hq, date_key="session_date", direction_key=None,
                           price_key="ltp", source_file="engine/rbe_engine.py")

    # One observation per natural key, exactly as the writer would keep it.
    groups = defaultdict(list)
    for r in built:
        groups[(r["observation_type"], r["session_date"])].append(r)
    ledger = [x for g in groups.values() for x in obs.plan(g, [])]

    stocks = root / "data/processed/stocks"
    as_of = so.latest_ingested_session(stocks)
    extra = load_extra(a.extra_bars) if a.extra_bars else None
    if extra is not None and len(extra):
        as_of = max(as_of, max(extra["date"]))

    def prices(sym):
        import pandas as pd
        df = so.load_prices(sym, stocks)
        if extra is None:
            return df
        add = extra[extra["symbol"] == sym]
        if df is not None and len(df):
            add = add[add["date"] > df["date"].max()]
        if not len(add):
            return df
        cols = ["date", "open", "high", "low", "close", "prev_close", "volume"]
        base = df[cols] if df is not None else None
        return pd.concat([base, add[cols]] if base is not None else [add[cols]],
                         ignore_index=True).sort_values("date").reset_index(drop=True)

    scores, not_due, unresolvable = so.score_batch(
        ledger, as_of, prices=prices,
        surveillance=(so.load_surveillance if key else
                      (lambda sess: {s: None for s in sess})))

    if a.out:
        with open(a.out, "w") as fh:
            for o, sc in scores:
                fh.write(json.dumps({"type": o["observation_type"], "symbol": o["symbol"],
                                     "session": o["session_date"],
                                     "skip_reason": sc["skip_reason"],
                                     "all_reasons": sc["all_reasons"],
                                     "detail": sc["skip_detail"]}) + "\n")
    snaps = (["read from surveillance_captures"] if key else
             ["none readable: no service key, every session counts as unknown"])
    print(f"sessions {lo} .. {hi} ({len(sessions)}); latest bhavcopy in parquets: {as_of}")
    print(f"surveillance snapshots on disk: {len(snaps)} {snaps[-3:]}")
    print(f"observations reconstructed: {len(ledger)}  (from {len(built)} published rows; "
          f"{len(built) - len(ledger)} were re-sends of the same observation)")
    print(f"  by type: {dict(Counter(r['observation_type'] for r in ledger))}")
    print(f"not due (T+3 bar not yet in the parquets): {not_due}")
    print(f"unresolvable T+3 (calendar): {len(unresolvable)}")
    first = Counter(s["skip_reason"] or "SCORED" for _, s in scores)
    print(f"due: {len(scores)}  ->  would be scored {first.pop('SCORED', 0)}, "
          f"skipped {sum(first.values())}")
    print("  skipped, by recorded (first) reason:")
    for k in so.SKIP_ORDER:
        if first.get(k):
            print(f"    {k:<30} {first[k]}")
    every = Counter(r for _, s in scores for r in s["all_reasons"])
    print("  every reason that applied (one observation can carry several):")
    for k in so.SKIP_ORDER:
        if every.get(k):
            print(f"    {k:<30} {every[k]}")

    print("\nby session and type: due / scored / skipped / not due")
    per = defaultdict(lambda: Counter())
    for o, s in scores:
        c = per[(o["session_date"], o["observation_type"])]
        c["due"] += 1
        c["scored" if not s["skip_reason"] else "skipped"] += 1
    t3_of = {}
    for o in ledger:
        k = (o["session_date"], o["observation_type"])
        t3_of[o["session_date"]] = so.t_plus(date.fromisoformat(o["session_date"]))
        if k not in per:
            per[k] = Counter()
        per[k]["total"] += 1
    for (sd, t), c in sorted(per.items()):
        nd = c["total"] - c["due"] if c["total"] else 0
        print(f"  {sd}  T+3 {t3_of.get(sd)}  {t:<12} total {c['total']:>4}  due {c['due']:>4}  "
              f"scored {c['scored']:>4}  skipped {c['skipped']:>4}  not due {nd:>4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
