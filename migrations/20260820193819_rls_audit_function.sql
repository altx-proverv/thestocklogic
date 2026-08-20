-- The database enables RLS on every new table in public (event trigger
-- `ensure_rls` -> rls_auto_enable()) and creates no policy. That is the right
-- default, but it means a new table is born invisible to the frontend: anon
-- reads return 200 and an empty array, with no status to check and nothing in
-- any log. live_prices sat that way for eleven weeks while its feed wrote 500
-- symbols twice a day.
--
-- tools/check_schema.py talks to PostgREST, not Postgres, so it cannot read
-- pg_class/pg_policy directly. This exposes just the audit it needs.
--
-- SECURITY INVOKER, not DEFINER: the catalogs are readable by the calling role
-- already, so there is nothing to elevate, and a DEFINER function here would
-- add exactly the anon-callable-SECURITY-DEFINER surface the linter flags.
-- EXECUTE is revoked from PUBLIC (which anon and authenticated inherit) and
-- granted only to service_role.
CREATE OR REPLACE FUNCTION public.rls_audit()
RETURNS TABLE (table_name text, rls_enabled boolean, n_policies integer)
LANGUAGE sql
STABLE
SECURITY INVOKER
SET search_path = pg_catalog, public
AS $$
  SELECT c.relname::text,
         c.relrowsecurity,
         (SELECT count(*)::integer FROM pg_policy p WHERE p.polrelid = c.oid)
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE n.nspname = 'public'
    AND c.relkind IN ('r', 'p')
  ORDER BY c.relname;
$$;

REVOKE ALL ON FUNCTION public.rls_audit() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.rls_audit() FROM anon, authenticated;
GRANT EXECUTE ON FUNCTION public.rls_audit() TO service_role;

COMMENT ON FUNCTION public.rls_audit() IS
  'Per-table RLS state for tools/check_schema.py check 5. A table with rls_enabled and n_policies = 0 answers every anon read with 200 and an empty array. service_role only.';