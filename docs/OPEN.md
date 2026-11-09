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

### Fail-open gates on the order path (sweep of 2026-11-09). Findings, not fixes.
Each is now **counted** in `order_path_events` (273bded) without changing what it does.

### D-014 · Gate 0b block files fail open
- **Evidence** VERIFIED: `atlas/risk/symbol_block.py` `_load`, `atlas/execution/range_breaker.py` `_blocked_load` — unreadable file → nothing blocked. Deliberate, commented.
- **Impact** a symbol blocked this session (stop failure, range break) can be re-entered if the file cannot be read.

### D-015 · Gate 1 sentiment age is taken from Upstox's `timestamp`, which is the request time
- **Evidence** VERIFIED 2026-11-09: a quote fetched at 21:07 IST carried `timestamp` 21:07:08 (`atlas/execution/live_sentiment.py:291-305`). `last_trade_time` is the field with the price's age.
- **Impact** a frozen NIFTY-future price always looks seconds old; the sentiment verdict can be computed on it all session. The same defect CLAUDE.md records; still present. The whole-feed freeze detector in market_open (price differencing) is the only backstop and needs every price frozen.

### D-016 · An unrecognised or missing agent mode becomes NORMAL
- **Evidence** VERIFIED: `atlas/reporting/directives.py` `normalise_mode`; `HALT_MODES = ("PAUSED",)`. "PAUSE", "HALT", or a NULL mode all allow trading. `market_open.paused()` likewise matches only the exact string.
- **Status** fixed-verified 2026-11-09 (71f1b3d): only an exact NORMAL permits entries in `paused()`, the kill switch and `status()`; unknown is reported UNKNOWN and recorded REFUSED. Verified on the box against the real row: `paused()` True, `check()` blocks, `status().halted` True.

### D-017 · A signal with no direction is entered as LONG
- **Evidence** VERIFIED: `atlas/execution/atlas_entry.py` `signal.get("direction", "LONG")`; `check_entry_range` treats anything but SHORT as long.

### D-018 · With no `entry_ref`, the proximity check is skipped
- **Evidence** VERIFIED: `check_entry_range` applies distance only `if entry_ref and entry_ref > 0`.

### D-019 · Gate 4b stock breaker proceeds when it cannot evaluate
- **Evidence** VERIFIED: `atlas_entry.py` "FAIL OPEN, AND SAY SO". Deliberate.

### D-020 · Unreadable GTT list → [] → orphan-trigger cleanup sees nothing
- **Evidence** VERIFIED: `atlas/execution/gtt.py` `list_atlas_gtts` / `list_active_gtts`; passed to `reconcile_exits` at `market_open.py:911-918`. Comment there: "an orphaned trigger can RE-ENTER a symbol that is already closed".

### D-021 · Unreadable open positions → [] → the protection sweep reports a clean book
- **Evidence** VERIFIED: `market_open.open_positions_for_reconcile` returns [] on failure; its docstring argues this is safe for reconcile, but the same list feeds the per-cycle protection sweep (`market_open.py:937` on), which then finds 0 naked positions.

### D-022 · Reconcile records the row's pre-order price/qty as the fill
- **Evidence** VERIFIED: `atlas/execution/reconcile.py:205-206` — `average_price or row.entry_price`, `filled_quantity or row.qty`; the second sizes protection for shares that may not exist.

### D-023 · Sizing on an estimated margin when the broker's is unavailable
- **Evidence** VERIFIED: `atlas_entry.py` margin block; logged as a warning before 273bded, now also recorded.

### D-024 · `order_path_events` table not created
- **Status** open — needs you · **Found** 2026-11-09
- **Evidence** VERIFIED: applying `migrations/PENDING_order_path_events.sql` was refused by the session permission classifier. Until applied, events go only to `reports/order_path_events.jsonl` on the box.


### Failed reads that return the same value as an empty answer (pattern sweep, 2026-11-09)
Rule: a failed read must never return the same value as a successful empty read (CHARTER). AST sweep of `atlas/`, `engine/`, `radar/`: 156 except/non-200 branches returning an empty or default value. Most are distinguishable (a `(readable, value)` pair, an `ok: False` dict, `None` meaning failure where `[]` means none, or a success flag). The ones that are not:

