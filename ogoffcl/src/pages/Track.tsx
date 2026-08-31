import { useEffect, useState, FormEvent } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { motion } from "framer-motion";
import { money } from "../lib/money";
import { usePageMeta } from "../lib/seo";

interface Item { name: string; image: string | null; size: string | null; qty: number; price: number; }
interface Summary {
  orderNumber: string; status: string; paymentStatus: string; total: number;
  firstName: string | null; items: Item[];
}

const EASE = [0.22, 1, 0.36, 1] as const;

const STEPS = [
  { key: "confirmed", label: "Confirmed", sub: "Payment locked in" },
  { key: "processing", label: "Packing", sub: "Getting it boxed" },
  { key: "shipped", label: "Shipped", sub: "Handed to the courier" },
  { key: "out", label: "On the road", sub: "Rider is moving" },
  { key: "delivered", label: "Delivered", sub: "Wear it loud" },
];
const STEP_INDEX: Record<string, number> = {
  pending: 0, confirmed: 0, processing: 1, ready: 1,
  shipped: 2, out_for_delivery: 3, delivered: 4,
};

export default function Track() {
  const [params, setParams] = useSearchParams();
  const initial = params.get("id") || "";
  const [id, setId] = useState(initial);
  const [sum, setSum] = useState<Summary | null>(null);
  const [state, setState] = useState<"idle" | "loading" | "notfound">("idle");

  usePageMeta({
    title: "Track your order — OG OFFCL",
    description: "Track your OG OFFCL order status with your order number.",
    path: "/track",
    noindex: true,
  });

  const lookup = async (orderId: string) => {
    if (!orderId.trim()) return;
    setState("loading"); setSum(null);
    try {
      const r = await fetch(`/api/order/summary?ref=${encodeURIComponent(orderId.trim())}`);
      const j = r.ok ? await r.json() : null;
      if (j && j.orderNumber) { setSum(j); setState("idle"); }
      else setState("notfound");
    } catch { setState("notfound"); }
  };

  useEffect(() => { if (initial) lookup(initial); /* eslint-disable-next-line */ }, []);

  const submit = (e: FormEvent) => { e.preventDefault(); setParams(id.trim() ? { id: id.trim() } : {}); lookup(id); };

  const cancelled = sum?.status === "cancelled";
  const step = sum ? (STEP_INDEX[sum.status] ?? 0) : -1;

  return (
    <div className="max-w-2xl mx-auto px-4 py-16 sm:py-24">
      <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">Order tracking</p>
      <h1 className="display-xl text-5xl sm:text-6xl text-bone mb-3">Track<span className="text-acid">.</span></h1>
      <p className="text-bone/50 text-sm uppercase tracking-[0.2em] mb-8">Enter your order number — it's in your confirmation email.</p>

      <form onSubmit={submit} className="flex border-2 border-bone/20 focus-within:border-acid transition-colors mb-10">
        <input
          value={id} onChange={(e) => setId(e.target.value)} autoFocus={!initial}
          placeholder="OG-XXXXXXXX"
          className="flex-1 min-w-0 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.2em] text-sm text-bone placeholder:text-bone/25 focus:outline-none"
        />
        <button type="submit" className="btn-og bg-acid text-ink px-6 text-xs hover:bg-bone whitespace-nowrap">Track</button>
      </form>

      {state === "loading" && <p className="text-bone/40 text-xs uppercase tracking-[0.3em] animate-pulseSoft">Looking it up…</p>}
      {state === "notfound" && (
        <div className="border border-ash bg-smoke/50 px-5 py-8 text-center">
          <p className="font-display uppercase text-bone">No order found</p>
          <p className="text-bone/50 text-sm mt-2">Double-check the order number from your email. Still stuck? <a href="mailto:orders@ogoffcl.store" className="text-acid underline">Email us</a>.</p>
        </div>
      )}

      {sum && (
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, ease: EASE }}>
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border border-ash bg-smoke/60 px-5 py-4">
            <div>
              <p className="text-bone/40 text-[10px] uppercase tracking-[0.3em]">Order</p>
              <p className="font-display text-lg text-acid break-all">{sum.orderNumber}</p>
            </div>
            <span className={`text-[10px] uppercase tracking-widest px-3 py-1.5 ${cancelled ? "bg-blood/15 text-blood" : sum.paymentStatus === "paid" ? "bg-acid/15 text-acid" : "bg-bone/10 text-bone/60"}`}>
              {cancelled ? "Cancelled" : sum.status.replace(/_/g, " ")}
            </span>
          </div>

          {cancelled ? (
            <div className="border border-blood/30 bg-blood/5 px-5 py-8 mt-4 text-center">
              <p className="font-display uppercase text-blood">This order was cancelled</p>
              <p className="text-bone/50 text-sm mt-2">If that's unexpected, reply to your order email and we'll sort it out.</p>
            </div>
          ) : (
            <div className="mt-6">
              <div className="grid grid-cols-5 gap-px bg-ash border border-ash">
                {STEPS.map((s, i) => {
                  const done = i <= step;
                  return (
                    <div key={s.key} className={`p-3 sm:p-4 ${done ? "bg-acid/10" : "bg-ink"}`}>
                      <p className={`font-display text-[9px] sm:text-[10px] uppercase tracking-[0.15em] ${done ? "text-acid" : "text-bone/30"}`}>{done ? "●" : "○"} {i + 1}</p>
                      <p className={`font-display uppercase text-[11px] sm:text-sm mt-1.5 leading-tight ${done ? "text-bone" : "text-bone/40"}`}>{s.label}</p>
                      <p className="text-bone/30 text-[9px] sm:text-[11px] mt-0.5 leading-tight hidden sm:block">{s.sub}</p>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {sum.items.length > 0 && (
            <div className="border border-ash bg-smoke/40 mt-6 divide-y divide-ash/60">
              {sum.items.map((it, i) => (
                <div key={i} className="flex items-center gap-4 px-5 py-3.5">
                  <div className="w-12 h-14 bg-bone overflow-hidden shrink-0">
                    {it.image && <img src={it.image} alt="" className="w-full h-full object-cover mix-blend-multiply" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-display uppercase text-xs text-bone truncate">{it.name}</p>
                    <p className="text-bone/40 text-[11px] uppercase mt-0.5">{it.size ? `Size ${it.size} · ` : ""}Qty {it.qty}</p>
                  </div>
                  <span className="text-bone/80 text-sm whitespace-nowrap">{money(it.price * it.qty)}</span>
                </div>
              ))}
            </div>
          )}

          <div className="flex flex-wrap gap-4 mt-8">
            <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">Keep shopping →</Link>
            <Link to="/authentic-check" className="btn-og border-2 border-bone text-bone px-8 py-4 text-sm hover:bg-bone hover:text-ink">Verify authenticity</Link>
          </div>
        </motion.div>
      )}
    </div>
  );
}
