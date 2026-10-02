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

---

## A view that fails to be REPLACED is still a view (2026-10-01)

Two migrations pasted whole into the Supabase SQL editor applied only a prefix
of their statements and reported "Success. No rows returned".

```
20260930093000_exclude_wrong_side_zone_signals.sql   3 of 12 statements
  applied:  zone_side_valid column, its comment, the backfill
  silent:   v_marks_zone_valid, v_wrong_side_excluded, and the
            CREATE OR REPLACE of v_signal_window and v_window_summary

PENDING_publication_kind.sql                         1 of 6 statements
  applied:  publication_kind column
  silent:   CHECK constraint, column comment, v_watchlist
```

**The failure that matters is not the missing object.** A view that was never
created errors the first time anything queries it, which is a loud, same-day
discovery. `v_marks_zone_valid` and `v_watchlist` were in that category and cost
nothing, because nothing had started reading them yet.

The expensive one is `CREATE OR REPLACE VIEW` on a view that already exists.
When the REPLACE does not run, the view is still present, still queryable, still
returning plausible numbers — and holding its old definition. `v_signal_window`
and `v_window_summary` have existed since 21 August. The 30 September migration
was supposed to replace both so that accuracy excluded 75 wrong-side signals. It
didn't run. So for a day the dashboard carried the sentence "75 signals are
excluded as wrong-side and why" over figures that excluded nothing, and every
check anyone would naturally run — does the column exist, is it backfilled, does
the view exist, does the page render — passed.

**Why it was hard to see.** Each half looked finished from its own side. The
code side was complete and correct: the column was added, the backfill ran, 75
rows went false, the page copy was updated, the tests passed. The database side
had every object the code names. Nothing in either half reveals that one object's
*definition* is a version behind, because a stale view has no symptom except a
number that is slightly wrong — and 44.28% against 44.45% is not a number anyone
spots.

**The generalisation.** For additive DDL, existence is a sufficient check:
the object is there or it is not. For *replacing* DDL — `CREATE OR REPLACE`,
`ALTER ... SET`, a re-granted policy — existence proves nothing, because the old
version satisfies it. The check has to interrogate the content: does this view's
definition mention the column it was replaced to filter on.

```sql
SELECT position('zone_side_valid' IN definition) > 0
FROM pg_views WHERE viewname = 'v_signal_window';
```

**The second-order lesson, which is the one that generalises past Postgres.**
A tool's success message describes what the tool did, not what you asked for.
This is the same shape as the monkeypatched-dependency entry above: in both
cases a green signal was produced by something other than the thing under test —
there a stub standing in for the real function, here an editor reporting on the
statements it chose to run. The remedy is identical and it is not "be careful":
assert the end state, from the outside, against the artefact itself.

**What changed.** `migrations/README.md` now requires every file to end with a
catalogue query returning one row of booleans, and says to apply one statement at
a time. The repair file is written that way.

## A transport limit that an explicit limit cannot raise (2026-10-02)

`engine/excursions.py` was written to measure every signal and every detection
incrementally: fetch them all, skip the ones already in `signal_excursions`, walk
the rest. All three of its fetches were a single GET. PostgREST returns **at most
1,000 rows**, and an explicit `limit` does not raise it:

```
  limit=(none)      -> 1000 rows   (table holds 2,941)
  limit=2000        -> 1000 rows
  limit=200000      -> 1000 rows
```

HTTP 200 every time. No warning, no `Content-Range` complaint, nothing in the
log. `existing_keys()` even asked for `limit=200000` — which reads as a handled
case and is the more dangerous shape, because the next person sees a number and
assumes it was measured.

The three consequences were all silent:

| fetch | rows | what actually happened |
|---|---|---|
| `fetch_detections` | 2,787 | measures the newest 1,000 (36%), reports a complete run |
| `fetch_signals` | 845 | fine today, truncates the day it crosses 1,000 — oldest first |
| `existing_keys` | ~3,600 eventually | sees 1,000, calls the rest unmeasured, re-walks them every night forever |

The one that matters is the third, because it defeats the incremental design
without failing: the job keeps finishing, keeps writing identical rows, and the
log keeps saying it measured N signals.

**The lesson.** A paging limit is a property of the transport, not a parameter of
the request. Asking for more is not the same as being given more, and a successful
response is not evidence that it was complete. The only reliable test is whether
the last page came back short — which is now the single `_get_all()` every fetch
in both modules goes through, so the check cannot be omitted one call at a time.