### D-048 · Radar cards say "no disclosed trigger found" when NSE could not be asked
- **Status** open · **Evidence** VERIFIED: `radar/documents.py` `fetch_filings` returns [] on any failure; `attribute()` sets `checked = bool(docs) or True` — always True. The `fetch_filings` docstring says the distinction is "kept in the returned ok flag on attribute()"; there is no such flag. A comment asserting the opposite of its code.

### D-049 · `reconcile.stale_pending` returns [] on a failed read
- **Status** open · **Evidence** VERIFIED: `atlas/execution/reconcile.py:86, 90`; `settle()` then reports 0 checked. Rows stay PENDING (which blocks entries, the safe direction) but nothing says reconcile did not run.

### D-050 · daily_report shows "no positions" when the broker is unreadable
- **Status** open · **Evidence** VERIFIED: `broker.get_positions` [] on failure (`broker.py:181, 188`), read at `atlas/reporting/daily_report.py:438`.

### D-051 · An unreadable signal batch is alerted as "EOD pipeline down?"
- **Status** open · **Evidence** VERIFIED: `market_open.get_latest_batch_date` returns '' on HTTP != 200 (`:294`); `load_zone_map` alerts "no published signals at all". Fails closed; the alert names the wrong cause.

### D-052 · No bars → "stop held" for trailing
- **Status** open (safe direction) · **Evidence** VERIFIED: `preclose_gtt._bars_for` [] on failure; `trail()` holds the stop and reports ok.

### D-053 · publish_protection, rbe, excursions, 03b, 01b, fundamentals, radar.detect
- **Status** open · **Evidence** sites VERIFIED by the sweep; consequences INFERRED, callers not read: `engine/publish_protection.py:64` open_positions [] (page may show none open); `engine/rbe_engine.py:227` fired-today set() after a restart (a signal may fire twice); `engine/excursions.py:153, 187` []; `engine/03b_score.py:83, 93` sector bias / sector map {} (scoring inputs — with D-008); `engine/01b_download_bhavcopy.py:392, 407` delivery {}; `engine/fundamentals.py:936, 1046` cache {}; `radar/detect.py:231` one symbol's scan error reads as "no extreme".

### Found by the three CHARTER rules, not by the decision list
### D-054 · The day's range breakers fail open when they error
- **Status** open · **Evidence** VERIFIED: `atlas/signal/market_open.py` cycle — "A FAILURE HERE MUST NOT SKIP THE CYCLE ... Fail open: the breakers are a refinement". Rule 3 says a gate with missing input fails closed.

### D-055 · The kill switch's funds check is skipped when `capital_required` is absent
- **Status** open · **Evidence** VERIFIED: `kill_switch.check` runs `can_afford` only `if signal` and `capital_required > 0`; `broker.place_order` calls `kill_switch_check()` with no signal. Funds are checked earlier at Gate 6, so the broker-layer copy checks only the mode. Rule 3.

### D-056 · Rule 3 needs an exit clause
- **Status** open — wording decision · An exit that cannot price itself (no tick map, no LTP) places nothing, which leaves the position open; it alerts (`exits.emergency_exit`). "Fails closed" for an entry means no order; for an exit it cannot mean that silently. Proposed: "for an exit, failing closed means refusing the unsafe order AND alerting until a human acts."

---

## 2. OPEN DEFECTS

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

### D-031 · docs/open_questions.md says the market-hours timer is disabled
- **Status** open · **Evidence** VERIFIED 2026-11-09: `atlas-market-hours.timer` active; the engine runs every session in WATCH-ONLY because of the operator /pause of 2026-10-28 15:22 IST.
- **Detail** the pause is `atlas_state.mode = 'PAUSED'` (row id 1, the only row), enforced by `market_open.paused()`, `broker.place_order` and the kill switch.

### D-032 · No ISIN in raw bhavcopy before 2026-05-08
- **Status** won't fix (data we hold) · **Evidence** VERIFIED: 858 of 977 files are the legacy layout; renames before May cannot be checked from disk.

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
