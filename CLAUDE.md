# Standing instructions — The Stock Logic

Every rule here was paid for by a defect in this repo. The reason is attached
to each one because a rule whose cost is invisible gets argued with.

## Editing

- **Read the span before replacing it.** Four defects in one session came from
  replacements that spanned two anchors and silently deleted what sat between
  them — an auth gate, a CSS block, a config block, and a function that landed
  nested inside another.
- **Build large changes in stages, not one attempt.** Structure first, then
  each block, checking after each.
- **Assert the match count before replacing.** `assert s.count(old)==1` turns a
  wrong anchor into a stopped script instead of a silent partial edit.

## Verifying

- **Verify by clicking, not measuring.** Measuring a button's size is not
  testing a button, and calling a function from the console is not operating
  the interface. A page height can be true while the sentence describing it is
  false — that exact pair shipped.
- **Exercise the states that only exist after an interaction.** Open menus,
  pushed views, selected rows. A scan of a closed page cannot reach them, and
  two violations hid there for a full day.
- **Never invent an expected value in a check. Derive it.** A guessed
  expectation makes the check worthless — and a guess that happens to be wrong
  reads as a product bug.
- **A reported freshness or health value is a claim, not evidence.**
  `live_sentiment` logged `[quote 0.0s old]` for all 360 cycles of a dead feed
  because Upstox stamps a current time onto a frozen price. Difference the
  value itself, never trust a vendor's or a counter's self-report.
- **Verify where the thing actually runs**: the box for engine changes, the
  Vercel build for anything user-facing. A local pass proves the local file.
- **A test that matches nothing passes.** When a pattern stops matching because
  the code changed shape, the test goes quiet instead of failing. After
  changing a shape the pattern depends on, prove the test still bites by
  breaking the thing on purpose and watching it fail.
- **A failing test that contradicts production may be right.** Find out which
  side is wrong before weakening the assertion. Repoint a test when the ruling
  changed; never loosen one to make a change fit.
- **Judge the code, not the prose about it.** Strip comments before matching,
  or a test flags the sentence explaining the rule it is enforcing.

## Data honesty

- **A zero on something measured is information. A zero on something
  unmeasured is a lie.** Show the first, suppress the second.
- **Thresholds, windows and counts come from the data, not from reasoning
  about the data.** Check the number against what exists: a 252-session window
  was confirmed by reproducing a stored value on 40 of 40 symbols, not chosen
  because a year has about that many sessions.
- **Absence of data is indistinguishable from absence of news unless something
  checks that the lookup ran.** Gate any "nothing on file" claim on positive
  evidence that the producing job ran for that session.
- **A guard on the input does not guard the sentence.** A phrase claiming
  rarity needs a rarity test, not a minimum-size test. A predicate and the
  words describing it drift apart silently.
- **A comment can assert the opposite of its code. Trust the code.**
- **State the basis on screen** when two figures sit together on different
  bases, and never subtract across them.

## New scheduled work

- **A fetch is not done when the code works.** It is done when the schedule is
  proven to fire, a heartbeat records the run, and staleness is visible on the
  page. Four publishers sat committed and uninstalled for months; every table
  the page read was written only by hand, and nothing said so.
- **Prove a cron line fires, don't prove the command runs.** Install it on a
  temporary schedule minutes out, confirm from the job's own log that cron
  invoked it with the right environment, then remove the temporary line.

## The box and the crontab

- **Never `crontab <repo-file>`.** The live list carries 19 credential
  assignments including the Zerodha password and TOTP secret; the repo file
  carries none. Dump the live crontab, append or edit that dump, load the
  merge, and verify the credential count and the job count on both sides of
  the write.
- **Print the crontab with `grep -E "^[0-9*]"`** so credentials stay out of
  output. Never echo a credential value, in any command, ever.
- **Back up before any crontab write**, timestamped, and diff the result.
- **The box hard-resets from origin/main every five minutes.** Commit and
  push; never edit on the box. An edit there survives at most five minutes.
- **Never run as root.** `ubuntu`, always.
- **Do not touch the halt state** unless explicitly told to. Read it with
  `atlas.execution.breaker_public.state()`, which cannot write.

## Gates

- **`bash check_js.sh` from `~/thestocklogic` before any commit touching an
  HTML file.** Mandatory, no exceptions.
- **The anon key ships in every page.** Never grant anon SELECT on a gated
  table. A page that needs gated data reads it with the session token.
- **Never place, modify or cancel a broker order** from any page or script
  written here.

## Brand

- **`logo.svg` exactly as every other page uses it** — never redrawn, never
  substituted with a text wordmark, never recoloured.
- **The shared tokens**: `--navy-deep:#0F1F38 --navy:#1B3A6B
  --navy-panel:#16294A --sky:#3BA9EE --yellow:#F5D33D --orange:#F58445
  --white:#FFFFFF --up:#00d68f --down:#ff4d6d`. Bebas Neue display,
  Inconsolata body and numbers, Crimson Pro italic.
- **Every `var()` carries a hex fallback.** An unresolved `var()` renders
  transparent with no warning, and one page defining a token another does not
  is the normal case here — there is no shared stylesheet.
- **Pick a breakpoint from the values already in the file**, not a new one.

## Reporting

- **Say which claims were verified and which were inferred.** They are
  different things and the difference matters more than the conclusion.
- **No false positives.** If something cannot be measured, say so rather than
  estimating it and presenting the estimate as a measurement.
- **Report the number that was asked for**, including when it is unflattering,
  and name what was left out and why.
- **Correct your own errors plainly and move on.** No ceremony.
