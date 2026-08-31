// GET /api/order/authentic?code=OGA-XXXX
// Public authenticity check. Confirms a PAID order exists for the code and
// returns ONLY the products + purchase date + order number — never any PII
// (no name, email, phone or address). Unknown code → { authentic:false }.
import { sb, getOrderItems } from "../_lib.js";

export default async (req, res) => {
  try {
    const code = String((req.query && req.query.code) || "").trim().toUpperCase();
    if (!code || code.length < 6) return res.status(400).json({ error: "code required" });

    const { data } = await sb(
      "GET",
      `orders?authenticity_code=eq.${encodeURIComponent(code)}&payment_status=eq.paid&select=id,order_number,created_at,status&limit=1`,
    );
    const order = Array.isArray(data) && data[0];
    if (!order) return res.json({ authentic: false });

    const items = await getOrderItems(order.id);
    return res.json({
      authentic: true,
      orderNumber: order.order_number,
      purchasedOn: order.created_at,
      status: order.status,
      products: items.map((i) => ({
        name: i.product_name,
        image: i.product_image || null,
        size: i.size || null,
        qty: Number(i.quantity || 1),
      })),
    });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "check failed" });
  }
};
