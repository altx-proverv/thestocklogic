# migrations/

Every schema change applied to the Supabase project, in the order it was
applied. One file per migration, named exactly as Supabase records it:

    <version>_<name>.sql        e.g. 20260820192340_live_prices_anon_read_policy.sql

`version` is the UTC timestamp Supabase assigned. Sorting the directory by
filename gives the true apply order.

## Why these are here

Until 2026-08-20 they existed **only** in Supabase's `schema_migrations`
table. Nothing in the repo recorded that `atlas_state` had lost its capital
columns, that `signal_outcomes` had been re-keyed, or that `live_prices` was
sitting behind RLS with no policy. Reading the code told you nothing about the
shape of the database it talks to, and this codebase has lost track of things
that way before -- `gtt_trigger_id`, `delivery_pct` and `live_prices.volume`
were each a writer naming something the schema did not have.

The first eleven files are backfilled from Supabase's own history and were
verified byte-identical to the applied statements by md5 before being
committed. They are a record, not something to replay: **do not re-run them
against the live database.**

## Adding one

Apply the migration to Supabase, then save the exact SQL here under the
version Supabase assigned it. Same commit as the code that depends on it --
a page that reads a new column and a migration that adds it must land
together, or the deploy order decides whether the page works.

Then run the schema check, which will tell you if the two have drifted:

    SUPABASE_SERVICE_KEY=... python3 tools/check_schema.py

## RLS is on by default

The database has an event trigger, `ensure_rls` -> `rls_auto_enable()`, that
enables row level security on every new table created in `public` and creates
no policy. This is deliberate and worth keeping: a new table is private until
someone decides otherwise.

The cost is that the failure is silent. A table with RLS and no policy answers
every anon read with `200` and an empty array -- there is no status to check
and no error to log, so a page just renders nothing and looks like a quiet
day. `live_prices` sat that way for eleven weeks while its feed wrote 500
symbols twice a day.

So: **any new table the frontend reads needs a SELECT policy in the same
migration that creates it.** `tools/check_schema.py` check 5 flags tables in
that state, and check 4 compares what the anon key can actually see against
the service-role count.

Note that `atlas.html` sends the operator's `access_token` once signed in, so
its reads arrive as `authenticated`, not `anon`. A policy scoped to `anon`
alone breaks the page for exactly the person using it. Grant both roles.
