import { useEffect, useMemo, useRef, useState, FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase } from "../lib/supabase";
import { money, CURRENCY } from "../lib/money";
import { useCart } from "../store/CartContext";
import Reveal from "../components/Reveal";

interface Applied {
  code: string;
  percentage: number;
}

type PayState =
  | { step: "form" }
  | { step: "otp"; orderId: string; ref: string; message: string }
  | { step: "waiting"; orderId: string; ref: string }
  | { step: "failed"; orderId: string; error: string };

const NETWORKS: { key: "mtn" | "telecel" | "at"; label: string }[] = [
  { key: "mtn", label: "MTN MoMo" },
  { key: "telecel", label: "Telecel Cash" },
  { key: "at", label: "AT Money" },
];

export default function Checkout() {
  const { items, subtotal, clear } = useCart();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", phone: "", address: "", city: "", note: "" });
  const [momoPhone, setMomoPhone] = useState("");
  const [network, setNetwork] = useState<"mtn" | "telecel" | "at">("mtn");
  const [codeInput, setCodeInput] = useState("");
  const [applied, setApplied] = useState<Applied | null>(null);
  const [codeMsg, setCodeMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [pay, setPay] = useState<PayState>({ step: "form" });
  const [otp, setOtp] = useState("");
  const [otpBusy, setOtpBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  const discountAmount = useMemo(
    () => (applied ? Math.round(subtotal * (applied.percentage / 100) * 100) / 100 : 0),
    [applied, subtotal],
  );
  const total = Math.max(0, subtotal - discountAmount);

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  useEffect(() => () => { if (pollRef.current) window.clearInterval(pollRef.current); }, []);

  const applyCode = async () => {
    const code = codeInput.trim().toUpperCase();
    if (!code) return;
    setCodeMsg(null);
    const { data, error } = await supabase
      .from("discount_codes").select("*").eq("code", code).eq("is_active", true).single();
    if (error || !data) { setApplied(null); setCodeMsg("Invalid or inactive code."); return; }
    if (data.expires_at && new Date(data.expires_at) < new Date()) { setApplied(null); setCodeMsg("This code has expired."); return; }
    const pct = Number(data.percentage ?? 0);
    if (!pct) { setApplied(null); setCodeMsg("This code has no discount attached."); return; }
    setApplied({ code, percentage: pct });
    setCodeMsg(`Applied — ${pct}% off.`);
  };

  const startPolling = (orderId: string, ref: string) => {
    setPay({ step: "waiting", orderId, ref });
    let tries = 0;
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      tries += 1;
      try {
        const r = await fetch(`/api/pay/status?ref=${encodeURIComponent(ref)}`);
        const j = await r.json();
        if (j.state === "paid") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          clear();
          navigate(`/order-confirmation?ref=${encodeURIComponent(j.orderNumber || ref)}&paid=1`);
        } else if (j.state === "failed") {
          if (pollRef.current) window.clearInterval(pollRef.current);
          setPay({ step: "failed", orderId, error: j.message || "Payment failed or was declined." });
        } else if (tries > 40) {
          // ~2 minutes — leave the order pending, show the confirmation page.
          if (pollRef.current) window.clearInterval(pollRef.current);
          navigate(`/order-confirmation?ref=${encodeURIComponent(ref)}&paid=0`);
        }
      } catch { /* keep polling */ }
    }, 3000);
  };

  const initiate = async (orderId: string, otpcode?: string, ref?: string) => {
    const r = await fetch("/api/pay/initiate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      // ref is passed back verbatim on OTP submit so Moolre continues the SAME
      // charge (same externalref) instead of starting a new one + new OTP.
      body: JSON.stringify({ orderId, phone: momoPhone, channel: network, otpcode, ref }),
    });
    const j = await r.json().catch(() => ({}));
    setOtpBusy(false);
    if (j.state === "otp" && otpcode) setOtp(""); // wrong/expired code — clear for retry
    if (j.state === "paid") {
      clear();
      navigate(`/order-confirmation?ref=${encodeURIComponent(j.ref)}&paid=1`);
      return;
    }
    if (j.state === "otp") { setPay({ step: "otp", orderId, ref: j.ref, message: j.message }); return; }
    if (j.state === "pending") { startPolling(orderId, j.ref); return; }
    setPay({ step: "failed", orderId, error: j.error || "Could not start the payment." });
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (items.length === 0) return;
    if (!form.name.trim() || !form.email.includes("@") || !form.phone.trim() || !form.address.trim()) {
      setErr("Fill in your name, a valid email, phone and delivery address.");
      return;
    }
    const momo = momoPhone.replace(/\D/g, "");
    if (momo.length < 9) { setErr("Enter the mobile money number that will pay."); return; }
    setBusy(true);
    try {
      // Order is created SERVER-SIDE (service-role): bypasses RLS and computes
      // the real total from the DB so the price can't be tampered with.
      const r = await fetch("/api/order/create", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          customer: {
            name: form.name.trim(), email: form.email.trim(), phone: form.phone.trim(),
            address: form.address.trim(), city: form.city.trim(), note: form.note.trim(),
          },
          items: items.map((i) => ({ productId: i.productId, size: i.size, qty: i.qty })),
          discountCode: applied?.code ?? null,
        }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.orderId) throw new Error(j.error || "Could not create the order. Try again.");

      await initiate(j.orderId);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : "Something went wrong. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (items.length === 0 && pay.step === "form") {
    return (
      <div className="max-w-2xl mx-auto px-4 py-32 text-center">
        <p className="display-xl text-6xl text-bone/15">Empty bag</p>
        <p className="text-bone/50 uppercase tracking-widest text-sm mt-5">Add something to the cart before checkout.</p>
        <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-8 inline-flex hover:bg-bone">
          Back to the shop →
        </Link>
      </div>
    );
  }

  // ── Payment overlay states ────────────────────────────────────────────
  if (pay.step === "waiting") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <div className="w-20 h-20 mx-auto border-4 border-acid border-t-transparent rounded-full animate-spin mb-8" />
        <h1 className="display-xl text-4xl text-bone">Check your phone</h1>
        <p className="text-bone/60 mt-4 leading-relaxed">
          Approve the <strong className="text-bone">{money(total)}</strong> mobile-money prompt on{" "}
          <strong className="text-acid">{momoPhone}</strong>. This page updates automatically once it lands.
        </p>
        <p className="text-bone/40 text-xs mt-3">
          Payment is processed by <strong className="text-bone/70">Moolre</strong> — any SMS about this payment comes from Moolre.
        </p>
        <p className="text-bone/30 text-xs uppercase tracking-[0.25em] mt-8 animate-pulseSoft">Waiting for approval…</p>
      </div>
    );
  }

  if (pay.step === "otp") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-bone">Enter the code</h1>
        <p className="text-bone/60 mt-4">
          {pay.message || "A one-time code was sent to your phone."}
        </p>
        <p className="text-bone/40 text-xs uppercase tracking-[0.2em] mt-3">
          Look for an SMS from <strong className="text-acid">Moolre</strong> · sent to {momoPhone}
        </p>
        <form
          onSubmit={(e) => { e.preventDefault(); if (otp.trim()) { setOtpBusy(true); initiate(pay.orderId, otp.trim(), pay.ref); } }}
          className="mt-8 flex border-2 border-bone/20 focus-within:border-acid transition-colors"
        >
          <input
            autoFocus value={otp} onChange={(e) => setOtp(e.target.value)}
            inputMode="numeric" placeholder="OTP CODE"
            className="flex-1 min-w-0 bg-transparent border-0 px-5 py-4 font-display uppercase tracking-[0.3em] text-sm text-bone placeholder:text-bone/25 focus:outline-none"
          />
          <button type="submit" disabled={otpBusy} className="btn-og bg-acid text-ink px-6 text-xs hover:bg-bone whitespace-nowrap">
            {otpBusy ? "Verifying…" : "Verify & pay"}
          </button>
        </form>
        <p className="text-bone/30 text-[11px] mt-4">Didn't get it? Approve the prompt on your phone, or wait for the SMS.</p>
      </div>
    );
  }

  if (pay.step === "failed") {
    return (
      <div className="max-w-md mx-auto px-4 py-28 text-center">
        <h1 className="display-xl text-4xl text-blood">Payment failed</h1>
        <p className="text-bone/60 mt-4">{pay.error}</p>
        <div className="flex gap-3 justify-center mt-8">
          <button onClick={() => initiate(pay.orderId)} className="btn-og bg-acid text-ink px-6 py-3 text-xs hover:bg-bone">
            Try again
          </button>
          <button onClick={() => setPay({ step: "form" })} className="btn-og border-2 border-bone text-bone px-6 py-3 text-xs hover:bg-bone hover:text-ink">
            Edit details
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 py-12">
      <Reveal>
        <h1 className="display-xl text-6xl sm:text-7xl text-bone mb-12">
          Check<span className="text-stroke-acid">out</span>
        </h1>
      </Reveal>

      <div className="grid lg:grid-cols-[1fr_420px] gap-12">
        <form onSubmit={submit} className="space-y-5 order-2 lg:order-1">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Delivery details</p>
          <div className="grid sm:grid-cols-2 gap-4">
            <input required placeholder="Full name *" value={form.name} onChange={set("name")} className="px-4 py-4 text-sm w-full" />
            <input required type="email" placeholder="Email *" value={form.email} onChange={set("email")} className="px-4 py-4 text-sm w-full" />
          </div>
          <input required type="tel" placeholder="Phone (for the rider) *" value={form.phone} onChange={set("phone")} className="px-4 py-4 text-sm w-full" />
          <input required placeholder="Delivery address *" value={form.address} onChange={set("address")} className="px-4 py-4 text-sm w-full" />
          <div className="grid sm:grid-cols-2 gap-4">
            <input placeholder="City / Area" value={form.city} onChange={set("city")} className="px-4 py-4 text-sm w-full" />
            <input placeholder="Delivery note (optional)" value={form.note} onChange={set("note")} className="px-4 py-4 text-sm w-full" />
          </div>

          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 pt-4">Pay with mobile money</p>
          <div className="flex gap-2">
            {NETWORKS.map((n) => (
              <button
                key={n.key} type="button" onClick={() => setNetwork(n.key)}
                className={`btn-og flex-1 px-3 py-3 text-xs border-2 ${network === n.key ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}
              >
                {n.label}
              </button>
            ))}
          </div>
          <input
            required type="tel" placeholder="MoMo number that will pay *"
            value={momoPhone} onChange={(e) => setMomoPhone(e.target.value)}
            className="px-4 py-4 text-sm w-full"
          />

          {err && <p className="text-blood text-sm border border-blood/40 bg-blood/10 px-4 py-3">{err}</p>}

          <button type="submit" disabled={busy} className={`btn-og w-full py-5 text-sm ${busy ? "bg-ash text-bone/40" : "bg-acid text-ink hover:bg-bone"}`}>
            {busy ? "Sending prompt…" : `Pay ${money(total)} — approve on your phone`}
          </button>
          <p className="text-bone/30 text-[11px] uppercase tracking-widest text-center">
            MTN · Telecel · AT — secured by Moolre · <Link to="/refund-policy" className="underline hover:text-bone/60">Refund policy</Link>
          </p>
        </form>

        <aside className="order-1 lg:order-2">
          <div className="border border-ash bg-smoke/60 p-6 lg:sticky lg:top-24">
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-5">Order summary</p>
            <div className="space-y-4 max-h-72 overflow-y-auto pr-1">
              {items.map((i) => (
                <div key={i.productId + (i.size || "")} className="flex gap-3 items-center">
                  <div className="w-14 h-16 bg-bone overflow-hidden shrink-0">
                    {i.image && <img src={i.image} alt="" className="w-full h-full object-cover mix-blend-multiply" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-display uppercase text-xs text-bone truncate">{i.name}</p>
                    <p className="text-bone/40 text-[11px] uppercase mt-0.5">{i.size ? `Size ${i.size} · ` : ""}Qty {i.qty}</p>
                  </div>
                  <span className="text-bone/80 text-sm">{money(i.price * i.qty)}</span>
                </div>
              ))}
            </div>

            <div className="mt-6 flex">
              <input
                placeholder="DISCOUNT CODE" value={codeInput} onChange={(e) => setCodeInput(e.target.value)}
                className="flex-1 px-3 py-3 text-xs font-display uppercase tracking-[0.2em]"
              />
              <button type="button" onClick={applyCode} className="btn-og bg-bone text-ink px-5 text-xs hover:bg-acid">
                Apply
              </button>
            </div>
            {codeMsg && <p className={`text-xs mt-2 ${applied ? "text-acid" : "text-blood"}`}>{codeMsg}</p>}

            <div className="border-t border-ash mt-6 pt-4 space-y-2 text-sm">
              <div className="flex justify-between text-bone/60"><span>Subtotal</span><span>{money(subtotal)}</span></div>
              {applied && (
                <div className="flex justify-between text-acid"><span>{applied.code} (−{applied.percentage}%)</span><span>−{money(discountAmount)}</span></div>
              )}
              <div className="flex justify-between text-bone/60"><span>Delivery</span><span>Paid to rider / arranged</span></div>
              <div className="flex justify-between font-display uppercase text-bone text-lg pt-2">
                <span>Total</span><span className="text-acid">{money(total)}</span>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
