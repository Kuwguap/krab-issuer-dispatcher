import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import type { Product } from "../lib/types";
import { money } from "../lib/money";
import { publicImageUrl } from "../lib/supabase";
import { useCart } from "../store/CartContext";

export default function ProductCard({ p, index = 0 }: { p: Product; index?: number }) {
  const { add } = useCart();
  const imgs = [p.image, ...(p.images || [])].filter(Boolean).map((x) => publicImageUrl(String(x)));
  const primary = imgs[0] || "";
  const secondary = imgs[1] || imgs[0] || "";
  const soldOut = (p.stock ?? 0) <= 0 && p.stock !== null;
  const hasSizes = Array.isArray(p.sizes) && p.sizes.length > 0;

  return (
    <motion.div
      initial={{ opacity: 0, y: 26 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: "-40px" }}
      transition={{ duration: 0.55, delay: (index % 4) * 0.06, ease: [0.22, 1, 0.36, 1] }}
      className="group relative"
    >
      <Link to={`/product/${p.id}`} className="block">
        <div className="relative aspect-[4/5] bg-bone overflow-hidden border border-ash group-hover:border-acid/60 transition-colors">
          {primary ? (
            <>
              <img
                src={primary}
                alt={p.name}
                loading="lazy"
                className="absolute inset-0 w-full h-full object-cover mix-blend-multiply transition-all duration-700 ease-out group-hover:scale-[1.06]"
              />
              {secondary && secondary !== primary && (
                <img
                  src={secondary}
                  alt=""
                  loading="lazy"
                  className="absolute inset-0 w-full h-full object-cover mix-blend-multiply opacity-0 group-hover:opacity-100 transition-opacity duration-500"
                />
              )}
            </>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center font-display text-ink/10 text-5xl">OG</div>
          )}

          {p.product_category && (
            <span className="absolute top-3 left-3 bg-ink/85 text-bone text-[10px] font-display uppercase tracking-[0.18em] px-2.5 py-1.5">
              {p.product_category}
            </span>
          )}
          {soldOut && (
            <span className="absolute top-3 right-3 bg-blood text-bone text-[10px] font-display uppercase tracking-[0.18em] px-2.5 py-1.5">
              Sold out
            </span>
          )}

          {/* quick add */}
          {!soldOut && (
            <button
              onClick={(e) => {
                e.preventDefault();
                if (hasSizes) {
                  window.location.href = `/product/${p.id}`;
                  return;
                }
                add({ productId: p.id, name: p.name, price: Number(p.price), image: primary, size: null });
              }}
              className="btn-og absolute bottom-0 left-0 right-0 bg-acid text-ink py-3.5 text-xs translate-y-full group-hover:translate-y-0 transition-transform duration-300"
            >
              {hasSizes ? "Pick size →" : "+ Quick add"}
            </button>
          )}
        </div>

        <div className="pt-3 flex flex-wrap items-start justify-between gap-x-3 gap-y-1">
          <h3 className="font-display uppercase text-[13px] leading-snug text-bone group-hover:text-acid transition-colors min-w-0 break-words">
            {p.name}
          </h3>
          <span className="text-bone/90 text-sm whitespace-nowrap">{money(p.price)}</span>
        </div>
      </Link>
    </motion.div>
  );
}
