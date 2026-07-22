// Mark an order paid exactly once, then side effects: stock decrement +
// customer/store emails. Shared by status polling and the Moolre callback.
import {
  sb, getOrderItems, updateOrder, appendStatusHistory,
  sendEmail, emailShell, orderItemsHtml, env,
} from "./_lib.js";

async function finalizePaidOrder(order, moolreTx) {
  if (!order) return { ok: false, error: "no order" };
  if (order.payment_status === "paid") return { ok: true, already: true };

  const nowIso = new Date().toISOString();
  await updateOrder(order.id, {
    payment_status: "paid",
    status: order.status === "pending" ? "confirmed" : order.status,
    paid_at: nowIso,
    payment_method: "moolre_momo",
    payment_ref: moolreTx?.transactionid ? String(moolreTx.transactionid) : (order.payment_ref || null),
  });
  await appendStatusHistory(order, { status: "confirmed", note: "Payment received (Moolre MoMo)" });

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

  return { ok: true };
}

export { finalizePaidOrder };
