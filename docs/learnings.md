# Learnings

Findings general enough to change how the next thing gets checked. Not a
changelog — commit messages carry the detail, and each entry here names the
commit rather than repeating it.

---

## A safety property held two modules from the gate relying on it

`60630ad` · 2026-09-29

**The shape.** `atlas_entry.regime_allows_side` blocked every short. Its SHORT
branch required `regime == "bull"` AND `extreme_bearish`. Both conditions are
individually satisfiable, so the branch was not self-blocking — it blocked only
because `build_market._classify` cannot emit that pair: `extreme_bearish`
requires `broken` (close < 200DMA × 0.97), and `broken` assigns
`market_regime = "bear"` *after* the bull assignment.

So "shorts are unreachable" was a property of the **classifier**, and the module
depending on it was two files away, asserting it in a comment that described the
conclusion rather than the mechanism. Nobody editing the classifier would know
they were holding up a safety property in the execution path.

**Why reading could not find it.** Both readings of the gate were wrong in
different directions. Reading the SHORT branch quickly suggested only
`extreme_bearish` gated it, which would have made a bear-market bounce reachable
— wrong, there is a second guard. Reading it carefully reproduced the comment's
claim that the two conditions conflict — also wrong, because they conflict only
elsewhere. What found it was enumerating all 32 combinations of regime × hedge
flag × sentiment and asserting the outcome of each. The hole was one cell.

**What to do instead.**

- When a property matters where the consequence lands, enforce it *there*, in one
  greppable place, not as the emergent product of conditions that are each
  satisfiable. `ALLOW_SHORT_ENTRIES = False`, checked first, is what makes the
  entry path independent of the classifier.
- Sweep the combination space for anything phrased as "X cannot happen". A
  property asserted as a conjunction is a claim about the *joint* space, and a
  sample of that space is not a proof of it. The sweeps are cheap; this one is
  four nested loops.
- When the guarantee genuinely does rest on an invariant in another module, test
  **that invariant** as well, and make the test prove it visited the relevant
  state. An invariant "confirmed" by a sweep that never entered the state in
  question is not confirmed — so the classifier test reports the regimes it
  actually saw and how many days carried the flag, and fails if either is absent.
- A comment stating a conclusion ("the two cannot hold at once") outlives the code
  that made it true. Prefer a comment that names the mechanism and the module it
  lives in, so a reader knows what to go and check.

**The generalisation worth carrying.** Redundant guards across modules are good
when each is independently sufficient. They are a trap when they are jointly
necessary — then there is no redundancy at all, only a dependency nobody
declared, and the failure mode is that removing an apparently unrelated condition
silently opens a path.

---

## A null result from a population that never tests the hypothesis

`ac43f53` · `20260930090000` · 2026-09-30

**The setup.** 75 signals were published with the entry zone on the wrong side of
price. Comparing them against correctly zoned signals from the *same two
sessions* — an unusually clean control, since the whole zone-known population
falls on those two days — gave opposite answers by direction:

```
LONG   wrong-side   41 resolved   mean −3.65%   win  2.4%   TARGET 1 / STOP 40
LONG   correct     221 resolved   mean +2.31%   win 61.5%   TARGET 136 / STOP 85
SHORT  wrong-side   32 resolved   mean −0.27%   win 43.8%   TARGET 0 / STOP 0
SHORT  correct      24 resolved   mean −0.40%   win 45.8%   TARGET 0 / STOP 0
```

**The trap.** The short rows look like evidence that the zone does not matter.
They are not evidence of anything. Every short in the record — both groups —
resolved `SAME_DAY`, with zero targets and zero stops, because shorts are
intraday MIS and are squared off before either level is reached. The zone's only
causal path to the outcome is via the entry and stop levels, and a same-day exit
severs it. The measurement cannot distinguish "the zone is irrelevant" from "we
closed before the zone could matter".

**The generalisation.** Before reading a null result, check that the population
could have produced a non-null one. A control group that is structurally
incapable of expressing the effect gives a null with the same shape as a real
one, and the smaller the sample the more persuasive it looks. Here the tell was
sitting in the data: `TARGET 0 / STOP 0` on both sides says the exit mechanism,
not the entry, decided every short outcome.

The long rows, by contrast, are strong: 40 of 41 stopped out at a 2.4% win rate
against 61.5% on the same days. A long entered above price on a bear FVG is
buying into resistance with the stop below, and it behaves exactly as that
description predicts.

**The open question, which this does NOT answer.** Whether the zone or the
0.30% entry-distance gate is doing the work is untested. The two are confounded
by construction: a signal only publishes when price is already at the zone, so
"at the zone" and "within 0.30%" are nearly the same condition on every row that
survives. Separating them needs signals that pass one and fail the other, which
the current gate never emits. Worth designing the outcome analysis around
deliberately, rather than inferring it from a comparison that cannot carry it.

