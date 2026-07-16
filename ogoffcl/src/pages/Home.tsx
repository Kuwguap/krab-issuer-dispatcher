import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { supabase, publicImageUrl } from "../lib/supabase";
import type { Product } from "../lib/types";
import Marquee from "../components/Marquee";
import Reveal from "../components/Reveal";
import ProductCard from "../components/ProductCard";

const HERO_WORDS = ["ORIGINAL", "GANGSTER", "OFFICIAL"];

export default function Home() {
  const [featured, setFeatured] = useState<Product[]>([]);
  const [heroImgs, setHeroImgs] = useState<string[]>([]);

  useEffect(() => {
    supabase
      .from("products")
      .select("*")
      .eq("is_active", true)
      .order("created_at", { ascending: false })
      .limit(8)
      .then(({ data }) => {
        const rows = (data || []) as Product[];
        setFeatured(rows);
        setHeroImgs(
          rows
            .map((p) => publicImageUrl(String(p.image || (p.images || [])[0] || "")))
            .filter(Boolean)
            .slice(0, 3),
        );
      });
  }, []);

  return (
    <div>
      <Marquee fast />

      {/* HERO */}
      <section className="relative min-h-[88vh] flex flex-col justify-center overflow-hidden">
        {/* floating product cutouts */}
        <div className="absolute inset-0 pointer-events-none">
          {heroImgs.map((src, i) => (
            <motion.div
              key={i}
              className={`absolute w-40 sm:w-56 md:w-72 aspect-[4/5] border border-ash overflow-hidden ${
                i === 0
                  ? "right-[4%] top-[10%] rotate-[4deg]"
                  : i === 1
                    ? "right-[22%] bottom-[6%] -rotate-[5deg] hidden sm:block"
                    : "left-[2%] bottom-[12%] rotate-[7deg] hidden lg:block"
              }`}
              initial={{ opacity: 0, y: 60, rotate: 0 }}
              animate={{ opacity: 0.9, y: 0 }}
              transition={{ delay: 0.5 + i * 0.18, duration: 0.9, ease: [0.22, 1, 0.36, 1] }}
            >
              <div className="animate-floaty w-full h-full bg-bone" style={{ animationDelay: `${i * 0.8}s` }}>
                <img src={src} alt="" className="w-full h-full object-cover mix-blend-multiply" />
              </div>
            </motion.div>
          ))}
        </div>

        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 w-full py-20">
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 0.2 }}
            className="font-display uppercase text-acid tracking-[0.4em] text-xs sm:text-sm mb-6"
          >
            Accra → The World
          </motion.p>

          <h1 className="display-xl text-[17vw] sm:text-[13vw] lg:text-[9.5rem]">
            {HERO_WORDS.map((w, i) => (
              <span key={w} className="block overflow-hidden">
                <motion.span
                  className={`block ${i === 1 ? "text-stroke" : "text-bone"}`}
                  initial={{ y: "110%" }}
                  animate={{ y: 0 }}
                  transition={{ delay: 0.25 + i * 0.13, duration: 0.8, ease: [0.22, 1, 0.36, 1] }}
                >
                  {w}
                </motion.span>
              </span>
            ))}
          </h1>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.9, duration: 0.6 }}
            className="mt-10 flex flex-wrap items-center gap-4"
          >
            <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
              Shop the drop →
            </Link>
            <Link to="/shop?c=og-femme" className="btn-og border-2 border-bone text-bone px-8 py-4 text-sm hover:bg-bone hover:text-ink">
              OG Femme
            </Link>
          </motion.div>
        </div>

        {/* scroll cue */}
        <motion.div
          className="absolute bottom-6 left-1/2 -translate-x-1/2 text-bone/40 text-[10px] uppercase tracking-[0.4em]"
          animate={{ y: [0, 8, 0] }}
          transition={{ repeat: Infinity, duration: 1.8 }}
        >
          Scroll ↓
        </motion.div>
      </section>

      <Marquee text="NEW DROP — LIMITED RUN — NO RESTOCKS — NEW DROP — LIMITED RUN — NO RESTOCKS — " className="bg-bone text-ink" />

      {/* LATEST DROP */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 py-20">
        <Reveal>
          <div className="flex items-end justify-between gap-6 mb-10">
            <h2 className="display-xl text-5xl sm:text-7xl text-bone">
              Latest<br /><span className="text-stroke-acid">Drop</span>
            </h2>
            <Link to="/shop" className="link-sweep font-display uppercase text-xs tracking-[0.25em] text-bone/70 hover:text-bone whitespace-nowrap pb-2">
              View all →
            </Link>
          </div>
        </Reveal>

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 sm:gap-6">
          {featured.map((p, i) => (
            <ProductCard key={p.id} p={p} index={i} />
          ))}
          {featured.length === 0 &&
            [...Array(4)].map((_, i) => (
              <div key={i} className="aspect-[4/5] bg-smoke border border-ash animate-pulseSoft" />
            ))}
        </div>
      </section>

      {/* STATEMENT */}
      <section className="border-y border-ash bg-smoke/50 overflow-hidden">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-24 grid md:grid-cols-2 gap-12 items-center">
          <Reveal>
            <h2 className="display-xl text-5xl sm:text-6xl text-bone">
              Not a brand.<br />
              <span className="text-acid">A statement.</span>
            </h2>
          </Reveal>
          <Reveal delay={0.15}>
            <p className="text-bone/60 leading-relaxed text-lg">
              OG OFFCL is streetwear with no apologies — heavyweight fabric, loud prints, limited runs.
              Every piece is made to be seen. When it's gone, it's gone.
            </p>
            <div className="flex gap-10 mt-8">
              {[
                ["100%", "Heavyweight cotton"],
                ["LTD", "Limited runs only"],
                ["GH", "Born in Accra"],
              ].map(([big, small]) => (
                <div key={small}>
                  <p className="font-display text-3xl text-acid">{big}</p>
                  <p className="text-bone/40 text-xs uppercase tracking-widest mt-1">{small}</p>
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </section>

      {/* CATEGORY SPLIT */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 py-20 grid sm:grid-cols-2 gap-6">
        {[
          { label: "OG", sub: "The mainline", to: "/shop?c=og" },
          { label: "OG Femme", sub: "For her", to: "/shop?c=og-femme" },
        ].map((c, i) => (
          <Reveal key={c.label} delay={i * 0.1}>
            <Link
              to={c.to}
              className="group relative block aspect-[16/10] bg-smoke border border-ash overflow-hidden hover:border-acid transition-colors"
            >
              <div className="absolute inset-0 flex items-center justify-center">
                <span className="display-xl text-[18vw] sm:text-[7rem] text-bone/10 group-hover:text-acid/20 transition-colors duration-500 whitespace-nowrap">
                  {c.label}
                </span>
              </div>
              <div className="absolute bottom-0 left-0 right-0 p-6 flex items-end justify-between">
                <div>
                  <p className="display-xl text-3xl text-bone group-hover:text-acid transition-colors">{c.label}</p>
                  <p className="text-bone/50 text-xs uppercase tracking-[0.25em] mt-1">{c.sub}</p>
                </div>
                <span className="btn-og border-2 border-bone text-bone text-xs px-4 py-2 group-hover:bg-acid group-hover:border-acid group-hover:text-ink">
                  Shop →
                </span>
              </div>
            </Link>
          </Reveal>
        ))}
      </section>
    </div>
  );
}
