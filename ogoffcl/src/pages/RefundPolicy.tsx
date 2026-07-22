import { Link } from "react-router-dom";
import Reveal from "../components/Reveal";

const SECTIONS: [string, string][] = [
  [
    "Wrong or damaged item",
    "If we sent the wrong piece, the wrong size, or something arrived damaged, tell us within 48 hours of delivery and we'll make it right — a replacement where stock allows, or a full refund straight back to the mobile money number you paid with.",
  ],
  [
    "Change of mind",
    "Limited runs move fast, so change-of-mind returns are accepted within 3 days of delivery as long as the piece is unworn, unwashed and in its original condition. Delivery fees aren't refundable and the return ride is on you.",
  ],
  [
    "Order cancelled before shipping",
    "Cancel any time before your order ships and you get a full refund, no questions asked.",
  ],
  [
    "How refunds are paid",
    "Refunds go back to the same mobile money number that paid, usually instantly once processed on our side. You'll get an email confirmation the moment it's sent.",
  ],
  [
    "How to start one",
    "Reply to any of your order emails, or reach us on Instagram @ogoffcl with your order number (it looks like OG-XXXXXXXX). We answer fast.",
  ],
];

export default function RefundPolicy() {
  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 py-16">
      <Reveal>
        <p className="font-display uppercase text-acid tracking-[0.45em] text-[11px] mb-4">The fine print</p>
        <h1 className="display-xl text-5xl sm:text-7xl text-bone mb-10">
          Refund <span className="text-stroke-acid">policy</span>
        </h1>
      </Reveal>
      <div className="space-y-8">
        {SECTIONS.map(([h, body], i) => (
          <Reveal key={h} delay={i * 0.05}>
            <div className="border border-ash bg-smoke/40 p-6">
              <h2 className="font-display uppercase text-bone text-lg tracking-wide mb-2">{h}</h2>
              <p className="text-bone/60 leading-relaxed text-sm">{body}</p>
            </div>
          </Reveal>
        ))}
      </div>
      <Reveal delay={0.2}>
        <div className="mt-12 text-center">
          <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm hover:bg-bone">
            Back to the shop →
          </Link>
        </div>
      </Reveal>
    </div>
  );
}
