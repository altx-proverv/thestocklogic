# ATLAS — long only, one mechanism

**2026-10-12, revised 2026-10-13 · the simplification passes after 7 October**

> **THE RULE** · revised 2026-10-13
>
> **Longs are permitted by SENTIMENT alone.** Regime is not read at entry — the
> nightly scoring stage already filters on it, over the full universe, with the
> pipeline's own data. Longs only; shorts stay disabled.

> **WHY REGIME LEFT THE ENTRY GATE**
>
> It was **double-counting**. `engine/03b_score.py`'s
> `bear_regime_no_reversal` disqualified **42,187 longs on 7 October**, so a
> signal reaching `allowed_side()` has already passed a regime test. And the
> entry-side copy is the weaker one: a gate applied a second time to a
> population the better one already filtered cannot add safety — it can only
> subtract trades. It did: **one tradeable state out of twenty**.
>
> The nightly filter is untouched and remains the regime authority.
> `tests/test_batch_and_report.py` now asserts the backstop *there*, which is
> where it actually lives. The regime gate's data source is a separate job.
>
> | | Before | Now |
> | --- | --- | --- |
> | Tradeable states | 1 of 20 | **4 of 16** — every bullish-sentiment cell |
> | Regime at entry | applied | **reported, not applied** |
> | `blocked_by` labels | CONFLICT / LONG_ONLY / STRICT_RULE / SENTIMENT / REGIME | **LONG_ONLY / SENTIMENT** |

---

## 01 · Why the complexity was the bug

Every failure of 7 October came out of the hedge-short path, as a chain of
forced consequences rather than a mistake anyone made:

```
shorts        →  forced MIS          (rule 10, no overnight short)
MIS           →  no GTT available    (GTT is CNC-only)
no GTT        →  forced SL-M as the stop
SL-M          →  Kite refuses it via API without market_protection
rejected      →  fallback was a MARKET order, refused by the SAME rule
```

Removing the first link removes the class. Longs are CNC, CNC takes a GTT, and
a GTT is the mechanism the operator already places by hand and trusts.

The rule was **also already documented and not implemented**. The comment above
`ALLOW_SHORT_IN_SIDEWAYS` in `atlas/config.py` said *"the entry gate now
requires a bull regime … AND positive sentiment …, so sideways does not
qualify"* — while `allowed_side()` followed sentiment in a sideways regime and
took a long. The comment was right and the code was wrong.

---

## 02 · What the trading path is now

| | 7 Oct | After 12 Oct | **Now (13 Oct)** |
| --- | --- | --- | --- |
| Sides | LONG and SHORT | LONG only, 1 of 20 states | **LONG only, 4 of 16** |
| Entry permission | regime AND sentiment | regime AND sentiment, strictly | **sentiment alone** |
| Product | CNC longs, MIS shorts | CNC only | CNC only |
| Entry order | MARKET | LIMIT at the live price | LIMIT at the live price |
| Day-0 stop | SL-M → rejected | one OCO GTT | **regular SL order** |
| Day-1 stop | — | the same OCO GTT | **two GTTs: OCO + stop-only** |
| Target | none | 2R on the whole position | **2R on half** |
| Trailing | none | none | **breakeven at +3%, then swing lows** |
| Fallback | MARKET — same rule, failed too | marketable LIMIT + self-halt | marketable LIMIT + self-halt |

### The permission rule

```
sentiment   →  side
bullish        LONG       regardless of regime
bearish        cash       the side it names is a short
mixed          cash       breadth and the index disagree
unknown        cash       an unreadable input is never a side
```

`blocked_by` is still **attributed**, because it exists to be counted from
`atlas_entry_log` without parsing prose — but there are two labels now, not
five, and that is a consequence of the rule rather than a loss:

| State | `blocked_by` | Meaning |
| --- | --- | --- |
| any regime + bullish | — | the trade |
| any regime + bearish | `LONG_ONLY` | the side it names is a short |
| any + mixed / unknown | `SENTIMENT` | the reading is unusable |

`CONFLICT` is gone. It meant *"structure and participation disagree"* and was
only meaningful while both were applied here; with regime reported and not
applied, a bull regime with bearish breadth is simply bearish sentiment.
`STRICT_RULE` and `REGIME` are gone for the same reason.

### What this costs, and what it buys

The previous rule took **zero trades** over the live history, because the regime
has been sideways throughout and the strict AND required bull. On sentiment
alone, a sideways-but-bullish market is tradeable — which is the state the
strategy was designed for and the one the old strict AND locked it out of.