**Also worth keeping:** the excluded rows were kept, not deleted. They are the
only evidence either way, and an exclusion that destroys its own justification
cannot be reviewed later.

---

## A redundancy argument is worth no more than the weaker guard's failure path

`c310594` · 2026-09-30

**The argument.** `06_push` dropped longs in a bearish regime and shorts in a
bullish one, duplicating a check the entry gate already makes. The case for
keeping both: they answer different questions, and redundancy is right where one
side failing open would quietly change behaviour — specifically, a missing
`market_regime` silently widening what ATLAS sees.

**Why it was wrong.** The publisher's filter sat inside a `try`, and its `except`
pushed everything:

```python
except Exception as e:
    log.warning(f"Could not fetch regime — pushing all signals: {e}")
```

So in the exact scenario the redundancy was invoked to cover — the regime
unreadable — the publisher's guard **opened**. Meanwhile the entry gate fails
*closed* on the same input: `regime == "unknown"` returns False for both sides
via `DEFAULT_ON_UNKNOWN_REGIME = CASH`. The guard argued as the backstop was the
only one that failed open in the case it was supposed to back up. It was not
redundancy; it was one working guard and one that abstained precisely when asked.

**The generalisation.** To claim two guards are redundant, read each one's
behaviour *under the specific failure you are defending against*, not its happy
path. A guard's value is entirely determined by what it does when its input is
missing or wrong — which is the one branch that never runs in normal operation
and therefore the one nobody has watched. "There are two checks" is not a safety
property. "Both checks deny by default when the input is absent" is.

**The symmetry with the first entry in this file.** That one was guards that are
*jointly necessary* being mistaken for redundant — remove either and a path
opens. This is guards asserted as *redundant* where one fails open. Opposite
errors, same omission: nobody read the failure path. In both cases the code that
mattered was a branch that does not execute on a normal day.

**Cheap test that would have caught it.** Assert the denial, not the presence:
feed each guard the degraded input — absent regime, unreadable file, timed-out
fetch — and require it to refuse. `test_regime_gate` now does this for the entry
gate; the publisher's filter never had such a test, which is part of why its
`except` went unexamined for four months.

---

## A monkeypatched dependency tests the stub, not the code (2026-10-01)

`MAX_CONCURRENT_EXPOSURE` shipped in `d9b8ba7` with two gates reading a helper
that could not possibly have satisfied them:

```python
# the gate, in atlas_entry.enter_trade
exp_ok, open_exposure, open_count = get_open_exposure()   # expects 3

# the helper it called
def get_open_exposure() -> tuple:
    """(readable, rupees, count) of capital already committed. ..."""
    ...
    return True, len(r.json() or [])                      # returns 2
```

The docstring was rewritten to describe rupees and a count. The body was never
changed from the old position-*count* query it had been. So the first live entry
attempt would have raised `ValueError: not enough values to unpack` at Gate 3 —
before any quote was fetched, on every symbol, every cycle.

**Why the test suite was green.** The test exercised the gate by replacing the
helper:

```python
AE.get_open_exposure = lambda r=readable, c=spent: (r, c, 3)
```

which is a *correct* 3-tuple. The test then asserted that the gate's arithmetic
mapped committed-rupees to allow / `SKIPPED_EXPOSURE` / `BLOCKED_NO_LEDGER`, and
it did. Every assertion passed against a stub that had the interface the caller
wanted, while the real implementation had a different one. The one thing that was
broken was the only thing the monkeypatch removed from the test's reach.

**The generalisation.** Monkeypatching a dependency to test a caller deletes the
contract between them from the test. That is the point — it is why you patch, so
the caller can be driven through states the real dependency cannot cheaply
produce — but it means the patch's shape is now an *unverified claim* about the
real function, and the test will not notice when the claim goes false. A
docstring is not an interface; it was in fact the thing that made this bug
invisible on reading, because it described the tuple the caller wanted.

**Cheap tests that would have caught it, in order of cost.** (1) Call the real
function once with no network and assert its arity — `len(...) == 3` — even if
the values are a failure case; a `return False, 0` path still proves the shape.
(2) Assert the patched signature against the real one with `inspect.signature`
or a return-annotation check. (3) Import-time smoke: exercise the gate once with
the real helper against an unreachable Supabase, which should yield
`BLOCKED_NO_LEDGER`, not a `ValueError`.

**What made it moot, and why that is not the lesson.** The cap was removed the
next day — broker funds are the only bound on exposure — so the broken gate never
ran. The bug survived a green 17-suite run, a self-review, and a push, and only
turned up because the code was being deleted. Deletion is not a test strategy.
