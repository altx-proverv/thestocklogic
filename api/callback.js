export default async function handler(req, res) {
  const code = req.query.code;
  if (!code) {
    return res.status(400).json({ error: "No code provided" });
  }

  const params = new URLSearchParams({
    code,
    client_id:     process.env.UPSTOX_API_KEY,
    client_secret: process.env.UPSTOX_API_SECRET,
    redirect_uri:  "https://thestocklogic.com/callback",
    grant_type:    "authorization_code",
  });

  const r = await fetch("https://api.upstox.com/v2/login/authorization/token", {
    method:  "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body:    params.toString(),
  });

  const data = await r.json();

  if (data.access_token) {
    // Store token in Supabase
    await fetch(`${process.env.SUPABASE_URL}/rest/v1/upstox_tokens`, {
      method:  "POST",
      headers: {
        "apikey":        process.env.SUPABASE_SERVICE_KEY,
        "Authorization": `Bearer ${process.env.SUPABASE_SERVICE_KEY}`,
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
      },
      body: JSON.stringify({
        token_date:   new Date().toISOString().split("T")[0],
        access_token: data.access_token,
        created_at:   new Date().toISOString(),
      }),
    });

    return res.status(200).send(`
      <html><body style="font-family:monospace;background:#06080a;color:#00d68f;padding:40px;text-align:center">
        <h2>✓ Token Generated</h2>
        <p>Upstox connected for today.</p>
        <p>You can close this tab.</p>
      </body></html>
    `);
  }

  return res.status(500).json({ error: data });
}
