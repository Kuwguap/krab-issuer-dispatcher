// POST /api/email/welcome  { email, source: waitlist|newsletter }
// Fired by the site right after a successful subscribe. Best-effort.
import { sendEmail, emailShell, notifyBot, sbCount } from "../_lib.js";

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  try {
    const email = String((req.body || {}).email || "").trim().toLowerCase();
    const source = String((req.body || {}).source || "newsletter");
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) return res.status(400).json({ error: "invalid email" });

    const waitlist = source === "waitlist";
    const r = await sendEmail({
      to: email,
      subject: waitlist ? "You're on the OG OFFCL waitlist 🔒" : "Welcome to OG OFFCL 🖤",
      html: emailShell(
        waitlist ? "You're on the list." : "First to know, first to cop.",
        waitlist
          ? `<p>The store is locked while we load the next drop — and you're now first in line. The second the door opens, you'll hear it here first.</p>
             <p style="color:#8b877e;">Limited runs. No restocks. Move fast.</p>`
          : `<p>You're in. Drops, restocks and codes land in your inbox before anywhere else.</p>
             <p style="color:#8b877e;">Heavyweight fabric. Loud prints. Born in Accra.</p>`
      ),
    });

    // best-effort push to the Telegram bot with the running list size
    const count = await sbCount("subscribers");
    await notifyBot("waitlist.joined", { email, source, count });

    return res.json({ ok: !!r.ok, skipped: !!r.skipped });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "welcome failed" });
  }
};
