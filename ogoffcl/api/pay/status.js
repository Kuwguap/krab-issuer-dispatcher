// GET /api/pay/status?ref=<externalref>
// The checkout polls this. Confirms with Moolre's status API (source of
// truth) and finalizes the order the moment the charge succeeds.
const { moolreStatus, getOrderByNumber, sb } = require("../_lib");
const { finalizePaidOrder } = require("../_finalize");

async function orderForRef(ref) {
  // externalref is "<order_number>-<suffix>"; also match a bare order number.
  const { data } = await sb(
    "GET",
    `orders?payment_ref=eq.${encodeURIComponent(ref)}&select=*&limit=1`
  );
  if (Array.isArray(data) && data[0]) return data[0];
  const orderNumber = String(ref).replace(/-[A-Z0-9]{5}$/, "");
  return getOrderByNumber(orderNumber);
}

module.exports = async (req, res) => {
  try {
    const ref = String((req.query && req.query.ref) || "").trim();
    if (!ref) return res.status(400).json({ error: "ref required" });

    const order = await orderForRef(ref);
    if (order && order.payment_status === "paid") {
      return res.json({ state: "paid", orderNumber: order.order_number });
    }

    const resp = await moolreStatus(ref);
    const tx = resp && typeof resp.data === "object" ? resp.data : null;
    const txstatus = tx ? Number(tx.txstatus) : null;

    if (txstatus === 1) {
      if (order) await finalizePaidOrder(order, tx);
      return res.json({ state: "paid", orderNumber: order ? order.order_number : null });
    }
    if (txstatus === 0 || /fail|declin|cancel/i.test(String(resp.message || ""))) {
      return res.json({ state: "failed", message: resp.message || "Payment failed." });
    }
    return res.json({ state: "pending" });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "status check failed" });
  }
};
