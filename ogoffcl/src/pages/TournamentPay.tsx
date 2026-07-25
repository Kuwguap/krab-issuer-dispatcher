import { useEffect, useRef, useState, FormEvent } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { motion } from "framer-motion";
import { usePageMeta } from "../lib/seo";
import MomoHelp from "../components/MomoHelp";
import { gtagOnce } from "../lib/track";

/**
 * Standalone payment page for an existing tournament registration —
 * the admin sends this link when someone's payment timed out or failed.
 * The player id (unguessable UUID) is the capability; the API returns
 * only sanitized fields for it (gamertag/fee/status — no PII).
 */
const SNAP_GROUP = "https://snapchat.com/t/kHlg89aj";

const NETWORKS: { key: "mtn" | "telecel" | "at"; label: string }[] = [
  { key: "mtn", label: "MTN MoMo" },
  { key: "telecel", label: "Telecel Cash" },
  { key: "at", label: "AT Money" },
];

interface PlayerInfo {
  id: string; gamertag: string; platform: string; hub: boolean;
  fee: number; paymentStatus: string;
}

type PayState =
  | { step: "form" }
  | { step: "otp"; ref: string; message: string }
  | { step: "waiting"; ref: string }
  | { step: "done" }
  | { step: "failed"; error: string };

