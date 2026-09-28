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
