import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { motion } from "framer-motion";
import { supabase, publicImageUrl } from "../lib/supabase";
import type { Product } from "../lib/types";
import { money } from "../lib/money";
import { useCart } from "../store/CartContext";
import ProductCard from "../components/ProductCard";
import { usePageMeta, useJsonLd, SITE_URL } from "../lib/seo";

interface VariantRow {
  id: string;
  variant_type: string | null;
  variant_value: string | null;
  stock: number | null;
  is_available: boolean | null;
  price_adjustment: number | null;
}

export default function ProductPage() {
  const { id } = useParams();
  const { add } = useCart();
  const [p, setP] = useState<Product | null>(null);
  const [variants, setVariants] = useState<VariantRow[]>([]);
  const [related, setRelated] = useState<Product[]>([]);
  const [imgIdx, setImgIdx] = useState(0);
  const [size, setSize] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [added, setAdded] = useState(false);

  useEffect(() => {
    setP(null); setSize(null); setImgIdx(0); setMissing(false);
    if (!id) return;
    supabase.from("products").select("*").eq("id", id).single().then(({ data }) => setP((data as Product) || null));
    supabase.from("product_variants").select("*").eq("product_id", id).then(({ data }) => setVariants((data || []) as VariantRow[]));
  }, [id]);

  useEffect(() => {
    if (!p) return;
    supabase
      .from("products")
      .select("*")
      .eq("is_active", true)
      .neq("id", p.id)
      .eq("category_id", p.category_id)
      .limit(4)
      .then(({ data }) => setRelated((data || []) as Product[]));
    window.scrollTo({ top: 0 });
  }, [p?.id]);

  const sizes = useMemo(() => {
    const fromVariants = variants
      .filter((v) => (v.variant_type || "").toLowerCase() === "size" && v.is_available !== false)
      .map((v) => String(v.variant_value || "").toUpperCase())
      .filter(Boolean);
    const fromProduct = Array.isArray(p?.sizes) ? p!.sizes!.map((s) => String(s).toUpperCase()) : [];
    return [...new Set([...fromVariants, ...fromProduct])];
  }, [variants, p]);

  // SEO: per-product title/description/OG + schema.org Product rich result
  usePageMeta({
    title: p ? `${p.name} — OG OFFCL | ${money(Number(p.price))}` : "OG OFFCL — Original Gangster Official",
    description: p
      ? `${p.name} by OG OFFCL — ${String(p.description || "bold streetwear born in Accra").slice(0, 150)}. ${money(Number(p.price))}, mobile money accepted.`
      : "Bold streetwear born in Accra.",
    path: id ? `/product/${id}` : undefined,
    image: p ? publicImageUrl(String(p.image || (p.images || [])[0] || "")) : undefined,
    type: "product",
  });
  useJsonLd(
    "product",
    p
      ? {
          "@context": "https://schema.org",
          "@type": "Product",
          name: p.name,
          image: [p.image, ...(p.images || [])].filter(Boolean).map((x) => publicImageUrl(String(x))),
          description: String(p.description || `${p.name} — bold streetwear by OG OFFCL, born in Accra.`),
          brand: { "@type": "Brand", name: "OG OFFCL" },
          offers: {
            "@type": "Offer",
            url: `${SITE_URL}/product/${p.id}`,
            price: Number(p.price),
            priceCurrency: "GHS",
            availability:
              (p.stock ?? 0) <= 0 && p.stock !== null
                ? "https://schema.org/OutOfStock"
                : "https://schema.org/InStock",
          },
        }
      : null,
  );

  if (!p) {
    return (
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-20 grid lg:grid-cols-2 gap-10">
        <div className="aspect-[4/5] bg-smoke border border-ash animate-pulseSoft" />
        <div className="space-y-5 pt-8">
          <div className="h-12 w-3/4 bg-smoke animate-pulseSoft" />
          <div className="h-6 w-1/3 bg-smoke animate-pulseSoft" />
          <div className="h-32 w-full bg-smoke animate-pulseSoft" />
        </div>
      </div>
    );
  }

  const imgs = [p.image, ...(p.images || [])].filter(Boolean).map((x) => publicImageUrl(String(x)));
  const uniqueImgs = [...new Set(imgs)];
  const soldOut = (p.stock ?? 0) <= 0 && p.stock !== null;

  const handleAdd = () => {
    if (sizes.length > 0 && !size) {
      setMissing(true);
      return;
    }
    add({ productId: p.id, name: p.name, price: Number(p.price), image: uniqueImgs[0] || "", size });
    setAdded(true);
    setTimeout(() => setAdded(false), 1600);
  };

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10">
      <nav className="text-bone/40 text-xs uppercase tracking-[0.25em] mb-8">
        <Link to="/shop" className="hover:text-acid">Shop</Link>
        <span className="mx-2">/</span>
        <span className="text-bone/70">{p.product_category || "Piece"}</span>
      </nav>

      <div className="grid lg:grid-cols-2 gap-10 lg:gap-16">
        {/* gallery */}
        <div>
          <motion.div
            key={imgIdx}
            initial={{ opacity: 0.4, scale: 1.015 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ duration: 0.35 }}
            className="relative aspect-[4/5] bg-bone border border-ash overflow-hidden"
          >
            {uniqueImgs[imgIdx] ? (
              <img src={uniqueImgs[imgIdx]} alt={p.name} className="w-full h-full object-cover mix-blend-multiply" />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center font-display text-ink/10 text-7xl">OG</div>
            )}
            {soldOut && (
              <span className="absolute top-4 right-4 bg-blood text-bone text-xs font-display uppercase tracking-widest px-3 py-2">Sold out</span>
            )}
          </motion.div>
          {uniqueImgs.length > 1 && (
            <div className="flex gap-3 mt-3 overflow-x-auto pb-1">
              {uniqueImgs.map((src, i) => (
                <button
                  key={i}
                  onClick={() => setImgIdx(i)}
                  className={`w-20 aspect-[4/5] shrink-0 overflow-hidden border-2 bg-bone transition-colors ${i === imgIdx ? "border-acid" : "border-ash hover:border-bone/50"}`}
                >
                  <img src={src} alt="" className="w-full h-full object-cover mix-blend-multiply" />
                </button>
              ))}
            </div>
          )}
        </div>

        {/* info */}
        <div className="lg:pt-4">
          {p.product_category && (
            <p className="font-display uppercase text-acid tracking-[0.35em] text-xs mb-4">{p.product_category}</p>
          )}
          <h1 className="display-xl text-4xl sm:text-5xl text-bone">{p.name}</h1>
          <p className="text-2xl text-bone mt-5">{money(p.price)}</p>

          {p.description && (
            <p className="text-bone/60 leading-relaxed mt-6 whitespace-pre-line">{p.description}</p>
          )}

          {sizes.length > 0 && (
            <div className="mt-8">
              <div className="flex items-center justify-between mb-3">
                <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/60">Select size</p>
                {missing && <p className="text-blood text-xs uppercase tracking-widest">Pick a size first</p>}
              </div>
              <div className="flex flex-wrap gap-2">
                {sizes.map((s) => (
                  <button
                    key={s}
                    onClick={() => { setSize(s); setMissing(false); }}
                    className={`btn-og min-w-[3.2rem] px-4 py-3 text-sm border-2 ${size === s ? "bg-acid border-acid text-ink" : "border-ash text-bone hover:border-bone"}`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="mt-10 flex flex-col sm:flex-row gap-3">
            <button
              onClick={handleAdd}
              disabled={soldOut}
              className={`btn-og flex-1 py-5 text-sm ${
                soldOut
                  ? "bg-ash text-bone/30 cursor-not-allowed"
                  : added
                    ? "bg-bone text-ink"
                    : "bg-acid text-ink hover:bg-bone"
              }`}
            >
              {soldOut ? "Sold out" : added ? "✓ Added to cart" : "Add to cart"}
            </button>
            <Link to="/checkout" onClick={soldOut ? (e) => e.preventDefault() : handleAdd} className={`btn-og border-2 px-8 py-5 text-sm ${soldOut ? "border-ash text-bone/30 pointer-events-none" : "border-bone text-bone hover:bg-bone hover:text-ink"}`}>
              Buy now
            </Link>
          </div>

          <div className="mt-10 border-t border-ash pt-6 space-y-2 text-bone/40 text-xs uppercase tracking-[0.2em]">
            <p>— Heavyweight quality, limited run</p>
            <p>— Ships from Accra · 1–3 days in Ghana</p>
            <p>— Secure Paystack checkout (GHS)</p>
          </div>
        </div>
      </div>

      {related.length > 0 && (
        <section className="mt-24">
          <h2 className="display-xl text-4xl sm:text-5xl text-bone mb-8">
            More <span className="text-stroke-acid">heat</span>
          </h2>
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
            {related.map((rp, i) => (
              <ProductCard key={rp.id} p={rp} index={i} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
