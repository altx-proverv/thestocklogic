# tests/

Harnesses for properties that span more than one module.

## Why this exists

Modules here carry their own self-tests in an `if __name__ == "__main__":`
block — `atlas/risk/position_sizing.py`, `atlas/risk/breaker.py`,
`engine/02b_smc_signals.py` all do. That covers logic that lives inside one
file, and it is the right home for it.

It cannot cover an ORDERING property. "No death between the ledger write and
the order placement can leave a position Gate 3b cannot see" is a statement
about `atlas_entry`, `broker`, `config` and `reconcile` at once, and about the
sequence they run in. There is no single module whose `__main__` can assert it.

The other thing these catch is the failure this codebase keeps having: an error
path that returns a plausible-looking value instead of an error.

    broker.get_positions()    returned [] when Kite was unavailable, which is
                              also what a flat book returns
    place_order()             returned success=False for a margin refusal and
                              for a socket timeout alike
    _log_intent()             returned without raising when PostgREST answered
                              400, because requests.post does not raise on 4xx
    compute_sector_momentum() returned a bare DataFrame on its empty path and a
                              tuple everywhere else

None of those throw. Each one is only visible if something drives the failing
path and checks what comes back, which is what these harnesses do.

## Running them

Plain scripts, no framework, no new dependencies. Each exits non-zero on
failure, so they work from a shell, from cron, or from CI unchanged:

    python3 tests/run_all.py            # everything
    python3 tests/test_entry_ordering.py
    python3 tests/test_reconcile.py

They stub Supabase, Kite and Telegram, so they touch no network and need no
credentials. Running them cannot place an order or write a row.

## Adding one

A test belongs here when it asserts something no single module owns — an
ordering, a handoff between two modules, or a failure path whose wrong answer
looks like a right one. Anything provable inside one file belongs in that
file's `__main__` instead.
