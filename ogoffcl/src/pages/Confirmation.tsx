import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import Marquee from "../components/Marquee";
import { money } from "../lib/money";
import { usePageMeta } from "../lib/seo";

interface SummaryItem {
  name: string;
  image: string | null;
  size: string | null;
  qty: number;
  price: number;
}

interface Summary {
  orderNumber: string;
  status: string;
  paymentStatus: string;
  total: number;
  discountCode: string | null;
  discountAmount: number;
  firstName: string | null;
  maskedEmail: string | null;
  maskedPhone: string | null;
  items: SummaryItem[];
}

const EASE = [0.22, 1, 0.36, 1] as const;

/** Where the order sits in the journey — drives the timeline highlight. */
const STEPS = [
  { key: "confirmed", label: "Confirmed", sub: "Payment locked in" },
  { key: "processing", label: "Packing", sub: "Getting it boxed" },
  { key: "out_for_delivery", label: "On the road", sub: "Rider is moving" },
  { key: "delivered", label: "At your door", sub: "Wear it loud" },
];
const STEP_INDEX: Record<string, number> = {
  pending: 0, confirmed: 0, processing: 1, ready: 1,
  out_for_delivery: 2, shipped: 2, delivered: 3,
};

export default function Confirmation() {
  const [params] = useSearchParams();
  const ref = params.get("ref") || "—";
  const paid = params.get("paid") === "1";

  const [sum, setSum] = useState<Summary | null>(null);

  usePageMeta({
    title: "Order confirmation — OG OFFCL",
    description: "Your OG OFFCL order status.",
    path: "/order-confirmation",
    noindex: true,
  });

  useEffect(() => {
    if (!ref || ref === "—") return;
    let alive = true;
    fetch(`/api/order/summary?ref=${encodeURIComponent(ref)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (alive && j && j.orderNumber) setSum(j); })
      .catch(() => {});
    return () => { alive = false; };
  }, [ref]);

  const isPaid = paid || sum?.paymentStatus === "paid";
  const orderNo = sum?.orderNumber || ref;
  const activeStep = isPaid ? (STEP_INDEX[sum?.status || "confirmed"] ?? 0) : -1;

  return (
    <div>
      {isPaid && <Marquee text="ORDER CONFIRMED — YOU'RE OFFICIAL — ORDER CONFIRMED — YOU'RE OFFICIAL — " fast />}

      <div className="max-w-2xl mx-auto px-4 py-16 sm:py-24">
        {/* ── mark + headline ─────────────────────────────────── */}
        <div className="text-center">
          <motion.div
            initial={{ scale: 0.6, opacity: 0, rotate: -8 }}
            animate={{ scale: 1, opacity: 1, rotate: 0 }}
            transition={{ duration: 0.55, ease: EASE }}
            className="relative w-24 h-24 mx-auto mb-8"
          >
            <div className={`absolute inset-0 border-4 ${isPaid ? "border-acid" : "border-bone/30"}`} />
            {isPaid && (
              <motion.div
                initial={{ opacity: 0.7, scale: 1 }}
                animate={{ opacity: 0, scale: 1.55 }}
                transition={{ duration: 1.1, ease: "easeOut", delay: 0.25 }}
                className="absolute inset-0 border-2 border-acid"
              />
            )}
            <span className={`absolute inset-0 flex items-center justify-center font-display text-5xl ${isPaid ? "text-acid" : "text-bone/40"}`}>
              {isPaid ? "✓" : "…"}
            </span>
          </motion.div>

          <motion.h1
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.6, ease: EASE, delay: 0.1 }}
            className="display-xl text-5xl sm:text-6xl text-bone"
          >
            {isPaid ? (
              <>{sum?.firstName ? `${sum.firstName}, you're` : "You're"} <span className="text-acid">official.</span></>
            ) : (
              <>Payment <span className="text-stroke">pending</span></>
            )}
          </motion.h1>

          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.3, duration: 0.6 }}
            className="text-bone/60 mt-5 leading-relaxed max-w-md mx-auto"
          >
            {isPaid
              ? "Payment received — your order is locked in and heading for the packing table."
              : "Your order was saved but the payment window closed before completing. Reach us with your order number and we'll sort you out — or try checkout again."}
          </motion.p>

          {isPaid && sum?.maskedEmail && (
            <motion.p
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.45, duration: 0.6 }}
              className="text-bone/35 text-[11px] uppercase tracking-[0.25em] mt-4"
            >
              Receipt sent to <span className="text-bone/70 normal-case tracking-normal">{sum.maskedEmail}</span>
            </motion.p>
          )}
        </div>

        {/* ── order card ──────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0, y: 24 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ delay: 0.35, duration: 0.6, ease: EASE }}
          className="border border-ash bg-smoke/60 mt-12"
        >
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 px-5 py-4 border-b border-ash">
            <p className="text-bone/40 text-[10px] uppercase tracking-[0.3em]">Order number</p>
            <p className="font-display text-lg sm:text-xl text-acid break-all">{orderNo}</p>
          </div>

          {sum && sum.items.length > 0 && (
            <>
              <div className="divide-y divide-ash/60">
                {sum.items.map((it, i) => (
                  <div key={i} className="flex items-center gap-4 px-5 py-3.5">
                    <div className="w-12 h-14 bg-bone overflow-hidden shrink-0">
                      {it.image && <img src={it.image} alt="" className="w-full h-full object-cover mix-blend-multiply" />}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="font-display uppercase text-xs text-bone truncate">{it.name}</p>
                      <p className="text-bone/40 text-[11px] uppercase mt-0.5">
                        {it.size ? `Size ${it.size} · ` : ""}Qty {it.qty}
                      </p>
                    </div>
                    <span className="text-bone/80 text-sm whitespace-nowrap">{money(it.price * it.qty)}</span>
                  </div>
                ))}
              </div>
              <div className="px-5 py-4 border-t border-ash space-y-1.5 text-sm">
                {sum.discountCode && (
                  <div className="flex justify-between text-acid/80 text-xs">
                    <span>{sum.discountCode}</span><span>−{money(sum.discountAmount)}</span>
                  </div>
                )}
                <div className="flex justify-between font-display uppercase text-bone">
                  <span className="text-bone/50 text-xs tracking-[0.25em] self-center">{isPaid ? "Total paid" : "Total due"}</span>
                  <span className="text-acid text-lg">{money(sum.total)}</span>
                </div>
              </div>
            </>
          )}
        </motion.div>

        {/* ── what happens next ───────────────────────────────── */}
        {isPaid && (
          <motion.div
            initial={{ opacity: 0, y: 24 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.5, duration: 0.6, ease: EASE }}
            className="mt-10"
          >
            <p className="font-display uppercase text-[10px] tracking-[0.35em] text-bone/40 mb-5 text-center">What happens next</p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-px bg-ash border border-ash">
              {STEPS.map((s, i) => {
                const done = i <= activeStep;
                return (
                  <div key={s.key} className={`p-4 ${done ? "bg-acid/10" : "bg-ink"}`}>
                    <p className={`font-display text-[10px] uppercase tracking-[0.2em] ${done ? "text-acid" : "text-bone/30"}`}>
                      {done ? "●" : "○"} Step {i + 1}
                    </p>
                    <p className={`font-display uppercase text-sm mt-1.5 ${done ? "text-bone" : "text-bone/50"}`}>{s.label}</p>
                    <p className="text-bone/35 text-[11px] mt-0.5">{s.sub}</p>
                  </div>
                );
              })}
            </div>
            {sum?.maskedPhone && (
              <p className="text-bone/35 text-[11px] uppercase tracking-[0.2em] mt-4 text-center">
                We'll call <span className="text-bone/70">{sum.maskedPhone}</span> when the rider moves
              </p>
            )}
          </motion.div>
        )}

        {/* ── actions ─────────────────────────────────────────── */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 0.65, duration: 0.6 }}
          className="flex flex-wrap justify-center gap-4 mt-12"
        >
          <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
            Keep shopping →
          </Link>
          <Link to="/" className="btn-og border-2 border-bone text-bone px-8 py-4 text-sm hover:bg-bone hover:text-ink">
            Home
          </Link>
        </motion.div>

        {isPaid && (
          <p className="text-bone/25 text-[10px] uppercase tracking-[0.3em] text-center mt-10">
            Limited runs — no restocks. You caught this one.
          </p>
        )}
      </div>
    </div>
  );
}
