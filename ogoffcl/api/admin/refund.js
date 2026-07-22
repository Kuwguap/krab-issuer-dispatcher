// POST /api/admin/refund  { orderId, amount?, phone?, channel?, reason? }
// Admin-only. Sends money back to the customer's MoMo via Moolre transfer,
// records it on the order, and emails the customer.
import {
  env, moolreConfigured, getOrder, updateOrder, appendStatusHistory,
  moolreTransfer, sendEmail, emailShell,
} from "../_lib.js";

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (String(req.headers["x-admin-password"] || "") !== env.adminPassword) {
    return res.status(401).json({ error: "unauthorized" });
  }
  try {
    if (!moolreConfigured()) return res.status(503).json({ error: "Set MOOLRE_* env vars first." });
    const { orderId, amount, phone, channel, reason } = req.body || {};
    const order = await getOrder(orderId);
    if (!order) return res.status(404).json({ error: "Order not found." });
    if (order.refund_status === "refunded") return res.status(400).json({ error: "Order already refunded." });
    if (order.payment_status !== "paid") return res.status(400).json({ error: "Only paid orders can be refunded." });

    const refundAmount = Number(amount || order.total_amount || 0);
    if (!(refundAmount > 0) || refundAmount > Number(order.total_amount || 0)) {
      return res.status(400).json({ error: "Invalid refund amount." });
    }
    const target = String(phone || order.momo_phone || "").replace(/\D/g, "");
    const net = String(channel || order.momo_channel || "mtn");
    if (target.length < 9) return res.status(400).json({ error: "No MoMo number on file — pass phone explicitly." });

    const externalref = `RF-${order.order_number}-${Date.now().toString(36).slice(-4).toUpperCase()}`;
    const resp = await moolreTransfer({
      channel: net, phone: target, amount: refundAmount, externalref,
      reference: `OG OFFCL refund ${order.order_number}`,
    });

    if (Number(resp.status) !== 1) {
      await updateOrder(order.id, { refund_status: "failed" });
      return res.status(400).json({ error: resp.message || `Refund failed (${resp.code || "unknown"}).`, code: resp.code });
    }

    const nowIso = new Date().toISOString();
    await updateOrder(order.id, {
      refund_status: "refunded",
      refund_amount: refundAmount,
      refund_ref: externalref,
      refunded_at: nowIso,
      status: "refunded",
    });
    await appendStatusHistory(order, {
      status: "refunded",
      note: `GH₵${refundAmount} → ${target} (${net})${reason ? ` — ${reason}` : ""}`,
    });

    const email = (order.customer_email || "").trim();
    if (email) {
      await sendEmail({
        to: email,
        subject: `Refund sent — ${order.order_number}`,
        html: emailShell("Refund on the way", `
          <p>We've sent <strong style="color:#C8FF00;">GH₵${refundAmount}</strong> back to your mobile money number ending in <strong>${target.slice(-4)}</strong> for order <strong>${order.order_number}</strong>.</p>
          ${reason ? `<p style="color:#8b877e;">Reason: ${String(reason)}</p>` : ""}
          <p style="color:#8b877e;">MoMo transfers usually land instantly. Questions? Just reply to this email.</p>
        `),
      }).catch(() => {});
    }

    return res.json({ ok: true, refund_ref: externalref, amount: refundAmount });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "Refund failed." });
  }
};
