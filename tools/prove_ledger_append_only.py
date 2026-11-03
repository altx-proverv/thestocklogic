#!/usr/bin/env python3
"""Prove the observation ledger refuses UPDATE, DELETE and TRUNCATE -- in Postgres.

The suite can check that migrations/20261103181336_observation_ledger.sql CONTAINS the
triggers. Only a database can show they FIRE. This builds a throwaway cluster,
creates the three Supabase roles, applies the migration exactly as written, and
tries to rewrite history every way it knows, as the owner and as service_role.

Needs Postgres binaries (Postgres.app on the Mac: PG_BIN=/Applications/Postgres.app/
Contents/Versions/17/bin -- production is 17.6). Touches no network and no
real database; the cluster lives in a temp dir and is deleted afterwards.

    PG_BIN=/Applications/Postgres.app/Contents/Versions/17/bin \
        python3 tools/prove_ledger_append_only.py

Exits non-zero if any probe has the wrong result.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = Path(os.environ.get("LEDGER_MIGRATION",
                               ROOT / "migrations" / "20261103181336_observation_ledger.sql"))
SURV_MIGRATION = Path(os.environ.get("SURVEILLANCE_MIGRATION",
                                    ROOT / "migrations" / "20261026185608_surveillance_snapshots.sql"))
PG_BIN = Path(os.environ.get("PG_BIN", "/Applications/Postgres.app/Contents/Versions/17/bin"))
PORT = os.environ.get("PG_PORT", "55437")

failures = []


def psql(sql, user="postgres", ok_expected=True, label=""):
    r = subprocess.run([str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", PORT,
                        "-U", user, "-d", "ledger", "-v", "ON_ERROR_STOP=1",
                        "-X", "-q", "-t", "-A", "-c", sql],
                       capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if label:
        good = (r.returncode == 0) == ok_expected
        if not good:
            failures.append(label)
        verdict = "ok" if good else "FAIL"
        what = "accepted" if r.returncode == 0 else "refused"
        print(f"  {label:<64} {what:<9} {verdict}")
        if r.returncode != 0:
            print(f"      {out.splitlines()[-1][:110] if out else ''}")
    return r.returncode, out


def surveillance_probes():
    """migrations/20261026185608_surveillance_snapshots.sql: the three-run dedupe and
    the same append-only layers, on the real unique key."""
    rc = subprocess.run([str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", PORT, "-U",
                         "postgres", "-d", "ledger", "-v", "ON_ERROR_STOP=1", "-X",
                         "-q", "-f", str(SURV_MIGRATION)],
                        capture_output=True, text=True).returncode
    print(f"\nsurveillance migration applied: {'yes' if rc == 0 else 'NO'}")
    if rc != 0:
        failures.append("surveillance migration applies cleanly")
        return
    # Exactly what PostgREST sends for on_conflict=... + resolution=ignore-duplicates.
    batch = ("insert into surveillance_snapshots (session_date, symbol, framework, "
             "stage, stage_label, nse_stamp, captured_at, source_url) values "
             "('2026-10-27','AAA','ASM','LTASM - I (13)','Stage I','27-Oct-2026',"
             "'{t}','https://www.nseindia.com/api/reportASM'),"
             "('2026-10-27','AAA','ASM','STASM - I (11)','Stage I','27-Oct-2026',"
             "'{t}','https://www.nseindia.com/api/reportASM'),"
             "('2026-10-27','CCC','GSM','IBC I & GSM 0 (58)','0','27-Oct-2026 08:06:02',"
             "'{t}','https://www.nseindia.com/api/reportGSM') "
             "on conflict (symbol, framework, stage, session_date) do nothing")
    print("\nThree captures of one session are one record")
    for t, label in (("2026-10-27T05:00:00Z", "morning"), ("2026-10-27T09:05:00Z", "afternoon"),
                     ("2026-10-27T11:00:00Z", "close")):
        psql(batch.format(t=t), user="service_role", label=f"{label} capture accepted")
    _, n = psql("select count(*) from surveillance_snapshots")
    good = n.strip() == "3"
    print(f"  {'three runs x 3 listings leave 3 rows, not 9':<64} {n.strip():<9} "
          f"{'ok' if good else 'FAIL'}")
    if not good:
        failures.append("three-run dedupe in Postgres")
    # In UTC explicitly: the cluster inherits the host's timezone, and the
    # 05:00Z morning capture reads as 10:30 on an IST machine.
    _, t = psql("select distinct (captured_at at time zone 'UTC')::time "
                "from surveillance_snapshots where symbol <> 'CCC' or stage <> 'GSM I (59)'")
    good = t.strip() == "05:00:00"
    print(f"  {'each row keeps the first capture time':<64} {t.strip():<9} "
          f"{'ok' if good else 'FAIL'}")
    if not good:
        failures.append("first capture time kept")
    psql("insert into surveillance_snapshots (session_date, symbol, framework, stage, "
         "nse_stamp, captured_at, source_url) values ('2026-10-27','CCC','GSM',"
         "'GSM I (59)','27-Oct-2026','2026-10-27T09:05:00Z','x')", user="service_role",
         label="a stage change mid-session appends a second row")
    psql("insert into surveillance_snapshots (session_date, symbol, framework, stage, "
         "nse_stamp, captured_at, source_url) values ('2026-10-27','DDD','NSE',"
         "'x','27-Oct-2026','2026-10-27T09:05:00Z','x')", user="service_role",
         ok_expected=False, label="a framework other than GSM|ASM is refused")
    print("\nThe capture log")
    psql("insert into surveillance_captures (session_date, captured_at, run_label, ok, "
         "asm_rows, gsm_rows, error) values ('2026-10-27','2026-10-27T05:00:00Z',"
         "'morning',false,null,null,'HTTP 401')", user="service_role",
         label="a failed attempt is recorded")
    psql("insert into surveillance_captures (session_date, captured_at, run_label, ok, "
         "asm_rows, gsm_rows, error) values ('2026-10-27','2026-10-27T05:00:00Z',"
         "'morning',true,null,null,null)", user="service_role", ok_expected=False,
         label="an 'ok' capture with no row counts is refused")
    psql("insert into surveillance_captures (session_date, captured_at, run_label, ok, "
         "asm_rows, gsm_rows, error) values ('2026-10-27','2026-10-27T05:00:00Z',"
         "'morning',true,189,77,'but it failed')", user="service_role", ok_expected=False,
         label="an 'ok' capture carrying an error is refused")
    print("\nNo role can rewrite what NSE said")
    for tbl in ("surveillance_snapshots", "surveillance_captures"):
        for user in ("service_role", "postgres"):
            psql(f"update {tbl} set session_date='2026-10-20'", user=user,
                 ok_expected=False, label=f"UPDATE {tbl} as {user}")
            psql(f"delete from {tbl}", user=user, ok_expected=False,
                 label=f"DELETE {tbl} as {user}")
            psql(f"truncate {tbl}", user=user, ok_expected=False,
                 label=f"TRUNCATE {tbl} as {user}")
        for priv in ("UPDATE", "DELETE", "TRUNCATE"):
            _, has = psql(f"select has_table_privilege('service_role', 'public.{tbl}', '{priv}')")
            good = has.strip() == "f"
            print(f"  {'service_role holds no ' + priv + ' on ' + tbl:<64} "
                  f"{has.strip():<9} {'ok' if good else 'FAIL'}")
            if not good:
                failures.append(f"no {priv} on {tbl}")
    psql("set role anon; select count(*) from surveillance_snapshots", ok_expected=False,
         label="anon cannot read the snapshots")


def main():
    if not (PG_BIN / "initdb").exists():
        print(f"no Postgres binaries at {PG_BIN} — set PG_BIN. NOT PROVEN.")
        return 2
    tmp = Path(tempfile.mkdtemp(prefix="ledger_pg_"))
    data = tmp / "data"
    subprocess.run([str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres",
                    "--auth=trust", "-E", "UTF8"], check=True,
                   capture_output=True)
    subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-o",
                    f"-p {PORT} -k {tmp}", "-l", str(tmp / "log"), "-w",
                    "start"], check=True, capture_output=True)
    try:
        subprocess.run([str(PG_BIN / "createdb"), "-h", "127.0.0.1", "-p", PORT,
                        "-U", "postgres", "ledger"], check=True)
        ver = psql("select current_setting('server_version')")[1]
        print(f"Postgres {ver}, throwaway cluster in {tmp}")

        # The three roles Supabase has, with Supabase's default grant: ALL on
        # new public tables. The migration must take that back.
        psql("create role anon nologin; create role authenticated nologin; "
             "create role service_role login bypassrls; "
             "grant usage on schema public to anon, authenticated, service_role; "
             "alter default privileges in schema public grant all on tables "
             "to anon, authenticated, service_role;")
        rc, out = subprocess.run(
            [str(PG_BIN / "psql"), "-h", "127.0.0.1", "-p", PORT, "-U",
             "postgres", "-d", "ledger", "-v", "ON_ERROR_STOP=1", "-X", "-q",
             "-f", str(MIGRATION)], capture_output=True, text=True).returncode, ""
        print(f"\nmigration applied: {'yes' if rc == 0 else 'NO'}")
        if rc != 0:
            failures.append("migration applies cleanly")
            return 1

        ins = ("insert into observations (observation_type, natural_key, symbol, "
               "direction, session_date, published_at, recorded_at, reference_price, "
               "reference_basis, lookback_window, source_file, computed_values, "
               "values_sha256) values ('signal','2026-10-16|ABC|LONG|signal','ABC',"
               "'LONG','2026-10-16','2026-10-16T13:00:00Z','2001-01-01T00:00:00Z',"
               "100,'close_T0',252,'engine/06_push_supabase.py','{\"x\":1}','h1') "
               "returning observation_id")

        print("\nINSERT is the one write allowed")
        rc, oid = psql(ins, user="service_role", label="service_role inserts an observation")
        oid = oid.splitlines()[0] if rc == 0 else None
        _, rec = psql(f"select recorded_at < now() - interval '1 day' from observations "
                      f"where observation_id = '{oid}'")
        check = rec.strip() == "f"
        print(f"  {'recorded_at sent as 2001 is replaced by the database clock':<64} "
              f"{'2001 discarded' if check else 'KEPT 2001':<9} {'ok' if check else 'FAIL'}")
        if not check:
            failures.append("recorded_at is the database's")

        print("\nNo role can rewrite an observation")
        for user in ("service_role", "postgres"):
            psql(f"update observations set symbol='XYZ' where observation_id='{oid}'",
                 user=user, ok_expected=False, label=f"UPDATE as {user}")
            psql(f"delete from observations where observation_id='{oid}'",
                 user=user, ok_expected=False, label=f"DELETE as {user}")
            psql("truncate observations cascade", user=user, ok_expected=False,
                 label=f"TRUNCATE as {user}")
        # anon is NOLOGIN in Supabase, so connecting AS anon fails at login --
        # which would read as "refused" and prove nothing about privileges.
        # SET ROLE gets anon's privileges on an open connection.
        psql("set role anon; select count(*) from observations", ok_expected=False,
             label="anon cannot read the ledger (SET ROLE, not a login)")
        psql("set role anon; " + ins.replace("'ABC'", "'DEF'").replace("|ABC|", "|DEF|"),
             ok_expected=False, label="anon cannot write it (SET ROLE)")
        psql("set role anon; select 1", label="  control: SET ROLE anon itself works")
        _, n = psql("select count(*) from observations where symbol='ABC'")
        check = n.strip() == "1"
        print(f"  {'after all of that the row is still there, unchanged':<64} "
              f"{n.strip():<9} {'ok' if check else 'FAIL'}")
        if not check:
            failures.append("row survives")

        print("\nCorrections append; duplicates of an original do not")
        psql(ins, user="service_role", ok_expected=False,
             label="a second ORIGINAL for the same natural key is refused")
        psql(ins.replace("'h1')", "'h2')").replace(
                 "values_sha256)", "values_sha256, corrects_observation_id, correction_reason)")
             .replace("'h2')", f"'h2','{oid}','republished with different values')"),
             user="service_role", label="a correction referencing it is accepted")
        psql(ins.replace("'h1')", "'h3')").replace(
                 "values_sha256)", "values_sha256, corrects_observation_id)")
             .replace("'h3')", f"'h3','{oid}')"),
             user="service_role", ok_expected=False,
             label="a correction with no reason is refused")
        _, sc = psql("select scored from v_observations")
        check = sc.strip() == "f"
        print(f"  {'v_observations shows one original, scored = false':<64} "
              f"{sc.strip():<9} {'ok' if check else 'FAIL'}")
        if not check:
            failures.append("v_observations scored false")

        print("\nA score is a number or a reason, never both and never neither")
        base = ("insert into observation_scores (observation_id, scored_version_id, "
                "t3_session, reference_price, exit_price, move_pct, cost_pct, net_pct, "
                "outcome, skip_reason) values ('{o}','{o}','2026-10-22',{cols})")
        psql(base.format(o=oid, cols="100,99,null,0.23,null,null,'lookback_too_short'")
             .replace("move_pct, cost_pct, net_pct,", "move_pct, cost_pct, net_pct,"),
             user="service_role", label="a skip with no number is accepted")
        psql(base.format(o=oid, cols="100,99,-1,0.23,-1.23,'net_negative',"
                         "'lookback_too_short'"),
             user="service_role", ok_expected=False,
             label="a skip that ALSO carries a number is refused")
        psql(base.format(o=oid, cols="100,99,-1,0.23,-1.23,'net_negative',null"),
             user="service_role", ok_expected=False,
             label="a second original score for the same observation is refused")
        psql(base.format(o=oid, cols="null,null,null,null,null,null,'made_up_reason'"),
             user="service_role", ok_expected=False,
             label="an unlisted skip reason is refused")
        rins = (ins.replace("'signal','2026-10-16|ABC|LONG|signal','ABC','LONG'",
                            "'radar_event','2026-10-16|RDR|-','RDR',null"))
        rc, rid = psql(rins, user="service_role", label="a radar_event is ledgered")
        rid = rid.splitlines()[0] if rc == 0 else "00000000-0000-0000-0000-000000000000"
        psql(base.format(o=rid, cols="100,99,null,null,null,null,'no_price_data'"),
             user="service_role", ok_expected=False,
             label="a score row for a radar_event is refused, even a skip")
        # Its own observation: reusing rid would be refused by the one-score
        # index once the probe above had wrongly succeeded, and pass for the
        # wrong reason.
        rc, rid2 = psql(rins.replace("RDR", "RDQ"), user="service_role")
        rid2 = rid2.splitlines()[0] if rc == 0 else "00000000-0000-0000-0000-000000000000"
        psql(base.format(o=rid2, cols="100,99,null,null,null,null,'no_price_data'"),
             ok_expected=False, label="  and refused for the owner too")
        _, sc = psql("select scored from v_observations where symbol='ABC'")
        check = sc.strip() == "t"
        print(f"  {'v_observations now shows scored = true, with no UPDATE':<64} "
              f"{sc.strip():<9} {'ok' if check else 'FAIL'}")
        if not check:
            failures.append("v_observations scored true")

        print("\nNo role can rewrite a score or a run record")
        for tbl in ("observation_scores", "observation_scorer_runs"):
            if tbl == "observation_scorer_runs":
                psql("insert into observation_scorer_runs (as_of_session, considered, "
                     "not_due, scored, skipped, skipped_by_reason, errors) values "
                     "('2026-10-26',1,0,0,1,'{}',0)", user="service_role",
                     label="service_role records a scorer run")
            for user in ("service_role", "postgres"):
                psql(f"update {tbl} set engine_sha='x'", user=user, ok_expected=False,
                     label=f"UPDATE {tbl} as {user}")
                psql(f"delete from {tbl}", user=user, ok_expected=False,
                     label=f"DELETE {tbl} as {user}")
                psql(f"truncate {tbl} cascade", user=user, ok_expected=False,
                     label=f"TRUNCATE {tbl} as {user}")

        # EACH LAYER ON ITS OWN. The probes above are refused by whichever
        # layer answers first, so deleting one layer leaves them green -- the
        # REVOKE is masked by the trigger, and observations' TRUNCATE trigger
        # by the foreign key plus observation_scores' trigger. These isolate.
        print("\nEach layer, isolated from the others")
        for tbl in ("observations", "observation_scores", "observation_scorer_runs"):
            for priv in ("UPDATE", "DELETE", "TRUNCATE"):
                _, has = psql(f"select has_table_privilege('service_role', "
                              f"'public.{tbl}', '{priv}')")
                good = has.strip() == "f"
                print(f"  {'service_role holds no ' + priv + ' on ' + tbl:<64} "
                      f"{has.strip():<9} {'ok' if good else 'FAIL'}")
                if not good:
                    failures.append(f"no {priv} privilege on {tbl}")
        surveillance_probes()

        # Last, because it removes a protection from the throwaway cluster.
        psql("drop trigger observation_scores_no_truncate on observation_scores")
        psql("truncate observations cascade", ok_expected=False,
             label="observations' OWN truncate trigger, with the scores one gone")
    finally:
        subprocess.run([str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "fast",
                        "stop"], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"FAILED — {len(failures)}: {failures}")
        return 1
    print("PROVEN — the ledger refuses UPDATE, DELETE and TRUNCATE from every role tried")
    return 0


if __name__ == "__main__":
    sys.exit(main())
