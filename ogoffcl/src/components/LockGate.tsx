import { useEffect, useState, ReactNode, FormEvent } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { cachedLocked, fetchSiteLocked, subscribe } from "../lib/settings";

/**
 * Site lock with waitlist. Lock state lives in site_settings and is flipped
 * from Admin → Site & Mail. VITE_SITE_LOCKED=1 force-locks regardless.
 *
 * Staff bypass is GENERATION-AWARE: each lock stamps value.locked_at, and a
 * bypass is only valid if it was granted AFTER the current lock started —
 * re-locking the store always locks every browser out again (this is what
 * made the lock look "broken" before: old bypasses lived forever).
 */
const ENV_FORCED = /^(1|true|yes|on)$/i.test(String(import.meta.env.VITE_SITE_LOCKED ?? "").trim());
const PASSWORD = String(import.meta.env.VITE_SITE_PASSWORD ?? "OG2026").replace(/\\r|\\n/g, "").trim() || "OG2026";
const BYPASS_KEY = "ogoffcl_unlocked_v2";

interface Grant { pw: string; at: number }

function readGrant(): Grant | null {
  try {
    const raw = localStorage.getItem(BYPASS_KEY);
    if (!raw) return null;
    const g = JSON.parse(raw) as Grant;
    if (g && g.pw === PASSWORD && Number.isFinite(g.at)) return g;
  } catch {}
  return null;
}

function grantBypass() {
  try { localStorage.setItem(BYPASS_KEY, JSON.stringify({ pw: PASSWORD, at: Date.now() })); } catch {}
}

/** Consume ?unlock=<code> — grants a fresh bypass for the CURRENT lock. */
function consumeUnlockQuery(): boolean {
  try {
    const q = new URLSearchParams(window.location.search).get("unlock");
    if (q && q === PASSWORD) { grantBypass(); return true; }
  } catch {}
  return false;
}

function bypassValid(lockedAt: number): boolean {
  const g = readGrant();
  if (!g) return false;
  return g.at > lockedAt;
}

