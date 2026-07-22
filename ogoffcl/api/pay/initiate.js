// POST /api/pay/initiate  { orderId, phone, channel: mtn|telecel|at, otpcode?, ref? }
// Starts (or resumes, when otpcode is present) a Moolre MoMo charge for an
// order. Amount ALWAYS comes from the order row in the database — never from
// the client. Also sends the "order received" email on first attempt.
// `ref` is the externalref echoed back by the client on OTP submit: Moolre
// only completes the OTP flow when the SAME externalref is reused — a new one
// starts a brand-new charge and triggers ANOTHER OTP.
import {
  moolreConfigured, getOrder, updateOrder, appendStatusHistory,
  moolrePay, sendEmail, emailShell, orderItemsHtml, getOrderItems,
} from "../_lib.js";

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  try {
    if (!moolreConfigured()) {
      return res.status(503).json({ error: "Payments not configured yet (set MOOLRE_* env vars on Vercel)." });
    }
    const { orderId, phone, channel, otpcode, ref } = req.body || {};
    const digits = String(phone || "").replace(/\D/g, "");
    if (!orderId || digits.length < 9) return res.status(400).json({ error: "orderId and a valid MoMo number are required." });
    if (!["mtn", "telecel", "at"].includes(String(channel))) return res.status(400).json({ error: "channel must be mtn, telecel or at." });

    const order = await getOrder(orderId);
    if (!order) return res.status(404).json({ error: "Order not found." });
    if (order.payment_status === "paid") return res.json({ state: "paid", ref: order.order_number });

    const amount = Number(order.total_amount || 0);
    if (!(amount > 0)) return res.status(400).json({ error: "Order has no payable amount." });

    // Unique per attempt (Moolre requires non-repeating externalref); the
    // order number prefix keeps callbacks resolvable back to the order.
    // On OTP submit, resume the SAME externalref: prefer the ref echoed by the
    // client (works even before the payment_ref column migration has run),
    // fall back to the stored payment_ref. Only accept refs for THIS order.
    const clientRef = typeof ref === "string" && ref.startsWith(`${order.order_number}-`) ? ref : null;
    const resumeRef = otpcode ? (clientRef || order.payment_ref || null) : null;
    const attempt = resumeRef || `${order.order_number}-${Date.now().toString(36).slice(-5).toUpperCase()}`;

    await updateOrder(order.id, {
      momo_phone: digits,
      momo_channel: channel,
      payment_method: "moolre_momo",
      payment_ref: attempt,
    });

    // first-attempt "order received" email (fire and forget)
    if (!otpcode && (order.customer_email || "").trim()) {
      getOrderItems(order.id).then((items) =>
        sendEmail({
          to: order.customer_email.trim(),
          subject: `We got your order — ${order.order_number}`,
          html: emailShell("Order received", `
            <p>Your order <strong style="color:#C8FF00;">${order.order_number}</strong> is in. Approve the mobile-money prompt on <strong>${digits}</strong> to lock it down.</p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0;">${orderItemsHtml(items)}</table>
            <p style="font-size:16px;color:#F5F2EA;">Total: <strong style="color:#C8FF00;">GH₵${amount}</strong></p>
            <p style="color:#8b877e;">Didn't get the prompt? Head back to checkout and try again — your order is saved.</p>
          `),
        })
      ).catch(() => {});
    }

    const resp = await moolrePay({
      channel, phone: digits, amount, externalref: attempt,
      reference: `OG OFFCL ${order.order_number}`,
      otpcode,
    });

    if (resp.code === "TP14") {
      return res.json({ state: "otp", ref: attempt, message: resp.message || "Enter the OTP sent to you by SMS." });
    }
    if (Number(resp.status) === 1) {
      await appendStatusHistory(order, { status: "payment_prompted", note: `MoMo prompt sent to ${digits} (${channel})` });
      return res.json({ state: "pending", ref: attempt, message: "Approve the prompt on your phone." });
    }
    return res.status(400).json({
      state: "failed", ref: attempt,
      error: resp.message || `Payment could not be started (${resp.code || "unknown"}).`,
      code: resp.code,
    });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "Payment initiation failed." });
  }
};