Related: the same class of defect as [a monkeypatched dependency tests the stub,
not the code] — in both cases the code ran, returned, and was wrong, and a test
over its output could not see it.

## A check that matches its own rationale — the fifth time (2026-10-02)

`tests/test_detection_scoring.py` asserted that `score_live_outcomes.py` does not
try to raise the row cap with `limit=200000`. It failed. The module's own
docstring says *"limit=200000 returns exactly 1,000"* — in order to explain why
that approach is wrong. The check found the explanation and reported it as the
defect.

This has now happened five times in this repo: `MAX_ENTRY_DIST_PCT`, the MERIDIAN
atlas-separation check, the no-keyword check, the no-swing-data check, and this
one. Every time the fix was the same, and every time I wrote the next one the same
wrong way. The pattern is not three coincidences and it is not five either — it is
that a grep over source text cannot distinguish a prohibition from its own
rationale, and the better a module documents why it avoids something, the more
likely a text search over it fails.

`tests/_srcutil.py` now holds `executable_source()` — `ast.parse`, strip every
docstring, `ast.unparse` — so comments and prose are gone and only what executes
remains. It is shared rather than copied precisely because the inline copy in
`test_excursions.py` did not stop me writing the fifth one.

**The lesson.** Interrogate the runtime object: the module namespace, the code
object, the parsed tree. Never the file as text. And when the same fix lands
twice, put it somewhere the third author has to find.

## abs() on a P&L turns a loss into a win (2026-10-02)

`update_outcomes.evaluate` booked a winning trade as:

```python
pnl = abs(exit_price - actual_entry) * qty
```

The `abs()` reads as defensive — "P&L magnitude, sign comes from the branch" —
and it is, for every row where the target is actually beyond the fill. On 7 rows
where it was not, the trade touched a level *between* the entry and the stop,
`WIN_T1` was written, and `abs()` reported the resulting loss as a profit. OFSS
2026-06-01 sits in the record at **+226.36 per share while actually losing
226.36**.

The LOSS branch directly below it uses `-abs(actual_entry - sl)` and is correct,
which is what made the pair look considered rather than wrong.

Three defects had to line up, and each on its own looked harmless:

1. the win level came from `entry_ref` instead of the fill, so it could land
   anywhere relative to the actual entry;
2. nothing asserted the target was on the winning side of the fill;
3. `abs()` erased the sign of whatever came out.

**The lesson.** `abs()` in a P&L expression is a silent assumption about
direction, and the place it fails is exactly the place the direction was already
wrong. Sign the arithmetic from the direction field and let a negative number be
negative — a loss booked as a loss is visible, and the guard in (2) then has
something to catch. The same block existed in `trade_review.py` with the same
`abs()`; two copies of a resolver is the actual defect, and only one of them was
in the published path, which is why nothing ever visibly diverged.

## Having the helper is not the same as reaching for it (2026-10-02)

One turn after writing `tests/_srcutil.py` specifically to stop checks matching
their own prose, I wrote `test_outcome_integrity.py` with
`inspect.getsource(mod)` and a text search for `abs(` — and it failed on the
comment explaining why the `abs()` had been removed. Sixth occurrence, second
since the fix existed.

Then the corrected version failed *again*: `executable_source()` stripped the
prose, but the 400-character window after the marker ran past the win branch into
the LOSS branch, where `abs(actual_entry - sl)` is correct. A scoping bug
replacing a prose bug.

The version that works finds the `if outcome == "WIN_T1"` node with `ast` and
reads only that branch's body — and asserts the loss branch *still* contains a
magnitude, so the check proves it is scoped rather than merely finding nothing.

**The lesson.** "Search the source for a forbidden string" fails twice over: once
on prose that discusses the string, and once on adjacent code where the string is
correct. Both are symptoms of the same thing — the unit of the assertion was a
character range when it should have been a syntax node. Name the node.

## pandas has no Series `<<`, and the two sites looked identical (2026-10-02)

`gates_failed_mask` is built twice. In `score_vectorized`:

```python
c = cond.to_numpy(dtype=bool, na_value=False)
bits |= (c.astype(np.int32) << i)          # ndarray — fine
```

and in `compute_trade_levels_vectorized`:

```python
df["gates_failed_mask"] | (invalid.astype(int) << GATE_ZONE_ENTRY_BIT)
```

`invalid` is a column. pandas implements `&`, `|` and `^` on a Series and does
**not** implement `<<` or `>>`, so the second raises
`TypeError: unsupported operand type(s) for <<: 'Series' and 'int'`. 03b died on
the box, nothing qualified, no parquet was written, and the thing downstream of it
had nothing to read.

