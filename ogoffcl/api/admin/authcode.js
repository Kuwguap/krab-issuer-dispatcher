// POST /api/admin/authcode  { orderId }   (header x-admin-password)
// Ensures an order has an authenticity_code — generates + saves one if it's
// missing (older orders, or those paid before the feature) — and returns it,
// so the admin can print an authenticity certificate for ANY order.
import { env, getOrder, updateOrder } from "../_lib.js";

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
      // updateOrder drops the column gracefully if migration_oct_features.sql
      // hasn't been run — in that case the code can't persist. Re-read to confirm.
      await updateOrder(order.id, { authenticity_code: code });
      const check = await getOrder(order.id);
      if (!check || check.authenticity_code !== code) {
        return res.status(503).json({ error: "Authenticity column missing — run migration_oct_features.sql, then retry." });
      }
    }
    return res.json({ ok: true, code });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "authcode failed" });
  }
};