---

## 02b · Protection: day 0 is an order, day 1 onwards is a GTT

**The seam.** A GTT checks the **demat** account; shares bought today reach
demat at T+1. A regular SL order checks the **order book**, which knows about
today's buy. Different systems, different requirements.

```
09:20–15:20   day-0 SL order resting        (order book)
15:20         both GTTs placed              (overlap — the SL is still live)
16:00         the SL order is flushed
next open     the GTTs carry the position   (demat)
```

**Verified against the live broker rules before any of this was written**
(2026-10-13):

> *"Shares you purchase for delivery using the CNC product type appear under
> positions on the same day… Showing delivery shares in positions on the
> purchase day allows you to participate in intraday trading."*

> *"If your SL is triggered the same day, your trade will be considered
> intraday… however, you'll have to place a fresh SL order every day since all
> pending orders are cancelled at the end of the day."*

> *"Unmatched pending orders are automatically cancelled when the market
> closes."* — equity, 4:00 PM.

So a SELL SL on a same-day CNC buy **is accepted**, it works, and it dies at
the close. That is the day-0 mechanism and the reason it must hand over before
the close rather than next morning — a GTT placed next morning leaves the
position naked overnight, which is exactly when gaps happen. A GTT placed at
15:20 cannot fire today, so the two overlap instead of leaving a window.

### A defect this verification found in the GTT already shipped

The stop leg was priced **at** the stop (95.00) with its trigger 0.1% inside it
(94.90) — so for a SELL, the limit sat **above** the trigger. Price falls
through 94.90, the GTT fires, and a SELL LIMIT at 95.00 rests above the market
unfilled. Zerodha names it exactly:

> *"Your order's Last Traded Price (LTP) did not match the limit price you
> set… position your limit price more favorably (above trigger for buys, below
> trigger for sells)."*

**A stop that triggers and does not fill is worse than no stop, because the
position looks protected.** `gtt_leg_limit()` now puts every SELL leg's limit
below its own trigger, on both GTTs and on the day-0 SL.

### The 50/50 split — two GTTs per position

| Half | Instruction | Purpose |
| --- | --- | --- |
| half the quantity | **OCO GTT** — stop + target | booked at 2R |
| half the quantity | **single-leg GTT** — stop only | trailed, runs |

An OCO forces a target to be named, which fights *"winners are held and
trailed."* Doing both on the same position resolves it. The 500-pending-GTT cap
binds at 250 concurrent positions, so two per position is irrelevant.

**The odd share goes to the trailed half** — that is the half whose purpose is
to run, and at qty=1 the alternative is a position with a target and no
trailing leg. At **qty=1 there is no OCO leg at all** and the stop-only GTT
carries the position; a zero-quantity instruction is never sent.

| qty | OCO | trailed |
| --- | --- | --- |
| 1 | 0 | 1 |
| 2 | 1 | 1 |
| 3 | 1 | 2 |
| 45 | 22 | 23 |

### Trailing — stops move up only. Absolute.

```
price up 3% from entry  →  stop to the entry price (breakeven)
thereafter              →  trail to the most recent swing low as it forms
price through the stop  →  the stop fires. The trade was wrong.
```

**`tighten_only()` REFUSES a loosening update.** It is a structural guard, not
a convention: *"no code moves the stop down"* is a property of today's code
that the next edit can remove without anyone noticing — which is the shape of
the 7 October failure, where the guarantee lived in another module's behaviour
rather than in a local check.

It **raises** rather than returning the old value, because silently keeping the
old stop would make a loosening attempt invisible: the caller would log a
successful trail. Equal is allowed and is a no-op.

Every stop change in the codebase passes through it, and `next_stop()` ends by
calling it — so even if the arithmetic above produced a looser stop, it cannot
reach the broker. A swing low forming **below** the current stop in a pullback
is ignored, not followed down.

Measured from the **high since entry**, not the last close: a position that ran
8% and came back to +1% has earned its breakeven stop. Run **once** after the
close by `atlas/execution/preclose_gtt.py` — no intraday churn, because a stop
that moves on every tick is a stop that gets hit by noise.

### ⚠ One precondition the engine cannot create for itself

A sell GTT needs **DDPI (or POA)** on the account, or a CDSL TPIN authorisation
that is valid for **one day** and must be redone every trading day after 7 AM:

> *"You must authorise sell GTT orders on equity holdings using the CDSL TPIN,
> unless you have submitted a POA or DDPI."*

