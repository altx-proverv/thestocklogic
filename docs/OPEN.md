# OPEN — every item, one place

Rules live in [CHARTER.md](CHARTER.md). This file holds items: things that are
wrong, undecided, waiting on data, settled, or fixed. Seeded 2026-11-09 from the
week of 2026-11-03; `docs/open_questions.md` has not been folded in yet beyond
the pointers in section 3.

**Fields, every item.** ID (stable, never reused) · Status (open / decided / in
progress / fixed-unverified / fixed-verified / won't fix) · Found (date, how) ·
Evidence (file:line or query; marked VERIFIED or INFERRED) · Impact (what is
wrong, where, since when) · Decision (and date) · Fix (commit) · Verified (how,
where it ran) · Related.

---

## 1. WAITING ON YOU — decisions

### D-001 · `live_prices.change_pct` is structurally 0
- **Status** decided 2026-11-09 · **Found** 2026-11-09, substitution sweep
- **Evidence** VERIFIED: `engine/upstox_ws.py:557` divides by Upstox `ohlc.close`, which intraday is the current price; 744/744 rows read 0.0 on 2026-11-09 (CHOLAFIN 1773.40 vs prior close 1785.00 is −0.65%, stored 0.0).
- **Impact** every consumer shows an unmeasured 0: D-002, D-003.
- **Decision** (2026-11-09, adopted from the recommendation) real value with age — compute against our own prior close (`daily_closes`); store NULL until then.
- **Related** D-002, D-003, D-007
- **Partly:** D-001 itself not yet implemented.

### D-002 · The ORB tape prints 0.00% for every break
- **Status** decided 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED: `engine/publish_orb.py:117` copies D-001; 23/23 `orb_breaks` rows ever published carry 0.
- **Decision** (2026-11-09, adopted from the recommendation) follows D-001.

### D-003 · signals.html "● LIVE" badge: `change_pct||0`, no age read
- **Status** decided 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED: `signals.html:663`; `:630` selects `symbol,ltp,change_pct` — no `updated_at`.
- **Decision** (2026-11-09, adopted from the recommendation) real value with age: LIVE only for a row from this session, else "as of HH:MM"; change % blank until D-001.

### D-004 · BTST: missing/zero price → previous close, unlabelled; "live" is ~3h old
- **Status** decided 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED: `engine/btst_engine.py:137, 299`; `reports/btst.log`: 147 of 212 runs loaded 0 live prices (49 sessions, 2026-06-05..08-12). INFERRED from the schedule: BTST runs 09:00 UTC, the last live_prices write before it is 06:00 UTC.
- **Decision** (2026-11-09, adopted from the recommendation) labelled substitute ("last close, no live quote") plus the price's age.

### D-005 · upstox_ws writes `ltp = 0` for a quote with no last_price
- **Status** decided 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED: `engine/upstox_ws.py:556`; was CHOLAFIN's row 2026-09-22..11-06.
- **Decision** (2026-11-09, adopted from the recommendation) show nothing: skip the row so the old one keeps its honest timestamp.

### D-006 · 167 published outcomes scored on an invented ±0.2% zone
- **Status** decided 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED: `engine/update_outcomes.py:111-112` (also `engine/trade_review.py:60-61`); 167 pre-October `signals` rows with no entry band, all in `signal_outcomes`. None after October.
- **Decision** (2026-11-09, adopted from the recommendation) labelled substitute: mark those outcomes as scored on a synthetic zone. It is the published record.
- **Related** [signal record defects]

### D-007 · `upstox_ws.day_ret` is structurally 0 (no reader)
- **Status** decided 2026-11-09 · **Evidence** VERIFIED: `engine/upstox_ws.py:174`; 9/9 rows in `live_longs_closing.parquet`. Nothing reads it.
- **Decision** (2026-11-09, adopted from the recommendation) drop or compute with D-001.

### D-008 · 03b fills missing scoring inputs with plausible values
- **Status** decided 2026-11-09: IN SCOPE. A missing ATR defaulting to 2% of close sets stops and targets, so it is a risk number. Own task; not started
- **Evidence** VERIFIED: `engine/03b_score.py:411` VIX→15, `:490` %-from-52w→−5, `:578` ATR→2% of close (sets stops and targets), `:577` close→0. How often: NOT MEASURED.

### D-009 · market_open's entry Telegram shows a signal price or ₹0 as the fill
- **Status** decided 2026-11-09 · **Evidence** VERIFIED: `atlas/signal/market_open.py:1251-1253`.
- **Decision** (2026-11-09, adopted from the recommendation) labelled: "fill unknown, signal ₹x".

### D-010 · trade_outcome_checker writes daily/weekly P&L 0 on a failed read
- **Status** decided 2026-11-09 · **Evidence** VERIFIED: `atlas/execution/trade_outcome_checker.py:238, 245`; PATCH result unchecked. VERIFIED no gate reads `atlas_state.daily_pnl` (`kill_switch.py:141`, caps removed) and no page does.
- **Decision** (2026-11-09, adopted from the recommendation) show nothing: skip the write, log.

### D-011 · signals.html ORB ticker: `ltp||entry`; missing PDH counts as confirmed
- **Status** decided 2026-11-09 · **Evidence** VERIFIED: `signals.html:1230, 1244`.
- **Decision** (2026-11-09, adopted from the recommendation) no PDH flag without a price.

### D-012 · atlas.html `riskOf` → 0 when the stop is missing
- **Status** decided 2026-11-09 · **Evidence** VERIFIED: `atlas.html:472`.
- **Decision** (2026-11-09, adopted from the recommendation) count "n without a stop" beside the total.

### D-013 · 13 "< 250 sessions" exclusions no longer hold
- **Status** decided 2026-11-09: lift all 13 as one dated change; boundary date 2026-11-09 (row counts measured through that session). Not yet applied — held with the other code changes after the operator asked for only the pause fix on the night of 2026-11-09 · **Found** 2026-11-09
- **Evidence** VERIFIED (parquet row counts, 2026-11-09): CANHLIFE 268, GROWW 253, JAINREC 279, LENSKART 255, LGEINDIA 271, OLAELEC 564, PINELABS 251, PIRAMALFIN 256, SCHNEIDER 967, TATACAP 272, TMCV 253, TMPV 265, URBANCO 289. Still true: EMMVEE, ICICIAMC, JSWDULUX, LTM, MEESHO, PWL, TENNIND. INFERRED: SCHNEIDER was short only because its BE sessions were not ingested before 2026-10-15.
- **Also checked** price < ₹50: all 14 still true; HFCL, MOREPENLAB still BE. NOT checked: CEMPRO, CPPLUS (band), SPLPETRO (value), HEGAM (deliberate review).
- **Fixed 2026-11-09** (530ed3e): EXCLUDED 40 -> 27, ALL_SYMBOLS 705 -> 718; index.html and waitlist.html re-stamped.

### Fail-open gates on the order path (sweep of 2026-11-09). Findings, not fixes.
Each is now **counted** in `order_path_events` (273bded) without changing what it does.

### D-014 · Gate 0b block files fail open
- **Evidence** VERIFIED: `atlas/risk/symbol_block.py` `_load`, `atlas/execution/range_breaker.py` `_blocked_load` — unreadable file → nothing blocked. Deliberate, commented.
- **Impact** a symbol blocked this session (stop failure, range break) can be re-entered if the file cannot be read.

### D-015 · Gate 1 sentiment age is taken from Upstox's `timestamp`, which is the request time
- **Evidence** VERIFIED 2026-11-09: a quote fetched at 21:07 IST carried `timestamp` 21:07:08 (`atlas/execution/live_sentiment.py:291-305`). `last_trade_time` is the field with the price's age.
- **Impact** a frozen NIFTY-future price always looks seconds old; the sentiment verdict can be computed on it all session. The same defect CLAUDE.md records; still present. The whole-feed freeze detector in market_open (price differencing) is the only backstop and needs every price frozen.
- **Fixed 2026-11-09** (16732cc): age is how long since we saw price, VWAP or volume change, on our clock; first read refused; one-shot callers use read_settled(). The only vendor-timestamp freshness check in the repo. Live behaviour first observable in the 2026-11-11 session (2026-11-10 is an NSE holiday).

### D-016 · An unrecognised or missing agent mode becomes NORMAL
- **Evidence** VERIFIED: `atlas/reporting/directives.py` `normalise_mode`; `HALT_MODES = ("PAUSED",)`. "PAUSE", "HALT", or a NULL mode all allow trading. `market_open.paused()` likewise matches only the exact string.
- **Status** fixed-verified 2026-11-09 (71f1b3d): only an exact NORMAL permits entries in `paused()`, the kill switch and `status()`; unknown is reported UNKNOWN and recorded REFUSED. Verified on the box against the real row: `paused()` True, `check()` blocks, `status().halted` True.


### D-017 · A signal with no direction is entered as LONG
- **Evidence** VERIFIED: `atlas/execution/atlas_entry.py` `signal.get("direction", "LONG")`; `check_entry_range` treats anything but SHORT as long.
- **Fixed 2026-11-09** (c5beb9b): REJECTED_NO_DIRECTION before any gate, recorded REFUSED.

### D-018 · With no `entry_ref`, the proximity check is skipped
- **Evidence** VERIFIED: `check_entry_range` applies distance only `if entry_ref and entry_ref > 0`.

### D-019 · Gate 4b stock breaker proceeds when it cannot evaluate
- **Evidence** VERIFIED: `atlas_entry.py` "FAIL OPEN, AND SAY SO". Deliberate.
- **Under the new rule** (less exposure): unchanged and still open; the market breaker's equivalent (D-054) is fixed.

### D-020 · Unreadable GTT list → [] → orphan-trigger cleanup sees nothing
- **Evidence** VERIFIED: `atlas/execution/gtt.py` `list_atlas_gtts` / `list_active_gtts`; passed to `reconcile_exits` at `market_open.py:911-918`. Comment there: "an orphaned trigger can RE-ENTER a symbol that is already closed".
- **Fixed 2026-11-09** with D-021 (52da7d5): GTT reads return None on failure; an unreadable list alerts and flags every flat position whose triggers could only be checked from recorded legs; gtt_summary says UNKNOWN.

### D-021 · Unreadable open positions → [] → the protection sweep reports a clean book
- **Evidence** VERIFIED: `market_open.open_positions_for_reconcile` returns [] on failure; its docstring argues this is safe for reconcile, but the same list feeds the per-cycle protection sweep (`market_open.py:937` on), which then finds 0 naked positions.
- **Fixed 2026-11-09** with D-020 (52da7d5): positions read once per cycle, None on failure, alert OPEN POSITIONS UNREADABLE; reconcile skipped and the sweep reports unknown, not 0.

### D-022 · Reconcile records the row's pre-order price/qty as the fill
- **Evidence** VERIFIED: `atlas/execution/reconcile.py:205-206` — `average_price or row.entry_price`, `filled_quantity or row.qty`; the second sizes protection for shares that may not exist.
- **Fixed 2026-11-09** (c5beb9b): an unreadable fill stays PENDING (blocks the symbol), recorded REFUSED FILL_UNREADABLE, alerted every reconcile pass (~5 min) until resolved. The alerts stop when the engine stops at the close -- see D-058.

### D-023 · Sizing on an estimated margin when the broker's is unavailable
- **Evidence** VERIFIED: `atlas_entry.py` margin block; logged as a warning before 273bded, now also recorded.
- Still counted, not changed (decision pending under the new rule).

### D-024 · `order_path_events` table not created
- **Status** open — needs you · **Found** 2026-11-09
- **Evidence** VERIFIED: applying `migrations/PENDING_order_path_events.sql` was refused by the session permission classifier. Until applied, events go only to `reports/order_path_events.jsonl` on the box.
- **Applied 2026-11-10** (MCP, operator-approved), with the pilot's event types ORDER_SENT / GTT_SENT / GTT_CANCELLED / WOULD_PLACE / PREFLIGHT. VERIFIED: anon has no SELECT, authenticated no INSERT, RLS on, append-only triggers present, 0 rows. Dated copy: migrations/20261110_order_path_events.sql.

### Failed reads that return the same value as an empty answer (pattern sweep, 2026-11-09)
Rule: a failed read must never return the same value as a successful empty read (CHARTER). AST sweep of `atlas/`, `engine/`, `radar/`: 156 except/non-200 branches returning an empty or default value. Most are distinguishable (a `(readable, value)` pair, an `ok: False` dict, `None` meaning failure where `[]` means none, or a success flag). The ones that are not:

### D-048 · Radar cards say "no disclosed trigger found" when NSE could not be asked
- **Status** open · **Evidence** VERIFIED: `radar/documents.py` `fetch_filings` returns [] on any failure; `attribute()` sets `checked = bool(docs) or True` — always True. The `fetch_filings` docstring says the distinction is "kept in the returned ok flag on attribute()"; there is no such flag. A comment asserting the opposite of its code.
- **Fixed 2026-11-09** (b05196a): three states end to end (found / none_found / not_checked), each with its own sentence; validate() refuses disagreement; home.html eventCard read payload fields no card has -- all 58 current cards printed "No disclosed trigger found", 54 of them citing a filing -- and now renders the card's own q01.why. No caller was written against the imaginary `ok` flag. VERIFIED: the 4 current none-found cards (ITI, TARC, J&KBANK, MRPL) were genuinely checked -- the radar log shows a successful NSE answer with 0 filings for each.

### D-049 · `reconcile.stale_pending` returns [] on a failed read
- **Status** open · **Evidence** VERIFIED: `atlas/execution/reconcile.py:86, 90`; `settle()` then reports 0 checked. Rows stay PENDING (which blocks entries, the safe direction) but nothing says reconcile did not run.
- **Fixed 2026-11-09** (98bd1c9): pending_readable False, checked None, RECONCILE UNREADABLE alert at both call sites.

### D-050 · daily_report shows "no positions" when the broker is unreadable
- **Status** open · **Evidence** VERIFIED: `broker.get_positions` [] on failure (`broker.py:181, 188`), read at `atlas/reporting/daily_report.py:438`.
- **Fixed 2026-11-09** (98bd1c9): "Positions: UNREADABLE"; also the report's mode read turned a failure into NORMAL (hiding PAUSED) and now reads UNREADABLE.

### D-051 · An unreadable signal batch is alerted as "EOD pipeline down?"
- **Status** open · **Evidence** VERIFIED: `market_open.get_latest_batch_date` returns '' on HTTP != 200 (`:294`); `load_zone_map` alerts "no published signals at all". Fails closed; the alert names the wrong cause.
- **Fixed 2026-11-09** (98bd1c9): raises on HTTP != 200 -> SIGNALS UNREADABLE.

### D-052 · No bars → "stop held" for trailing
- **Status** open (safe direction) · **Evidence** VERIFIED: `preclose_gtt._bars_for` [] on failure; `trail()` holds the stop and reports ok.
- **Fixed 2026-11-09** (98bd1c9): evaluated False, own TRAIL NOT EVALUATED alert (not the failed-handover path, whose text says the stop is missing).

### D-053 · publish_protection, rbe, excursions, 03b, 01b, fundamentals, radar.detect
- **Status** open · **Evidence** sites VERIFIED by the sweep; consequences INFERRED, callers not read: `engine/publish_protection.py:64` open_positions [] (page may show none open); `engine/rbe_engine.py:227` fired-today set() after a restart (a signal may fire twice); `engine/excursions.py:153, 187` []; `engine/03b_score.py:83, 93` sector bias / sector map {} (scoring inputs — with D-008); `engine/01b_download_bhavcopy.py:392, 407` delivery {}; `engine/fundamentals.py:936, 1046` cache {}; `radar/detect.py:231` one symbol's scan error reads as "no extreme".
- **Callers read 2026-11-09 (cc78a53). Two inferences held, five were wrong:**
  - publish_protection: CORRECTED -- no page renders position_protection (atlas.html fetches it and shows nothing); the job logged "no open positions" and exited 0. Now UNREADABLE, exit 1.
  - rbe fired-today: CONFIRMED (a restart could re-fire every breakout). Now None; nothing fires until the state is readable.
  - excursions: CORRECTED -- [] only for a missing schema column, with a warning; otherwise raises.
  - 03b sector maps: REFINED -- every symbol "avoid": withholds the +3 tailwind and publishes sector_bias=avoid. With D-008.
  - 01b delivery: CORRECTED -- NaN (absent) and logged.
  - fundamentals: CORRECTED -- fails closed (blocks every symbol), logged.
  - radar scan_symbol: CONFIRMED and unlogged; now logged.

### Found by the three CHARTER rules, not by the decision list
### D-054 · The day's range breakers fail open when they error
- **Status** open · **Evidence** VERIFIED: `atlas/signal/market_open.py` cycle — "A FAILURE HERE MUST NOT SKIP THE CYCLE ... Fail open: the breakers are a refinement". Rule 3 says a gate with missing input fails closed.
- **Fixed 2026-11-09** (1febcb7): a raise or CANNOT_EVALUATE refuses every entry that cycle, recorded and alerted. 0 CANNOT_EVALUATE and 0 raises in the last 1,080 cycles.

### D-055 · The kill switch's funds check is skipped when `capital_required` is absent
- **Status** open · **Evidence** VERIFIED: `kill_switch.check` runs `can_afford` only `if signal` and `capital_required > 0`; `broker.place_order` calls `kill_switch_check()` with no signal. Funds are checked earlier at Gate 6, so the broker-layer copy checks only the mode. Rule 3.
- **Fixed 2026-11-09** (1febcb7): with a signal, capital_required must be positive or the entry is blocked. The no-signal broker-layer call stays a mode check by design.

### D-056 · Rule 3 needs an exit clause
- **Status** open — wording decision · An exit that cannot price itself (no tick map, no LTP) places nothing, which leaves the position open; it alerts (`exits.emergency_exit`). "Fails closed" for an entry means no order; for an exit it cannot mean that silently. Proposed: "for an exit, failing closed means refusing the unsafe order AND alerting until a human acts."

---

## 2. OPEN DEFECTS
- **Fixed 2026-11-09** (1febcb7): CHARTER rule 3 replaced: fail in the direction of less exposure, not less action.

### D-025 · `radar.store._get` pages with offset and no ORDER BY
- **Status** open · **Found** 2026-11-08 · **Evidence** VERIFIED: a multi-day read lost whole dates; with `order=id` totals matched SQL exactly. Single-page reads (volume_alerts) unaffected.

### D-026 · Radar wrote Hot 10 and scanner rows dated 2026-10-20, a holiday
- **Status** open · **Evidence** VERIFIED: rows in `radar_card_versions` and `radar_scanner_daily`; `trading_calendar` holiday "Dussehra"; no bhavcopy file.

### D-027 · 934 pre-October `signals` rows have NULL `publication_kind`
- **Status** open · **Evidence** VERIFIED (2025-05-12..2026-09-30). The migration header claims 0 NULL rows and NOT NULL.

### D-028 · `engine/universe.py` claims "Auto-generated. Do not edit manually."
- **Status** open · recorded in open_questions.md 2026-11-09. No generator exists; the file is hand-edited.

### D-029 · JSWDULUX's Upstox key is the pre-split ISIN
- **Status** open · **Evidence** VERIFIED: map `INE133A01011`; trades as `INE133A01029` since the 1:10 split 2026-10-22. Symbol excluded; named as a known gap in `engine/quote_coverage.py`.

### D-030 · The exit test suites are red on main
- **Status** open · **Found** 2026-11-09 · **Evidence** VERIFIED: `tests/test_exits.py` ("emergency exit places and reports ok" False, then IndexError at :345) and `tests/test_exit_path_rebuild.py` (IndexError); failures identical before and after ab67fd8/273bded.
- **Diagnosis** 2026-11-09: STALE TEST, not a code defect. Both suites read the production tick map (`atlas.config.TICK_MAP_FILE`, `/var/lib/atlas/tick_map.json`); without it `price_for` raises NO_TICK_SIZE_CANNOT_PRICE and the code correctly refuses to place. VERIFIED: both pass on the box (exit 0) and pass locally with `ATLAS_TICK_MAP_FILE` pointed at a copy of the box map. Not hermetic; would also fail on the box if that file were missing. Not fixed, by instruction.
- **Fixed 2026-11-09** (e3562fc): both suites run on tests/fixtures/tick_map.json (temp copy; RELIANCE removed -> both fail; bogus env path -> both pass). The real map has its own check, tools/check_tick_map.py, whose line is in the volume_alerts daily message. VERIFIED on the box: real map SANE, 718/718 covered; suites pass there without touching it.

### D-031 · docs/open_questions.md says the market-hours timer is disabled
- **Status** open · **Evidence** VERIFIED 2026-11-09: `atlas-market-hours.timer` active; the engine runs every session in WATCH-ONLY because of the operator /pause of 2026-10-28 15:22 IST.
- **Detail** the pause is `atlas_state.mode = 'PAUSED'` (row id 1, the only row), enforced by `market_open.paused()`, `broker.place_order` and the kill switch.

### D-032 · No ISIN in raw bhavcopy before 2026-05-08
- **Status** won't fix (data we hold) · **Evidence** VERIFIED: 858 of 977 files are the legacy layout; renames before May cannot be checked from disk.


### D-057 · radar_scanner_daily.filing_recent cannot store "unknown"
- **Status** open · **Found** 2026-11-09 with D-048 · The column is `boolean NOT NULL DEFAULT false`; had_recent_filing now returns None when NSE could not be asked, which is stored as False (hides the pill, claims nothing in words), counted and logged by run_detection. Needs a migration to allow NULL.

### D-058 · Every "alert until a human acts" stops at the close (found by the new rule 3)
- **Status** open · **Evidence** VERIFIED: the repeating alerts -- market_open.alert's 900 s cooldown, the protection register's 900 s repeat, D-022's per-reconcile alert -- all live in the market-hours engine, which exits 09:50 UTC. preclose_gtt's PRECLOSE HANDOVER FAILED is sent once. An unprotected overnight CNC position therefore stops being alerted at the close. The old rule ("fails closed") never asked about an exit's alert lifetime; "alert without stopping until a human acts" does.
- **2026-11-10, by decision:** overnight 30-minute repeat removed after it was seen working (message_id 6467, 18:30 UTC 9 Nov). Box crontab 47→46 jobs, 19 credential lines identical; PROOF-202611092350 acked. Outstanding alarms now appear only in the 19:50 IST message; immediate alerts during market hours.
- 2026-11-10 (b60ed50): the 19:50 IST message also runs Saturday (box crontab 1-5 -> 1-6, 46 jobs, 19 credentials identical), and its non-session line now carries the exit-alarm line -- before this, holidays dropped it. Saturday's first fire (14 Nov 14:20 UTC) is not yet observed.

### D-059 · broker.place_order's docstring says EXIT is always permitted; its kill switch blocks any intent
- **Status** open (latent) · **Evidence** VERIFIED: the kill-switch call is at function level, after the `intent != "EXIT"` block, so a PAUSED or unreadable state would block an exit. No current exit calls broker.place_order (exits use kite.place_order directly; place_sl_order is only reached from dead trade_management.py). Found by the new rule. Not changed: it is the order-placing function.

### D-060 · Test suites can delete production state when run on the box
- **Status** open · **Evidence** VERIFIED: `tick_map.reset_for_tests()` unlinks whatever TICK_MAP_FILE points at; `symbol_block.reset_for_tests()` likewise for its file. A suite run on the box without redirecting the env would remove the live file. VERIFIED 2026-11-09: the exit-suite runs on the box changed nothing under /var/lib/atlas (all mtimes predate the runs). The exit suites now use a temp fixture; the others still rely on env redirection.
- **Fixed 2026-11-10** (4dbb3b4): atlas/disposable.py refuses any destructive test-helper path outside the temp dir (symlinks resolved, no override); a script under tests/ is a drill however started; order_events never POSTs under a drill; mutate_gate_bypass mutates a temp copy (it rewrote live source and rmtree'd the venv's caches). Swept every suite, run_all and direct, behind a record-only fs/HTTP hook: nothing writes outside temp except engine modules appending their own reports/*.log at import. VERIFIED on the box: full suite, 0 changes under /var/lib/atlas and data/reference.

### D-061 · test_page_claims was red on main from 71f1b3d to b05196a
- **Status** fixed-verified · My miss: the pause fix changed normalise_mode and I did not run that suite. Repointed to the new ruling in b05196a.

### D-062 · test_data_rights is red on main and was before 2026-11-09
- **Status** open · **Evidence** VERIFIED: fails at 2f6c91b; 21 datasets in use against 16 register rows (NSE announcements/XBRL/holiday master/sec_list, Upstox candles/instruments/option chain, Kite account data/instruments/quotes/ticker). Also: test_calendar_source fails on a dev machine whose holiday cache is older than 7 days (environment, not code).
- 2026-11-10: rows added for the shareholding master and corporate XBRL (16 -> 14 missing).

### D-063 · Market cap: a licence question, not yet a data question
- **Status** open — needs you (stopped here by instruction) · **Found** 2026-11-10
- **Evidence** VERIFIED: every NSE row in compliance/data_rights.csv is "unconfirmed: rights request emailed to NSE; no reply on file" -- the daily bhavcopy included. The natural source of shares outstanding, NSE's shareholding-pattern master (api/corporate-share-holdings-master and the SHP XBRL it links), is already read by engine/tier1_fetch.py and has NO register row (D-062).
- **Data side, for when the licence is settled:** tier1_sources.json keeps each quarter's SHP XBRL URL and its as-of date (RELIANCE: 30-SEP-2026); those filings state the total share count, which tier1_fetch does not parse today. Shares outstanding with an as-of date is therefore a small parse of files we already fetch -- not built.
- **2026-11-10, route (a) built** (6f252b3): tier1_fetch stores the newest SHP filing's grand-total NumberOfShares (column VII, incl. shares under DRs) with quarter and filing date; never a market cap. Register rows added for the shareholding master and the XBRL, unconfirmed like the rest. VERIFIED on the box: 714/718 resolved, 701 from the Sep-30 quarter. Unresolved: ABBOTINDIA, BAYERCROP, MCX (master fetch returned nothing usable; cause not investigated), ICICIPRULI (no price parquet either, D-039 family). ALKEM and AVL newest filing is Jun-30: refused as stale. Cross-check total vs promoter shares/promoter %: 677/682 within 1%; INFY 0.916, INDUSINDBK 0.953, M&M/RELIANCE/USHAMART ~0.98 -- a different denominator in the promoter %, not gated on.

### D-064 · "The next two supports below price": near the 52-week low there usually are none
- **Status** open — decision needed before sizing · **Found** 2026-11-10
- **Evidence** VERIFIED, 717 universe symbols, session 2026-11-09, using engine/active_zones.py's own _collect/_mitigated (unmitigated demand OB + bullish FVG below the close):
  - current rules (MAX_ZONE_AGE 60 sessions, MAX_ZONE_DIST_PCT 15%): all 0/1/2+ = 219/157/341; within 5% of the 252-session low 30/2/1 (n=33); within 10% 79/32/20 (n=131)
  - no age or distance limit (history since 2023): all 45/34/638; within 5% of the low 13/2/18
- **Proposed shape, not built:** supports_below(df, n=2) beside add_active_zones, same rules, nearest first, each with source/age/distance, and an explicit count and reason ("none: price is below every unmitigated demand zone on file") -- never a level invented to make two. What sizing does with 0 or 1 is your rule to set.

### D-065 · Order of work recorded 2026-11-10
- The eleven decided substitutions (D-001..D-012 less D-008), D-001 change_pct first -- the root of three visible lies (D-001, D-002, D-003) on pages meant to be sold -- come AFTER the backtest machinery. Screener critical path, not ATLAS's. Not to be dropped.

### D-066 · The box deploys main without running the suite (D4)
- **Status** open · Bit twice in two days: test_page_claims red from 71f1b3d to b05196a (D-061), and nine suites written 2026-11-09/10 never registered in run_all (fixed in 4dbb3b4, with a check that fails run_all on an unregistered test). Nothing gates the five-minute `git reset --hard origin/main` on a green suite.
- **FIXED 2026-11-10: a pre-push hook** (.githooks/pre-push, enabled with core.hooksPath) runs check_js.sh and the full suite on exactly the commit being pushed and refuses on red; it also refuses when tracked files are dirty or the pushed ref is not HEAD. Chosen over CI because the box resets to origin/main regardless of CI status -- a CI gate would also need the box to deploy only CI-green commits (a token on the box and a second branch) and a CI image matching py3.14/pandas 3.0.3 and the local data the tests read. Pre-existing reds cleared first so the gate can be strict: data rights (14 register rows added, all unconfirmed) and calendar source (stale local cache refreshed). Limits: per clone (a fresh clone must run the git config line), and `--no-verify` bypasses it.

### D-067 · Stored daily prices are not adjusted for splits, bonuses or demergers
- **Status** open · **Found** 2026-11-10 · not fixed
- **Evidence** VERIFIED: data/processed/smc/*.parquet close series show 43 universe symbols with a one-day move below -40% or above +80% since 2025-07-01 (ANGELONE -90% 2026-02-26, KOTAKBANK -80% 2026-01-14, VEDL -65% 2026-04-30, CONCORDBIO -47% 2026-11-04, ...). Effects seen: a split inside the last 20 sessions inflates ATR (CONCORDBIO 20-day ATR 13.9% of price, putting an ATR tranche 3 at -41.7%); a split inside 252 sessions makes the post-split price look "near the 52-week low"; any price-ratio scaling of a market cap breaks. Zones, ATR, 52-week levels and any rvol/price feature computed across the date read the same series. Scope of downstream damage not measured.


### D-068 · Long-equity sizing rule built, not wired to entry
- **Status** built 2026-11-10 (b60ed50) · wiring into atlas_entry not started -- needs your go, it is the order path
- atlas/sizing.py; tests/test_long_sizing.py (8 mutants caught). Universe on 2026-11-09: 681/718 sized, 37 refused (28 history_discontinuity, 3 no share count, 2 stale count, 2 split after the count, 1 no parquet, MRF at Rs 1 cr capital buys no share).
- **Open questions it raises:** total capital is not in config (demo used Rs 1 cr); "70% max deployed" read as 70% of the long book; supports are capped at 15% below price by the zone rules, so a deep T3 support beyond that is never used.
- **Wired 2026-11-10** (678897b, 2433d1b) as the long pilot: Gate 5 sizes LONG via size_pilot on LONG_PILOT_CAPITAL (Rs2,00,000). See D-072..D-076.

### D-069 · TDPOWERSYS: NSE's own bhavcopy disagrees with itself
- **Status** open · **Found** 2026-11-10 · VERIFIED: cm06Nov2026bhav.csv ClsPric 576.50; cm09Nov2026bhav.csv PrvsClsgPric 609.85 (5.8% apart, under the 15% feed-continuity tolerance so unflagged). Cause unknown. Its 8.3% ATR is real (5-6 Nov fell -8.1% and -13.9% on ~4x volume); it is refused by sizing anyway (24-Aug split inside 252 sessions).


### D-070 · Comments and code that disagree, found building the pilot
- **Status** partly fixed 2026-11-10 · VERIFIED in code:
  - atlas/config.py ALLOW_AUTOMATED_STOP_LOSS comment said a position that cannot get a stop "is exited at market". False since 2026-10-16 (protect_or_flag keeps it open; emergency_exit is only called by the MIS square-off). **Fixed** -- and it drove a wrong warning to the operator on 2026-11-10.
  - daily_report footer said ATLAS "exits immediately" if a stop fails. **Fixed** (per-book sentence).
  - atlas_entry._complete_intent docstring says entry_price "HERE IS THE FILL"; it is called right after the order is sent, before await_fill, so the row gets the pre-fill quote and the post-fill fields (target, legs, stop_confirmed) set in `intent` are never written for the stop book. Not fixed (the long book patches the row after its fill itself).
  - broker.get_holdings() returns [] on a failed read. Not fixed (the pilot reads holdings itself and treats failure as unknown).
  - exits.reconcile_exits decides "flat" from positions()['net'] only. A CNC long held past settlement is in holdings, so it reads as flat and its GTT legs are deleted. No CNC position is open today, so nothing was hit now; whether it fired historically was not checked. The long book is kept out of it.

### D-071 · The sector map is not an NSE classification and has no recorded source
- **Status** open -- no per-sector rule may read it until fixed · **Found** 2026-11-10 (operator) · audited 2026-11-10
- **Evidence** VERIFIED against NSE index constituent lists (Nifty Total Market + 10 others): of 718, 491 match NSE's Industry, **4 contradict it** (COALINDIA METAL/Oil Gas, PRECWIRE METAL/Capital Goods, RAILTEL IT/Telecom, SUNDRMFAST INFRA/Auto), 13 banks sit in FINANCE (3 verified from NSE's PSU bank index), 145 cannot be judged because the 12-label vocabulary has no home for NSE's Consumer Durables/Services, Textiles, Media, Chemicals (tools/nse_sectors.py maps Durables/Services/Textiles/Media -> FMCG and Chemicals -> METAL by design: FMCG holds 141, only 40 of them NSE FMCG), 78 are in no fetched NSE list.
- **Provenance** engine/universe.py SYMBOL_SECTOR_MAP, a hand-edited literal dict. Created a7bca58 (2026-05-26, no source recorded); rewritten 084f1b0 (2026-05-27) whose header claims "Source: NSE ... Auto-generated" -- no generator exists, and that version put NTPC/ONGC/IOC/COALINDIA under DEFENCE, which NSE's file cannot produce; relabels 6782207 + 94e5e3b (2026-09-13, Hemal); 246 added c13c7a1 via tools/nse_sectors.py (2026-09-28). 451 lines still date from 084f1b0.
- **Consumers** engine/upstox_ws.py (the live intraday sector heatmap; shorts come from the bottom sectors), engine/07_sector_momentum.py -> sector_heatmap, engine/03b_score.py sector bias, session_selector no-duplicate-sector, fundamentals, radar, home.html's embedded copy. rbe_engine does not read it.
- **REBUILT 2026-11-10** (283de8d, d82517f; operator chose NSE's industries verbatim). engine/sector_map.json generated by tools/build_sector_map.py from 12 NSE index lists stored in engine/sector_sources/ (fetched 2026-11-10); a test rebuilds it and requires it identical. Over the 718: 23 labels (NSE's 21 industries less Financial Services, plus BANKING 18 / FINANCE 88), **UNKNOWN 79**. tools/nse_sectors.py retired. **The min-5 rule was not in the code** -- both heatmaps ranked every group; engine/sector_rank.py now leaves UNKNOWN and groups under 5 measured members unranked (9 Nov: UNKNOWN 80, Utilities 4, Diversified 1, Forest Materials 1 unranked; 20 sectors ranked), and home.html's reading uses the same rule. September relabels (47): NSE agrees with 27, disagrees with 2 (COALINDIA DEFENCE->METAL, NSE Oil Gas; RAILTEL DEFENCE->IT, NSE Telecommunication), 18 cannot be judged (NSE 'Services'/'Consumer Services'/'Diversified' or UNKNOWN). Banks not in any NSE bank index (CSBBANK, DCBBANK, EQUITASBNK, J&KBANK, JSFB, KTKBANK, TMB, UJJIVANSFB, IDBI ...) are FINANCE under the membership rule. Phone-width rendering of the long NSE names NOT verified (the browser could not be resized); at desktop width none is clipped.

### D-072 · Ledger types for ATLAS: migration written, NOT applied
- **Status** open -- needs you · migrations/20261110_observation_types_atlas.sql (adds atlas_entry, atlas_decision; atlas_decision never scored). Applying it was refused by the permission check on 2026-11-10. **Until it is applied the pilot places nothing**: the decision must be in the ledger before an order, so every candidate that passes all gates ends BLOCKED_PILOT_NO_EVIDENCE, and the ledger's own refusal alert goes to Telegram (held 60 min).
- **Applied 2026-11-10** (operator-approved). VERIFIED: the type CHECK lists atlas_entry and atlas_decision; the never-score function lists radar_event and atlas_decision.

### D-073 · At Rs2,00,000, T2/T3 round to zero shares for higher-priced names
- **Status** open -- decision needed · VERIFIED (box, 2026-11-10, 9 Nov closes): TATAELXSI (3% band, Rs6,000 ceiling) T1 rounds up to 1 share at Rs3,890, T2 and T3 (Rs1,800 each) buy 0 -> no ladder; same for JKCEMENT and GMBREW. A zero-quantity tranche is skipped and recorded REFUSED T2/T3_ZERO_QTY. Rule today: only T1 rounds up.
- **Decided 2026-11-10 (operator): refuse the symbol** -- no round-up, no skipped tranche; the ladder is the strategy (290ffcd). VERIFIED on the box, 9 Nov closes at Rs2,00,000: 565 sized, 87 refused for a zero-share tranche (TATAELXSI, JKCEMENT, GMBREW among them), 66 refused for other reasons. Still allowed: a one-share tranche (PRESTIGE T2 = 1 share).

### D-074 · Pilot behaviour to know
- The 5-entry cap counts atlas_trades rows with sizing_basis long_pilot_2026_11 (any status but CANCELLED) -- the pilot's lifetime, not per day. In watch-only, today's WOULD_PLACE symbols count too.
- Funds must cover the whole ladder (T1 + T2 + T3) at entry, since T2/T3 rest the moment T1 fills.
- "Fits inside the ceiling" for the T1 round-up is read against the position bound (the tighter of ceiling and liquidity cap).
- A resting BUY GTT is not cancelled by Zerodha when the position is sold; tranches.cleanup() cancels it within one reconcile cycle (60s in session) once positions AND holdings show no shares. A trigger that fires inside that window still buys. Outside market hours nothing runs, but a GTT cannot fire then either.
- T2/T3 GTT placement failure: recorded and alerted; not retried automatically.


### D-075 · Day-2 sweep: every reader of the broker's positions list (2026-11-10)
- **The bug class:** a CNC long leaves positions()['net'] for holdings() after settlement; a reader of positions alone sees it as flat from day 2.
- **Direct readers of the broker books, VERIFIED by reading each:**
  - exits.reconcile_exits -- positions only. **HAS THE BLIND SPOT.** Fed only stop-book trades since 678897b; today that is MIS shorts (intraday, where positions is right). Any future CNC stop book would be hit.
  - trade_management.modify_sl_to_breakeven -- positions only. **HAS THE BLIND SPOT**, but nothing imports trade_management (dead code).
  - broker.get_positions() -- positions only, and [] on a failed read. **HAS THE BLIND SPOT**; no caller.
  - trade_outcome_checker.get_zerodha_positions -- both books, quantity + t1_quantity (fixed after BPCL, 28 Aug). Not affected.
  - tranches.read_books / position_from, session_report.long_book_lines -- both books. Not affected.
- **Readers of atlas_trades, not the broker** (no blind spot of this kind): daily_report.get_positions, publish_protection.open_positions, tools/universe_filter.open_positions, mis_squareoff, Gate 3b, /positions.
- **The same class one step over -- the ROW goes stale on day 2:** atlas_trades.qty and entry_price are T1's and are never updated when T2/T3 fill. So: (a) trade_outcome_checker.close_trade computes realised P&L from row qty x (exit - row entry) -- an averaged position's P&L would count T1 only, which pollutes the evidence; (b) the session report's OPEN section and daily_report P&L use the row (the 15:50 LONG BOOK section uses the broker and is right). Not fixed -- needs a decision on where blended qty/cost is recorded.
- **Also found:** /positions answers "No open positions" on a failed read, and prints the legacy sl / target_1 columns (0 for pilot rows). publish_protection would publish a pilot position's stop as NONE (unprotected); position_protection has no live consumer today.


### D-076 · Docs that contradict their code -- a pattern, not a one-off
- **Status** open · recorded 2026-11-10 (operator)
- **Instances:** (1) engine/06_push_supabase.py docstring vs 06_push behaviour (2026-11-09); (2) engine/universe.py "auto-generated ... do not edit manually" with no generator (fixed 283de8d); (3) atlas/config.py ALLOW_AUTOMATED_STOP_LOSS "exited at market" (fixed 2026-11-10); (4) docs/MERIDIAN.md "NOTHING HERE IS BUILT" while the IV recorder had run daily since 2 Oct (banner corrected 2026-11-10). Also atlas_entry._complete_intent's docstring (D-070) and the atlas_entry module docstring ("No SL, no target, no exit orders").
- **What a standing check could look like** (proposal, not built):
  1. *Generated files prove it.* Any file saying "generated"/"auto-generated" must name its generator, and a test reruns the generator and diffs (the sector map does this now).
  2. *Status claims are machine-checked.* A short registry (docs/claims.yml) of each "is built / is not built / is never called / runs at" claim with a check: a module exists, a crontab.box line references it, a function has callers, a table has rows within N days. A test evaluates every entry; a claim with no check is not allowed in a status banner.
  3. *Behaviour sentences cite a test.* A comment that asserts what code does ("exits at market", "never places a stop") must name the test that asserts it; a lint fails a behaviour claim in config/docstrings with no test reference. Weakest of the three -- it catches missing tests, not wrong sentences.
  Cost: (1) is done per generator; (2) is about a day; (3) is a heuristic and will need tuning.
- **BUILT 2026-11-10 (operator chose (a) and (b)):** docs/claims.json + tests/test_claims.py. The scanner found 9 more unregistered claims on its first run: two FALSE and corrected (docs/TSL_FLASH.md and radar/detect.py still described universe.py as generated from ind_nifty500list.csv; tools/universe_filter.py said the fundamentals tiers did not exist), the rest registered -- checked where there is code (emergency_exit has no automatic caller, would_veto and signals_features are read by nothing else, trade_management is imported by nothing, the MERIDIAN recorder exists and two crontab lines run it) and as plans with a stated reason where there is none. Also found: engine/zerodha_tokens.py's "Mapped: 500 stocks" is true, but the universe is 718 -- 218 symbols have no Zerodha token in that map (not investigated). The test's first version passed vacuously (executable_source was handed text, raised, and was read as empty); the planted-tree checks caught it.

### D-077 · Six F&O stocks are outside our 718
- ICICILIFE, IDEA, LTM, SAGILITY, SUZLON, YESBANK (F&O bhavcopy 2026-11-09, 218 stock underlyings). Add them when the universe expands (operator, 2026-11-10).

### D-078 · F&O bhavcopy: built, not installed -- NSE's archive does not answer the box
- **Status** open -- needs you · **Found** 2026-11-10
- engine/fo_bhavcopy.py is committed and tested (0316a0e). A temporary cron line fired on the box at 21:23 UTC with the right environment (syslog CRON line; its failure alert reached Telegram, message_id 6484) -- but the fetch got no answer: 60 s read timeout, twice, from the box, while the same request returns in under a second from elsewhere. The equity bhavcopy reaches the same archive from the box daily through jugaad-data, which first warms an NSE session. Trying header/session variations from the box was refused by the permission check as bot-check circumvention, and was not pursued. Temp line removed (46 jobs, 19 credentials identical); the two real lines are NOT installed.
- Blocks: the daily F&O fetch, and the one-year IV rebuild (D-079), whose files would come from the same archive.
- **RESOLVED 2026-11-10 (operator: jugaad-data's own F&O route, current filename).** Only the route string is replaced; the library's session, headers and request are untouched. From the box: 9 Nov fetched in 1 s (41,392 contracts). Cron proven (temp line fired 21:42 UTC, fetched 6 Nov) and installed: 13:15 and 14:00 UTC weekdays; box 46 -> 48 jobs, 19 credentials identical. Year backfilled 2025-11-10..2026-11-05: 245 fetched, 0 failed; 247 sessions held, 274 MB.

### D-079 · IV from the bhavcopy (operator, 2026-11-10) -- not started
- Rules agreed: IV only from strikes that TRADED that session (untraded -> UNKNOWN, never a number); Black-76 on the same-expiry future; for index weeklies the forward backed out of traded call/put parity; lot size per contract. Rebuild one year from the archive; keep meridian/iv_recorder.py running until bhavcopy IV is validated against its 27 overlapping sessions. Blocked on D-078.


### D-080 · Box disk headroom
- **Status** open · VERIFIED 2026-11-10: 1.1 GB free of 6.7 GB (84%) after the F&O backfill (274 MB) and IV rebuild (~2 MB); disk alarm at 500 MB. The F&O fetch keeps a zstd parquet only (~1.25 MB/session, ~310 MB/year) -- the raw CSV (~7.5 MB) is validated and dropped, because a year of raw files (~1.9 GB) does not fit. At that rate the alarm fires in roughly two years of F&O alone, sooner with everything else growing. Not addressed.

---

## 3. OPEN QUESTIONS — waiting on data
- **D-033** The 54-feature re-run — see `docs/open_questions.md` §"The 54-feature re-run".
- **D-034** What separates a setup from a non-setup — see `docs/open_questions.md`.

---

## 4. SETTLED — do not reopen without new evidence
- **D-035** Exits — settled 2026-10-02 (`docs/open_questions.md`). Fourteen rules, all lose.
- **D-036** ICICIPRULI → ICICILIFE continuity: **option (c)** decided 2026-11-08 — current symbol forward, history left as written, alias table written by the ISIN guard. **Status** decided, not started. The observation ledger refuses UPDATE, so a rename can never be complete. **Related** D-037.
- **D-037** ISIN continuity guard (`tools/isin_continuity.py`) — built and proven 2026-11-08 (planted rename/delisting/series/ISIN-change/new-listing all fired). **Status** in progress: uncommitted, unscheduled, waits on Phase 2 with D-036.

---

## 5. FIXED AND VERIFIED — log
| ID | What | Fix | Status / verified |
|---|---|---|---|
| D-038 | 06_push `notify_atlas` docstring claimed Telegram alerts | d183e8a | fixed-verified: test binds docstring to body, mutation proves it bites |
| D-039 | No per-symbol volume alert | d183e8a, 0f7f8c6 | fixed-verified: cron proof message_id 6439; first scheduled run 2026-11-09 message_id 6452 |
| D-040 | CHOLAFIN Upstox key was a debenture (since 2026-05-27) | 57b2f6e | fixed-verified: 744 quotes on all four runs 2026-11-09; live_prices 1773.40 |
| D-041 | Upstox quote coverage unchecked (499→498 unnoticed 7 weeks) | 56dc281 | fixed-verified: cron proof message_id 6453; first scheduled check 2026-11-10 03:15 UTC |
| D-042 | rbe excluded CHOLAFIN on a false reason | 56dc281 | fixed-unverified: reason disproved from rbe's own log; first run with it lifted 2026-11-10 |
| D-043 | Entry with no quote used `entry_ref` (`atlas_entry.py` was :885) | ab67fd8 | fixed-unverified in production (ATLAS paused); tests + mutants |
| D-044 | Unreadable order state after cancel recorded as NO_FILL (was :628-634) | ab67fd8 | fixed-unverified in production; tests + mutants |
| D-045 | Entry count returned 0 on a failed read (was :466) | ab67fd8 | fixed-unverified; no caller today |
| D-047 | `/approve` could lift the pause on a failed read (`directives.get_agent_state` answered NORMAL; `/approve` wrote it back; `/pause` reply named `/approve` as a resume) | 71f1b3d | fixed-verified: tests + 4 mutants; real row reads paused on the box |
| D-046 | Was any order priced on a substituted `entry_ref`? | — | answered 2026-11-09: no evidence of one. Engine path (2026-09-12 on): fallback unreachable (every candidate carried an Upstox ltp; code at ad1ddc4, 0146641, HEAD). Pre-engine (2026-08-19..09-04): 0 of 14 ENTERED decisions at entry_ref. Engine logs before 2026-10-19 rotated out. |
- **New rule 2026-11-10 (operator):** at +16% on blended cost the engine cancels the remaining T2/T3 buy GTTs that cycle (tranches.flag_cancel). Blended cost has one definition, tranches.position_from(), shared with the 15:50 report. Between 15:20 and the next morning's first cycle the engine is not running; a buy GTT below market can only fire on a fall from +16%.
- **exits.reconcile_exits STILL CARRIES THE DAY-2 BLIND SPOT** (operator, 2026-11-10, recorded as asked). It decides "flat" from positions()['net'] alone. It is safe today ONLY because, since 678897b, it is fed intraday short (MIS) trades alone, where the positions list is correct. **Any future delivery book with broker stops walks straight into it**: from day 2 the position is in holdings, reads as flat, and its stop GTTs are deleted. This is a RECURRENCE, not a hypothetical -- it is the same bug that wrongly closed the BPCL position on 28 Aug (trade_outcome_checker counted holdings quantity without t1_quantity and read a live 215-share position as flat). Fix before any CNC stop book exists: read positions AND holdings (tranches.read_books / position_from).
- **Row follows the broker -- FIXED 2026-11-10** (operator decision): every cycle, long_pilot.sync_ladder sets each open long-pilot row's qty and entry_price to the broker's position and BLENDED cost via tranches.position_from (the one definition the +16% flag and the 15:50 report use); T1's own fill stays in exit_legs (t1_fill_price, t1_qty, t1_filled_at, t1_order_id). Each T2/T3 fill is its own event -- price, quantity, timestamp, % below T1, days after T1 -- in the observation ledger (atlas_decision T2_FILLED / T3_FILLED) and on the row (t2_fill_* / t3_fill_*). Price from today's order book; if the fill is not in it, derived from the change in blended cost and labelled derived, with the detection time. Refuses to sync when the broker holds more than T1+T2+T3 (another lot in the symbol). The outcome checker's realised P&L then reads the blended row.
- **/positions -- FIXED 2026-11-10**: a failed read says UNREADABLE (it said "No open positions"); the legacy sl/target_1 columns (0.0 on pilot rows) are replaced by stop_price/target_price, with a dash where absent.
- **REBUILT AND VALIDATED 2026-11-10** (meridian/iv_bhav.py, a28128f). Year: 247 sessions, 243 underlyings, 52,590 ATM IVs, 459 UNKNOWN (no traded ATM strike); forwards 52,847 from the future, 192 from parity, 10 unknown (future untraded). VALIDATION against meridian_iv_daily (5,469 comparable rows of 5,903; the other 434 are two NSE HOLIDAYS the recorder wrote, see below): stocks ATM median |diff| 0.29 vol pts, 88% within 1 pt, 94% within 2, corr 0.878; indices 0.14 pts, 97% within 1 pt. Both ATM sides traded (5,300 rows): 90% within 1 pt. Gap grows near expiry (stocks <=3 days: median 2.54 pts). Risk-free assumption moves IV 0.11 pts median. The largest gaps are the RECORDER: 99 rows where the ATM strike did not trade but the recorder stored an IV (median 50%, up to 401% -- 360ONE call 26 Oct); bhavcopy says UNKNOWN. The live recorder keeps running (operator) until you call the validation sufficient.
- **Recorder defect found:** meridian/iv_recorder.py ran and wrote 217 rows on 2026-10-02 (Gandhi Jayanti) and 2026-10-20 (Dussehra) -- NSE holidays -- so its '27 sessions' are 25 sessions + 2 holidays of stale chain data; its cron is 1-5, so it will do the same on 2026-11-10. Not changed (operator: keep it running as is until validated).

