# Open questions, and what would answer them

Live state as at **2026-10-02**: ATLAS paused, market-hours timer disabled, the
nightly chain still publishing and marking. The record grows with no capital at
risk.

---

## SETTLED — do not reopen without new evidence

### Exits
**Settled 2026-10-02.** Fourteen exit rules over the same 284 rebuilt price paths
— trailing stops at four aggressions, 3R and 4R targets, time stops at 5, 10 and
20 days. Every one has a 95% CI entirely below zero. The best (1R trailing stop,
−0.221R against the recorded −0.318R) does not survive Benjamini-Hochberg
(q = 0.32), splits 56/44 against the baseline, and is *worse* than the baseline
under the adverse reading of the bars.

The decisive measurement is not the rule table, it is the payoff curve: at every
target from +1R to +6R the hit rate falls short of its own breakeven by 8 to 14
points, and expectancy is flat at −0.28R to −0.34R across 1R–3R. There is no
target where the curve crosses, so no exit parameter is worth changing.

Holding longer is strictly worse: −0.376R / −0.403R / −0.462R at 5 / 10 / 20 days.

**Keep running** `engine/excursions.py` nightly so the question stays answerable
as the sample grows. **Stop proposing exit changes.**

---

## OPEN, WAITING ON DATA

### The 54-feature re-run
The feature analysis of 2026-09-30 found nothing separating winners from losers,
but it ran on 12 features of which 4 were untestable and the five sub-scores were
one number counted six ways. `signals_features` now carries 54 columns, so the
test can be done properly — **on new data, not on the same 293.**

Re-running a wider search over an unchanged sample raises the multiplicity burden
without adding information. The only possible outcome is a weaker result than last
time, and it would not be a new finding.

**Resolved signals required**, Mann-Whitney on AUC at 80% power, Bonferroni across
the feature count, win rate 18.8%:

| effect | 12 features | 54 features | 54 + all 1,431 pairs |
|---|---|---|---|
| AUC 0.60 | 760 | **950** | 1,360 |
| AUC 0.62 | 530 | 660 | 950 |
| AUC 0.65 | 340 | **420** | 610 |
| AUC 0.70 | 190 | 240 | 350 |

At 293 resolved today and roughly 71/month while publishing: **~420 (about two
months) to detect AUC 0.65 across the 54 singles**, ~950 (about nine months) for
AUC 0.60, and 1,360 if the pairs are tested too. Pick the effect size that would
be worth acting on *before* looking, and wait for that row.

Caveat that does not go away with sample size: the 54 columns only start being
written from 2026-10 onward, so the usable n is new signals only — the historical
293 have 12.

### What separates a setup from a non-setup
**The open question, and the record cannot answer it.** Every analysis so far
compares winners against losers — signals that qualified and then did or did not
work. None compares a setup against a non-setup, because the ~370k rejected
stock-days a night are never persisted. The loop can ask why near-misses failed;
it cannot ask what made something a candidate at all.

**Built 2026-10-02**, pending its migration. `engine/reject_sample.py` draws 10
rejected stock-days per qualifying one (k=10, where the precision curve flattens:
a 128:1 control ratio gives a CI only 0.4% narrower than 10:1), stratified by
(first failed gate) x (near miss) x direction. `engine/excursions.py` walks their
forward paths as `source='reject'`, so the test is not "what do the filters key
on" — which is knowable from having written them — but **did the filters select
better-than-random stock-days**.

The premise correction that shaped it: the "370k rejected stock-days" is the whole
788-day history 03b rescores on every run. One night's new rejects are 464 rows.
Sampling is right for the statistics, not the storage.

`gates_failed` had to be added to 03b first. Every gate is masked on
`~disqualified`, so `disqualify_reason` is the FIRST gate failed and is confounded
with gate position — the last gate's rejects are all near misses by construction,
the first gate's fail a median of three. Without a non-short-circuited count, a
genuine near miss is indistinguishable from a row that fails everything, and the
near misses are the only informative negatives.