> *"If you have a non-POA/DDPI account, you must pre-authorise your GTT sell
> order by entering the CDSL TPIN before the GTT is triggered to avoid
> rejection."*

**Without DDPI the overnight GTTs rest and are rejected at trigger, silently.**
The preflight prints this and cannot test it. Confirm DDPI is active before
relying on any of the overnight protection.

---

## 03 · The GTT, confirmed against the live docs

Checked 2026-10-12 against <https://kite.trade/docs/connect/v3/gtt/> and
<https://zerodha.com/tos/gtt/>. Not from memory.

**Payload** — `type: "two-leg"`, a `condition` carrying `exchange`,
`tradingsymbol`, `trigger_values` (two) and `last_price`, and an `orders` array
of two objects each with `exchange`, `tradingsymbol`, `transaction_type`,
`quantity`, `order_type`, `product`, `price`. The Python SDK takes the condition
fields as keyword arguments and assembles the object itself.

**Cap** — *"At a particular point of time, You / each client may only have a
maximum of 500 pending GTTs."* Handled as a placement failure, which means the
position is exited and ATLAS halts, rather than left silently unprotected. The
preflight checks headroom before the live test.

**Validity** — *"All GTT's are cancelled after 365 days."*

**No execution guarantee** — *"This GTT order does not assure execution of an
order."*

### ⚠ The settlement question is NOT settled, and it matters

The brief asked whether CNC holdings must settle before a sell GTT is accepted.
The docs do not answer it cleanly, and the honest answer is **placement is
fine, triggering may not be**:

- The GTT API accepts a sell GTT on a freshly bought position — Zerodha ships
  an "instant GTT on equity buys" feature that sets stop and target *in the buy
  order window*.
- But the rejection article says: *"If you do not have enough holdings of the
  specific stock in your demat account, the exchange will reject your GTT
  order"*, and the ToS says *"At all times while placing GTT, You are required
  to maintain … sufficient quantity of holdings of the respective scrip."*
- Shares bought today reach demat at **T+1**.

**So a stop leg that triggers on the entry day may be rejected for insufficient
holdings even though the GTT was accepted.** The exposure window is day 0 only;
from T+1 the GTT is sound, and ATLAS holds longs overnight so most of the life
of the position is covered.

A Zerodha forum comment raised exactly this and went unanswered by their team.
This is the single most important thing the test suite cannot prove.
**Resolving it needs either a Zerodha support answer in writing, or one
observed same-day trigger on a real position** — and since there is no
verification trade (§08), that observation will come from a real entry.

---

## 04 · The gap between fill and GTT

Unavoidable, so: **short and loud**.

1. `await_fill()` **blocks** in the same cycle as the entry. Detecting the fill
   on the next cycle would leave a minute of unprotected exposure on every
   entry.
2. The GTT is placed immediately on fill, not on the next cycle.
3. If it does not place, the position is exited **at once** on a marketable
   LIMIT, and ATLAS **self-halts**.

### Why the fallback is genuinely independent

`assert_independent()` runs at import and now checks a fourth axis — the **API
endpoint**:

| | primary | fallback |
| --- | --- | --- |
| mechanism | `GTT_OCO` | `LIMIT_THROUGH` |
| endpoint | `POST /gtt/triggers` | `POST /orders` |
| order type | GTT | LIMIT |
| needs a trigger | yes | no |
| market protection | no | no |
| immediate | no (rests) | yes |

Two mechanisms on two endpoints with two rule sets cannot share a rejection the
way SL-M and MARKET did. The axis has teeth: it now refuses `SL` +
`LIMIT_THROUGH`, which is the pair that shipped last week.

**`must_halt` is set whenever the GTT failed, even if the fallback worked.**
Halting only when *both* failed treats a working fallback as evidence that the
primary is fine — and that reasoning is what made a second unprotected position
possible on 7 October.

---

## 05 · The halt path failed open. Fixed.

`breaker.py` resolved the halt file as
`os.environ.get("ATLAS_HALT_FILE", "~/.atlas/halt")` while the real file is
`/var/lib/atlas/halt`. The variable was set in **exactly one of eight** places
that run ATLAS code:

| Entry point | Had the variable |
| --- | --- |
| `atlas-market-hours.service` | ✅ |
| `atlas-engine-report.service` | ❌ |
| `atlas-mis-squareoff.service` | ❌ |
| cron: `zerodha_morning` | ❌ |
| cron: `daily_report` | ❌ |
| cron: `bot_listener` | ❌ |
| cron: `trade_outcome_checker` | ❌ |
| cron: `decay` | ❌ |

