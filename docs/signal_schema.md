# The signal row

What `signals` carries, what it means, and which fields are not what their name
suggests. Written 2026-10-02 after an audit found three fields lying quietly:
`sector` constant at `OTHER` for seventeen months, `publication_kind` acting as a
disguised timestamp, and `score` perfectly collinear with the five sub-scores it
was being tested alongside.

Every field is tagged:

| tag | meaning |
|---|---|
| **RECORDED** | measured or observed at publication. Safe to use as a feature. |
| **DERIVED** | computed from other fields on the same row. Not independent evidence. |
| **CONSTANT** | the same on every row. Carries no per-row information. |
| **DEPRECATED** | written by nothing, or read by nothing, or both. |
| **PROVENANCE** | describes the row rather than the market. |

## Identity and provenance

| field | tag | notes |
|---|---|---|
| `id` | PROVENANCE | serial |
| `signal_date` | RECORDED | the batch date, not the entry date. Entry is the next session. |
| `symbol` | RECORDED | |
| `direction` | RECORDED | LONG / SHORT |
| `created_at` | PROVENANCE | |
| `publication_kind` | PROVENANCE | `signal` = ATLAS-actionable. `candidate` = watchlist only, never entered. **`NULL` = published before the column existed**, so actionability was never recorded. Do not read NULL as either value. |
| `sector_as_of` | PROVENANCE | `recorded` = written at publication. `backfill-YYYY-MM-DD` = imputed later from the then-current map. |
| `engine_sha` | PROVENANCE | short git SHA of the code that wrote the row. **NULL = written before 2026-10-02**, when provenance did not exist. The literal `unknown` = the SHA could not be read at run time. Two different facts, stored differently. |
| `engine_ran_at` | PROVENANCE | when the chain wrote it, UTC. Distinct from `signal_date` (the batch's trading date) and `created_at` (the database default): a batch re-run days later shares the signal_date and carries a later `engine_ran_at`, which is the only way to tell a replay from an original. |

## Levels and sizing

| field | tag | notes |
|---|---|---|
| `entry_ref` | RECORDED | the zone edge price must retrace into |
| `entry_low`, `entry_high` | RECORDED | the zone band; entry happens inside it |
| `sl` | RECORDED | structural stop, swing extreme forced outside the zone |
| `stop_pct` | RECORDED | percent. NULL before 2026-08-10. |
| `entry_dist_pct` | RECORDED | distance from the close to the zone. No longer gates anything — the entry-distance filter was removed 2026-10-01 — but still the watchlist sort key. |
| `qty`, `notional`, `risk_inr` | DERIVED | from the ₹3,000 / ₹1,00,000 sizing rule. NULL before 2026-06-09. |
| `product` | DERIVED | CNC for LONG, MIS for SHORT. No other branch. |
| `target_1`, `target_2` | DERIVED | 2R and 3R off the stop. **Measurement yardstick only** — ATLAS trades with no target (`ALLOW_AUTOMATED_TARGET = False`). |
| `rr_1` | CONSTANT | 2.0 on every row |
| `rr_2` | CONSTANT | 3.0 on every row |
| `sl_pct` | DEPRECATED | 06_push never writes it; NULL on 736 of 1,012. The page computes the distance instead. |

## Scoring

`score` is **exactly** the sum of the five components — verified on all 845 rows
that carry them, mean gap 0.00, zero exceptions. The six are one quantity and its
decomposition. Testing them as six features double-counts.

| field | tag |
|---|---|
| `score` | DERIVED — sum of the five below |
| `score_regime`, `score_smc`, `score_technical`, `score_volume`, `score_rr` | DERIVED components |
| `grade` | DERIVED from `score` thresholds. A+ ≥80, A ≥75, B ≥65. Grade `C` exists only from 2026-08-11, so a low grade is partly an era marker. |
| `setup_name` | RECORDED — but a *label* derived from the SMC trigger flags, which are in `signals_features`, not here. |

## Market and instrument state

| field | tag | notes |
|---|---|---|
| `rsi`, `rvol`, `atr_pct`, `delivery_pct` | RECORDED | NULL before 2026-06-09 |
| `structure_trend` | RECORDED | daily, not weekly. Nearly collinear with `direction`. |
| `zone_source` | RECORDED | which family the zone came from. NULL before 2026-08-10. |
| `zone_side_valid` | RECORDED | FALSE on 75 rows whose zone sat on the wrong side of price. NULL where `zone_source` is absent, i.e. unassessable rather than valid. |
| `sector` | RECORDED or imputed — **check `sector_as_of`** | |
| `sector_bias` | RECORDED | the sector's momentum reading that day. Never backfilled: a point-in-time value cannot be reconstructed. |
| `market_regime` | DEPRECATED | 567 rows hold the literal string `'nan'`, 272 `'unknown'`, 6 real values. Unusable. |
| `vix_close` | DEPRECATED | NULL on 1,006 of 1,012 |
| `trade_type` | DEPRECATED | 841 empty strings, 167 NULL, ~4 real values |

## Coverage cliffs

Every one is a code deployment, not a market event. Before `engine_sha` existed
these had to be inferred from which columns are null, which stops working the
moment two changes land in one week.

| from | what starts being recorded |
|---|---|
| 2026-06-09 | `rsi`, `rvol`, `atr_pct`, `delivery_pct`, `structure_trend`, `qty`, `risk_inr` |
| 2026-08-10 | `zone_source`, `entry_dist_pct`, `stop_pct`, `product`, `notional` |
| 2026-10-01 | `publication_kind` meaningful; earlier rows NULL |
| 2026-10-02 | `sector`, `sector_bias`, `sector_as_of`, `engine_sha` |

## Where the rest of it lives

`signals` is the product-facing row — the page and the resolvers read it, so it
changes rarely. The ~49 columns 03b computes and used to discard are in
**`signals_features`**, keyed `(signal_date, symbol, direction)`. That split
exists so adding a learning-loop input does not touch the table subscribers read.

`signal_excursions` holds the daily path after entry — high, low, close, MFE and
MAE per day for 20 days — because every resolved outcome exits at exactly −1R or
+2R by construction, which makes no exit rule testable from `signal_outcomes`
alone.
