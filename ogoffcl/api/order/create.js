// POST /api/order/create
// { customer:{name,email,phone,address,city,note}, items:[{productId,size,qty}], discountCode? }
// Creates the order + items + customer SERVER-SIDE using the service-role key.
// - Immune to client-side RLS (service role bypasses it) — fixes the
//   "new row violates row-level security policy for orders" error.
// - Price is computed from the products table, never trusted from the browser,
//   so a tampered cart can't underpay. Returns { orderId, orderNumber, total }.
import { sb, env } from "../_lib.js";

const money2 = (n) => Math.round(Number(n || 0) * 100) / 100;

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  try {
    const { customer = {}, items = [], discountCode } = req.body || {};
    const name = String(customer.name || "").trim();
    const email = String(customer.email || "").trim();
    const phone = String(customer.phone || "").trim();
    const address = String(customer.address || "").trim();
    if (!name || !email.includes("@") || !phone || !address) {
      return res.status(400).json({ error: "Name, a valid email, phone and delivery address are required." });
    }
    if (!Array.isArray(items) || items.length === 0) {
      return res.status(400).json({ error: "Your cart is empty." });
    }

    // Authoritative pricing: load the real products by id.
    const ids = [...new Set(items.map((i) => String(i.productId)).filter(Boolean))];
    if (ids.length === 0) return res.status(400).json({ error: "Invalid cart." });
    const inList = ids.map((id) => `"${id}"`).join(",");
    const prodRes = await sb("GET", `products?id=in.(${inList})&select=id,name,price,image,is_active,stock`);
    if (!prodRes.ok) return res.status(502).json({ error: "Could not load products." });
    const byId = Object.fromEntries((prodRes.data || []).map((p) => [String(p.id), p]));

    const lineItems = [];
    let subtotal = 0;
    for (const it of items) {
      const p = byId[String(it.productId)];
      if (!p || p.is_active === false) return res.status(400).json({ error: "A product in your cart is no longer available." });
      const qty = Math.max(1, Math.min(99, parseInt(it.qty, 10) || 1));
      const price = money2(p.price);
      const line = money2(price * qty);
      subtotal += line;
      lineItems.push({
        // order_items.size is NOT NULL — no-size products (caps/headwear) must
        // send "" not null, or the insert fails with "Could not save order items".
        product_id: p.id, product_name: p.name, product_image: p.image || null,
        size: it.size || "", quantity: qty, unit_price: price, price, subtotal: line,
      });
    }
    subtotal = money2(subtotal);

    // Discount (validated server-side).
    let discountAmount = 0;
    let appliedCode = null;
    if (discountCode) {
      const code = String(discountCode).trim().toUpperCase();
      const dRes = await sb("GET", `discount_codes?code=eq.${encodeURIComponent(code)}&is_active=eq.true&select=*&limit=1`);
      const d = dRes.ok && Array.isArray(dRes.data) && dRes.data[0];
      if (d && (!d.expires_at || new Date(d.expires_at) >= new Date())) {
        const pct = Number(d.percentage || 0);
        if (pct > 0) { discountAmount = money2(subtotal * (pct / 100)); appliedCode = code; }
      }
    }
    const total = money2(Math.max(0, subtotal - discountAmount));

    // Upsert customer for records / the mailing list — but do NOT link it to
    // the order via customer_id. The orders RLS policy only permits a null
    // customer_id for anonymous (guest) checkouts; setting a real id triggers
    // "new row violates row-level security policy for orders". The customer's
    // details are denormalized onto the order below, so nothing is lost.
    try {
      const ex = await sb("GET", `customers?email=eq.${encodeURIComponent(email)}&select=id&limit=1`);
      if (ex.ok && Array.isArray(ex.data) && ex.data[0]) {
        await sb("PATCH", `customers?id=eq.${ex.data[0].id}`, { name, phone, address, city: customer.city || null });
      } else {
        await sb("POST", "customers", { name, full_name: name, email, phone, address, city: customer.city || null, country: "Ghana" }, { Prefer: "return=minimal" });
      }
    } catch { /* non-fatal */ }

    const orderNumber = `OG-${Date.now().toString(36).toUpperCase()}${Math.floor(Math.random() * 90 + 10)}`;
    const shipping = [address, String(customer.city || "").trim()].filter(Boolean).join(", ") +
      (String(customer.note || "").trim() ? ` — Note: ${String(customer.note).trim()}` : "");

    const orderRes = await sb("POST", "orders", {
      order_number: orderNumber,
      customer_name: name, customer_email: email, customer_phone: phone,
      shipping_address: shipping, status: "pending", payment_status: "pending",
      payment_method: "moolre_momo", currency: "GHS", total_amount: total,
      discount_code: appliedCode, discount_amount: discountAmount || null,
    }, { Prefer: "return=representation" });
    if (!orderRes.ok || !Array.isArray(orderRes.data) || !orderRes.data[0]) {
      const detail = typeof orderRes.data === "object" ? JSON.stringify(orderRes.data).slice(0, 160) : String(orderRes.data);
      return res.status(500).json({ error: "Could not create the order.", detail });
    }
    const order = orderRes.data[0];

    const itemsRes = await sb("POST", "order_items", lineItems.map((li) => ({ ...li, order_id: order.id })), { Prefer: "return=minimal" });
    if (!itemsRes.ok) {
      // roll back the order so we don't leave an itemless ghost
      await sb("DELETE", `orders?id=eq.${order.id}`).catch(() => {});
      return res.status(500).json({ error: "Could not save order items.", detail: JSON.stringify(itemsRes.data).slice(0, 160) });
    }

    return res.json({ orderId: order.id, orderNumber, total, subtotal, discountAmount, appliedCode });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "Order creation failed." });
  }
};