All seven resolved the halt to `~/.atlas/halt`, which does not exist, read "not
halted", and would have traded. **Including `zerodha_morning`** — the job now
asked to *report* the halt state would have reported the wrong file and the
wrong answer.

**The fix:** one constant, `atlas/config.HALT_FILE`, **defaulting to
`/var/lib/atlas/halt`**. An unset environment is now the correct configuration
rather than the broken one, so losing the variable is no longer a failure mode.

The env var is still *readable*, so the test suite and the verification
harnesses can point at a scratch file without touching the live sentinel — and
when it points anywhere else, `halt_file_state()` says so, startup logs it at
**ERROR**, and the 08:30 Telegram leads with ⚠ **HALT PATH WRONG**. A silent
override is what was removed; an override that announces itself is not.

Reported on every startup and in the first message of the day:

```
▶️ Not halted · /var/lib/atlas/halt
🛑 HALTED — /var/lib/atlas/halt
   ATLAS self-halt 2026-10-07 — unprotected BHARTIARTL position
   No entries will be placed. /resume or delete the file.
```

---

## 06 · Dead code — listed, NOT deleted

Everything below is unreachable under long-only. The operator asked for a list
rather than a deletion in this pass.

### Unreachable by construction

| What | Where | Why it is dead |
| --- | --- | --- |
| **Opening-range gate** (Gate 2) | `atlas_entry.py` ~679, `OPENING_RANGE_GATE_APPLIES_TO = ("SHORT",)` | applies to shorts only, and `direction` can only be `LONG` |
| `index_state.get_market_direction()` | `atlas/execution/index_state.py` | its only live caller is that gate |
| **MIS square-off** | `atlas/execution/mis_squareoff.py`, `exits.squareoff_mis()`, `deploy/atlas-mis-squareoff.{timer,service}` | longs are CNC; there are no MIS positions to square off |
| MIS timing config | `MIS_EXIT_*`, `MIS_BROKER_CUTOFF*` in `config.py` | read only by the square-off |
| `exits._protect_mis_sl()` | `exits.py` | the SL path from `954b68e`. **Retired:** it now refuses and places nothing |
| `exits.stop_limit_price()`, `STOP_LIMIT_SLIP_PCT` | `exits.py` | only the SL path priced a limit past a trigger |
| `broker.place_sl_order()` | `broker.py` | its only caller is `trade_management.place_mis_bracket_order`, which nothing imports |
| `trade_management.py` | whole module | nothing imports it (already dead before this pass) |

### Config with no live effect

`ALLOW_SHORT_IN_BULLISH`, `ALLOW_SHORT_IN_BEARISH`, `ALLOW_SHORT_IN_SIDEWAYS`,
`REQUIRE_EXTREME_BEARISH_FOR_SHORTS`, `SHORT_PRODUCT_TYPE`,
`ALLOW_OVERNIGHT_SHORT`.

`SHORT_MARGIN_PCT_ESTIMATE` is still *referenced* — `position_sizing` branches
on direction — but the short half of every one of those branches is now
unreachable.

`ORDER_SPECS["SL"]`, `["SLM"]`, `["MARKET"]` are kept **deliberately**: they are
what `assert_independent()` names as the pairs *not* to use, and that is their
only remaining job.

### Superseded tooling

| What | Superseded by |
| --- | --- |
| `tools/live_exit_test.py` | **DELETED 2026-10-13** |
| `tools/live_gtt_check.py` | **DELETED 2026-10-13** |
| `tools/live_day0_check.py` | **DELETED 2026-10-13** |
| `docs/LIVE_EXIT_TEST.md` | **DELETED 2026-10-13** — a runbook for a deleted tool is the accident this policy prevents, and a DEPRECATED header is a half-measure |

All three live drivers are gone. **ATLAS does not run 1-share verifications
anywhere** — standing policy from 2026-10-13. The exit-path logic they drove
lives in `atlas/execution/exits.py` and is unchanged; only the drivers went.

### Partially dead

`exits.reconcile_exits()` still has a job — adopting positions at startup and
cancelling orphaned legs — but its **sibling-pairing** logic existed for the
two-single-GTT shape, which is gone. One OCO has no sibling to orphan.

---

## 07 · Partial booking — BUILT, 2026-10-13

> Hemal once described exiting 50% at target and trailing the rest with the
> stop at breakeven — but that was said about **MERIDIAN**, and its status is
> unclear.

