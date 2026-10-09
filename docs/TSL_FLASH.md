# TSL FLASH — Architecture

**R1 · 10 Oct 2026 · Radar, deterministic slice**

> **IN ONE LINE**
>
> Two modes over one facts engine: the **Brief** (pre-market context) and
> **Radar** (event cards plus an extremes scanner). Radar answers the three
> questions retail actually asks when a stock breaks, and answers them from
> bhavcopy and exchange filings — never from a story.

> **WHAT IS BUILT — R1**
>
> Radar only, and within Radar the deterministic half. Question 01 (what
> happened, and why) and the computable part of question 02 (will it recover)
> ship. Question 03 (how capital would behave) and the base-rate library are
> designed here and not built. **No LLM touches anything in R1.**
>
> | Slice | State |
> | --- | --- |
> | Detection, scanner, heat, Hot 10 | built |
> | Levels (§05) | built |
> | Cause attribution from filings | built |
> | Forward-outcome recording | built |
> | Base-rate library (§08) | designed, needs n |
> | Question 03 — capital behaviour | designed, not built |
> | The Brief | later slice |

---

## 01 · The three questions are the structure

An event card is not a paragraph with headings. The three questions are the
card's **visible structure**, numbered, in this wording, in this order:

```
01  What happened, and why?
02  Will it recover, or fall further?
03  How would my capital behave?
```

They stay numbered on the page because a reader arriving mid-panic scans for
the one they came for. R1 renders 01 fully, 02 from levels and recorded
outcomes, and 03 as a declared gap rather than an empty box.

**Why these three.** They are what a retail holder asks in that order, and
each has a different evidentiary standard. 01 is answerable only from a filed
document. 02 is answerable from price history and a base rate. 03 is
answerable only from the reader's own position, which the engine does not know.
Collapsing them into prose is how a page ends up implying an answer it does
not have.

---

## 02 · Modes, and the one engine underneath

```
                      ┌─────────────────────────┐
                      │   radar/levels.py       │  pure functions over bhavcopy
                      │   radar/bars.py         │  no model, no network
                      └────────────┬────────────┘
                                   │  the FACTS ENGINE
                   ┌───────────────┴───────────────┐
                   │                               │
        ┌──────────▼──────────┐        ┌───────────▼──────────┐
        │  BRIEF (later)      │        │  RADAR (R1)          │
        │  pre-market context │        │  event cards         │
        │                     │        │  extremes scanner    │
        └─────────────────────┘        └──────────────────────┘
```

The facts engine is the part that must never acquire a dependency on a model.
Both modes read it; neither writes it. When the Brief is built it adds a
consumer, not a branch.

**Module layout.** Everything lives under `radar/`, which imports from
`engine/universe.py` (the Nifty 500 list and sector map) and reads
`data/processed/stocks/*.parquet` and `data/raw/bhavcopy/*.csv`. It imports
nothing from `atlas/` and writes no `signals_*` or `atlas_*` table. The
coupling to the signals chain is one-way and read-only: Radar runs after it and
consumes its bhavcopy, nothing more.

| File | Does |
| --- | --- |
| `radar/config.py` | defaults, and the loader that lets `radar_config` override them |
| `radar/bars.py` | bhavcopy access: per-symbol frames, session rows, true VWAP |
| `radar/levels.py` | §05, pure functions, one per level type |
| `radar/detect.py` | §03 triggers and §04 scanner |
| `radar/heat.py` | §06 severity, decay, the Hot 10 set |
| `radar/documents.py` | NSE filings → `radar_documents`, and cause attribution |
| `radar/outcomes.py` | §07 forward returns, for every event including displaced |
| `radar/store.py` | the only module that talks to Supabase |
| `radar/run_detection.py` | the nightly entry point and its completion check |

---

## 03 · Detection — Mode A, the event triggers

Universe: the Nifty 500 list in `engine/universe.py` (`ALL_SYMBOLS`). That file
is auto-generated from `ind_nifty500list.csv` and currently resolves to **706
symbols** after exclusions — the list has drifted wider than its name. Radar
uses it as-is and records the count it ran against in every run row, so a
universe change is visible in the record rather than inferred.

An event fires when **any one** of these holds on the session being scored:

| Trigger | Condition |
| --- | --- |
| `shock_1d` | \|close/prev_close − 1\| ≥ **8%** AND volume ≥ **3.0×** its 20-day average |
| `shock_5d` | \|close/close₋₅ − 1\| ≥ **15%** AND 5-day volume ≥ **2.0×** 5 × the 20-day average |
| `circuit` | the session was **locked at a price band** (see below) |

