import { useEffect, useMemo, useRef, useState, FormEvent } from "react";
import { Link } from "react-router-dom";
import { motion, useInView } from "framer-motion";
import { uploadImage } from "../lib/supabase";
import { usePageMeta, useJsonLd, SITE_URL } from "../lib/seo";
import Marquee from "../components/Marquee";
import Reveal from "../components/Reveal";
import { buildSchedule, fmtDuration } from "../lib/fixtures";

const EASE = [0.22, 1, 0.36, 1] as const;
const BASE_FEE = 50;
const HUB_FEE = 20;
const PRIZE_CASH = 1000;
const EVENT_DATE = new Date("2026-08-01T10:00:00");
const EVENT_DATE_LABEL = "1st August";

/** Live countdown to kickoff — urgency in the hero. */
function Countdown() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const ms = EVENT_DATE.getTime() - now;
  if (ms <= 0) return <span className="text-acid font-display uppercase tracking-[0.2em]">It's game day.</span>;
  const d = Math.floor(ms / 86400_000);
  const h = Math.floor((ms % 86400_000) / 3600_000);
  const m = Math.floor((ms % 3600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  return (
    <div className="flex gap-2">
      {[[d, "days"], [h, "hrs"], [m, "min"], [s, "sec"]].map(([v, l]) => (
        <div key={l as string} className="border border-ash bg-smoke/60 px-3 py-2 text-center min-w-[62px]">
          <p className="font-display text-2xl text-acid leading-none">{String(v).padStart(2, "0")}</p>
          <p className="text-bone/35 text-[9px] uppercase tracking-[0.2em] mt-1">{l}</p>
        </div>
      ))}
    </div>
  );
}

const NETWORKS: { key: "mtn" | "telecel" | "at"; label: string }[] = [
  { key: "mtn", label: "MTN MoMo" },
  { key: "telecel", label: "Telecel Cash" },
  { key: "at", label: "AT Money" },
];

type PayState =
  | { step: "form" }
  | { step: "otp"; playerId: string; ref: string; message: string }
  | { step: "waiting"; playerId: string; ref: string }
  | { step: "done"; gamertag: string }
  | { step: "failed"; playerId: string; error: string };

interface BracketPlayer { id: string; gamertag: string; platform: string; photo: string | null; }
interface BracketMatch { id: string; round: number; slot: number; player1: string | null; player2: string | null; score1: number | null; score2: number | null; winner: string | null; }

/** Animated count-up for the stat tiles. */
function Stat({ value, label, suffix = "" }: { value: number; label: string; suffix?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const inView = useInView(ref, { once: true });
  const [n, setN] = useState(0);
  useEffect(() => {
    if (!inView) return;
    const t0 = performance.now();
    const tick = (t: number) => {
      const p = Math.min(1, (t - t0) / 900);
      setN(Math.round(value * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }, [inView, value]);
  return (
    <div ref={ref} className="bg-ink p-5 sm:p-6">
      <p className="font-display text-4xl sm:text-5xl text-acid">{n}{suffix}</p>
      <p className="text-bone/40 text-[10px] uppercase tracking-[0.25em] mt-2">{label}</p>
    </div>
  );
}

export default function Tournament() {
  usePageMeta({
    title: "FC26 Tournament — OG OFFCL | 1st August · GH₵1,000 prize pool",
    description:
      "The OG OFFCL FC26 Ultimate Team knockout — 1st August. GH₵1,000 cash + 2 merch pieces on the line, GH₵50 entry, limited spaces. Play from anywhere — or from the OGOFFCL Hub at KNUST Kumasi for GH₵20 extra. Register before the bracket fills.",
    path: "/tournament",
  });
  useJsonLd("tournament", {
    "@context": "https://schema.org",
    "@type": "Event",
    name: "OG OFFCL FC26 Tournament",
    description: "1v1 FC26 Ultimate Team knockout tournament by OG OFFCL. GH₵50 entry, GH₵1,000 cash + merch prize pool.",
    startDate: "2026-08-01",
    eventAttendanceMode: "https://schema.org/MixedEventAttendanceMode",
    eventStatus: "https://schema.org/EventScheduled",
    location: { "@type": "VirtualLocation", url: `${SITE_URL}/tournament` },
    organizer: { "@type": "Organization", name: "OG OFFCL", url: SITE_URL },
    offers: { "@type": "Offer", price: "50", priceCurrency: "GHS", url: `${SITE_URL}/tournament`, availability: "https://schema.org/InStock" },
  });

  // ── live bracket / roster ────────────────────────────────────
  const [players, setPlayers] = useState<BracketPlayer[]>([]);
  const [matches, setMatches] = useState<BracketMatch[]>([]);
  useEffect(() => {
    fetch("/api/tournament?action=bracket")
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => { if (j) { setPlayers(j.players || []); setMatches(j.matches || []); } })
      .catch(() => {});
  }, []);
  const byId = useMemo(() => Object.fromEntries(players.map((p) => [p.id, p])), [players]);

  // ── registration form ────────────────────────────────────────
  const [form, setForm] = useState({ fullName: "", email: "", phone: "", gamertag: "", platform: "ps5" });
  const [hub, setHub] = useState(false);
  const [photo, setPhoto] = useState<File | null>(null);
  const [photoPreview, setPhotoPreview] = useState<string | null>(null);
  const [momoPhone, setMomoPhone] = useState("");
  const [network, setNetwork] = useState<"mtn" | "telecel" | "at">("mtn");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pay, setPay] = useState<PayState>({ step: "form" });
  const [otp, setOtp] = useState("");
  const [otpBusy, setOtpBusy] = useState(false);
  const pollRef = useRef<number | null>(null);
  const formRef = useRef<HTMLDivElement>(null);

  const fee = BASE_FEE + (hub ? HUB_FEE : 0);
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm((f) => ({ ...f, [k]: e.target.value }));

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const pickPhoto = (f: File | null) => {
    setPhoto(f);
    if (photoPreview) URL.revokeObjectURL(photoPreview);
    setPhotoPreview(f ? URL.createObjectURL(f) : null);
  };

  const startPolling = (playerId: string, ref: string) => {
    setPay({ step: "waiting", playerId, ref });
    let tries = 0;
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      tries += 1;
      try {
        const r = await fetch(`/api/tournament?action=status&ref=${encodeURIComponent(ref)}`);
        const j = await r.json();
        if (j.state === "paid") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "done", gamertag: j.gamertag || form.gamertag });
        } else if (j.state === "failed") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "failed", playerId, error: j.message || "Payment failed or was declined." });
        } else if (tries > 40) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "failed", playerId, error: "The payment window timed out. Your registration is saved — try the payment again." });
        }
      } catch { /* keep polling */ }
    }, 3000);
  };

  const initiatePay = async (playerId: string, otpcode?: string, ref?: string) => {
    const r = await fetch("/api/tournament", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "pay", playerId, phone: momoPhone, channel: network, otpcode, ref }),
    });
    const j = await r.json().catch(() => ({}));
    setOtpBusy(false);
    if (j.state === "otp" && otpcode) setOtp("");
    if (j.state === "paid") { setPay({ step: "done", gamertag: form.gamertag }); return; }
    if (j.state === "otp") { setPay({ step: "otp", playerId, ref: j.ref, message: j.message }); return; }
    if (j.state === "pending") { startPolling(playerId, j.ref); return; }
    setPay({ step: "failed", playerId, error: j.error || "Could not start the payment." });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (!form.fullName.trim() || !form.email.includes("@") || form.phone.replace(/\D/g, "").length < 9 || !form.gamertag.trim()) {
      setErr("Fill in your name, a valid email, phone and your PSN / EA ID.");
      return;
    }
    if (!photo) { setErr("Upload your player photo — it goes on your match card."); return; }
    if (momoPhone.replace(/\D/g, "").length < 9) { setErr("Enter the mobile money number that will pay."); return; }
    setBusy(true);
    try {
      const url = await uploadImage(photo, "tournament");
      const photoPath = "tournament/" + url.split("/tournament/").pop();
      const r = await fetch("/api/tournament", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "register", ...form, hub, photoPath }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.playerId) throw new Error(j.error || "Could not save your registration.");
      await initiatePay(j.playerId);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : "Something went wrong — try again.");
    } finally {
      setBusy(false);
    }
  };

  const plan20 = useMemo(() => buildSchedule(20, { concurrent: 4 }), []);

  const roundsForDisplay = useMemo(() => {
    const map = new Map<number, BracketMatch[]>();
    for (const m of matches) {
      if (!map.has(m.round)) map.set(m.round, []);
      map.get(m.round)!.push(m);
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0]);
  }, [matches]);

  // ── payment overlay states ───────────────────────────────────
  if (pay.step === "done") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <motion.div initial={{ scale: 0.7, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="w-24 h-24 mx-auto border-4 border-acid flex items-center justify-center font-display text-5xl text-acid mb-8">✓</motion.div>
        <h1 className="display-xl text-4xl sm:text-5xl text-bone">{pay.gamertag}, <span className="text-acid">you're on the bracket.</span></h1>
        <p className="text-bone/60 mt-5 leading-relaxed">Payment received. Check your email — bracket, kickoff time and the official match group land there before game day.</p>
        <p className="text-bone/30 text-[11px] uppercase tracking-[0.25em] mt-6">No restocks on glory.</p>
      </div>
    );
  }
  if (pay.step === "waiting") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <div className="w-20 h-20 mx-auto border-4 border-acid border-t-transparent rounded-full animate-spin mb-8" />
        <h1 className="display-xl text-4xl text-bone">Check your phone</h1>
        <p className="text-bone/60 mt-4 leading-relaxed">Approve the <strong className="text-bone">GH₵{fee}</strong> mobile-money prompt on <strong className="text-acid">{momoPhone}</strong>.</p>
        <p className="text-bone/40 text-xs mt-3">Payment is processed by <strong className="text-bone/70">Moolre</strong> — any SMS about this payment comes from Moolre.</p>
      </div>
    );
  }
  if (pay.step === "otp") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-bone">Enter the code</h1>
        <p className="text-bone/60 mt-4">{pay.message || "A one-time code was sent to your phone."}</p>
        <p className="text-bone/40 text-xs uppercase tracking-[0.2em] mt-3">Look for an SMS from <strong className="text-acid">Moolre</strong> · sent to {momoPhone}</p>
        <form onSubmit={(e) => { e.preventDefault(); if (otp.trim()) { setOtpBusy(true); initiatePay(pay.playerId, otp.trim(), pay.ref); } }} className="mt-8 flex border-2 border-bone/20 focus-within:border-acid transition-colors">
          <input autoFocus value={otp} onChange={(e) => setOtp(e.target.value)} inputMode="numeric" placeholder="OTP CODE" className="flex-1 min-w-0 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.3em] text-sm text-bone placeholder:text-bone/25 focus:outline-none" />
          <button type="submit" disabled={otpBusy} className="btn-og bg-acid text-ink px-6 text-xs hover:bg-bone whitespace-nowrap">{otpBusy ? "Verifying…" : "Verify & pay"}</button>
        </form>
      </div>
    );
  }
  if (pay.step === "failed") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-blood">Payment failed</h1>
        <p className="text-bone/60 mt-4">{pay.error}</p>
        <div className="flex gap-3 justify-center mt-8">
          <button onClick={() => initiatePay(pay.playerId)} className="btn-og bg-acid text-ink px-6 py-3 text-xs hover:bg-bone">Try again</button>
          <button onClick={() => setPay({ step: "form" })} className="btn-og border-2 border-bone text-bone px-6 py-3 text-xs hover:bg-bone hover:text-ink">Edit details</button>
        </div>
      </div>
    );
  }

  return (
    <div className="overflow-hidden">
      {/* ── HERO — FC26 broadcast energy ───────────────────────── */}
      <section className="relative min-h-[88vh] flex flex-col justify-center border-b border-ash">
        {/* pitch grid backdrop */}
        <div className="absolute inset-0 opacity-[0.07] pointer-events-none" aria-hidden>
          <div className="absolute inset-0" style={{ backgroundImage: "linear-gradient(#F5F2EA 1px, transparent 1px), linear-gradient(90deg, #F5F2EA 1px, transparent 1px)", backgroundSize: "56px 56px" }} />
          <div className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 w-[42vw] h-[42vw] max-w-[500px] max-h-[500px] border-2 border-bone rounded-full" />
          <div className="absolute left-1/2 top-0 bottom-0 w-px bg-bone" />
        </div>
        {/* acid diagonal slash */}
        <motion.div
          initial={{ x: "-110%" }} animate={{ x: "0%" }} transition={{ duration: 0.9, ease: EASE, delay: 0.2 }}
          className="absolute -left-10 top-[18%] w-[130%] h-10 sm:h-14 bg-acid/10 border-y border-acid/30 -rotate-3 pointer-events-none" aria-hidden
        />

        <div className="relative max-w-7xl mx-auto px-4 sm:px-6 w-full py-20">
          <motion.p initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.1, duration: 0.5 }} className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-6">
            OG OFFCL presents · {EVENT_DATE_LABEL} · Online + The Hub, KNUST Kumasi
          </motion.p>

          <h1 className="display-xl text-[15vw] sm:text-[11vw] lg:text-[8rem] leading-[0.85]">
            {["FC26", "Knockout", "Tournament"].map((w, i) => (
              <span key={w} className="block overflow-hidden pb-[0.07em] -mb-[0.07em]">
                <motion.span
                  className={`block ${i === 1 ? "text-stroke-acid" : "text-bone"}`}
                  initial={{ y: "112%", rotate: 2 }} animate={{ y: 0, rotate: 0 }}
                  transition={{ delay: 0.25 + i * 0.12, duration: 0.85, ease: EASE }}
                >
                  {w}
                </motion.span>
              </span>
            ))}
          </h1>

          <motion.div initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ delay: 0.9, duration: 0.7, ease: EASE }} className="origin-left h-px bg-bone/25 w-full max-w-lg my-8" />

          <motion.div initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 1, duration: 0.5 }} className="flex flex-wrap items-center gap-4">
            <a href="#register" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
              Claim your spot — GH₵{BASE_FEE} →
            </a>
            <a href="#format" className="link-sweep font-display uppercase text-xs tracking-[0.3em] text-bone/80 hover:text-bone py-3">
              How it works
            </a>
          </motion.div>

          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 1.15, duration: 0.5 }} className="mt-8 flex flex-wrap items-center gap-4">
            <Countdown />
            <p className="text-blood font-display uppercase text-xs tracking-[0.25em]">
              ⚠ Limited spaces — when the bracket's full, it's full.
            </p>
          </motion.div>

          {/* scoreboard chips */}
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 1.25, duration: 0.6 }} className="flex flex-wrap gap-2 mt-10">
            {[EVENT_DATE_LABEL.toUpperCase(), "LIMITED SPOTS", "1V1 KNOCKOUT", "ULTIMATE TEAM", "6-MIN HALVES", "PS5 · XBOX · PC", "STREAMED LIVE", `GH₵${PRIZE_CASH.toLocaleString()} + MERCH POT`].map((t) => (
              <span key={t} className="border border-ash bg-smoke/60 text-bone/70 font-display text-[10px] uppercase tracking-[0.2em] px-3 py-2">{t}</span>
            ))}
          </motion.div>
        </div>
      </section>

      <Marquee text="1ST AUGUST — FC26 KNOCKOUT — GH₵50 ENTRY — LIMITED SPOTS — WINNER TAKES THE CROWN — " fast />

      {/* ── LIVE ROSTER COUNT + POT ────────────────────────────── */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 py-14">
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-px bg-ash border border-ash">
          <Stat value={PRIZE_CASH} label="Cash prize pool (GH₵)" />
          <Stat value={2} label="Merch pieces in the pot" />
          <Stat value={players.length} label="Players locked in" />
          <Stat value={BASE_FEE} label="Entry fee (GH₵)" />
        </div>
      </section>

      {/* ── THE POT ────────────────────────────────────────────── */}
      <section className="max-w-7xl mx-auto px-4 sm:px-6 pb-14">
        <Reveal>
          <div className="relative border-2 border-acid bg-acid/5 p-8 sm:p-12 overflow-hidden">
            <div className="absolute -right-6 -top-10 font-display text-[10rem] leading-none text-acid/10 select-none pointer-events-none" aria-hidden>₵</div>
            <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">The pot</p>
            <h2 className="display-xl text-5xl sm:text-7xl text-bone">
              GH₵{PRIZE_CASH.toLocaleString()} <span className="text-stroke-acid">+ 2 merch</span>
            </h2>
            <p className="text-bone/60 text-sm sm:text-base leading-relaxed mt-4 max-w-xl">
              Winner takes <strong className="text-acid">GH₵{PRIZE_CASH.toLocaleString()} cash</strong> and
              <strong className="text-bone"> two pieces from the OG OFFCL rack</strong> — your pick.
              Champion's name goes on the stream, the socials and the next drop.
            </p>
          </div>
        </Reveal>
      </section>

      {/* ── ROSTER WALL — the hype grid ────────────────────────── */}
      {players.length > 0 && (
        <section className="max-w-7xl mx-auto px-4 sm:px-6 pb-14">
          <Reveal>
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-5">The roster</p>
          </Reveal>
          <div className="grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-8 gap-2">
            {players.map((p, i) => (
              <Reveal key={p.id} delay={Math.min(i, 10) * 0.04}>
                <div className="group relative aspect-[3/4] bg-smoke border border-ash overflow-hidden">
                  {p.photo
                    ? <img src={p.photo} alt={p.gamertag} loading="lazy" className="w-full h-full object-cover grayscale group-hover:grayscale-0 transition-all duration-500" />
                    : <div className="w-full h-full flex items-center justify-center font-display text-3xl text-bone/15">{p.gamertag.slice(0, 2).toUpperCase()}</div>}
                  <div className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-ink via-ink/70 to-transparent px-2 pt-6 pb-1.5">
                    <p className="font-display uppercase text-[10px] text-bone truncate">{p.gamertag}</p>
                    <p className="text-acid text-[8px] uppercase tracking-[0.2em]">{p.platform}</p>
                  </div>
                </div>
              </Reveal>
            ))}
          </div>
        </section>
      )}

      {/* ── BRACKET (once the admin generates it) ──────────────── */}
      {roundsForDisplay.length > 0 && (
        <section className="border-y border-ash bg-smoke/30 py-14">
          <div className="max-w-7xl mx-auto px-4 sm:px-6">
            <Reveal>
              <div className="flex flex-wrap items-end justify-between gap-4 mb-8">
                <h2 className="display-xl text-4xl sm:text-6xl text-bone">The <span className="text-stroke-acid">bracket</span></h2>
                <Link to="/tournament/fixtures" className="link-sweep font-display uppercase text-xs tracking-[0.25em] text-acid hover:text-bone whitespace-nowrap py-3">
                  Live fixtures ↗
                </Link>
              </div>
            </Reveal>
            <div className="overflow-x-auto pb-4 -mx-4 px-4">
              <div className="flex gap-6 min-w-max">
                {roundsForDisplay.map(([round, ms]) => (
                  <div key={round} className="w-56 shrink-0">
                    <p className="font-display uppercase text-[10px] tracking-[0.25em] text-acid mb-3">
                      {round === 0 ? "Prelims" : ms.length === 1 ? "Final" : ms.length === 2 ? "Semi-finals" : ms.length === 4 ? "Quarter-finals" : `Round of ${ms.length * 2}`}
                    </p>
                    <div className="space-y-3">
                      {ms.map((m) => (
                        <div key={m.id} className="border border-ash bg-ink">
                          {[{ id: m.player1, score: m.score1 }, { id: m.player2, score: m.score2 }].map((side, si) => {
                            const pl = side.id ? byId[side.id] : null;
                            const won = m.winner && side.id === m.winner;
                            return (
                              <div key={si} className={`flex items-center gap-2 px-3 py-2 ${si === 0 ? "border-b border-ash/60" : ""} ${won ? "bg-acid/10" : ""}`}>
                                <span className={`flex-1 min-w-0 truncate font-display uppercase text-xs ${won ? "text-acid" : "text-bone/80"}`}>
                                  {pl ? pl.gamertag : "TBD"}
                                </span>
                                <span className={`font-display text-sm ${won ? "text-acid" : "text-bone/40"}`}>{side.score ?? "–"}</span>
                              </div>
                            );
                          })}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>
      )}

      {/* ── FORMAT ─────────────────────────────────────────────── */}
      <section id="format" className="max-w-7xl mx-auto px-4 sm:px-6 py-20">
        <Reveal>
          <h2 className="display-xl text-4xl sm:text-6xl text-bone mb-10">How it <span className="text-acid">works</span></h2>
        </Reveal>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-px bg-ash border border-ash">
          {[
            ["01", "Register & pay", `GH₵${BASE_FEE} locks your spot. Name, PSN / EA ID, platform and your player photo — it goes on your official match card.`],
            ["02", "Random draw", "The bracket is a fully random draw — no seeds, no favours. It goes live at ogoffcl.store/tournament/fixtures plus the official WhatsApp group. You'll know exactly who you face and when."],
            ["03", "Play your tie", "Add your opponent via EA ID → invite them from Ultimate Team → Friendlies → Play a Friend. 6-minute halves. Bring your best squad — UT is the battleground."],
            ["04", "Report the score", "Winner screenshots the full-time screen and drops it in the group. Bracket updates live on this page. Win out. Take the crown."],
          ].map(([n, t, d]) => (
            <Reveal key={n}>
              <div className="bg-ink p-6 h-full">
                <p className="font-display text-acid/40 text-3xl">{n}</p>
                <p className="font-display uppercase text-bone text-sm tracking-wide mt-3">{t}</p>
                <p className="text-bone/50 text-sm leading-relaxed mt-2">{d}</p>
              </div>
            </Reveal>
          ))}
        </div>

        {/* hub add-on banner */}
        <Reveal>
          <div className="mt-10 border-2 border-acid/50 bg-acid/5 p-6 sm:p-8 flex flex-wrap items-center gap-6">
            <div className="flex-1 min-w-[240px]">
              <p className="font-display uppercase text-acid text-lg tracking-wide">No console? Shaky internet? Play from The Hub.</p>
              <p className="text-bone/60 text-sm leading-relaxed mt-2">
                For GH₵{HUB_FEE} extra we seat you at the OGOFFCL Hub — console, screen and a
                <strong className="text-bone"> minimum 200&nbsp;Mbps connection</strong> handled. Hub matches also go straight onto the live stream.
              </p>
              <p className="text-acid/80 text-xs uppercase tracking-[0.2em] mt-3">
                ⚠ Hub seats are only available at KNUST, Kumasi — playing from anywhere else? You need your own console and a stable connection.
              </p>
            </div>
            <a href="#register" className="btn-og bg-acid text-ink px-6 py-3.5 text-xs hover:bg-bone whitespace-nowrap">Add The Hub — GH₵{BASE_FEE + HUB_FEE} total</a>
          </div>
        </Reveal>
      </section>

      {/* ── MATCH DAY TIMELINE (20-player example) ─────────────── */}
      <section className="border-y border-ash bg-smoke/30 py-16">
        <div className="max-w-7xl mx-auto px-4 sm:px-6">
          <Reveal>
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-2">Match day rhythm</p>
            <h2 className="display-xl text-4xl sm:text-5xl text-bone mb-8">~{fmtDuration(plan20.totalMinutes)} of war<span className="text-acid">.</span></h2>
          </Reveal>
          <div className="flex overflow-x-auto gap-px bg-ash border border-ash -mx-4 px-0 sm:mx-0">
            {plan20.rounds.map((r) => (
              <div key={r.round} className="bg-ink px-5 py-4 min-w-[130px] shrink-0">
                <p className="font-display uppercase text-[10px] tracking-[0.2em] text-acid">{r.name}</p>
                <p className="text-bone font-display text-xl mt-1">{r.matches} <span className="text-bone/30 text-xs">match{r.matches === 1 ? "" : "es"}</span></p>
                <p className="text-bone/40 text-[11px] mt-1">{fmtDuration(r.minutes)}{r.breakAfter ? ` + ${r.breakAfter}m break` : ""}</p>
              </div>
            ))}
          </div>
          <p className="text-bone/35 text-xs mt-4">Based on 20 players, 4 concurrent matches, 20 minutes per tie. Final bracket timing drops with the fixtures.</p>
        </div>
      </section>

      {/* ── REGISTRATION ───────────────────────────────────────── */}
      <section id="register" ref={formRef} className="max-w-7xl mx-auto px-4 sm:px-6 py-20">
        <Reveal>
          <h2 className="display-xl text-5xl sm:text-7xl text-bone mb-2">Lock <span className="text-stroke-acid">in</span></h2>
          <p className="text-bone/50 text-sm uppercase tracking-[0.25em] mb-3">GH₵{fee} · pays with mobile money · spot confirmed instantly</p>
          <p className="text-blood font-display uppercase text-xs tracking-[0.25em] mb-10">
            Kickoff {EVENT_DATE_LABEL} — limited spaces, first paid first on the bracket.
          </p>
        </Reveal>

        <form onSubmit={submit} className="grid lg:grid-cols-[1fr_380px] gap-10 lg:gap-14">
          <div className="space-y-5 min-w-0">
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Player details</p>
            <div className="grid sm:grid-cols-2 gap-4">
              <input required placeholder="Full name *" value={form.fullName} onChange={set("fullName")} className="px-4 py-4 text-sm w-full" />
              <input required type="email" placeholder="Email *" value={form.email} onChange={set("email")} className="px-4 py-4 text-sm w-full" />
            </div>
            <div className="grid sm:grid-cols-2 gap-4">
              <input required type="tel" placeholder="Phone / WhatsApp *" value={form.phone} onChange={set("phone")} className="px-4 py-4 text-sm w-full" />
              <input required placeholder="PSN / EA ID *" value={form.gamertag} onChange={set("gamertag")} className="px-4 py-4 text-sm w-full" />
            </div>

            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 pt-2">Platform</p>
            <div className="flex flex-wrap gap-2">
              {[["ps5", "PS5"], ["ps4", "PS4"], ["xbox", "Xbox"], ["pc", "PC"]].map(([k, label]) => (
                <button key={k} type="button" onClick={() => setForm((f) => ({ ...f, platform: k }))}
                  className={`btn-og px-5 py-3 text-xs border-2 ${form.platform === k ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}>
                  {label}
                </button>
              ))}
            </div>

            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 pt-2">Player photo *</p>
            <label className="flex items-center gap-4 border border-dashed border-ash hover:border-acid transition-colors p-4 cursor-pointer">
              <div className="w-16 h-20 bg-smoke border border-ash overflow-hidden shrink-0">
                {photoPreview
                  ? <img src={photoPreview} alt="" className="w-full h-full object-cover" />
                  : <div className="w-full h-full flex items-center justify-center text-bone/20 font-display text-2xl">+</div>}
              </div>
              <div className="min-w-0">
                <p className="text-bone/80 text-sm">{photo ? photo.name : "Upload a clear head-and-shoulders shot"}</p>
                <p className="text-bone/35 text-xs mt-1">Goes on your official match card + the stream graphics.</p>
              </div>
              <input type="file" accept="image/*" className="hidden" onChange={(e) => pickPhoto(e.target.files?.[0] || null)} />
            </label>

            <label className={`flex items-start gap-4 border-2 p-4 cursor-pointer transition-colors ${hub ? "border-acid bg-acid/5" : "border-ash hover:border-bone/40"}`}>
              <input type="checkbox" checked={hub} onChange={(e) => setHub(e.target.checked)} className="mt-1 accent-[#C8FF00] w-4 h-4" />
              <span className="min-w-0">
                <span className="font-display uppercase text-sm text-bone block">Play from the OGOFFCL Hub <span className="text-acid">+GH₵{HUB_FEE}</span> <span className="text-bone/40 text-xs normal-case">· KNUST, Kumasi only</span></span>
                <span className="text-bone/50 text-xs leading-relaxed block mt-1">Console, screen and a minimum 200 Mbps connection provided at the Hub at KNUST, Kumasi. Only pick this if you can be there in person on game day — everyone else plays on their own console and internet.</span>
              </span>
            </label>

            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 pt-2">Pay with mobile money</p>
            <div className="flex gap-2">
              {NETWORKS.map((n) => (
                <button key={n.key} type="button" onClick={() => setNetwork(n.key)}
                  className={`btn-og flex-1 px-3 py-3 text-xs border-2 ${network === n.key ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}>
                  {n.label}
                </button>
              ))}
            </div>
            <input required type="tel" placeholder="MoMo number that will pay *" value={momoPhone} onChange={(e) => setMomoPhone(e.target.value)} className="px-4 py-4 text-sm w-full" />

            {err && <p className="text-blood text-sm border border-blood/40 bg-blood/10 px-4 py-3">{err}</p>}

            <button type="submit" disabled={busy} className={`btn-og w-full py-5 text-sm ${busy ? "bg-ash text-bone/40" : "bg-acid text-ink hover:bg-bone"}`}>
              {busy ? "Locking your spot…" : `Pay GH₵${fee} — lock my spot`}
            </button>
            <p className="text-bone/30 text-[11px] uppercase tracking-widest text-center">MTN · Telecel · AT — secured by Moolre</p>
          </div>

          {/* summary card */}
          <aside className="min-w-0">
            <div className="border border-ash bg-smoke/60 p-6 lg:sticky lg:top-24">
              <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-5">Entry summary</p>
              <div className="space-y-2 text-sm">
                <div className="flex justify-between text-bone/60"><span>Tournament entry</span><span>GH₵{BASE_FEE}</span></div>
                {hub && <div className="flex justify-between text-acid"><span>OGOFFCL Hub seat</span><span>+GH₵{HUB_FEE}</span></div>}
                <div className="flex justify-between font-display uppercase text-bone text-lg pt-3 border-t border-ash mt-3">
                  <span>Total</span><span className="text-acid">GH₵{fee}</span>
                </div>
              </div>
              <div className="border-t border-ash mt-5 pt-4 space-y-2.5">
                {[`Shot at GH₵${PRIZE_CASH.toLocaleString()} cash + 2 merch pieces`, "Official match card with your photo", "Seeded knockout bracket spot", "Live-stream feature when you play"].map((t) => (
                  <p key={t} className="text-bone/50 text-xs flex gap-2"><span className="text-acid">✓</span>{t}</p>
                ))}
              </div>
            </div>
          </aside>
        </form>
      </section>

      {/* ── RULES ──────────────────────────────────────────────── */}
      <section className="border-t border-ash bg-smoke/30 py-14">
        <div className="max-w-7xl mx-auto px-4 sm:px-6">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-5">Ground rules</p>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-x-10 gap-y-3 text-sm text-bone/60">
            {[
              "Matches are FC26 Ultimate Team — Play a Friend, 6-minute halves. Bring your best squad.",
              "Winner reports the full-time screenshot in the official group within 10 minutes.",
              "No-show after 15 minutes past your kickoff time = walkover to your opponent.",
              "Disconnects: leading player takes the win if past 60'; otherwise replay the tie.",
              "Hub seats are KNUST (Kumasi) only, with consoles + 200 Mbps internet provided — limited, first paid, first seated.",
              "Playing remotely? Your own console and a stable connection are on you — test before game day.",
              "Entry fees are non-refundable once the bracket drops.",
              "Kickoff is 1st August. Spaces are limited — registration closes the moment the bracket fills.",
            ].map((r, i) => (
              <p key={i} className="flex gap-3"><span className="text-acid font-display">{String(i + 1).padStart(2, "0")}</span>{r}</p>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
