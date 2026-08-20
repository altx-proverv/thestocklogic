-- The anon key is embedded in every page (signals.html, index.html, atlas.html)
-- and is therefore public. Anyone could read the whole waitlist -- name, email
-- and free-text reason for every applicant -- with a single curl. Two rows
-- today; this comes off before there are real ones.
--
-- "Allow anon select own" was named as if it were scoped to the caller's own
-- row. Its USING clause was `true`. It returned the entire table, exactly like
-- "Allow anon read waitlist" next to it.
--
-- Nothing reads waitlist with the anon key. waitlist.html only POSTs (the
-- INSERT policy below is untouched, so the signup form keeps working) and
-- admin.html reads it with a service_role key, which bypasses RLS entirely.
--
-- NOT done here: the same change for subscribers. index.html:346 and
-- signals.html:897/911 read subscribers with the ANON key to decide whether a
-- signed-in user is approved, so revoking anon SELECT there locks every
-- subscriber out of the product. That needs an own-row authenticated policy
-- and a matching change to those two pages.
DROP POLICY IF EXISTS "Allow anon read waitlist" ON public.waitlist;
DROP POLICY IF EXISTS "Allow anon select own"    ON public.waitlist;

COMMENT ON TABLE public.waitlist IS
  'Signup applications. anon may INSERT only -- never SELECT: the anon key is public. Read it with service_role.';