The 20-day volume average is taken **through the prior session** and excludes
the event day itself. Including it would dilute the very spike being measured —
a 20× day raises its own denominator by about 95%, which would turn a 20×
reading into roughly 10× and make the threshold mean something other than what
it says.

### The circuit trigger is narrower than it sounds, and says so

Bhavcopy does not publish the price band. Neither the legacy layout
(`SYMBOL…AVG_PRICE…DELIV_PER`) nor the 2026 layout (`TckrSymb…TtlTrfVal…`)
carries one, and the hard rule is that every number traces to bhavcopy. So
Radar detects the one circuit state that *is* derivable: **`high == low`**, a
session where the price never moved, combined with a move within tolerance of a
published band width (2 / 5 / 10 / 20%).

What that catches: a stock locked at a band from the first trade to the last.
What it misses, and what the doc must say plainly: a stock that touched the
band intraday and came off it. That session has `high != low` and is
indistinguishable, at EOD, from any other volatile day. Radar does not guess at
it. A missed circuit touch shows up as nothing; it never shows up as a
different trigger wearing a circuit label.

### One event per episode, not one per session

**This rule is an addition to the brief and it is load-bearing.** POLICYBZR
fell 36% on 2026-09-24 and then satisfied `shock_5d` on 09-25, 09-28, 09-29 and
09-30 as the five-day window dragged the crash behind it. Without a dedup rule
one episode would occupy five of the ten cards and the Hot 10 would describe a
single stock.

So: while an event for a symbol was detected within the last
`event_dedup_sessions` (**5**) trading sessions, a fresh trigger **updates** that
event — raising its heat if the new severity is higher — rather than creating a
second one. Past that window the symbol can produce a genuinely new event.

---

## 04 · Detection — Mode B, the extremes scanner

Separate from the event triggers and far looser, because its job is a daily
sweep rather than an alarm:

- a new **52-week high** or **52-week low** on the session, and
- volume ≥ **2.0×** the 20-day average.

Ranked by volume multiple, **top 10 each side**. Each row carries: % from the
extreme, volume multiple, delivery %, sector, and a **filing in the last 5
trading days** flag.

A scanner row that *also* satisfies a Mode A trigger is promoted: it gets a
full event card and appears in both places. The scanner is a list of where
price is; the card is a claim that something happened.

---

## 05 · Levels — computed by code, never by a model

Pure functions over bhavcopy. Each is labelled by type and carries its distance
from the current price as a percentage. **No "entry" or "exit" wording
anywhere** — these are reference prices, not instructions, and the naming is
what keeps them that way.

| `level_type` | Source |
| --- | --- |
| `w52_high`, `w52_low` | 252-session rolling extreme of daily high / low |
| `ipo_price` | first close in the series — **only when the series genuinely begins at listing** |
| `event_high`, `event_low` | the event session's own range |
| `event_vwap` | `AVG_PRICE` from legacy bhavcopy, or `TtlTrfVal / TtlTradgVol` from the 2026 layout — NSE's own figure, not a `(H+L+C)/3` proxy |
| `pre_event_close` | the close before the event session |
| `dma_50`, `dma_200` | simple moving average of close |
| `swing_high_1..3`, `swing_low_1..3` | most recent three 5-bar fractal pivots each side, daily |
| `rsi_14` | 14-day Wilder RSI (stored as a level row; it is a reading, not a price) |

### IPO price is usually absent, and that is correct

The per-stock parquets begin at `START_DATE = 2023-01-01`, so the first row of
a series is the first *downloaded* session, not the listing. Reporting it as an
IPO price for a stock listed in 2015 would be a fabricated number with a
plausible shape. Radar emits `ipo_price` only when the series starts **after**
the download floor — i.e. the stock listed inside the window — and within
`ipo_max_age_years` (**5**). Otherwise the line is dropped. POLICYBZR, listed
2021 with data from 2023, correctly gets no IPO price.

---

## 06 · Heat and the Hot 10

**There is no fixed card lifetime.** The page always shows exactly
`hot_cards_n` (**10**) cards, ranked by heat.

```
severity = max( |move%| / move_threshold% , vol_multiple / vol_threshold )
heat     = severity × 0.5 ^ (sessions_since_event / heat_half_life_sessions)
```

