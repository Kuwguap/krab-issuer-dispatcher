import { Link } from "react-router-dom";
import Marquee from "./Marquee";

export default function Footer() {
  return (
    <footer className="mt-24 border-t border-ash">
      <Marquee text="WEAR THE CULTURE — OG OFFCL — ACCRA TO THE WORLD — " className="bg-smoke text-bone/60" />
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
          <p className="text-bone/30 text-xs mt-4">Payments secured by Paystack · GHS</p>
        </div>
      </div>
      <div className="border-t border-ash py-5 text-center text-bone/25 text-[11px] uppercase tracking-[0.3em]">
        © {new Date().getFullYear()} OG OFFCL — All rights reserved
      </div>
    </footer>
  );
}