export default function TournamentPay() {
  const { playerId } = useParams();
  const [params] = useSearchParams();
  const returnedRef = params.get("ref"); // set by Moolre's post-payment redirect
  usePageMeta({
    title: "Complete your entry — OG OFFCL FC26 Tournament",
    description: "Finish your OG OFFCL FC26 tournament payment and lock your spot on the bracket.",
    path: `/tournament/pay/${playerId || ""}`,
    noindex: true,
  });

  const [player, setPlayer] = useState<PlayerInfo | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [momoPhone, setMomoPhone] = useState("");
  const [network, setNetwork] = useState<"mtn" | "telecel" | "at">("mtn");
  // Payments run on Moolre's hosted page; direct-prompt fields only appear
  // if the hosted link ever fails.
  const [showDirect, setShowDirect] = useState(false);
  const [pay, setPay] = useState<PayState>({ step: "form" });
  const [otp, setOtp] = useState("");
  const [otpBusy, setOtpBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    if (!playerId) { setNotFound(true); return; }
    fetch(`/api/tournament?action=player&id=${encodeURIComponent(playerId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (!j || !j.id) { setNotFound(true); return; }
        setPlayer(j);
        if (j.paymentStatus === "paid") setPay({ step: "done" });
        // back from Moolre's hosted page — confirm the payment
        else if (returnedRef) startPolling(returnedRef);
      })
      .catch(() => setNotFound(true));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [playerId]);

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  // Google Ads conversion signal — tournament entry paid (pay-link flow).
  useEffect(() => {
    if (pay.step !== "done" || !player) return;
    gtagOnce(`trn_${player.id}`, "tournament_entry", { value: player.fee, currency: "GHS" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pay.step]);

  const startPolling = (ref: string) => {
    setPay({ step: "waiting", ref });
    let tries = 0;
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      tries += 1;
      try {
        const r = await fetch(`/api/tournament?action=status&ref=${encodeURIComponent(ref)}`);
        const j = await r.json();
        if (j.state === "paid") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "done" });
        } else if (j.state === "failed") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "failed", error: j.message || "Payment failed or was declined." });
        } else if (tries > 100) {
          // ~5 min — manual USSD approval takes a while; don't cut people off early
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "failed", error: "The payment window timed out — try again." });
        }
      } catch { /* keep polling */ }
    }, 3000);
  };

  const initiate = async (otpcode?: string, ref?: string) => {
    if (!playerId) return;
    const r = await fetch("/api/tournament", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "pay", playerId, phone: momoPhone, channel: network, otpcode, ref }),
    });
    const j = await r.json().catch(() => ({}));
    setOtpBusy(false);
    if (j.state === "otp" && otpcode) setOtp("");
    if (j.state === "paid") { setPay({ step: "done" }); return; }
    // hosted Moolre payment page — Moolre redirects back here (?ref=) after
    if (j.state === "link" && j.url) { window.location.href = j.url; return; }
    if (j.state === "need-details") { setShowDirect(true); setErr(j.error || "Enter your MoMo number to pay by direct prompt."); return; }
    if (j.state === "otp") { setPay({ step: "otp", ref: j.ref, message: j.message }); return; }
    if (j.state === "pending") { startPolling(j.ref); return; }
    setPay({ step: "failed", error: j.error || "Could not start the payment." });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (showDirect && momoPhone.replace(/\D/g, "").length < 9) { setErr("Enter the mobile money number that will pay."); return; }
    setBusy(true);
    try { await initiate(); } finally { setBusy(false); }
  };

  if (notFound) {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-bone">Link not <span className="text-stroke">found</span></h1>
        <p className="text-bone/60 mt-4 leading-relaxed">This payment link doesn't match a registration. It may have been removed — register again.</p>
        <Link to="/tournament" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-8 inline-flex hover:bg-bone">Go to the tournament →</Link>
      </div>
    );
  }
  if (!player) {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <div className="w-16 h-16 mx-auto border-4 border-acid border-t-transparent rounded-full animate-spin mb-6" />
        <p className="text-bone/40 text-xs uppercase tracking-[0.3em]">Loading your registration…</p>
      </div>
    );
  }

  if (pay.step === "done") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <motion.div initial={{ scale: 0.7, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} className="w-24 h-24 mx-auto border-4 border-acid flex items-center justify-center font-display text-5xl text-acid mb-8">✓</motion.div>
        <h1 className="display-xl text-4xl sm:text-5xl text-bone">{player.gamertag}, <span className="text-acid">you're on the bracket.</span></h1>
        <p className="text-bone/60 mt-5 leading-relaxed">Payment received. One thing left — join the Snapchat group. That's where the bracket, kickoff times and score reporting happen.</p>
        <a href={SNAP_GROUP} target="_blank" rel="noreferrer" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-8 inline-flex hover:bg-bone">
          👻 Join the Snapchat group →
        </a>
        <p className="text-bone/30 text-[11px] uppercase tracking-[0.25em] mt-8">No restocks on glory.</p>
      </div>
    );
  }
  if (pay.step === "waiting") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <div className="w-20 h-20 mx-auto border-4 border-acid border-t-transparent rounded-full animate-spin mb-8" />
        <h1 className="display-xl text-4xl text-bone">Check your phone</h1>
        <p className="text-bone/60 mt-4 leading-relaxed">
          {momoPhone
            ? <>Approve the <strong className="text-bone">GH₵{player.fee}</strong> mobile-money prompt on <strong className="text-acid">{momoPhone}</strong>.</>
            : <>Confirming your <strong className="text-bone">GH₵{player.fee}</strong> payment — this updates by itself.</>}
        </p>
        <p className="text-bone/40 text-xs mt-3">Payment is processed by <strong className="text-bone/70">Moolre</strong> — any SMS about this payment comes from Moolre.</p>
        <MomoHelp network={network} />
      </div>
    );
  }
  if (pay.step === "otp") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-bone">Enter the code</h1>
        <p className="text-bone/60 mt-4">{pay.message || "A one-time code was sent to your phone."}</p>
        <p className="text-bone/40 text-xs uppercase tracking-[0.2em] mt-3">Look for an SMS from <strong className="text-acid">Moolre</strong> · sent to {momoPhone}</p>
        <form onSubmit={(e) => { e.preventDefault(); if (otp.trim()) { setOtpBusy(true); initiate(otp.trim(), pay.ref); } }} className="mt-8 flex border-2 border-bone/20 focus-within:border-acid transition-colors">
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
        <button onClick={() => setPay({ step: "form" })} className="btn-og bg-acid text-ink px-6 py-3 text-xs mt-8 hover:bg-bone">Try again</button>
      </div>
    );
  }

  return (
    <div className="max-w-md mx-auto px-4 py-20">
      <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">Complete your entry · 1st August</p>
      <h1 className="display-xl text-4xl sm:text-5xl text-bone mb-2">
        {player.gamertag}<span className="text-acid">.</span>
      </h1>
      <p className="text-bone/50 text-sm uppercase tracking-[0.2em] mb-8">
        {player.platform.toUpperCase()}{player.hub ? " · Hub seat (KNUST)" : ""} · GH₵{player.fee} due
      </p>

      <div className="border border-ash bg-smoke/50 p-5 mb-8 text-sm space-y-2">
        <div className="flex justify-between text-bone/60"><span>Tournament entry</span><span>GH₵50</span></div>
        {player.hub && <div className="flex justify-between text-acid"><span>OGOFFCL Hub seat</span><span>+GH₵20</span></div>}
        <div className="flex justify-between font-display uppercase text-bone pt-2 border-t border-ash">
          <span>Total</span><span className="text-acid">GH₵{player.fee}</span>
        </div>
      </div>

      <form onSubmit={submit} className="space-y-5">
        {showDirect && (
          <>
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Pay with mobile money</p>
            <div className="flex gap-2">
              {NETWORKS.map((n) => (
                <button key={n.key} type="button" onClick={() => setNetwork(n.key)}
                  className={`btn-og flex-1 px-3 py-3 text-xs border-2 ${network === n.key ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}>
                  {n.label}
                </button>
              ))}
            </div>
            <input type="tel" placeholder="MoMo number that will pay *" value={momoPhone} onChange={(e) => setMomoPhone(e.target.value)} className="px-4 py-4 text-sm w-full" />
          </>
        )}
        {err && <p className="text-blood text-sm border border-blood/40 bg-blood/10 px-4 py-3">{err}</p>}
        <button type="submit" disabled={busy} className={`btn-og w-full py-5 text-sm ${busy ? "bg-ash text-bone/40" : "bg-acid text-ink hover:bg-bone"}`}>
          {busy ? "Opening secure payment…" : `Pay GH₵${player.fee} — lock my spot`}
        </button>
        <p className="text-bone/30 text-[11px] uppercase tracking-widest text-center">
          You'll enter your MoMo number on Moolre's secure page — MTN · Telecel · AT
        </p>
        <p className="text-bone/35 text-xs leading-relaxed text-center">
          Approve the payment on Moolre's page and you'll land right back here with your spot confirmed.
        </p>
      </form>
    </div>
  );
}