Severity is dimensionless on purpose: both terms are "how many times over the
bar it cleared", so a 4× move and a 4× volume spike are the same number and the
`max` picks whichever was more extreme. POLICYBZR on 2026-09-24: move 36.0/8 =
4.50, volume 20.41/3.0 = 6.80 → **severity 6.80**.

Half-life **5 sessions**, so a card at the top of the list sinks below a fresh
median detection inside a fortnight without anyone choosing a lifetime.

### Displacement

A new detection enters the set at its heat; the set is then trimmed to the ten
highest and whatever falls out gets `status='displaced'`.

Two consequences worth stating because both are deliberate:

1. **A weak new detection can displace itself.** If it enters below the current
   tenth, it is the one trimmed. It is still recorded, still measured, and
   never shown.
2. **Displacement is terminal for display.** A displaced card does not return
   to the list when the cards above it decay. Re-entry would make the Hot 10
   churn on arithmetic rather than on news, and a reader who saw a card
   disappear would see it reappear with no event behind it.

`status` is therefore a *display* state with one exception: `closed`, which
means the measurement window finished.

| `status` | Meaning |
| --- | --- |
| `live` | in the Hot 10 now |
| `displaced` | detected, measured, not shown |
| `closed` | `outcome_window_trading_days` elapsed; measurement complete |

---

## 07 · Display is not measurement

**A displaced event keeps recording forward returns.** Every detected event, on
the list or off it, accumulates:

- return at **5**, **20** and **60** trading days from the event-session close
- **maximum drawdown** over the window, as the worst close relative to that
  same close

Measured from the event **close**, not from the pre-event close: the pre-event
close is a price no reader could have acted on after the fact, and mixing the
two would put the shock itself inside the "recovery" number.

This is the part a displacement model would quietly starve. The base-rate
library in §08 is the reason Radar exists at all, and it needs every event —
including, especially, the unremarkable ones that never made the list. Ten
cards a day of survivors is a biased sample of exactly the wrong kind.
`tests/test_radar.py` holds a test that proves a displaced event still gets
outcome rows; it is not optional.

---

## 08 · The base-rate library (designed, not built)

Question 02 is only honestly answerable as a frequency: of the *n* prior events
that looked like this one, how many were higher 20 sessions later. That needs
`base_rate_min_n` (**20**) comparable events before it may be shown at all, and
below that threshold the card says so rather than quoting a percentage from
four observations.

R1 builds the collection side — §07 — and nothing on the query side. The
cohort definition (trigger type, severity band, sector, direction) is
deliberately not fixed yet: fixing it before there is a sample is how a cohort
gets chosen to flatter the first result.

---

## 09 · Cause attribution — no story without a document

**The single strictest rule in the product.** A cause may be shown only when an
exchange filing supports it.

- Documents come from NSE's own `corporate-announcements` API — the same
  endpoint `engine/tier1_fetch.py` already uses. **No news sites, scraped or
  otherwise.**
- Candidate window: the event session and the session before it, because a
  filing after one close moves the next session.
- Selection is by a **fixed category priority**, then most recent. No ranking
  model, no scoring, no tie-break by plausibility.
- Every document seen is stored in `radar_documents` with its URL, publish
  timestamp, symbols and a `text_hash`. The card cites one as primary; the rest
  remain inspectable.

When nothing is found the card says, verbatim:

> **No disclosed trigger found. Exchange clarification pending / not sought.**

Never a plausible story. An unsourced cause renders **visibly dimmer** than a
sourced one on the Hot 10, so the difference between "we know" and "we don't"
survives a glance.

### Delivery % does not identify a buyer

This sentence goes **on the card's face**, not in a footnote:

> Delivery % is settlement, not identity. It does not show who bought.

High delivery on a crash day is as consistent with forced selling into
long-term hands as with accumulation, and the page must not let a reader infer
the second. R1 enforces it as a required field of the card payload — a card
without it fails validation.

---

## 10 · Hard rules

1. **No buy / sell / target / accumulate / exit language** anywhere in output
   or UI copy. Enforced by a test over the page and the payload builders, not
   by review.
2. **Every number traces to NSE bhavcopy.** No scraped news sites. Filings come
   from NSE's own API and are used for *cause text*, never for a number.
3. **No cause without a cited document**, and the fixed sentence in §09 when
   there is none.
4. **Never infer a buyer from delivery %**, and say so on the card.
5. **Fail closed.** A missing or stale input **drops that line**. It never
   guesses, never substitutes a proxy, never carries yesterday's value forward.

