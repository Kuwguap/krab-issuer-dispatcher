// Mark an order paid exactly once, then side effects: stock decrement +
// customer/store emails. Shared by status polling and the Moolre callback.
import {
  sb, getOrderItems, updateOrder, appendStatusHistory,
  sendEmail, emailShell, orderItemsHtml, env, notifyBot,
} from "./_lib.js";

async function finalizePaidOrder(order, moolreTx) {
  if (!order) return { ok: false, error: "no order" };
  if (order.payment_status === "paid") return { ok: true, already: true };

  const nowIso = new Date().toISOString();
  // authenticity code — assigned once, when the order is paid (only real
  // completed purchases are "authentic"). updateOrder drops the column
  // gracefully if migration_oct_features.sql hasn't been run yet.
  const authCode = order.authenticity_code ||
    `OGA-${Date.now().toString(36).toUpperCase()}${Math.random().toString(36).slice(2, 8).toUpperCase()}`;
  await updateOrder(order.id, {
    payment_status: "paid",
    status: order.status === "pending" ? "confirmed" : order.status,
    paid_at: nowIso,
    payment_method: "moolre_momo",
    payment_ref: moolreTx?.transactionid ? String(moolreTx.transactionid) : (order.payment_ref || null),
    authenticity_code: authCode,
  });
  await appendStatusHistory(order, { status: "confirmed", note: "Payment received (Moolre MoMo)" });

  // Count a discount redemption once, ON payment — so abandoned checkouts don't
  // burn a code's use and max_uses reflects real redemptions.
  if (order.discount_code) {
    try {
      const { data } = await sb("GET", `discount_codes?code=eq.${encodeURIComponent(order.discount_code)}&select=id,used_count&limit=1`);
      const dc = Array.isArray(data) && data[0];
      if (dc) await sb("PATCH", `discount_codes?id=eq.${dc.id}`, { used_count: Number(dc.used_count || 0) + 1 });
    } catch { /* non-fatal */ }
  }

  const items = await getOrderItems(order.id);

  // best-effort stock decrement
  for (const it of items) {
    try {
      if (!it.product_id) continue;
      const { data } = await sb("GET", `products?id=eq.${it.product_id}&select=id,stock&limit=1`);
      const p = Array.isArray(data) && data[0];
      if (p && p.stock !== null && p.stock !== undefined) {
        await sb("PATCH", `products?id=eq.${it.product_id}`, {
          stock: Math.max(0, Number(p.stock) - Number(it.quantity || 1)),
        });
      }
    } catch { /* non-fatal */ }
  }

  // customer email
  const email = (order.customer_email || "").trim();
  if (email) {
    await sendEmail({
      to: email,
      subject: `Order confirmed — ${order.order_number} ✓`,
      html: emailShell("You're official. 🎉", `
        <p>Payment received — your order <strong style="color:#C8FF00;">${order.order_number}</strong> is confirmed and we're getting it packed.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">${orderItemsHtml(items)}</table>
        <p style="font-size:16px;color:#F5F2EA;">Total paid: <strong style="color:#C8FF00;">GH₵${Number(order.total_amount || 0)}</strong></p>
        <p style="color:#8b877e;">Delivery: ${order.shipping_address || "—"}<br/>We'll reach you on ${order.customer_phone || "your phone"} when it's moving.</p>
        <p style="margin:20px 0 6px;"><a href="${env.siteUrl}/track?id=${encodeURIComponent(order.order_number)}" style="display:inline-block;background:#C8FF00;color:#0A0A0A;font-weight:900;text-decoration:none;padding:13px 22px;text-transform:uppercase;letter-spacing:1px;">📦 Track your order</a></p>
        <p style="color:#8b877e;font-size:13px;">Track anytime at <a href="${env.siteUrl}/track" style="color:#C8FF00;">${env.siteUrl.replace(/^https?:\/\//, "")}/track</a> with your order number <strong style="color:#F5F2EA;">${order.order_number}</strong>.<br/>
        Authenticity code: <strong style="color:#F5F2EA;">${authCode}</strong> — verify any OG OFFCL piece at <a href="${env.siteUrl}/authentic-check" style="color:#C8FF00;">${env.siteUrl.replace(/^https?:\/\//, "")}/authentic-check</a>.</p>
      `),
    }).catch(() => {});
  }

  // store notification
  if (env.storeNotify) {
    await sendEmail({
      to: env.storeNotify,
      subject: `💰 PAID ${order.order_number} — GH₵${Number(order.total_amount || 0)} (${order.customer_name || "customer"})`,
      html: emailShell("New paid order", `
        <p><strong>${order.order_number}</strong> · GH₵${Number(order.total_amount || 0)} · ${order.customer_name || "—"} · ${order.customer_phone || "—"}</p>
        <table style="width:100%;border-collapse:collapse;margin:12px 0;">${orderItemsHtml(items)}</table>
        <p style="color:#8b877e;">Ship to: ${order.shipping_address || "—"}</p>
      `),
    }).catch(() => {});
  }

  // best-effort push to the Telegram bot (owner alert). Never affects the order.
  await notifyBot("order.paid", {
    id: order.id,
    order_number: order.order_number,
    total_amount: order.total_amount,
    customer_name: order.customer_name,
    item_count: items.length,
  });

  return { ok: true };
}

export { finalizePaidOrder };
