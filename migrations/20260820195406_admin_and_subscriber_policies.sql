-- Step 1 of 2, deliberately ADDITIVE. admin.html carried a service_role key in
-- its page source (const SK), served publicly at /admin, which bypasses RLS on
-- every table. These are the policies that let admin.html and the subscriber
-- gate work with an ordinary session token instead, so the key can come out.
--
-- The anon SELECT on subscribers is NOT dropped here. index.html and
-- signals.html still read it with the anon key until the matching code change
-- ships; dropping it now would sign every subscriber out mid-deploy. That is
-- migration 2, applied after the pages are live.

-- Who is an admin. SECURITY DEFINER on purpose: the subscribers SELECT policy
-- below calls this, and a SECURITY INVOKER function reading subscribers from
-- inside a policy ON subscribers recurses infinitely. The function is owned by
-- postgres, which owns the table and does not have FORCE ROW LEVEL SECURITY
-- set, so the read inside runs without re-entering the policy.
--
-- Keyed on user_id, not email: auth.uid() is exact, and an email comparison is
-- one casing difference away from a silent lockout. Both subscriber rows have
-- user_id populated and all resolve to a real auth.users row (verified).
--
-- EXECUTE revoked from PUBLIC (anon inherits it) and granted to authenticated,
-- which is the role that evaluates the policies.
CREATE OR REPLACE FUNCTION public.is_admin()
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_catalog
AS $$
  SELECT EXISTS (
    SELECT 1 FROM public.subscribers s
     WHERE s.user_id = auth.uid()
       AND s.plan    = 'admin'
       AND s.status  = 'active'
  );
$$;

REVOKE ALL ON FUNCTION public.is_admin() FROM PUBLIC;
REVOKE ALL ON FUNCTION public.is_admin() FROM anon;
GRANT EXECUTE ON FUNCTION public.is_admin() TO authenticated, service_role;

COMMENT ON FUNCTION public.is_admin() IS
  'True when the caller is an active subscriber with plan = admin. SECURITY DEFINER to avoid recursion when called from a policy on subscribers. Not callable by anon.';

-- A signed-in user reads their own subscriber row and nothing else. This is
-- what replaces the blanket anon read the login gate currently depends on.
CREATE POLICY "subscribers read own row"
  ON public.subscribers
  FOR SELECT TO authenticated
  USING (user_id = auth.uid());

-- The admin dashboard counts every subscriber.
CREATE POLICY "subscribers admin read all"
  ON public.subscribers
  FOR SELECT TO authenticated
  USING (public.is_admin());

-- admin.html lists the waitlist and approves/rejects rows. anon still cannot
-- SELECT it at all (20260820194548) and still can INSERT, which is the signup
-- form.
CREATE POLICY "waitlist admin read"
  ON public.waitlist
  FOR SELECT TO authenticated
  USING (public.is_admin());

CREATE POLICY "waitlist admin update"
  ON public.waitlist
  FOR UPDATE TO authenticated
  USING (public.is_admin())
  WITH CHECK (public.is_admin());