---

## 11 · Schema

Six tables, `radar_` prefixed, sharing nothing with `signals_*` or `atlas_*`.

| Table | Holds |
| --- | --- |
| `radar_events` | one row per detected event; `heat`, `status`, `outcome` jsonb |
| `radar_documents` | every filing seen, with `url`, `published_at`, `symbols[]`, `text_hash` |
| `radar_card_versions` | the rendered card payload per event per trade date, with `validation_status` |
| `radar_levels` | one row per `(event, level_type)` with `price` and `computed_at` |
| `radar_scanner_daily` | the extremes sweep, one row per symbol-side-day |
| `radar_config` | the knobs below, editable without a deploy |

`radar_card_versions` is versioned by `(event_id, trade_date)` rather than
overwritten: the card for an event *changes* as sessions pass — heat decays,
distances move, outcomes fill in — and the record of what a reader was shown on
a given day is the only way to audit a claim after the fact.

### RLS

Base tables: **anon has no access and there is no anon policy**; writes are
service-role only. The page reads three **narrow views** —
`v_radar_hot10`, `v_radar_card_latest`, `v_radar_scanner_today` — which expose
display columns only and are granted to `anon`.

This is the one place R1 interpreted the brief rather than following it
literally. "Anon no access" is applied to the tables, and public read is
confined to views carrying nothing but what the page already renders, because
TSL Flash is a public page like the screener. To make Flash fully private
instead, revoke `anon` on those three views and switch the page's fetch to a
session token; nothing else changes.

---

## 12 · Config — `radar_config`, editable without a deploy

Values are seeded by the migration and read at the start of every run. A key
missing from the table falls back to the default in `radar/config.py`, and the
run logs which values it actually used.

| Key | Default | Notes |
| --- | --- | --- |
| `universe` | `nifty500` | resolves to `engine/universe.ALL_SYMBOLS` |
| `shock_1d_pct` | `8` | |
| `shock_1d_vol_mult` | `3.0` | |
| `shock_5d_pct` | `15` | |
| `shock_5d_vol_mult` | `2.0` | |
| `scanner_vol_mult` | `2.0` | |
| `scanner_top_n_each_side` | `10` | |
| `hot_cards_n` | `10` | |
| `heat_half_life_sessions` | `5` | |
| `outcome_window_trading_days` | `60` | |
| `base_rate_min_n` | `20` | §08 will not display below this |
| `swing_fractal_bars` | `5` | |

**Keys added beyond the brief**, each because the slice cannot be correct
without it — listed separately so the addition is visible, not buried:

| Key | Default | Why |
| --- | --- | --- |
| `event_dedup_sessions` | `5` | §03 — without it one crash fills the Hot 10 |
| `vol_avg_window` | `20` | the averaging window the thresholds are stated against |
| `filing_lookback_sessions` | `5` | the scanner's filing flag needs a window |
| `circuit_bands_pct` | `2,5,10,20` | §03 — the band widths a locked session is matched against |
| `ipo_max_age_years` | `5` | §05 |
| `rsi_period` | `14` | §05 |
| `dma_periods` | `50,200` | §05 |

---

## 13 · Scheduling

Radar runs **after** the nightly signals chain, as its **own cron entry**. It is
deliberately **not** joined to the `&&` chain.

```
05 13 * * 1-5   the signals chain (01b → build_market → 02b → 07 → 03b → 06_push → mark_signals)
40 13 * * 1-5   radar detection          ← separate entry, own log
```

**A Radar failure must never stop signals publishing.** Joined with `&&`, a
crash in a brand-new module would take out the entire screener for the night —
which is the failure mode the chain has already had twice, from a module far
better tested than this one.

It gets the same completion check the chain now has: **read back what it wrote**,
exit non-zero and send Telegram on zero. A scheduled job that fails quietly is
indistinguishable from one that succeeded with nothing to do.

---

## 14 · What R1 does not do

- **No LLM, anywhere.** Not for cause text, not for summaries, not for
  validation. R1 is the deterministic floor the generated layer will later sit
  on, and it has to be trustworthy on its own first.
- **No intraday anything.** Detection is EOD, from bhavcopy.
- **No base rates**, until §08 has its `n`.
- **No question 03.** Capital behaviour needs the reader's position, and
  inventing one would be the exact category of plausible story the rest of this
  document exists to prevent.
- **No circuit-touch detection.** See §03.