**RESOLVED 2026-10-13 — the 50/50 split is built.** See §02b. Half the quantity
takes an OCO with a 2R target; half takes a stop-only GTT and is trailed. The
tension below is what that resolves, and the note is kept because the reasoning
is still the reasoning.

The tension this leaves is real and worth naming: a fixed 2R target caps every
winner at 2R, against the *"target = NONE. Winners are held and trailed"* line
in `engine/zone_entry.py`. Before today, `ALLOW_AUTOMATED_TARGET=False` resolved
that tension by placing no target at all — but an OCO needs both legs by
construction, so that option no longer exists without changing the mechanism.

**Open question for the operator.** Three possibilities, none of them taken:

1. full 2R target, as now;
2. partial booking at 2R with the remainder trailed — needs a second
   instruction and reopens the orphan-sibling problem the OCO removes;
3. a wider target, so the cap bites less often.

---

## 08 · How ATLAS goes live

### Position size

**₹10,000 notional per trade**, share count **rounded to the nearest whole
share** — `FIXED_NOTIONAL_PER_TRADE` in `atlas/config.py`, the only place the
figure appears.

| price | shares | notional |
| --- | --- | --- |
| 150 | 67 | 10,050 |
| 1,200 | 8 | 9,600 |
| 4,000 | 3 | 12,000 |
| 6,666 | 2 | 13,332 |
| 6,667 | 1 | 6,667 |
| 15,000 | 1 | 15,000 |

Rounded, not floored: flooring put the median position 10.7% off target across
1,484 published signals, rounding to the nearest single share 1.7%. Above
₹10,000 a share, one share is taken regardless and the trade is flagged
`oversized` — the instrument must not be blind to MRF or MARUTI because of its
own test size.

**Expected to move to ₹1–1.5 lakh per trade once the system is proven.** At
that size the share count stops being 1–3 and the rounding question stops
mattering; the figure changes in one constant.

### There is no separate verification trade

**The first real trades are the verification.** There is no 1-share dry run and
no live driver — `tools/live_exit_test.py`, `tools/live_gtt_check.py` and
`tools/live_day0_check.py` are all deleted. ATLAS does not run 1-share
verifications anywhere. That is standing policy, not a gap.

### What is verified, and what is not

The day-0 stop-loss path and the GTT handover are verified against **Zerodha's
documented behaviour** and against **the test suite** — 39 suites,
`verify_exit_path` 78/0, `verify_long_only` 33/0, `verify_day0_and_trail` 47/0.

They are **not** verified against a live fill.

**Broker acceptance of the day-0 SL is expected but unproven.** The expectation
rests on Zerodha's own documentation, quoted in §02b: delivery shares appear
under Positions on the purchase day explicitly so they can be traded
intraday, and *"if your SL is triggered the same day, your trade will be
considered intraday."* That is a strong reading. It is not a fill.

The first real entry is where it stops being a reading. Watch that one.

### Stops and targets are indicative, not a guarantee

The protection layer is a **best effort**, and the gaps are named rather than
glossed:

- Zerodha: a GTT *"does not assure execution of an order."*
- A sell GTT needs **DDPI or POA**, else a daily CDSL TPIN authorisation —
  without it the overnight GTTs rest and are **rejected at trigger, silently**
  (§02b).
- A GTT triggering on day 0 can be rejected for want of demat holdings, which
  is the whole reason day 0 uses an SL order instead.
- A triggered stop is a LIMIT, and a limit can go unfilled in a gap.

**Hemal watches positions manually during market hours.** The automation
reduces how much has to be watched; it does not replace watching.

### Entry conditions, and why zero entries is correct

An entry requires **both**:

1. price **inside the zone band** `[entry_low, entry_high]`, not mitigated, and
   on the correct side of the stop; and
2. price **within `ENTRY_PROXIMITY_PCT` (1.0%) of `entry_ref`**.

Plus bullish sentiment, long only, CNC only.

**Expect very few entries per session — sometimes zero.** Measured on the
2026-10-12 batch: of 394 published zones with a live price, **3** passed the
band test and **1** was an enterable LONG signal. Within 1% of `entry_ref`: 30
of 394. The band is currently the stricter of the two.

**Zero entries is correct behaviour, not a fault.** The gates are there to sit
out; a session with no trade means nothing came to its level. The thing to
investigate is not a quiet day — it is a day where `atlas_entry_log` is empty,
because that means the engine did not look.
