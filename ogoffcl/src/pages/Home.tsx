import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { motion, useReducedMotion, useScroll, useTransform } from "framer-motion";
import { supabase, publicImageUrl } from "../lib/supabase";
import type { Category, Product } from "../lib/types";
import { money } from "../lib/money";
import Marquee from "../components/Marquee";
import Reveal from "../components/Reveal";
import ProductCard from "../components/ProductCard";
import { usePageMeta } from "../lib/seo";

const EASE = [0.22, 1, 0.36, 1] as const;

function firstImage(p: Product): string {
  return publicImageUrl(String(p.image || (p.images || [])[0] || ""));
}

/** Live Accra time — small editorial detail in the hero meta bar. */
function useAccraClock(): string {
  const [t, setT] = useState("");
  useEffect(() => {
    const fmt = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", timeZone: "Africa/Accra",
    });
    const tick = () => setT(fmt.format(new Date()));
    tick();
    const id = setInterval(tick, 15_000);
    return () => clearInterval(id);
  }, []);
  return t;
}

/** One vertical lookbook column for the runway strip. */
function StripCol({ imgs, reverse = false }: { imgs: string[]; reverse?: boolean }) {
  if (imgs.length === 0) return null;
  const loop = [...imgs, ...imgs];
  return (
    <div className="relative h-full overflow-hidden flex-1">
      <div className={`strip-col ${reverse ? "rev" : ""}`}>
        {loop.map((src, i) => (
          <div key={i} className="aspect-[4/5] bg-bone shrink-0 overflow-hidden">
            <img src={src} alt="" loading={i < 4 ? "eager" : "lazy"} className="w-full h-full object-cover mix-blend-multiply" />
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Home() {
  const [products, setProducts] = useState<Product[]>([]);
  const [cats, setCats] = useState<Category[]>([]);
  const clock = useAccraClock();

  usePageMeta({
    title: "OG OFFCL — Original Gangster Official | Streetwear from Accra, Ghana",
    description:
      "Bold streetwear born in Accra: heavyweight tees, Gye Nyame jerseys, hoodies and the OG Femme line. Limited runs, no restocks. Pay with MTN, Telecel or AT mobile money.",
    path: "/",
  });
  const reduced = useReducedMotion();

  const heroRef = useRef<HTMLElement>(null);
  const { scrollYProgress } = useScroll({ target: heroRef, offset: ["start start", "end start"] });
  const headY = useTransform(scrollYProgress, [0, 1], [0, reduced ? 0 : -90]);
  const stripY = useTransform(scrollYProgress, [0, 1], [0, reduced ? 0 : 60]);

  const railRef = useRef<HTMLDivElement>(null);
  const [railProgress, setRailProgress] = useState(0);

  useEffect(() => {
    supabase.from("products").select("*").eq("is_active", true)
      .order("created_at", { ascending: false })
      .then(({ data }) => setProducts((data || []) as Product[]));
    supabase.from("categories").select("*").then(({ data }) => setCats((data || []) as Category[]));
  }, []);

  const stripImgs = useMemo(() => {
    const urls = [...new Set(products.map(firstImage).filter(Boolean))];
    return [urls.filter((_, i) => i % 2 === 0).slice(0, 6), urls.filter((_, i) => i % 2 === 1).slice(0, 6)];
  }, [products]);

  const rail = products.slice(0, 10);
  const jerseys = useMemo(
    () => products.filter((p) => /gye\s*nyame/i.test(p.name)).slice(0, 5),
    [products],
  );

  const catBySlug = (slug: string) => cats.find((c) => c.slug === slug);
  const previewFor = (slug: string | null) => {
    if (!slug) return firstImage(products[0] || ({} as Product));
    const c = catBySlug(slug);
    const p = products.find((x) => x.category_id === c?.id);
    return p ? firstImage(p) : "";
  };

  const indexRows: { label: string; sub: string; to: string; slug: string | null }[] = [
    { label: "OG", sub: "The mainline", to: "/shop?c=og", slug: "og" },
    { label: "OG Femme", sub: "For her", to: "/shop?c=og-femme", slug: "og-femme" },
    { label: "All pieces", sub: `${products.length || "—"} on the rack`, to: "/shop", slug: null },
  ];

  const onRailScroll = () => {
    const el = railRef.current;
    if (!el) return;
    const max = el.scrollWidth - el.clientWidth;
    setRailProgress(max > 0 ? el.scrollLeft / max : 0);
  };

  return (
    <div>
      {/* ── HERO — runway strip ─────────────────────────────────── */}
      <section ref={heroRef} className="relative h-[92svh] min-h-[560px] overflow-hidden border-b border-ash">
        {/* lookbook columns */}
        <motion.div
          style={{ y: stripY }}
          className="absolute inset-y-[-6%] right-0 w-[62%] sm:w-[46%] lg:w-[42%] max-w-[620px] flex gap-3 opacity-40 sm:opacity-100"
          aria-hidden
        >
          <StripCol imgs={stripImgs[0]} />
          <StripCol imgs={stripImgs[1]} reverse />
        </motion.div>
        {/* legibility gradient over the strip on small screens */}
        <div className="absolute inset-0 bg-gradient-to-r from-ink via-ink/85 to-transparent sm:via-ink/40 pointer-events-none" />

        {/* headline */}
        <motion.div style={{ y: headY }} className="relative z-10 h-full max-w-7xl mx-auto px-4 sm:px-6 flex flex-col justify-center">
          <motion.p
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 0.15, duration: 0.6, ease: EASE }}
            className="font-display uppercase text-acid tracking-[0.45em] text-[11px] sm:text-xs mb-7"
          >
            Streetwear · Accra, Ghana
          </motion.p>

          <h1 className="display-xl text-[15vw] sm:text-[12vw] lg:text-[8.6rem]">
            {["Original", "Gangster", "Official"].map((w, i) => (
              <span key={w} className="block overflow-hidden pb-[0.06em] -mb-[0.06em]">
                <motion.span
                  className={`block ${i === 1 ? "text-stroke" : "text-bone"}`}
                  initial={{ y: "112%", rotate: 2.5 }}
                  animate={{ y: 0, rotate: 0 }}
                  transition={{ delay: 0.28 + i * 0.12, duration: 0.9, ease: EASE }}
                >
                  {w}
                </motion.span>
              </span>
            ))}
          </h1>

          <motion.div
            initial={{ scaleX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{ delay: 0.85, duration: 0.8, ease: EASE }}
            className="origin-left h-px bg-bone/25 w-full max-w-md my-8"
          />

          <motion.div
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ delay: 1, duration: 0.6, ease: EASE }}
            className="flex flex-wrap items-center gap-4"
          >
            <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
              Shop the drop →
            </Link>
            <Link to="/shop?c=og-femme" className="link-sweep font-display uppercase text-xs tracking-[0.3em] text-bone/80 hover:text-bone py-3">
              OG Femme
            </Link>
          </motion.div>
        </motion.div>

        {/* meta bar */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ delay: 1.3, duration: 0.8 }}
          className="absolute bottom-0 inset-x-0 z-10 border-t border-ash bg-ink/70 backdrop-blur-sm"
        >
          <div className="max-w-7xl mx-auto px-4 sm:px-6 h-11 flex items-center justify-between text-[9px] tracking-[0.2em] sm:text-[11px] sm:tracking-[0.3em] uppercase text-bone/50">
            <span>Accra {clock || "—"} GMT</span>
            <span className="hidden sm:block">{products.length || "—"} pieces live</span>
            <span className="text-acid">Limited runs — no restocks</span>
          </div>
        </motion.div>
      </section>

      <Marquee text="OG OFFCL — NEW DROP LIVE — WEAR THE CULTURE — OG OFFCL — NEW DROP LIVE — WEAR THE CULTURE — " />

      {/* ── THE RACK — horizontal lookbook rail ─────────────────── */}
      <section className="py-20 overflow-hidden">
        <div className="max-w-7xl mx-auto px-4 sm:px-6">
          <Reveal>
            <div className="flex items-end justify-between gap-6 mb-2">
              <h2 className="display-xl text-4xl sm:text-7xl text-bone">
                The <span className="text-stroke-acid">Rack</span>
              </h2>
              <Link to="/shop" className="link-sweep font-display uppercase text-xs tracking-[0.25em] text-bone/70 hover:text-bone whitespace-nowrap pb-2">
                View all →
              </Link>
            </div>
            <p className="text-bone/40 text-[11px] uppercase tracking-[0.3em] mb-8">Latest drop — drag through the looks</p>
          </Reveal>
        </div>

        <div
          ref={railRef}
          onScroll={onRailScroll}
          className="no-scrollbar flex gap-4 sm:gap-6 overflow-x-auto snap-x snap-mandatory px-4 sm:px-[max(1.5rem,calc((100vw-80rem)/2+1.5rem))] pb-2"
        >
          {rail.map((p, i) => (
            <div key={p.id} className="snap-start shrink-0 w-[70vw] sm:w-[40vw] lg:w-[23.5rem]">
              <p className="font-display text-[10px] uppercase tracking-[0.35em] text-bone/35 mb-2">
                Look {String(i + 1).padStart(2, "0")}
              </p>
              <ProductCard p={p} index={0} />
            </div>
          ))}
          {rail.length === 0 &&
            [...Array(4)].map((_, i) => (
              <div key={i} className="shrink-0 w-[70vw] sm:w-[40vw] lg:w-[23.5rem] aspect-[4/5] bg-smoke border border-ash animate-pulseSoft" />
            ))}
        </div>

        {/* rail progress */}
        <div className="max-w-7xl mx-auto px-4 sm:px-6 mt-6">
          <div className="h-px bg-ash relative">
            <div
              className="absolute inset-y-0 left-0 -top-px h-[3px] bg-acid transition-[width] duration-150"
              style={{ width: `${Math.max(8, railProgress * 100)}%` }}
            />
          </div>
        </div>
      </section>

      {/* ── GYE NYAME campaign strip ────────────────────────────── */}
      {jerseys.length >= 3 && (
        <section className="border-y border-ash bg-smoke/40 py-20">
          <div className="max-w-7xl mx-auto px-4 sm:px-6">
            <Reveal>
              <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">The jersey line</p>
              <div className="flex flex-wrap items-end justify-between gap-6 mb-10">
                <h2 className="display-xl text-4xl sm:text-7xl text-bone">
                  One symbol.<br />
                  <span className="text-stroke">{jerseys.length} colorways.</span>
                </h2>
                <p className="text-bone/50 text-sm max-w-xs leading-relaxed">
                  Gye Nyame — "except God". The Adinkra symbol, worn loud. {money(Number(jerseys[0]?.price || 0))} each, while they last.
                </p>
              </div>
            </Reveal>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
              {jerseys.map((j, i) => (
                <Reveal key={j.id} delay={i * 0.07}>
                  <Link to={`/product/${j.id}`} className="group block">
                    <div className="aspect-[4/5] bg-bone overflow-hidden border border-ash group-hover:border-acid transition-colors">
                      <img
                        src={firstImage(j)}
                        alt={j.name}
                        loading="lazy"
                        className="w-full h-full object-cover mix-blend-multiply transition-transform duration-700 ease-out group-hover:scale-[1.05]"
                      />
                    </div>
                    <p className="font-display uppercase text-[10px] tracking-[0.2em] text-bone/60 mt-2 truncate group-hover:text-acid transition-colors">
                      {j.name.replace(/og\s*gye\s*nyame\s*/i, "").replace(/jersey/i, "").trim() || j.name}
                    </p>
                  </Link>
                </Reveal>
              ))}
            </div>
          </div>
        </section>
      )}

      {/* ── INDEX — editorial category rows ─────────────────────── */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 py-20">
        <Reveal>
          <p className="font-display uppercase text-bone/40 tracking-[0.45em] text-[11px] mb-8">Index</p>
        </Reveal>
        <div className="border-t border-ash">
          {indexRows.map((row, i) => (
            <Reveal key={row.label} delay={i * 0.06}>
              <Link
                to={row.to}
                className="group relative flex items-center gap-6 border-b border-ash py-7 sm:py-9 px-2 sm:px-4 overflow-hidden transition-colors duration-300 hover:bg-bone"
              >
                <span className="font-display text-[10px] uppercase tracking-[0.35em] text-bone/30 group-hover:text-ink/40 w-8">
                  {String(i + 1).padStart(2, "0")}
                </span>
                <span className="display-xl text-4xl sm:text-6xl text-bone group-hover:text-ink transition-colors duration-300">
                  {row.label}
                </span>
                <span className="hidden sm:block text-bone/40 group-hover:text-ink/50 text-xs uppercase tracking-[0.25em] transition-colors duration-300">
                  {row.sub}
                </span>

                {/* photo preview */}
                {previewFor(row.slug) && (
                  <span className="pointer-events-none absolute right-16 sm:right-24 top-1/2 -translate-y-1/2 h-[82%] aspect-[4/5] bg-bone border border-ash overflow-hidden opacity-0 scale-90 rotate-[3deg] group-hover:opacity-100 group-hover:scale-100 group-hover:rotate-0 transition-all duration-500 ease-out hidden sm:block">
                    <img src={previewFor(row.slug)} alt="" loading="lazy" className="w-full h-full object-cover mix-blend-multiply" />
                  </span>
                )}

                <span className="ml-auto font-display text-2xl sm:text-3xl text-bone/30 group-hover:text-ink group-hover:translate-x-2 transition-all duration-300">
                  →
                </span>
              </Link>
            </Reveal>
          ))}
        </div>
      </section>

      {/* ── STATEMENT ───────────────────────────────────────────── */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 pb-8">
        <div className="border border-ash bg-smoke/40 px-6 sm:px-12 py-16 grid md:grid-cols-[1fr_auto] gap-10 items-center">
          <Reveal>
            <h2 className="display-xl text-4xl sm:text-5xl text-bone max-w-xl">
              Not a brand. <span className="text-acid">A statement.</span>
            </h2>
            <p className="text-bone/55 leading-relaxed mt-5 max-w-lg">
              Heavyweight fabric, loud prints, limited runs. Every piece is made to be seen — when it's gone, it's gone.
            </p>
          </Reveal>
          <Reveal delay={0.12}>
            <div className="flex flex-wrap gap-x-8 gap-y-5 md:flex-col md:gap-6 md:text-right">
              {[
                ["100%", "Heavyweight cotton"],
                ["LTD", "Limited runs only"],
                ["GH", "Born in Accra"],
              ].map(([big, small]) => (
                <div key={small}>
                  <p className="font-display text-2xl text-acid">{big}</p>
                  <p className="text-bone/40 text-[10px] uppercase tracking-[0.25em] mt-1">{small}</p>
                </div>
              ))}
            </div>
          </Reveal>
        </div>
      </section>
    </div>
  );
}
