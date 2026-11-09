# CHARTER — what The Stock Logic is for, and the rules it keeps

Items (defects, decisions, questions) live in [OPEN.md](OPEN.md). This file holds
rules: things that stay true until deliberately changed. Working practice for
anyone editing the repo is in `CLAUDE.md`; a rule here governs what the product
does.

Most of this file is to be filled by Hemal from the charter kept outside the
repo. Section 3 was seeded on 2026-11-09 from decisions made that week, each
with the decision it came from.

---

## 1. MANDATE
_To be filled._ (What the product is, who it is for, what it will not do.)

## 2. THE INVESTING MANDATE (ATLAS)
_To be filled._ Known on 2026-11-09: ATLAS will place GTT and limit orders under
the new mandate; it is paused by operator directive (2026-10-28) and runs
watch-only until resumed.

## 3. RULES

### Data honesty
- **A zero on something measured is information; a number on something
  unmeasured is a lie.** Where a value is missing, show the real value with its
  age, show nothing, or show a substitute labelled as one. Never a plausible
  number in silence. (2026-11-09, substitution sweep — OPEN D-001..D-012.)
- **A vendor's freshness stamp is a claim, not evidence.** Upstox stamps the
  request time on a quote; age comes from `last_trade_time` or from differencing
  the values we hold. (2026-11-09 — OPEN D-015.)

### The order path
- **No price, no order.** An entry with no measured price is refused and alerted,
  the same rule as the emergency exit. (2026-11-09 — OPEN D-043.)
- **A read failure is never a number.** A count, limit or balance that cannot be
  read raises; it never comes back as 0. (2026-11-09 — OPEN D-045.)
- **Unknown order state means MAY BE FILLED.** Never record a position state
  that could not be read; alert and stop. (2026-11-09 — OPEN D-044.)
- **Every refusal, substitution and skipped check on the order path is
  recorded** with its reason in `order_path_events`, so "did this ever fire" is
  one query. (2026-11-09 — OPEN D-024.)
- **Never place, modify or cancel a broker order from any page or script written
  outside ATLAS's own order path.** (Standing, CLAUDE.md.)

### Alerts and checks
- **HTTP 200 is not delivery.** A message counts only with a `message_id`.
- **Silence must mean breakage.** Every scheduled check speaks on a quiet day,
  and a dead check is distinguishable from a clean result. (volume_alerts, quote
  coverage — OPEN D-039, D-041.)
- **Known exceptions are named, with a reason, and re-checked** — never a count
  tolerance tuned to today's broken state. (Quote coverage, 2026-11-09.)
- **A check is not done until it has been shown to fire** on a planted failure.

### Identity of a security
- **History is keyed on symbol; identity is the ISIN.** On a rename the current
  symbol is used going forward, history is left as written, and an alias table
  maps old to new. The observation ledger cannot be rewritten, so a rename is
  never made to look complete. (2026-11-08, option (c) — OPEN D-036.)

## 4. SPECS
_To be filled._ (Page and job specifications the product is held to.)
