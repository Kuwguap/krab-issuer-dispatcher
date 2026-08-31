// GET /api/order/cert?code=OGA-XXXX
// Certificate data for the printable A4 sheet — products + order number only,
// NO PII (no name/email/phone/address). Works for ANY order carrying the code
// (the admin decides which to print); the public /authent-check stays
// paid-only. The unguessable code is the capability.
import { sb, getOrderItems } from "../_lib.js";

export default async (req, res) => {
  try {
    const code = String((req.query && req.query.code) || "").trim().toUpperCase();
    if (!code || code.length < 6) return res.status(400).json({ error: "code required" });

    const { data } = await sb(
      "GET",
      `orders?authenticity_code=eq.${encodeURIComponent(code)}&select=id,order_number,created_at,status,payment_status&limit=1`,
    );
    const order = Array.isArray(data) && data[0];
    if (!order) return res.json({ found: false });

    const items = await getOrderItems(order.id);
    return res.json({
      found: true,
      orderNumber: order.order_number,
      purchasedOn: order.created_at,
      status: order.status,
      paid: order.payment_status === "paid",
      products: items.map((i) => ({
        name: i.product_name, image: i.product_image || null,
        size: i.size || null, qty: Number(i.quantity || 1),
      })),
    });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "cert failed" });
  }
};
