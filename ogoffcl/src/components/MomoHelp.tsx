/**
 * "No prompt?" rescue card for the payment waiting screens. Push prompts —
 * MTN especially — often never pop up on the phone; the payer must approve
 * the pending debit manually from their wallet's Approvals menu. This is the
 * #1 reason payments stall on "pending".
 */
const STEPS: Record<string, { code: string; path: string }> = {
  mtn: { code: "*170#", path: "6) My Wallet → 3) My Approvals" },
  telecel: { code: "*110#", path: "My Approvals" },
  at: { code: "*110#", path: "My Money → Approvals" },
};

export default function MomoHelp({ network }: { network: "mtn" | "telecel" | "at" }) {
  const s = STEPS[network] || STEPS.mtn;
  return (
    <div className="mt-8 border border-acid/40 bg-acid/5 p-5 text-left">
      <p className="font-display uppercase text-acid text-xs tracking-[0.25em] mb-2">No prompt? Approve it manually</p>
      <p className="text-bone/70 text-sm leading-relaxed">
        Prompts often don't pop up. Dial <strong className="text-acid">{s.code}</strong> on the paying phone,
        open <strong className="text-bone">{s.path}</strong>, and approve the OG OFFCL payment waiting there.
      </p>
      <p className="text-bone/35 text-xs mt-2">This page keeps checking — it updates the moment you approve.</p>
    </div>
  );
}
