-- v_observations is auto-updatable, and Supabase's default privileges granted
-- service_role UPDATE, DELETE and TRUNCATE on it. A write through the view was
-- stopped only by the row trigger on observations (proven 2026-11-03), so the
-- revoked-grants layer did not cover it. The view is for reading.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.v_observations FROM service_role;
