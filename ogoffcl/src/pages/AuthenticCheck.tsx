import { useEffect, useState, FormEvent } from "react";
import { useSearchParams, Link } from "react-router-dom";
import { motion } from "framer-motion";
import { usePageMeta } from "../lib/seo";

interface Product { name: string; image: string | null; size: string | null; qty: number; }
interface Result { authentic: boolean; orderNumber?: string; purchasedOn?: string; products?: Product[]; }

const EASE = [0.22, 1, 0.36, 1] as const;

export default function AuthenticCheck() {
  const [params, setParams] = useSearchParams();
  const initial = params.get("code") || "";
  const [code, setCode] = useState(initial);
  const [res, setRes] = useState<Result | null>(null);
  const [state, setState] = useState<"idle" | "loading">("idle");

  usePageMeta({
    title: "Authenticity check — OG OFFCL",
    description: "Verify that your OG OFFCL piece is genuine. Enter your authenticity code or scan the QR on your certificate.",
    path: "/authentic-check",
    noindex: true,
  });

  const check = async (c: string) => {
    if (!c.trim()) return;
    setState("loading"); setRes(null);
    try {
      const r = await fetch(`/api/order/authentic?code=${encodeURIComponent(c.trim())}`);
      const j = r.ok ? await r.json() : { authentic: false };
      setRes(j);
    } catch { setRes({ authentic: false }); }
    setState("idle");
  };

  useEffect(() => { if (initial) check(initial); /* eslint-disable-next-line */ }, []);

  const submit = (e: FormEvent) => { e.preventDefault(); setParams(code.trim() ? { code: code.trim() } : {}); check(code); };

  return (
    <div className="max-w-2xl mx-auto px-4 py-16 sm:py-24">
      <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">Authenticity check</p>
      <h1 className="display-xl text-5xl sm:text-6xl text-bone mb-3">Real<span className="text-stroke-acid">?</span></h1>
      <p className="text-bone/50 text-sm uppercase tracking-[0.2em] mb-8">Scan the QR on your certificate, or enter the authenticity code.</p>

      <form onSubmit={submit} className="flex border-2 border-bone/20 focus-within:border-acid transition-colors mb-10">
        <input
          value={code} onChange={(e) => setCode(e.target.value)} autoFocus={!initial}
          placeholder="OGA-XXXXXXXX"
          className="flex-1 min-w-0 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.2em] text-sm text-bone placeholder:text-bone/25 focus:outline-none"
        />
        <button type="submit" className="btn-og bg-acid text-ink px-6 text-xs hover:bg-bone whitespace-nowrap">Verify</button>
      </form>

      {state === "loading" && <p className="text-bone/40 text-xs uppercase tracking-[0.3em] animate-pulseSoft">Verifying…</p>}

      {res && !res.authentic && (
        <motion.div initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }} className="border-2 border-blood/50 bg-blood/5 px-6 py-10 text-center">
          <p className="display-xl text-4xl text-blood">Not verified</p>
          <p className="text-bone/60 text-sm mt-4 max-w-md mx-auto leading-relaxed">
            We couldn't match this code to a genuine OG OFFCL order. Check the code and try again — and if you bought this as authentic, <a href="mailto:orders@ogoffcl.store" className="text-acid underline">let us know</a>.
          </p>
        </motion.div>
      )}

      {res && res.authentic && (
        <motion.div initial={{ opacity: 0, y: 18 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, ease: EASE }}>
          <div className="relative border-2 border-acid bg-acid/5 px-6 py-8 text-center overflow-hidden">
            <motion.div initial={{ opacity: 0.7, scale: 1 }} animate={{ opacity: 0, scale: 1.5 }} transition={{ duration: 1.1, ease: "easeOut", delay: 0.2 }} className="absolute left-1/2 top-8 -translate-x-1/2 w-20 h-20 border-2 border-acid rounded-full" />
            <div className="relative w-20 h-20 mx-auto border-4 border-acid rounded-full flex items-center justify-center text-acid font-display text-4xl mb-5">✓</div>
            <p className="display-xl text-4xl sm:text-5xl text-bone">Authentic.</p>
            <p className="text-acid text-xs uppercase tracking-[0.3em] mt-3">Genuine OG OFFCL piece</p>
            {res.purchasedOn && (
              <p className="text-bone/40 text-[11px] uppercase tracking-[0.25em] mt-4">
                Order {res.orderNumber} · Purchased {new Date(res.purchasedOn).toLocaleDateString()}
              </p>
            )}
          </div>

          {res.products && res.products.length > 0 && (
            <div className="border border-ash bg-smoke/40 mt-6 divide-y divide-ash/60">
              {res.products.map((p, i) => (
                <div key={i} className="flex items-center gap-4 px-5 py-3.5">
                  <div className="w-12 h-14 bg-bone overflow-hidden shrink-0">
                    {p.image && <img src={p.image} alt="" className="w-full h-full object-cover mix-blend-multiply" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-display uppercase text-xs text-bone truncate">{p.name}</p>
                    <p className="text-bone/40 text-[11px] uppercase mt-0.5">{p.size ? `Size ${p.size} · ` : ""}Qty {p.qty}</p>
                  </div>
                  <span className="text-acid text-[10px] uppercase tracking-widest font-display">Verified</span>
                </div>
              ))}
            </div>
          )}
          <p className="text-bone/30 text-[11px] text-center mt-6 uppercase tracking-[0.2em]">Original Gangster Official · Accra, Ghana</p>
          <div className="text-center mt-6"><Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone inline-flex">Shop the drop →</Link></div>
        </motion.div>
      )}
    </div>
  );
}
