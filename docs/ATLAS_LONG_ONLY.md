# ATLAS — long only, one mechanism

**2026-10-12 · the simplification pass after 7 October**

> **THE RULE**
>
> Trade only when regime **AND** sentiment are both bullish. Otherwise stay in
> cash. Strict, both must hold. That means **longs only**.

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

| | Before | Now |
| --- | --- | --- |
| Sides | LONG and SHORT, from a 4-branch matrix | **LONG only**, one cell of sixteen |
| Product | CNC for longs, MIS for shorts | **CNC only** |
| Entry | MARKET (+ `market_protection`) | **LIMIT at the live price** |
| Stop | GTT for CNC, SL for MIS | **one two-leg OCO GTT** |
| Target | off (`ALLOW_AUTOMATED_TARGET=False`) | **on — an OCO needs both legs** |
| Fallback | marketable LIMIT | **marketable LIMIT, and now a self-halt** |
| On GTT failure | halt only if the exit ALSO failed | **halt whenever the GTT failed** |

### The matrix, collapsed

```
regime     sentiment   →  side
bull       bullish        LONG          the only tradeable state
everything else           cash
```

`blocked_by` is still **attributed precisely**, because it exists to be counted
from `atlas_entry_log` without parsing prose:

| State | `blocked_by` | Meaning |
| --- | --- | --- |
| bull + bullish | — | the one trade |
| bull + bearish, bear + bullish | `CONFLICT` | structure and participation disagree |
| bear/sideways + bearish | `LONG_ONLY` | the side it names is a short |
| sideways + bullish | `STRICT_RULE` | the branch the strict rule removes |
| any + mixed/unknown | `SENTIMENT` | an unreadable input is never a side |
| unknown regime | `REGIME` | |

### The cost, stated

The regime has been **sideways throughout the live history**, and
sideways + bullish was the only branch that produced a long in that state. On
this rule ATLAS takes **zero trades** over that period. That is the rule
working, not a regression.

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
This is the single most important thing the live check cannot prove, and it is
called out in `tools/live_gtt_check.py` as well as here. **Resolving it needs
either a Zerodha support answer in writing, or one observed same-day trigger.**

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
| `tools/live_exit_test.py` | `tools/live_gtt_check.py` — it tests the retired SL path |
| `docs/LIVE_EXIT_TEST.md` | §08 below |

### Partially dead

`exits.reconcile_exits()` still has a job — adopting positions at startup and
cancelling orphaned legs — but its **sibling-pairing** logic existed for the
two-single-GTT shape, which is gone. One OCO has no sibling to orphan.

---

## 07 · Not built: partial booking

> Hemal once described exiting 50% at target and trailing the rest with the
> stop at breakeven — but that was said about **MERIDIAN**, and its status is
> unclear.

**Full-quantity stop and target are what shipped.** Both GTT legs are for the
whole position.

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

## 08 · The live check

`tools/verify_exit_path.py` and `tools/verify_long_only.py` run against stubs
that enforce the documented rules. **A stub cannot tell you what Zerodha does.**

```
# on the box, market open, 10:00–14:00 IST
cd /home/ubuntu/thestocklogic
venv/bin/python -m tools.live_gtt_check --preflight
venv/bin/python -m tools.live_gtt_check --run --live
```

Buys **one share** CNC on a LIMIT, places the real OCO GTT through
`exits.protect()`, **reads it back from Zerodha** and checks both legs and both
trigger prices against what was sent, deletes the GTT, sells the share, and
confirms the account is flat with no GTT left resting.

**A tool must never print READY when one of its own checks failed its stated
expectation.** Every stated expectation goes through `expect()` and the verdict
is the AND of all of them — including *"ATLAS is HALTED"*, which the previous
preflight printed with "(expected True)" beside it and did not score.

ATLAS stays halted until this passes.
