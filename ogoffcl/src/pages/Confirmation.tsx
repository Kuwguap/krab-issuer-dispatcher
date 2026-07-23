import { Link, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import Marquee from "../components/Marquee";

export default function Confirmation() {
  const [params] = useSearchParams();
  const ref = params.get("ref") || "—";
  const paid = params.get("paid") === "1";

  return (
    <div>
      {paid && <Marquee text="ORDER CONFIRMED — YOU'RE OFFICIAL — ORDER CONFIRMED — YOU'RE OFFICIAL — " fast />}
      <div className="max-w-2xl mx-auto px-4 py-24 text-center">
        <motion.div initial={{ scale: 0.8, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}>
          <div className={`w-24 h-24 mx-auto flex items-center justify-center border-4 ${paid ? "border-acid text-acid" : "border-bone/30 text-bone/40"} font-display text-5xl mb-8`}>
            {paid ? "✓" : "…"}
          </div>
        </motion.div>
        <h1 className="display-xl text-5xl sm:text-6xl text-bone">
          {paid ? (
            <>You're <span className="text-acid">official.</span></>
          ) : (
            <>Payment <span className="text-stroke">pending</span></>
          )}
        </h1>
        <p className="text-bone/60 mt-6 leading-relaxed">
          {paid
            ? "Payment received. Your order is confirmed and we're getting it packed. You'll hear from us on the phone number you dropped."
            : "Your order was saved but the payment window closed before completing. Reach us with your order number and we'll sort you out — or try checkout again."}
        </p>
        <div className="inline-block max-w-full border border-ash bg-smoke px-5 sm:px-8 py-4 mt-8">
          <p className="text-bone/40 text-[11px] uppercase tracking-[0.3em]">Order number</p>
          {/* break-all: the pending path carries the full payment ref, which is
              longer than the mobile content box and otherwise wraps at hyphens */}
          <p className="font-display text-xl sm:text-2xl text-acid mt-1 break-all">{ref}</p>
        </div>
        <div className="flex flex-wrap justify-center gap-4 mt-12">
          <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
            Keep shopping →
          </Link>
          <Link to="/" className="btn-og border-2 border-bone text-bone px-8 py-4 text-sm hover:bg-bone hover:text-ink">
            Home
          </Link>
        </div>
      </div>
    </div>
  );
}
