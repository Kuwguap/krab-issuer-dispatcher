import { useEffect, useMemo, useState, ReactNode, FormEvent } from "react";
import { AnimatePresence, motion } from "framer-motion";

/**
 * Site lock. Controlled by env so the DB stays untouched:
 *   VITE_SITE_LOCKED=1        -> lock the whole site
 *   VITE_SITE_PASSWORD=xxxxx  -> password to unlock (default OG2026)
 * Unlock persists per-browser in localStorage. `?unlock=<password>` also works.
 */
const LOCKED = /^(1|true|yes|on)$/i.test(String(import.meta.env.VITE_SITE_LOCKED ?? "").trim());
const PASSWORD = String(import.meta.env.VITE_SITE_PASSWORD ?? "OG2026").replace(/\\r|\\n/g, "").trim() || "OG2026";
const KEY = "ogoffcl_unlocked_v1";

export default function LockGate({ children }: { children: ReactNode }) {
  const preUnlocked = useMemo(() => {
    if (!LOCKED) return true;
    try {
      if (localStorage.getItem(KEY) === PASSWORD) return true;
      const q = new URLSearchParams(window.location.search).get("unlock");
      if (q && q === PASSWORD) {
        localStorage.setItem(KEY, PASSWORD);
        return true;
      }
    } catch {}
    return false;
  }, []);

  const [unlocked, setUnlocked] = useState(preUnlocked);
  const [input, setInput] = useState("");
  const [shake, setShake] = useState(0);

  useEffect(() => {
    document.body.style.overflow = unlocked ? "" : "hidden";
    return () => { document.body.style.overflow = ""; };
  }, [unlocked]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (input.trim() === PASSWORD) {
      try { localStorage.setItem(KEY, PASSWORD); } catch {}
      setUnlocked(true);
    } else {
      setShake((s) => s + 1);
      setInput("");
    }
  };

  return (
    <>
      <AnimatePresence>
        {!unlocked && (
          <motion.div
            key="lock"
            className="fixed inset-0 z-[100] bg-ink flex flex-col items-center justify-center px-6 overflow-hidden"
            exit={{ y: "-100%", transition: { duration: 0.7, ease: [0.76, 0, 0.24, 1] } }}
          >
            {/* moving backdrop type */}
            <div className="absolute inset-0 opacity-[0.06] pointer-events-none select-none">
              {[...Array(7)].map((_, r) => (
                <div key={r} className="whitespace-nowrap font-display uppercase text-[11vh] leading-none" style={{ transform: `translateX(${r % 2 ? -12 : 0}%)` }}>
                  {Array(8).fill("ORIGINAL GANGSTER ").join("")}
                </div>
              ))}
            </div>

            <motion.div
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
              className="relative text-center w-full max-w-md"
            >
              <div className="font-display uppercase text-acid tracking-[0.35em] text-xs mb-6 animate-pulseSoft">
                Site locked · Members only
              </div>
              <h1 className="display-xl text-bone text-6xl sm:text-7xl mb-3">
                OG<span className="text-acid">.</span>OFFCL
              </h1>
              <p className="text-bone/50 text-sm mb-10 tracking-wide uppercase">
                The drop is behind the door. Enter the code.
              </p>

              <motion.form
                key={shake}
                initial={shake ? { x: 0 } : false}
                animate={shake ? { x: [0, -14, 12, -8, 6, 0] } : undefined}
                transition={{ duration: 0.45 }}
                onSubmit={submit}
                className="flex border-2 border-bone/20 focus-within:border-acid transition-colors"
              >
                <input
                  autoFocus
                  type="password"
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  placeholder="ACCESS CODE"
                  className="flex-1 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.25em] text-sm text-bone placeholder:text-bone/25 focus:outline-none"
                />
                <button type="submit" className="btn-og bg-acid text-ink px-7 text-sm hover:bg-bone">
                  Enter
                </button>
              </motion.form>
              {shake > 0 && (
                <p className="text-blood text-xs uppercase tracking-widest mt-4">Wrong code. Try again.</p>
              )}

              <div className="mt-14 text-bone/30 text-[11px] uppercase tracking-[0.3em]">
                Original Gangster Official — Accra
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
      {unlocked && children}
    </>
  );
}
