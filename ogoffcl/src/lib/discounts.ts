import type { DiscountCode } from "./types";
import { CURRENCY } from "./money";

/** Which kind of discount a row is, tolerant of legacy rows (percent default). */
export function discountKind(d: Partial<DiscountCode>): "percent" | "amount" {
  if (d.discount_type === "amount" || d.discount_type === "percent") return d.discount_type;
  if (d.amount_off != null && d.percentage == null) return "amount";
  return "percent";
}

/** Human label for a code's discount, e.g. "20% off" or "GH₵50 off". */
export function discountLabel(d: Partial<DiscountCode>): string {
  return discountKind(d) === "amount"
    ? `${CURRENCY}${Number(d.amount_off ?? 0)} off`
    : `${Number(d.percentage ?? d.value ?? 0)}% off`;
}

export interface DiscountEval {
  ok: boolean;
  amount: number;   // discount amount in currency units
  reason?: string;  // why it was rejected (when !ok)
}

/**
 * Evaluate a discount code against an order. Pure, and the server
 * (api/order/create.js) mirrors this exact logic so preview == charge.
 * `isReturning` is only knowable server-side; leave it undefined on the client
 * to skip the audience gate there (the server still enforces it).
 */
export function evaluateDiscount(
  d: Partial<DiscountCode> | null | undefined,
  subtotal: number,
  opts: { isReturning?: boolean } = {},
): DiscountEval {
  if (!d) return { ok: false, amount: 0, reason: "Invalid or inactive code." };
  if (d.is_active === false) return { ok: false, amount: 0, reason: "This code is not active." };
  if (d.expires_at && new Date(d.expires_at as string) < new Date())
    return { ok: false, amount: 0, reason: "This code has expired." };

  const min = Number(d.min_subtotal ?? 0);
  if (min > 0 && subtotal < min)
    return { ok: false, amount: 0, reason: `Spend at least ${CURRENCY}${min} to use this code.` };

  if (d.max_uses != null && Number(d.used_count ?? 0) >= Number(d.max_uses))
    return { ok: false, amount: 0, reason: "This code has been fully redeemed." };

  const audience = (d.audience as string) || "all";
  if (opts.isReturning !== undefined) {
    if (audience === "returning" && !opts.isReturning)
      return { ok: false, amount: 0, reason: "This code is for returning customers only." };
    if (audience === "new" && opts.isReturning)
      return { ok: false, amount: 0, reason: "This code is for first-time customers only." };
  }

  let amount = discountKind(d) === "amount"
    ? Math.min(Number(d.amount_off ?? 0), subtotal)
    : subtotal * (Number(d.percentage ?? d.value ?? 0) / 100);
  amount = Math.round(amount * 100) / 100;
  if (amount <= 0) return { ok: false, amount: 0, reason: "This code has no discount attached." };
  return { ok: true, amount };
}
