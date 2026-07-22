// POST /api/pay/callback — the URL you feed to Moolre.
// Moolre's webhook has no documented signature, so we NEVER trust the body:
// we extract the reference, re-verify against Moolre's status API with our
// keys, and only then finalize. Always answers 200 so Moolre stops retrying.
import { moolreStatus, sb, getOrderByNumber } from "../_lib.js";
import { finalizePaidOrder } from "../_finalize.js";

export default async (req, res) => {
  try {
    const body = req.body || {};
    const d = typeof body.data === "object" && body.data ? body.data : body;
    const ref = String(d.externalref || d.reference || d.ref || "").trim();
    if (!ref) return res.status(200).json({ received: true });

    const verify = await moolreStatus(ref);
    const tx = verify && typeof verify.data === "object" ? verify.data : null;
    if (tx && Number(tx.txstatus) === 1) {
      let order = null;
      const q = await sb("GET", `orders?payment_ref=eq.${encodeURIComponent(ref)}&select=*&limit=1`);
      if (Array.isArray(q.data) && q.data[0]) order = q.data[0];
      if (!order) order = await getOrderByNumber(String(ref).replace(/-[A-Z0-9]{5}$/, ""));
      if (order) await finalizePaidOrder(order, tx);
    }
    return res.status(200).json({ received: true });
  } catch {
    return res.status(200).json({ received: true });
  }
};