The two expressions read as the same operation. The difference is that one had a
`.to_numpy()` three lines earlier and the other did not — and the one that did was
written first, which is what made the second look already-proven.

**A second defect was hiding behind it.** `gates_failed` and `gates_failed_mask`
were missing from `process_direction`'s column-propagation list, so once the shift
worked the values were computed and immediately discarded. Three lines above that
list sits a comment warning that exactly this happens. Fixing the crash would have
produced a column that was always zero, which is worse than a crash: a reject
sampler recording every zone-validation rejection as failing nothing.

**Why the suite passed.** The test rebuilt the mask itself with numpy arrays
rather than calling the production functions. A test that reimplements the thing
it checks passes whatever the reimplementation does — the same shape as [a
monkeypatched dependency tests the stub, not the code], and the third instance of
that class in one session. The fix is the same every time: call the real function
on a real frame.

**The lesson.** When one expression is copied to a second site, the thing that
made it correct may not have come with it. And "`&` works on a Series" is not
evidence that `<<` does — NumPy semantics are available on pandas objects
selectively, not wholesale. Run the production path.

## A recovered row is a case, not a control (2026-10-02)

Running the real pipeline to verify the above surfaced something the synthetic
fixture could not. The accumulation screen runs *after* 03b's disqualifier block
and can un-disqualify a row: it clears `disqualified` and `disqualify_reason`
while `gates_failed` keeps its count. So a **qualifying** row can carry
`gates_failed >= 1`.

The test asserted "qualifying rows fail no gates". True of the fixture, false in
production.

This is not a bug — `gates_failed` means "this row's features failed a gate", not
"this row was rejected", and recording the failure of a recovered row is the
useful behaviour. But it sets a trap for the analysis it exists to serve: defining
the control group as `gates_failed >= 1` instead of `disqualified == True` would
put every screen-recovered row on the wrong side of the comparison, and the
membership test would measure the accumulation screen rather than the filters.

**The lesson.** A derived column's meaning is fixed by where in the pipeline it is
computed, not by what its name suggests. When a later stage can reverse an earlier
decision, any column written before that stage records the earlier decision
forever — so the definition of a group has to name the stage it comes from.

## A module that silences its own warnings cannot be warned (2026-10-03)

03b crashed on the box a second time:

```
TypeError: Invalid value '[4 4 4 ... 4 1 1]' for dtype 'int16'
```

`score_vectorized` built `gates_failed` as `int16`; `compute_trade_levels_vectorized`
rebuilt it as `int64`; `process_direction` then assigned those values home through
`df.loc[qual_mask, col]`. pandas 2 warns and silently widens. **pandas 3 raises** —
and the box runs pandas 3 while the dev venv runs 2.3.3.

So the environments diverge by a major version, and the entire class of
dtype-coercion defect is a passing test locally and a crash in production. That
alone would be the lesson. But the actual reason nobody saw it coming is worse:

```python
# engine/03b_score.py:17
warnings.filterwarnings("ignore")
```

pandas spent **a year** emitting `FutureWarning: Setting an item of incompatible
dtype is deprecated and will raise an error in a future version` — and the module
silenced it at import, along with every other deprecation in the scoring engine.
Three other pipeline stages had the same line. The advance notice was delivered and
discarded.

Setting `PYTHONWARNINGS=error::FutureWarning` in the test runner does nothing
against that, because the module-level `filterwarnings` call runs at import and
wins. A blanket suppression is not a local decision; it overrides the operator.

**Why a crash-based test is the wrong instrument here.** Three things all had to
line up to reproduce it: pandas 2 only *warns*, the warning only fires on a
**partial** boolean mask (a full-coverage `.loc` assignment is treated as a column
replacement and is allowed to change dtype silently), and the module suppressed
warnings anyway. An all-True fixture — which the first test used — cannot trigger
it on any pandas version.

The test that works asserts the **invariant** instead: a stage hands back the dtype
it was given. `int16` in, `int64` out is a failure on every pandas version,
immediately, with no partial mask and no warning machinery involved.

**Three lessons.**
1. Suppress warnings by category or message, never wholesale. Deprecations are the
   only advance notice that a library upgrade will break the pipeline.
2. Know your production interpreter's library versions. A suite on pandas 2
   validating code that runs on pandas 3 is theatre for this whole class.
3. When a defect needs several conditions to surface, test the invariant it
   violates rather than the symptom it produces. The symptom needs all the
   conditions; the invariant needs none.
