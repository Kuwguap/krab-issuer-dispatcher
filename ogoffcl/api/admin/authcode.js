// POST /api/admin/authcode  { orderId }   (header x-admin-password)
// Ensures an order has an authenticity_code — generates + saves one if it's
// missing (older orders, or those paid before the feature) — and returns it,
// so the admin can print an authenticity certificate for ANY order.
import { env, getOrder, updateOrder, sb } from "../_lib.js";

/** Decode the `role` claim from a Supabase JWT (no secret exposed). */
function keyRole(key) {
  try {
    const seg = String(key || "").split(".")[1];
    if (!seg) return "unknown";
    const json = Buffer.from(seg.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
    return JSON.parse(json).role || "unknown";
  } catch { return "unknown"; }
}

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (!env.adminPassword || String(req.headers["x-admin-password"] || "") !== env.adminPassword) {
    return res.status(401).json({ error: "unauthorized" });
  }
  try {
    const { orderId } = req.body || {};
    const order = await getOrder(orderId);
    if (!order) return res.status(404).json({ error: "Order not found." });

    let code = order.authenticity_code;
    if (!code) {
      code = `OGA-${Date.now().toString(36).toUpperCase()}${Math.random().toString(36).slice(2, 8).toUpperCase()}`;
      await updateOrder(order.id, { authenticity_code: code });
      const check = await getOrder(order.id);
      if (!check || check.authenticity_code !== code) {
        // The write didn't persist. Two distinct causes — report the real one
        // instead of always blaming the migration:
        //  (a) column missing  → migration not run
        //  (b) column present but write no-ops → the server is on the ANON key,
        //      so RLS silently blocks writes to orders (0 rows changed).
        const colOk = (await sb("GET", "orders?select=authenticity_code&limit=1")).ok;
        const role = keyRole(env.supabaseKey);
        if (!colOk) {
          return res.status(503).json({ error: "Authenticity column missing — run migration_oct_features.sql in Supabase, then retry." });
        }
        return res.status(500).json({
          error: role === "service_role"
            ? "The code could not be saved (update affected 0 rows). Check RLS / policies on the orders table."
            : "Server can't write orders: it's using the Supabase ANON key. Set SUPABASE_SERVICE_ROLE_KEY in Vercel (Project Settings -> Environment Variables) and redeploy.",
          serverKeyRole: role,
        });
      }
    }
    return res.json({ ok: true, code });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "authcode failed" });
  }
};
