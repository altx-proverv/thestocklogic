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
