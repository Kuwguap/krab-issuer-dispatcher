import { useState, FormEvent } from "react";
import { Link } from "react-router-dom";
import Marquee from "./Marquee";
import { subscribe } from "../lib/settings";

function NewsletterForm() {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "busy" | "done">("idle");
  const [err, setErr] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    setState("busy");
    const res = await subscribe(email, "newsletter");
    if (res.ok) setState("done");
    else { setState("idle"); setErr(res.error || "Try again."); }
  };

  if (state === "done") {
    return <p className="font-display uppercase text-acid text-sm tracking-widest">✓ You're subscribed.</p>;
  }
  return (
    <form onSubmit={submit} className="flex border border-ash focus-within:border-acid transition-colors max-w-sm">
      <input
        type="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="YOUR@EMAIL.COM"
        className="flex-1 min-w-0 bg-transparent border-0 px-4 py-3 font-display uppercase tracking-[0.15em] text-xs text-bone placeholder:text-bone/25 focus:outline-none"
      />
      <button type="submit" disabled={state === "busy"} className="btn-og bg-bone text-ink px-5 text-[10px] hover:bg-acid whitespace-nowrap">
        {state === "busy" ? "…" : "Sign up"}
      </button>
      {err && <span className="sr-only">{err}</span>}
    </form>
  );
}

export default function Footer() {
  return (
    <footer className="mt-24 border-t border-ash">
      <Marquee text="WEAR THE CULTURE — OG OFFCL — ACCRA TO THE WORLD — " className="bg-smoke text-bone/60" />
      <div className="border-b border-ash">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10 flex flex-col sm:flex-row sm:items-center gap-5 justify-between">
          <div>
            <p className="font-display uppercase text-bone text-lg tracking-wide">First to know, first to cop.</p>
            <p className="text-bone/40 text-xs uppercase tracking-[0.25em] mt-1">Drops, restocks and codes — straight to your inbox.</p>
          </div>
          <NewsletterForm />
        </div>
      </div>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-14 grid gap-10 md:grid-cols-3">
        <div>
          <p className="display-xl text-4xl text-bone">
            OG<span className="text-acid">.</span>OFFCL
          </p>
          <p className="text-bone/50 text-sm mt-4 max-w-xs leading-relaxed">
            Original Gangster Official. Bold streetwear born in Accra — tees, hoodies and the OG Femme line.
          </p>
        </div>
        <div className="flex flex-col gap-3">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/40 mb-1">Shop</p>
          <Link to="/shop" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">All products</Link>
          <Link to="/shop?c=og" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">OG</Link>
          <Link to="/shop?c=og-femme" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">OG Femme</Link>
        </div>
        <div className="flex flex-col gap-3">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/40 mb-1">Connect</p>
          <a href="https://instagram.com" target="_blank" rel="noreferrer" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">Instagram</a>
          <a href="mailto:ogoffcl@gmail.com" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">Email us</a>
          <Link to="/refund-policy" className="link-sweep w-fit text-bone/80 hover:text-bone uppercase text-sm font-display tracking-wider">Refund policy</Link>
          <p className="text-bone/30 text-xs mt-4">Mobile money payments secured by Moolre · GHS</p>
        </div>
      </div>
      <div className="border-t border-ash py-5 text-center text-bone/25 text-[11px] uppercase tracking-[0.3em]">
        © {new Date().getFullYear()} OG OFFCL — All rights reserved
      </div>
    </footer>
  );
}
