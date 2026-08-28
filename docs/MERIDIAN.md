# MERIDIAN — Architecture

**v2 · 28 Aug 2026 · runs parallel to ATLAS**

> **STATUS — NOTHING HERE IS BUILT**
>
> No part of MERIDIAN exists in this repository. This document is design intent,
> recorded so that future work has the context behind it. Every phase in §07 is
> unstarted, Phase 0 included.
>
> A backtesting engine is planned ahead of the build. It exists to answer
> questions this design depends on rather than to validate the design itself —
> principally the measured time-to-resolution that §03 needs before horizon
> classification can stop being a guess, and the untested fill assumption noted
> in §08.

> **IN ONE LINE**
>
> A continuous engine that scans all session, classifies each setup by horizon,
> enters when price arrives, and places real stops and targets — managing every
> position to its own exit rules rather than holding everything forever.

---

## 01 · Parallel, not replacement

**MERIDIAN runs alongside ATLAS on the same signals.** Separate capital
allocation, separate trade table, separate dashboard section.

|                   | ATLAS                        | MERIDIAN                                                  |
| ----------------- | ---------------------------- | --------------------------------------------------------- |
| **Evaluates**     | Once, 09:37                  | Continuously, 09:20–15:20                                  |
| **Entries**       | 3/day max                    | Capped by open positions and capital, not a daily count    |
| **Position size** | ₹3,000 risk / ₹1L notional   | ₹3,000 risk / **₹50,000 notional**                         |
| **Stops**         | Recommended, placed manually | Placed as GTT at fill                                      |
| **Targets**       | None                         | 50% booked at target, rest runs                            |
| **Exits**         | Manual                       | Managed to horizon rules                                   |
| **Horizon**       | Everything held as CNC       | Scalp / intraday / BTST / hold, each managed differently   |

**Why parallel matters.** ATLAS is your control group — the only real fills you
have, on a known ruleset. Replacing it means losing the comparison at exactly
the moment you need it. Run both for a quarter, compare on the same signals,
then decide.

---

## 02 · Position and exit rules

### Sizing

- **₹50,000 notional cap per position** — half of ATLAS's, because MERIDIAN
  takes more of them
- ₹3,000 risk budget unchanged; quantity still derives from entry-to-stop
  distance
- Quantity a multiple of 5
- The smaller cap will bind more often — on a tight stop, notional limits size
  before risk does

### Exit ladder — long positions

1. **Entry fills** → GTT stop placed immediately at the structural level. A
   position is never unprotected.
2. **Target 1 (2R) hit** → sell 50% of quantity, book the profit.
3. **Same moment** → cancel the original stop, place a new GTT stop on the
   remaining 50% **at the entry price**. The runner is now risk-free.
4. **Runner continues** → trail the stop up to each new higher swing low. Never
   loosen it.
5. **Runner stops out** → position closed at breakeven or better. No target on
   the second half; it runs until the trail takes it.

> **WHAT THIS ACHIEVES**
>
> Half the position banks a defined 2R. The other half runs with zero downside
> from that point. A winner that keeps going is captured; a winner that reverses
> still books half. The worst case after target 1 is breakeven on the remainder.

### Intraday shorts

The ladder above does **not** apply. Shorts are MIS, defensive, and rare —
permitted only when `extreme_bearish` holds. Fixed target, hard exit before
15:20, no scaling out, no runner. They hedge; they do not compound.

> **EXPLICITLY NOT BUILT**
>
> **No averaging down.** The stop is an exit, not an averaging trigger. Adding
> to a losing position removes protection precisely when the thesis is most
> wrong, and grows exposure without limit. Considered and rejected.

---

## 03 · Horizon classification

Each signal carries a horizon at entry, and the horizon determines product, exit
rules and time limit. **Phase 1 assigns it from a rules table; Phase 3 replaces
that with something derived from outcome data.**

| Horizon      | Product | Exit                             | Time limit         |
| ------------ | ------- | -------------------------------- | ------------------ |
| **Scalp**    | MIS     | Target or stop, whichever first  | Close by 15:15     |
| **Intraday** | MIS     | Ladder, compressed               | Close by 15:15     |
| **BTST**     | CNC     | Ladder                           | Exit next session  |
| **Hold**     | CNC     | Full ladder, trail indefinitely  | None               |

**Initial rules, to be replaced by measurement:** a setup whose median
time-to-resolution is one day is intraday whatever it's labelled. ATR percentile
and zone width give a natural horizon — a 1.5% stop on a low-volatility name is
a different trade from a 6% stop on a volatile one. Regime narrows it further:
hold horizons only make sense in bull or sideways.

> **RECORDED AT ENTRY, ALWAYS**
>
> Even in Phase 1, where nothing acts on it beyond product selection, the
> intended horizon and exit rule are written to the trade record. Otherwise you
> get today's situation — positions with no exit plan, all held forever by
> default.

---

## 04 · The scan loop

Polling every 60 seconds, 09:20 to 15:20. **Not a websocket** — zones update
once nightly, so millisecond resolution buys nothing, and the one persistent
service that exists ran a full session receiving zero ticks with no error
logged.

