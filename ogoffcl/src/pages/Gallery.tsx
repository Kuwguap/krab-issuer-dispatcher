import { useEffect, useState, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { supabase, publicImageUrl, STORAGE_BUCKET } from "../lib/supabase";
import Reveal from "../components/Reveal";
import Marquee from "../components/Marquee";
import { usePageMeta } from "../lib/seo";

/**
 * Public brand gallery — shoots, lookbook, lifestyle. Fed straight from the
 * storage bucket's gallery/ folder (same source the admin uploads to), shown
 * at natural aspect ratio in a masonry flow. Tapping a shot opens a full-res
 * lightbox — images are served untouched, original quality.
 */
interface Shot { name: string; url: string; }

export default function Gallery() {
  const [shots, setShots] = useState<Shot[] | null>(null);
  const [open, setOpen] = useState<number | null>(null);

  usePageMeta({
    title: "Gallery — OG OFFCL | Streetwear shoots from Accra",
    description:
      "The OG OFFCL gallery — lookbook shoots, drops and the culture in frame. Bold streetwear born in Accra, worn loud.",
    path: "/gallery",
  });

  useEffect(() => {
    supabase.storage
      .from(STORAGE_BUCKET)
      .list("gallery", { limit: 500, sortBy: { column: "created_at", order: "desc" } })
      .then(({ data }) => {
        setShots(
          (data || [])
            .filter((o) => o.name && !o.name.startsWith(".") && (o.metadata as { mimetype?: string } | null)?.mimetype?.startsWith("image/"))
            .map((o) => ({ name: o.name, url: publicImageUrl(`gallery/${o.name}`) })),
        );
      });
  }, []);

  const close = useCallback(() => setOpen(null), []);
  const step = useCallback((d: number) => {
    setOpen((i) => (i === null || !shots?.length ? i : (i + d + shots.length) % shots.length));
  }, [shots]);

  // lightbox keyboard: esc closes, arrows navigate
  useEffect(() => {
    if (open === null) return;
    const fn = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
      if (e.key === "ArrowRight") step(1);
      if (e.key === "ArrowLeft") step(-1);
    };
    window.addEventListener("keydown", fn);
    document.body.style.overflow = "hidden";
    return () => { window.removeEventListener("keydown", fn); document.body.style.overflow = ""; };
  }, [open, close, step]);

  return (
    <div>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 pt-12 pb-6">
        <Reveal>
          <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">The culture in frame</p>
          <h1 className="display-xl text-5xl sm:text-7xl text-bone">
            The <span className="text-stroke-acid">Gallery</span>
          </h1>
          <p className="text-bone/50 text-sm uppercase tracking-[0.25em] mt-5 max-w-md leading-relaxed">
            Shoots, drops and the streets that wear them.
          </p>
        </Reveal>
      </div>

      <Marquee text="ACCRA TO THE WORLD — SHOT ON LOCATION — OG OFFCL — WEAR THE CULTURE — " className="bg-smoke text-bone/60" />

      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10">
        {shots === null && (
          <div className="columns-2 md:columns-3 gap-3 [column-fill:_balance]">
            {[...Array(6)].map((_, i) => (
              <div key={i} className={`mb-3 bg-smoke border border-ash animate-pulseSoft ${i % 2 ? "aspect-[3/4]" : "aspect-square"}`} />
            ))}
          </div>
        )}

        {shots && shots.length === 0 && (
          <div className="text-center py-24">
            <p className="display-xl text-5xl text-bone/15">Loading the looks</p>
            <p className="text-bone/40 text-xs uppercase tracking-[0.3em] mt-4">The first shoot drops here soon.</p>
          </div>
        )}

        {shots && shots.length > 0 && (
          <div className="columns-2 md:columns-3 gap-3">
            {shots.map((s, i) => (
              <Reveal key={s.name} delay={Math.min(i, 8) * 0.05}>
                <button
                  onClick={() => setOpen(i)}
                  className="group relative block w-full mb-3 border border-ash bg-smoke overflow-hidden break-inside-avoid focus:outline-none focus:border-acid"
                >
                  <img
                    src={s.url}
                    alt={`OG OFFCL shoot ${i + 1}`}
                    loading={i < 4 ? "eager" : "lazy"}
                    className="w-full h-auto block transition-transform duration-700 ease-out group-hover:scale-[1.03]"
                  />
                  <span className="absolute inset-0 bg-ink/0 group-hover:bg-ink/20 transition-colors" />
                  <span className="absolute bottom-2 right-2 font-display text-[10px] uppercase tracking-[0.2em] text-bone/0 group-hover:text-bone/80 transition-colors">
                    View ↗
                  </span>
                </button>
              </Reveal>
            ))}
          </div>
        )}
      </div>

      {/* ── lightbox — full-res, untouched ─────────────────────── */}
      <AnimatePresence>
        {open !== null && shots && shots[open] && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            className="fixed inset-0 z-[90] bg-ink/95 backdrop-blur-sm flex flex-col"
            onClick={close}
          >
            <div className="flex items-center justify-between px-4 sm:px-6 h-14 shrink-0">
              <span className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/50">
                {open + 1} / {shots.length} — OG OFFCL
              </span>
              <button
                aria-label="Close"
                onClick={close}
                className="min-w-[44px] min-h-[44px] text-bone text-3xl font-display leading-none hover:text-acid"
              >
                ×
              </button>
            </div>

            <div className="flex-1 min-h-0 flex items-center justify-center px-2 pb-4" onClick={(e) => e.stopPropagation()}>
              <motion.img
                key={shots[open].name}
                initial={{ opacity: 0, scale: 0.97 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.3 }}
                src={shots[open].url}
                alt=""
                className="max-h-full max-w-full object-contain border border-ash/60 select-none"
              />
            </div>

            {shots.length > 1 && (
              <div className="flex items-center justify-center gap-6 pb-6 shrink-0" onClick={(e) => e.stopPropagation()}>
                <button
                  aria-label="Previous"
                  onClick={() => step(-1)}
                  className="btn-og border-2 border-bone/25 text-bone px-6 py-3 text-sm hover:border-acid hover:text-acid"
                >
                  ←
                </button>
                <button
                  aria-label="Next"
                  onClick={() => step(1)}
                  className="btn-og border-2 border-bone/25 text-bone px-6 py-3 text-sm hover:border-acid hover:text-acid"
                >
                  →
                </button>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