export default function LockGate({ children }: { children: ReactNode }) {
  // null = resolving (black splash). Optimistically show the site only when
  // the cache says "unlocked"; everything else waits for the real answer.
  const [locked, setLocked] = useState<boolean | null>(() => {
    const fresh = consumeUnlockQuery();
    if (ENV_FORCED) return !(fresh || readGrant() !== null);
    const cached = cachedLocked();
    if (cached === false) return false;
    return null;
  });

  useEffect(() => {
    if (ENV_FORCED) return;
    let alive = true;
    fetchSiteLocked().then(({ locked: dbLocked, lockedAt }) => {
      if (!alive) return;
      setLocked(dbLocked && !bypassValid(lockedAt));
    });
    return () => { alive = false; };
  }, []);

  // waitlist form
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done" | "already">("idle");
  const [err, setErr] = useState<string | null>(null);

  // staff access
  const [staffOpen, setStaffOpen] = useState(false);
  const [code, setCode] = useState("");
  const [shake, setShake] = useState(0);

  useEffect(() => {
    document.body.style.overflow = locked ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [locked]);

  const join = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    setState("busy");
    const res = await subscribe(email, "waitlist");
    if (res.ok) setState(res.already ? "already" : "done");
    else { setState("idle"); setErr(res.error || "Could not save your email — try again."); }
  };

  const staffSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (code.trim() === PASSWORD) {
      grantBypass();
      setLocked(false);
    } else {
      setShake((s) => s + 1);
      setCode("");
    }
  };

  if (locked === null) {
    return (
      <div className="fixed inset-0 bg-ink flex items-center justify-center">
        <span className="font-display uppercase tracking-[0.4em] text-bone/25 text-xs animate-pulseSoft">OG.OFFCL</span>
      </div>
    );
  }

  return (
    <>
      <AnimatePresence>
        {locked && (
          <motion.div
            key="lock"
            className="fixed inset-0 z-[100] bg-ink flex flex-col items-center justify-center px-6 overflow-hidden"
            exit={{ y: "-100%", transition: { duration: 0.7, ease: [0.76, 0, 0.24, 1] } }}
          >
            {/* moving backdrop type */}
            <div className="absolute inset-0 opacity-[0.05] pointer-events-none select-none" aria-hidden>
              {[...Array(7)].map((_, r) => (
                <div key={r} className="whitespace-nowrap font-display uppercase text-[11vh] leading-none" style={{ transform: `translateX(${r % 2 ? -12 : 0}%)` }}>
                  {Array(8).fill("NEXT DROP LOADING ").join("")}
                </div>
              ))}
            </div>

            <motion.div
              initial={{ opacity: 0, y: 24 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
              className="relative text-center w-full max-w-md"
            >
              <div className="font-display uppercase text-acid tracking-[0.4em] text-[11px] mb-6 animate-pulseSoft">
                Next drop loading
              </div>
              <h1 className="display-xl text-bone text-6xl sm:text-7xl mb-4">
                OG<span className="text-acid">.</span>OFFCL
              </h1>
              <p className="text-bone/50 text-sm mb-10 uppercase tracking-[0.2em] leading-relaxed">
                The store is closed while we load the next drop.
                <br />Join the waitlist — first to know, first to cop.
              </p>

              {state === "done" || state === "already" ? (
                <motion.div initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} className="border-2 border-acid px-6 py-8">
                  <p className="font-display uppercase text-acid text-2xl mb-2">
                    {state === "already" ? "Already on the list" : "You're on the list"}
                  </p>
                  <p className="text-bone/50 text-xs uppercase tracking-[0.25em]">
                    We'll email you the second the door opens.
                  </p>
                </motion.div>
              ) : (
                <form onSubmit={join} className="flex border-2 border-bone/20 focus-within:border-acid transition-colors">
                  <input
                    autoFocus
                    type="email"
                    required
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="YOUR@EMAIL.COM"
                    className="flex-1 min-w-0 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.15em] text-sm text-bone placeholder:text-bone/25 focus:outline-none"
                  />
                  <button type="submit" disabled={state === "busy"} className="btn-og bg-acid text-ink px-6 text-xs whitespace-nowrap hover:bg-bone">
                    {state === "busy" ? "…" : "Join waitlist"}
                  </button>
                </form>
              )}
              {err && <p className="text-blood text-xs uppercase tracking-widest mt-4">{err}</p>}

              {/* staff access */}
              <div className="mt-14">
                {!staffOpen ? (
                  <button onClick={() => setStaffOpen(true)} className="text-bone/25 hover:text-bone/60 text-[10px] uppercase tracking-[0.35em] transition-colors">
                    Staff access →
                  </button>
                ) : (
                  <motion.form
                    key={shake}
                    initial={shake ? { x: 0 } : { opacity: 0 }}
                    animate={shake ? { x: [0, -12, 10, -6, 4, 0], opacity: 1 } : { opacity: 1 }}
                    transition={{ duration: 0.4 }}
                    onSubmit={staffSubmit}
                    className="flex max-w-xs mx-auto border border-bone/15 focus-within:border-bone/50 transition-colors"
                  >
                    <input
                      autoFocus
                      type="password"
                      value={code}
                      onChange={(e) => setCode(e.target.value)}
                      placeholder="STAFF CODE"
                      className="flex-1 min-w-0 bg-transparent border-0 px-4 py-2.5 font-display uppercase tracking-[0.25em] text-xs text-bone placeholder:text-bone/20 focus:outline-none"
                    />
                    <button type="submit" className="btn-og border-l border-bone/15 text-bone/60 px-4 text-[10px] hover:text-acid">
                      Enter
                    </button>
                  </motion.form>
                )}
              </div>

              <div className="mt-10 text-bone/25 text-[10px] uppercase tracking-[0.3em]">
                Original Gangster Official — Accra
              </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>
      {!locked && children}
    </>
  );
}