| months sampling | cases | controls | detectable AUC |
|---|---|---|---|
| 1 | 76 | 759 | 0.597 |
| 3 | 228 | 2,277 | **0.556** |
| 6 | 455 | 4,554 | 0.540 |

A reject's R is **counterfactual** — it has no stop, so one is constructed from
its own `atr_pct` clipped to the 0.5–5.0% band. `source` keeps the three
populations from being pooled.

---

## THE SCHEDULE — deploy/crontab.box is the authority as of 2026-10-05

The box's crontab was never in version control. Pasted and committed, with three
defects it exposed:

- **`atlas/signal/decay.py` ran every 10 minutes and had been deleted** on
  2026-10-02. 48 failing invocations a weekday into `reports/atlas.log`.
- **`daily_report` shared minute `35 13` with `update_outcomes`, which it reads.**
  Moved to `45 13`.
- **Four jobs were merged but never installed** — `market_flash`,
  `reject_sample`, `excursions`, `score_live_outcomes`. `signal_excursions` and
  `reject_sample` are empty because nothing has ever run them, not because they
  were waiting for tonight.

`tests/test_crontab.py` checks both directions: every path the schedule names must
exist, and every job the repo ships must be scheduled. The four `deploy/*.cron`
fragments are deleted — four places to look is how the minute collision survived.

systemd still owns three things separately: `atlas-market-hours.timer` (DISABLED
while paused), `atlas-mis-squareoff.timer`, `atlas-engine-report.timer`.

## MIGRATION STATE — verified against the live schema 2026-10-03

**Applied** (renamed out of `PENDING_`): `atlas_trades_product`,
`live_signals_kind`, `signal_excursions` (source CHECK admits `reject`),
`reject_sample` (six-for-six), `repair_measurement_targets`,
`meridian_iv_recorder`.

**Still pending**, probed rather than assumed:

| file | what is missing |
|---|---|
| `atlas_live_zones` | table exists; `inside`, `held`, `sl`, `setup_name`, `batch_date`, `cycle_n` do not. The live view has never populated and fails silently. |
| `repair_partial_applications` | `v_marks_zone_valid` and `v_wrong_side_excluded` are absent; `v_signal_window` still holds its pre-30-September definition. **The wrong-side exclusion is still not in force.** |
| `sector_pubkind_derived` | `signals.sector`, `sector_bias`, `sector_as_of` all absent — so `sector=OTHER` is still what the row says. |
| `signals_features_provenance` | `signals_features` does not exist. The 54-column learning-loop table is not there, so the re-run in the section above has nowhere to read from. |
| `market_flash` | table absent, and it is scheduled nowhere regardless. |

## FIXED AND APPLIED

`PENDING_repair_measurement_targets.sql` — the measurement basis moved from
`entry_ref` to the actual fill, and 31 signals carry a target that is not 2R.
**Applied 2026-10-03. The outcome, measured:** 282 resolved (from 293), **26.2%**
hit rate (CI 21.5–31.7), expectancy **−0.2128R** (from −0.3459R), and **16
AMBIGUOUS** rows correctly refusing to take a side. My prediction of ~23.2% was
low. Three numbers were in play: **22.2%** published, **18.8%** the old record
restated honestly (the size of the overstatement), **26.2%** what it actually
became. The headline rate goes slightly *up*
while the record gets stricter, because what changes is what a win means — 65 wins
at a median 2.08R of a risk nobody took, 10 under +1R and 7 losing money, become
63 wins at exactly 2R of the risk taken. ~13 rows move to AMBIGUOUS or OPEN and
leave the resolved set. Step 3 releases all 293 price-based verdicts; without it
the record sits on two measurement bases at once.

`PENDING_reject_sample.sql` — the negative class. Authenticated-only, unlike
signals: this is the rejection logic row by row, not the inputs behind a published
claim.
