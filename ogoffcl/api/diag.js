// GET /api/diag  — TEMPORARY read-only diagnostic. Exposes NO secrets:
// only the ROLE claim of the configured Supabase key (anon vs service_role)
// and whether the orders.authenticity_code column exists. This is enough to
// explain why server writes to orders were silently no-opping. Remove after use.
import { env, sb } from "./_lib.js";

export default async (req, res) => {
  let role = "unknown";
  try {
    const seg = String(env.supabaseKey || "").split(".")[1];
    if (seg) {
      const json = Buffer.from(seg.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
      role = JSON.parse(json).role || "unknown";
    }
  } catch { /* ignore */ }

  const colProbe = await sb("GET", "orders?select=authenticity_code&limit=1");

  return res.json({
    supabaseKeyRole: role,                 // "service_role" = correct, "anon" = misconfigured
    supabaseKeyConfigured: !!env.supabaseKey,
    supabaseUrlConfigured: !!env.supabaseUrl,
    ordersAuthColumnExists: colProbe.ok,   // false => migration_oct_features.sql not applied
    ordersProbeStatus: colProbe.status,
  });
};
