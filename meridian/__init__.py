"""MERIDIAN — the F&O layer.

Separate from ATLAS by construction: no module here imports from atlas.*, reads
no ATLAS table and writes none. The two share this Postgres database, the
Supabase service key, and the Upstox access token FILE -- which meridian reads
and never refreshes, because rewriting it badly would take out the equity price
feed. That is the whole of the coupling.

Only the IV recorder exists. Everything else in docs/MERIDIAN.md is unbuilt.
"""
