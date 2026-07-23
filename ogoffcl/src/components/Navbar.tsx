import { useEffect, useState } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { AnimatePresence, motion } from "framer-motion";
import { useCart } from "../store/CartContext";

const links = [
  { to: "/", label: "Home" },
  { to: "/shop", label: "Shop" },
  { to: "/shop?c=og", label: "OG" },
  { to: "/shop?c=og-femme", label: "OG Femme" },
];

export default function Navbar() {
  const { count, setOpen } = useCart();
  const [menu, setMenu] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const loc = useLocation();

  useEffect(() => setMenu(false), [loc.pathname, loc.search]);
  useEffect(() => {
    const fn = () => setScrolled(window.scrollY > 24);
    fn();
    window.addEventListener("scroll", fn, { passive: true });
    return () => window.removeEventListener("scroll", fn);
  }, []);
  useEffect(() => {
    document.body.style.overflow = menu ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [menu]);

  return (
    <>
      <header
        className={`sticky top-0 z-40 transition-all duration-300 border-b ${
          scrolled ? "bg-ink/90 backdrop-blur-md border-ash" : "bg-transparent border-transparent"
        }`}
      >
        <div className="max-w-7xl mx-auto px-4 sm:px-6 h-16 flex items-center justify-between gap-4">
          {/* burger (mobile) */}
          <button
            aria-label="Menu"
            onClick={() => setMenu(true)}
            className="md:hidden flex flex-col justify-center gap-1.5 p-3 -ml-3 min-h-[44px] min-w-[44px]"
          >
            <span className="block w-6 h-0.5 bg-bone" />
            <span className="block w-4 h-0.5 bg-acid" />
            <span className="block w-6 h-0.5 bg-bone" />
          </button>

          <Link to="/" className="font-display uppercase text-xl sm:text-2xl tracking-tight text-bone hover:text-acid transition-colors">
            OG<span className="text-acid">.</span>OFFCL
          </Link>

          <nav className="hidden md:flex items-center gap-8">
            {links.map((l) => (
              <NavLink key={l.label} to={l.to} className="link-sweep font-display uppercase text-xs tracking-[0.2em] text-bone/80 hover:text-bone">
                {l.label}
              </NavLink>
            ))}
          </nav>

          <button
            onClick={() => setOpen(true)}
            className="btn-og relative border-2 border-bone px-4 py-2 text-xs text-bone hover:bg-acid hover:border-acid hover:text-ink"
          >
            Cart
            <span className="ml-1 inline-flex items-center justify-center min-w-[1.4rem] h-[1.4rem] px-1 bg-acid text-ink text-[11px] font-display group-hover:bg-ink">
              {count}
            </span>
          </button>
        </div>
      </header>

      {/* full-screen mobile menu */}
      <AnimatePresence>
        {menu && (
          <motion.div
            className="fixed inset-0 z-50 bg-ink flex flex-col"
            initial={{ x: "-100%" }}
            animate={{ x: 0 }}
            exit={{ x: "-100%" }}
            transition={{ duration: 0.45, ease: [0.76, 0, 0.24, 1] }}
          >
            <div className="h-16 px-4 flex items-center justify-between border-b border-ash">
              <span className="font-display uppercase text-xl text-bone">OG<span className="text-acid">.</span>OFFCL</span>
              <button aria-label="Close" onClick={() => setMenu(false)} className="text-bone font-display text-2xl px-2">
                ×
              </button>
            </div>
            {/* min-h-0 + overflow: on landscape phones the links are taller than
                the viewport — scroll instead of clipping the first/last link */}
            <nav className="flex-1 min-h-0 overflow-y-auto flex flex-col px-8 py-4">
              <div className="m-auto w-full flex flex-col gap-2">
              {links.map((l, i) => (
                <motion.div
                  key={l.label}
                  initial={{ opacity: 0, x: -30 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: 0.15 + i * 0.07 }}
                >
                  <NavLink to={l.to} className="display-xl text-5xl text-bone hover:text-acid transition-colors block py-2">
                    {l.label}
                  </NavLink>
                </motion.div>
              ))}
              </div>
            </nav>
            <div className="p-8 text-bone/40 text-xs uppercase tracking-[0.3em]">Original Gangster Official</div>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}