### Each cycle

1. **Read the zone map** — computed nightly, held in memory, refreshed once per
   session
2. **Fetch live prices** — Upstox, which authenticates itself
3. **Match** — which symbols are within 0.30% of a zone right now
4. **Gate** — reuse ATLAS's stack unchanged: regime, holdings, entry range,
   sizing, funds, kill switch
5. **Enter** — market order, then place the stop GTT immediately
6. **Manage** — for every open position, check whether target 1 hit, whether the
   trail should move, whether a time limit has passed
7. **Reconcile** — compare the engine's view against the broker's, every cycle

### Idempotency

- Never re-enter a symbol already open — Gate 3b, already built
- Never place a second stop on a position that has one
- Never act twice on the same target hit — record the transition, don't infer it
- A restart mid-session must resume from the broker's state, not from memory

---

## 05 · Position state machine

Every position has an explicit state. Transitions are logged with their trigger.
**Most of this codebase's failures were undefined states** — a GTT that rested
with no database row, a position closed by a T+1 quantity of zero, six positions
closed by one exception.

| State           | Means                                  | Exits to                |
| --------------- | -------------------------------------- | ----------------------- |
| **PENDING**     | Order placed, not filled               | OPEN, or CANCELLED      |
| **OPEN**        | Filled, stop placed, full quantity     | HALF_BOOKED, or CLOSED  |
| **HALF_BOOKED** | Target 1 hit, 50% sold, stop at entry  | CLOSED                  |
| **CLOSED**      | Fully exited                           | terminal                |
| **UNKNOWN**     | Broker and engine disagree             | **halts the loop**      |

**UNKNOWN is not a soft state.** If reconciliation finds a position the engine
doesn't know about, or a quantity that doesn't match, the loop stops and alerts.
It does not guess, and it does not continue trading on a book it can't verify.

---

## 06 · Failure handling

Derived from this week: three bugs in two days shared one shape — an error path
returning something that looked like valid data.

| Failure                                | Response                                                                                                                          |
| -------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| **Price feed unreachable**             | Skip the cycle, log, retry next minute. Three consecutive → Telegram                                                               |
| **Broker unreadable**                  | Skip the cycle. **Never interpret as flat.** Alert immediately                                                                     |
| **Order rejected**                     | Log with the broker's reason, don't retry blindly, alert                                                                           |
| **Stop placement fails after a fill**  | **Highest severity.** Position is open and unprotected. Alert loudly, retry, and if it still fails, exit the position at market     |
| **Reconciliation mismatch**            | Halt the loop, alert                                                                                                               |
| **Token expired mid-session**          | Attempt one re-login, then halt                                                                                                    |

Every failure alerts. **Silence must never read as success** — at 400 cycles a
day, a quiet failure is a quiet failure four hundred times.

---

## 07 · Build order

| # | Phase                   | Delivers                                                                   | Blocked by                          |
| - | ----------------------- | -------------------------------------------------------------------------- | ----------------------------------- |
| 0 | Prerequisites           | Elastic IP · secrets hardening · test/live seam                            | —                                   |
| 1 | Scan loop, entries only | Enters whenever price arrives, records intended horizon, no exits yet      | Phase 0                             |
| 2 | Stops and the ladder    | GTT stop at fill · 50% at target · breakeven stop · trailing               | Phase 1                             |
| 3 | Horizon from data       | Rules table replaced by measured time-to-resolution                        | Phase 1–2 data + outcome analysis   |
| 4 | Fundamentals            | Quality disqualifier on the hold book                                      | Phase 3                             |
| 5 | News                    | Earnings blackout first, sentiment last                                    | Phase 4                             |

### Phase 0 is not optional

- **Elastic IP** — Zerodha permits one whitelist change per week. An unplanned
  restart without a fixed IP costs a week of order placement, and a continuous
  engine restarts more than a nightly cron does
- **Secrets hardening** — the box now holds a password and TOTP secret that can
  move money, in crontab plaintext
- **Test/live seam** — there is none. A test run currently writes into the
  production Supabase rows. For a nightly batch that's a nuisance; for a
  continuous engine under active development it's a hazard

---

## 08 · What this changes, and what it doesn't

> **IT CHANGES**
>
> Coverage — no longer blind for six hours. Protection — every position has a
> stop from the moment it fills. Capture — winners run instead of being held
> indefinitely with no plan. Discipline — exits happen by rule rather than by
> attention.

> **IT DOES NOT CHANGE**
>
> **Signal quality.** MERIDIAN executes the same selection ATLAS does, faster
> and more often. If the selection is mediocre, MERIDIAN produces mediocre
> trades sooner and in greater number.
>
> Measured directional accuracy is 45.3% and the resolved basis shows structure
> but rests on a fill assumption that hasn't been tested. **The outcome analysis
> remains the highest-value work on the list**, and it is not part of this
> build.

---

*MERIDIAN Architecture v2 · 28 Aug 2026 · parallel to ATLAS · Phase 0 blocks
everything*
