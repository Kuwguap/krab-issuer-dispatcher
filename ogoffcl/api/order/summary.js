// GET /api/order/summary?ref=OG-XXXXXXXX  (also accepts a full payment
// attempt ref like OG-XXXXXXXX-YYYYY — the order number prefix is used).
// Sanitized order summary for the confirmation screen: items + totals +
// status, first name only, masked contacts. No addresses, no raw PII.
import { getOrderByNumber, getOrderItems } from "../_lib.js";

function maskEmail(raw) {
  const e = String(raw || "").trim();
  const at = e.indexOf("@");
  if (at < 1) return null;
  return `${e[0]}${"•".repeat(Math.max(2, Math.min(6, at - 1)))}${e.slice(at)}`;
}

function maskPhone(raw) {
  const d = String(raw || "").replace(/\D/g, "");
  if (d.length < 6) return null;
  return `${d.slice(0, 3)}${"•".repeat(d.length - 5)}${d.slice(-2)}`;
}

export default async (req, res) => {
  try {
    const raw = String((req.query && req.query.ref) || "").trim();
    if (!raw) return res.status(400).json({ error: "ref required" });

    let order = await getOrderByNumber(raw);
    if (!order) {
      // timeout path lands here with the payment attempt ref — strip the suffix
      const m = raw.match(/^(.+)-[A-Za-z0-9]{5}$/);
      if (m) order = await getOrderByNumber(m[1]);
    }
    if (!order) return res.status(404).json({ error: "not found" });

    const items = await getOrderItems(order.id);
    return res.json({
      orderNumber: order.order_number,
      status: order.status,
      paymentStatus: order.payment_status,
      total: Number(order.total_amount || 0),
      discountCode: order.discount_code || null,
      discountAmount: Number(order.discount_amount || 0) || 0,
      firstName: String(order.customer_name || "").trim().split(/\s+/)[0] || null,
      maskedEmail: maskEmail(order.customer_email),
      maskedPhone: maskPhone(order.customer_phone),
      createdAt: order.created_at,
      items: items.map((i) => ({
        name: i.product_name,
        image: i.product_image || null,
        size: i.size || null,
        qty: Number(i.quantity || 1),
        price: Number(i.price ?? i.unit_price ?? 0),
      })),
    });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "summary failed" });
  }
};
