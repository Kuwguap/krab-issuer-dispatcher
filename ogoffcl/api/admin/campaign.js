// POST /api/admin/campaign  { subject, html, audience: waitlist|newsletter|both, test? }
// Admin-only. test = send only to the given test email. Otherwise fetches the
// audience from subscribers, sends via Resend batch, records the campaign.
import { env, sb, sendEmail, sendEmailBatch, emailShell } from "../_lib.js";

export default async (req, res) => {
  if (req.method !== "POST") return res.status(405).json({ error: "POST only" });
  if (String(req.headers["x-admin-password"] || "") !== env.adminPassword) {
    return res.status(401).json({ error: "unauthorized" });
  }
  try {
    const { subject, html, audience = "both", test } = req.body || {};
    if (!subject || !html) return res.status(400).json({ error: "subject and html are required" });

    const wrapped = emailShell(subject, html);

    if (test) {
      const r = await sendEmail({ to: String(test).trim(), subject: `[TEST] ${subject}`, html: wrapped });
      return res.json({ ok: !!r.ok, test: true, error: r.error });
    }

    let q = "subscribers?select=email,source&order=created_at.desc";
    if (audience === "waitlist" || audience === "newsletter") q += `&source=eq.${audience}`;
    const { ok, data } = await sb("GET", q);
    if (!ok) return res.status(500).json({ error: "Could not load subscribers (run migration_waitlist.sql?)" });
    const emails = [...new Set((data || []).map((s) => String(s.email || "").trim().toLowerCase()).filter(Boolean))];
    if (emails.length === 0) return res.status(400).json({ error: "No subscribers in that audience yet." });

    const result = await sendEmailBatch(
      emails.map((to) => ({ to: [to], subject, html: wrapped }))
    );

    await sb("POST", "email_campaigns", {
      subject, html, audience, sent_count: result.sent, failed_count: result.failed,
    }).catch(() => {});

    return res.json({ ok: result.ok, sent: result.sent, failed: result.failed, audience, total: emails.length });
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "campaign failed" });
  }
};
