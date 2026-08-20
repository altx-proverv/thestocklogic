// Approve a waitlist application: create the auth user, add the subscriber row,
// mark the waitlist row approved.
//
// This exists because admin.html used to do all three from the browser with a
// service_role key hardcoded in its page source (const SK), served publicly at
// /admin. That key bypasses RLS on every table, read and write, and the login
// form above it was decorative -- the key was in the HTML before you signed in.
//
// Everything else admin.html does is now an ordinary session-token request
// governed by RLS. This one operation cannot be: creating an auth user goes
// through GoTrue's /auth/v1/admin/users, which requires service_role and which
// no policy can grant. So it moves to the server, where the key lives in an
// environment variable and never reaches a browser.
//
// The caller's session token is verified here and checked against
// public.is_admin()'s definition. Do not trust anything in the request body
// except the ids -- a caller can send whatever they like.

const REQUIRED_ENV = ["SUPABASE_URL", "SUPABASE_SERVICE_KEY"];

function genPassword() {
  // Excludes look-alike glyphs (0/O, 1/l/I) -- this is read off a screen and
  // typed by hand.
  const chars = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789!@#";
  const bytes = new Uint32Array(14);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.setHeader("Allow", "POST");
    return res.status(405).json({ error: "Method not allowed" });
  }

  const missing = REQUIRED_ENV.filter((k) => !process.env[k]);
  if (missing.length) {
    // Loud, and without naming the value. A silent no-op here would look
    // exactly like a successful approval that created nothing.
    console.error(`admin-approve: missing env ${missing.join(", ")}`);
    return res.status(500).json({ error: "Server is not configured" });
  }

  const SB = process.env.SUPABASE_URL;
  const SK = process.env.SUPABASE_SERVICE_KEY;
  const svc = {
    apikey: SK,
    Authorization: `Bearer ${SK}`,
    "Content-Type": "application/json",
  };

  // ── 1. who is calling ────────────────────────────────────────────────
  const auth = req.headers.authorization || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
  if (!token) return res.status(401).json({ error: "Not signed in" });

  const who = await fetch(`${SB}/auth/v1/user`, {
    headers: { apikey: SK, Authorization: `Bearer ${token}` },
  });
  if (!who.ok) return res.status(401).json({ error: "Session is not valid" });
  const user = await who.json();
  if (!user || !user.id) return res.status(401).json({ error: "Session is not valid" });

  // ── 2. are they an admin ─────────────────────────────────────────────
  // Same rule as public.is_admin(): active subscriber, plan = admin, matched on
  // user_id rather than email so a casing difference cannot decide it.
  const chk = await fetch(
    `${SB}/rest/v1/subscribers?user_id=eq.${encodeURIComponent(user.id)}` +
      `&plan=eq.admin&status=eq.active&select=id&limit=1`,
    { headers: svc }
  );
  if (!chk.ok) {
    console.error(`admin-approve: admin check failed HTTP ${chk.status} ${(await chk.text()).slice(0, 200)}`);
    return res.status(500).json({ error: "Could not verify admin" });
  }
  const isAdmin = (await chk.json()).length > 0;
  if (!isAdmin) return res.status(403).json({ error: "Admin only" });

  // ── 3. inputs ────────────────────────────────────────────────────────
  const body = typeof req.body === "string" ? JSON.parse(req.body || "{}") : req.body || {};
  const waitlistId = body.waitlistId;
  const email = String(body.email || "").trim().toLowerCase();
  const name = String(body.name || "").trim();
  if (!waitlistId || !email) {
    return res.status(400).json({ error: "waitlistId and email are required" });
  }

  const password = genPassword();

  // ── 4. create the auth user ──────────────────────────────────────────
  const mk = await fetch(`${SB}/auth/v1/admin/users`, {
    method: "POST",
    headers: svc,
    body: JSON.stringify({ email, password, email_confirm: true }),
  });
  const created = await mk.json().catch(() => ({}));
  if (!mk.ok || !created.id) {
    const detail = created.msg || created.message || `HTTP ${mk.status}`;
    console.error(`admin-approve: create user failed: ${detail}`);
    return res.status(400).json({ error: `Could not create user: ${detail}` });
  }

  // ── 5. subscriber row ────────────────────────────────────────────────
  // Checked, unlike the version this replaces: admin.html fired all three of
  // these and looked at none of the responses, so a rejected insert produced a
  // credentials modal for an account with no subscription behind it.
  const sub = await fetch(`${SB}/rest/v1/subscribers`, {
    method: "POST",
    headers: { ...svc, Prefer: "return=minimal" },
    body: JSON.stringify({
      user_id: created.id,
      email,
      name,
      status: "active",
      plan: "beta",
    }),
  });
  if (!sub.ok) {
    const detail = (await sub.text()).slice(0, 300);
    console.error(`admin-approve: subscriber insert failed HTTP ${sub.status} ${detail}`);
    // The auth user exists but has no subscription; say so rather than
    // handing over credentials that will bounce off the approval gate.
    return res.status(500).json({
      error: "Auth user was created but the subscriber row failed. Fix before sharing credentials.",
      detail,
    });
  }

  // ── 6. mark the application approved ─────────────────────────────────
  const wl = await fetch(`${SB}/rest/v1/waitlist?id=eq.${encodeURIComponent(waitlistId)}`, {
    method: "PATCH",
    headers: { ...svc, Prefer: "return=minimal" },
    body: JSON.stringify({ status: "approved", approved_at: new Date().toISOString() }),
  });
  if (!wl.ok) {
    const detail = (await wl.text()).slice(0, 300);
    console.error(`admin-approve: waitlist patch failed HTTP ${wl.status} ${detail}`);
    // Non-fatal: the subscriber is live. Report it so the row can be fixed.
    return res.status(207).json({
      password,
      warning: "Subscriber created, but the waitlist row was not marked approved.",
      detail,
    });
  }

  return res.status(200).json({ password });
}
