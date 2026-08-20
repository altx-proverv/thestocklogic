-- Step 2 of 2. Held back until the pages that depend on it were live.
--
-- "Allow anon read" let anyone holding the anon key -- which is embedded in
-- every page and therefore public -- read every subscriber row: email, name,
-- plan and status. index.html and signals.html relied on it to decide whether
-- a signed-in user was approved, which is why it could not simply be dropped:
-- doing so while those pages still read with the anon key signs every
-- subscriber out with "Your account is pending approval".
--
-- Verified live at https://www.thestocklogic.com before applying: / and
-- /dashboard now send the session token and filter on user_id, and no page
-- serves a service_role key any more.
--
-- Replaced by "subscribers read own row" (auth.uid() = user_id) and
-- "subscribers admin read all" (public.is_admin()), both added in
-- 20260820195406. anon now has no read path to this table at all.
DROP POLICY IF EXISTS "Allow anon read" ON public.subscribers;

COMMENT ON TABLE public.subscribers IS
  'Subscriber roster. anon has NO read access -- the anon key is public. A signed-in user sees only their own row; admins see all. Writes are service_role only (api/admin-approve).';