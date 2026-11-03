#!/usr/bin/env python3
"""Prove index_quotes.price_changed_at tracks the VALUE, not the write -- in Postgres.

The page decides "this price has stopped" from price_changed_at, so the trigger
that sets it is the whole check. A unit test of the page cannot show the
trigger fires the way PostgREST writes: INSERT ... ON CONFLICT DO UPDATE, where
the BEFORE INSERT and BEFORE UPDATE branches both run. Only a database can.

Builds a throwaway cluster, creates index_quotes as production has it, applies
the migration file exactly as written, and drives it with PostgREST's upsert.
Then it replaces the trigger function with the wrong one a person would write
(stamp on every write) and requires the same probes to FAIL -- a proof that
cannot fail proves nothing.

    PG_BIN=/Applications/Postgres.app/Contents/Versions/17/bin \\
        python3 tools/prove_price_changed_at.py

Touches no network and no real database. Exits non-zero if any probe is wrong.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = Path(os.environ.get("PRICE_MIGRATION", next(
    (ROOT / "migrations").glob("*index_quotes_price_changed_at.sql"))))
PG_BIN = Path(os.environ.get("PG_BIN", "/Applications/Postgres.app/Contents/Versions/17/bin"))
PORT = os.environ.get("PG_PORT", "55438")

# index_quotes as production has it (information_schema, 2026-11-04).
TABLE = """
create table public.index_quotes (
  symbol text primary key, display_name text not null, instrument text,
  exchange text, ltp numeric, prev_close numeric, change_pct numeric,
  is_future boolean not null default false, expiry date,
  updated_at timestamptz not null default now());
"""


def psql(sql):
    r = subprocess.run([str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", PORT, "-U",
                        "postgres", "-d", "q", "-v", "ON_ERROR_STOP=1", "-X", "-q",
                        "-t", "-A", "-c", sql], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def upsert(ltp):
    """What PostgREST sends for Prefer: resolution=merge-duplicates. The payload
    never carries price_changed_at."""
    psql("insert into index_quotes (symbol, display_name, ltp, updated_at) "
         f"values ('NIFTY50', 'NIFTY 50', {ltp}, now()) "
         "on conflict (symbol) do update set display_name = excluded.display_name, "
         "ltp = excluded.ltp, updated_at = excluded.updated_at")


def stamp():
    return psql("select coalesce(price_changed_at::text, 'NULL') from index_quotes "
                "where symbol = 'NIFTY50'")


def probes():
    """-> list of (label, ok). Uses pg_sleep so each write has its own now()."""
    out = []
    psql("truncate index_quotes")
    upsert(100)
    s1 = stamp()
    out.append(("a first insert stamps price_changed_at", s1 != "NULL"))
    psql("select pg_sleep(0.05)")
    upsert(100)
    s2 = stamp()
    out.append(("the same price written again keeps the stamp", s2 == s1))
    psql("select pg_sleep(0.05)")
    upsert(100.05)
    s3 = stamp()
    out.append(("a different price moves the stamp", s3 != s2 and s3 > s2))
    psql("select pg_sleep(0.05)")
    psql("update index_quotes set updated_at = now(), change_pct = 1 where symbol = 'NIFTY50'")
    out.append(("an update that leaves ltp alone keeps the stamp", stamp() == s3))
    psql("select pg_sleep(0.05)")
    psql("update index_quotes set price_changed_at = '2001-01-01' where symbol = 'NIFTY50'")
    out.append(("a client cannot set the stamp itself", stamp() == s3))
    psql("insert into index_quotes (symbol, display_name, ltp) values ('NULLP', 'x', null) "
         "on conflict (symbol) do update set ltp = excluded.ltp")
    psql("select pg_sleep(0.05)")
    before = psql("select price_changed_at from index_quotes where symbol='NULLP'")
    psql("insert into index_quotes (symbol, display_name, ltp) values ('NULLP', 'x', null) "
         "on conflict (symbol) do update set ltp = excluded.ltp")
    after = psql("select price_changed_at from index_quotes where symbol='NULLP'")
    out.append(("NULL to NULL is not a change", before == after))
    return out


# The wrong implementation a person would write: stamp whenever the row is
# written. Every probe that distinguishes value from write must fail on it.
WRONG = """
create or replace function public.index_quotes_stamp_price_change()
returns trigger language plpgsql as $$
begin new.price_changed_at := now(); return new; end; $$;
"""


def main():
    if not (PG_BIN / "initdb").exists():
        print(f"no Postgres binaries at {PG_BIN} — set PG_BIN. NOT PROVEN.")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="pca_pg_"))
    data = tmp / "data"
    subprocess.run([str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres",
                    "--auth=trust", "-E", "UTF8"], check=True, capture_output=True)
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-o",
                    f"-p {PORT} -k {tmp}", "-l", str(tmp / "log"), "-w", "start"],
                   check=True, capture_output=True)
    failures = []
    try:
        subprocess.run([str(PG_BIN / "createdb"), "-h", "127.0.0.1", "-p", PORT,
                        "-U", "postgres", "q"], check=True)
        print(f"Postgres {psql('show server_version')}, throwaway cluster in {tmp}")
        psql(TABLE)
        r = subprocess.run([str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", PORT, "-U",
                            "postgres", "-d", "q", "-v", "ON_ERROR_STOP=1", "-X", "-q",
                            "-f", str(MIGRATION)], capture_output=True, text=True)
        print(f"migration {MIGRATION.name} applied: {'yes' if r.returncode == 0 else 'NO'}")
        if r.returncode:
            print(r.stderr)
            return 1

        print("\nTHE MIGRATION AS WRITTEN")
        for label, ok in probes():
            print(f"  {label:<60} {'ok' if ok else 'FAIL'}")
            if not ok:
                failures.append(label)

        print("\nBITE-PROOF: the same probes against stamp-on-every-write must fail")
        psql(WRONG)
        bitten = [label for label, ok in probes() if not ok]
        for label in bitten:
            print(f"  fails on the wrong trigger: {label}")
        must_bite = {"the same price written again keeps the stamp",
                     "an update that leaves ltp alone keeps the stamp"}
        if not must_bite <= set(bitten):
            failures.append("the probes do not catch a stamp-on-every-write trigger")
            print("  ** the probes did NOT catch the wrong trigger **")
    finally:
        subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast", "stop"],
                       capture_output=True)
        subprocess.run(["rm", "-rf", str(tmp)])

    print("\n" + "=" * 80)
    if failures:
        print(f"NOT PROVEN — {len(failures)} failure(s): {failures}")
        return 1
    print("PROVEN — price_changed_at moves with the value, not with the write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
