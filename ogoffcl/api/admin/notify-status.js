// POST /api/admin/notify-status  { orderId, status, note? }
// Admin-only. Records the status change on the order timeline and emails the
// customer a tracking update.
import {
  env, getOrder, updateOrder, appendStatusHistory, sendEmail, emailShell,
} from "../_lib.js";

const FRIENDLY = {
  confirmed: ["Order confirmed", "Your order is confirmed and getting packed."],
  processing: ["Being prepared", "Your pieces are being prepped and packed right now."],
  shipped: ["On the move", "Your order has been handed to the courier."],
  out_for_delivery: ["Out for delivery", "The rider is on the way — keep your phone close."],
  delivered: ["Delivered", "Your order has been delivered. Wear it loud. 🖤"],
  cancelled: ["Order cancelled", "Your order has been cancelled. If this is unexpected, reply to this email."],
};

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (!env.adminPassword || String(req.headers["x-admin-password"] || "") !== env.adminPassword) {
    return res.status(401).json({ error: "unauthorized" });
  }
  try {
    const { orderId, status, note } = req.body || {};
    const order = await getOrder(orderId);
    if (!order) return res.status(404).json({ error: "Order not found." });

    await updateOrder(order.id, { status, ...(note ? { tracking_note: note } : {}) });
    await appendStatusHistory(order, { status, note: note || null });

    let emailed = false;
    const email = (order.customer_email || "").trim();
    const f = FRIENDLY[status];
    if (email && f) {
      const r = await sendEmail({
        to: email,
        subject: `${f[0]} — ${order.order_number}`,
        html: emailShell(f[0], `
          <p>${f[1]}</p>
          <p>Order: <strong style="color:#C8FF00;">${order.order_number}</strong></p>
          ${note ? `<p style="color:#8b877e;">${String(note)}</p>` : ""}
        `),
      });
      emailed = !!r.ok;
    }
    return res.json({ ok: true, emailed });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "notify failed" });
  }
};
