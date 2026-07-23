import { useEffect, useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { isMissingTable } from "../lib/settings";
import { adminApi } from "./AdminLayout";

interface Template { id: string; name: string; subject: string; html: string; updated_at?: string }
interface Campaign { id: string; subject: string; audience: string; sent_count: number; failed_count: number; created_at: string }

const AUDIENCES = [
  { key: "waitlist", label: "Waitlist" },
  { key: "newsletter", label: "Newsletter" },
  { key: "both", label: "Both (everyone)" },
];

const STARTERS: { label: string; subject: string; html: string }[] = [
  {
    label: "Drop announcement",
    subject: "🔥 NEW DROP LIVE — move fast",
    html: `<p>The wait is over — the new drop just went live.</p>
<p>Limited runs, no restocks. When it's gone, it's gone.</p>
<p style="text-align:center;margin:24px 0;"><a href="https://ogoffcl.store/shop" style="background:#C8FF00;color:#0A0A0A;padding:14px 28px;text-decoration:none;font-weight:900;text-transform:uppercase;letter-spacing:1px;">Shop the drop →</a></p>`,
  },
  {
    label: "Discount code",
    subject: "A code, just for you 🖤",
    html: `<p>Because you're on the list: take <strong style="color:#C8FF00;">10% off</strong> your next piece.</p>
<p style="text-align:center;font-size:24px;letter-spacing:4px;margin:20px 0;color:#C8FF00;"><strong>OGLIST10</strong></p>
<p style="text-align:center;margin:24px 0;"><a href="https://ogoffcl.store/shop" style="background:#C8FF00;color:#0A0A0A;padding:14px 28px;text-decoration:none;font-weight:900;text-transform:uppercase;letter-spacing:1px;">Use it now →</a></p>`,
  },
  {
    label: "Store unlocked",
    subject: "🔓 The door is open",
    html: `<p>You waited — it's here. The store is unlocked and the drop is live.</p>
<p>You're hearing it first because you joined the waitlist. Don't sleep.</p>
<p style="text-align:center;margin:24px 0;"><a href="https://ogoffcl.store" style="background:#C8FF00;color:#0A0A0A;padding:14px 28px;text-decoration:none;font-weight:900;text-transform:uppercase;letter-spacing:1px;">Enter the store →</a></p>`,
  },
];

export default function AdminEmail() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [tableMissing, setTableMissing] = useState(false);
  const [name, setName] = useState("");
  const [subject, setSubject] = useState("");
  const [html, setHtml] = useState("");
  const [audience, setAudience] = useState("both");
  const [testEmail, setTestEmail] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [activeTemplate, setActiveTemplate] = useState<string | null>(null);

  const load = async () => {
    const t = await supabase.from("email_templates").select("*").order("updated_at", { ascending: false });
    if (t.error) setTableMissing(isMissingTable(t.error));
    else setTemplates((t.data || []) as Template[]);
    const c = await supabase.from("email_campaigns").select("id, subject, audience, sent_count, failed_count, created_at").order("created_at", { ascending: false }).limit(20);
    if (!c.error) setCampaigns((c.data || []) as Campaign[]);
  };
  useEffect(() => { load(); }, []);

  const say = (m: string) => { setMsg(m); setTimeout(() => setMsg(null), 4000); };

  const saveTemplate = async () => {
    if (!name.trim() || !subject.trim() || !html.trim()) { say("Name, subject and body are required to save."); return; }
    setBusy("save");
    const payload = { name: name.trim(), subject, html, updated_at: new Date().toISOString() };
    const res = activeTemplate
      ? await supabase.from("email_templates").update(payload).eq("id", activeTemplate)
      : await supabase.from("email_templates").insert(payload);
    setBusy(null);
    if (res.error) say(isMissingTable(res.error) ? "Run supabase/migration_moolre_email.sql first." : res.error.message);
    else { say(activeTemplate ? "Template updated ✓" : "Template saved ✓"); load(); }
  };

  const openTemplate = (t: Template) => {
    setActiveTemplate(t.id); setName(t.name); setSubject(t.subject); setHtml(t.html);
  };
  const newTemplate = () => { setActiveTemplate(null); setName(""); setSubject(""); setHtml(""); };
  const delTemplate = async (t: Template) => {
    if (!confirm(`Delete template "${t.name}"?`)) return;
    await supabase.from("email_templates").delete().eq("id", t.id);
    if (activeTemplate === t.id) newTemplate();
    load();
  };

  const sendTest = async () => {
    if (!testEmail.includes("@")) { say("Enter a test email address."); return; }
    if (!subject.trim() || !html.trim()) { say("Subject and body required."); return; }
    setBusy("test");
    const r = await adminApi("/api/admin/campaign", { subject, html, test: testEmail.trim() });
    setBusy(null);
    say(r.ok ? `Test sent to ${testEmail} ✓` : `Test failed: ${r.error || "check RESEND_API_KEY"}`);
  };

  const sendCampaign = async () => {
    if (!subject.trim() || !html.trim()) { say("Subject and body required."); return; }
    const label = AUDIENCES.find((a) => a.key === audience)?.label || audience;
    if (!confirm(`Send "${subject}" to the ${label} list NOW? This emails real subscribers.`)) return;
    setBusy("send");
    const r = await adminApi("/api/admin/campaign", { subject, html, audience });
    setBusy(null);
    if (r.ok) { say(`Sent to ${r.sent} subscriber${Number(r.sent) === 1 ? "" : "s"} ✓${Number(r.failed) ? ` (${r.failed} failed)` : ""}`); load(); }
    else say(`Send failed: ${r.error || "unknown"}`);
  };

  const preview = useMemo(() => `<!doctype html><html><body style="margin:0;background:#0A0A0A;padding:24px 12px;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#141414;border:1px solid #2a2a2a;">
    <div style="padding:20px 24px;border-bottom:2px solid #C8FF00;"><span style="font-size:22px;font-weight:900;color:#F5F2EA;letter-spacing:1px;">OG<span style="color:#C8FF00;">.</span>OFFCL</span></div>
    <div style="padding:24px;color:#d8d5cc;font-size:14px;line-height:1.65;">
      <h1 style="margin:0 0 14px;font-size:20px;color:#F5F2EA;text-transform:uppercase;letter-spacing:1px;">${subject || "Subject preview"}</h1>
      ${html || "<p style='color:#6b675e'>Your email body will preview here…</p>"}
    </div>
    <div style="padding:16px 24px;border-top:1px solid #2a2a2a;color:#6b675e;font-size:11px;text-transform:uppercase;letter-spacing:2px;">Original Gangster Official — Accra</div>
  </div></body></html>`, [subject, html]);

  return (
    <div className="space-y-10">
      {msg && <div className="fixed top-4 inset-x-4 sm:inset-x-auto sm:right-4 z-50 bg-acid text-ink font-display uppercase text-xs px-4 py-3 shadow-lg">{msg}</div>}
      {tableMissing && (
        <p className="border border-blood/40 bg-blood/10 text-blood text-sm px-4 py-3">
          Email tables don't exist yet — run <code>supabase/migration_moolre_email.sql</code> in the Supabase SQL editor, then reload.
        </p>
      )}

      <div className="grid lg:grid-cols-[300px_1fr] gap-8">
        {/* templates sidebar */}
        <div>
          <div className="flex items-center justify-between mb-3">
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Templates</p>
            <button onClick={newTemplate} className="btn-og bg-acid text-ink px-3 py-1.5 text-[10px] hover:bg-bone">+ New</button>
          </div>
          <div className="border border-ash divide-y divide-ash mb-5">
            {templates.map((t) => (
              <div key={t.id} className={`flex items-center gap-2 px-3 py-2.5 cursor-pointer ${activeTemplate === t.id ? "bg-acid/10" : "hover:bg-bone/5"}`} onClick={() => openTemplate(t)}>
                <span className="text-sm text-bone truncate flex-1">{t.name}</span>
                <button onClick={(e) => { e.stopPropagation(); delTemplate(t); }} className="px-3 py-2.5 -my-2.5 -mr-1 text-blood/60 hover:text-blood text-xs">✕</button>
              </div>
            ))}
            {templates.length === 0 && <p className="px-3 py-4 text-bone/40 text-xs">No saved templates yet.</p>}
          </div>

          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-3">Starters</p>
          <div className="space-y-1.5">
            {STARTERS.map((s) => (
              <button key={s.label} onClick={() => { setActiveTemplate(null); setName(s.label); setSubject(s.subject); setHtml(s.html); }}
                className="btn-og w-full border-2 border-ash text-bone/70 px-3 py-2 text-[10px] hover:border-acid hover:text-bone justify-start">
                ⚡ {s.label}
              </button>
            ))}
          </div>

          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mt-8 mb-3">Recent campaigns</p>
          <div className="space-y-2">
            {campaigns.map((c) => (
              <div key={c.id} className="border border-ash px-3 py-2 text-xs">
                <p className="text-bone truncate">{c.subject}</p>
                <p className="text-bone/40 mt-0.5">{c.audience} · sent {c.sent_count}{c.failed_count ? ` · failed ${c.failed_count}` : ""} · {new Date(c.created_at).toLocaleDateString()}</p>
              </div>
            ))}
            {campaigns.length === 0 && <p className="text-bone/40 text-xs">No campaigns sent yet.</p>}
          </div>
        </div>

        {/* composer */}
        <div className="space-y-4">
          <div className="grid sm:grid-cols-2 gap-3">
            <input placeholder="Template name (for saving)" value={name} onChange={(e) => setName(e.target.value)} className="px-4 py-3 text-sm" />
            <select value={audience} onChange={(e) => setAudience(e.target.value)} className="px-4 py-3 text-sm">
              {AUDIENCES.map((a) => <option key={a.key} value={a.key}>Audience: {a.label}</option>)}
            </select>
          </div>
          <input placeholder="Email subject" value={subject} onChange={(e) => setSubject(e.target.value)} className="w-full px-4 py-3 text-sm" />
          <textarea
            placeholder="Email body (HTML). Keep it simple: <p>, <strong>, <a>. It gets wrapped in the OG OFFCL branded shell automatically."
            rows={10} value={html} onChange={(e) => setHtml(e.target.value)}
            className="w-full px-4 py-3 text-xs font-mono leading-relaxed"
          />

          <div className="flex flex-wrap gap-2">
            <button disabled={busy === "save"} onClick={saveTemplate} className="btn-og border-2 border-bone text-bone px-5 py-2.5 text-xs hover:bg-bone hover:text-ink">
              {busy === "save" ? "Saving…" : activeTemplate ? "Update template" : "Save as template"}
            </button>
            <div className="flex ml-auto">
              <input placeholder="you@email.com" value={testEmail} onChange={(e) => setTestEmail(e.target.value)} className="px-3 py-2.5 text-xs w-44" />
              <button disabled={busy === "test"} onClick={sendTest} className="btn-og bg-bone text-ink px-4 text-xs hover:bg-acid">
                {busy === "test" ? "…" : "Send test"}
              </button>
            </div>
            <button disabled={busy === "send"} onClick={sendCampaign} className="btn-og bg-acid text-ink px-6 py-2.5 text-xs hover:bg-bone">
              {busy === "send" ? "Sending…" : `Send to ${AUDIENCES.find((a) => a.key === audience)?.label}`}
            </button>
          </div>

          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 pt-2">Live preview</p>
          <iframe title="preview" srcDoc={preview} className="w-full h-[420px] border border-ash bg-ink" />
        </div>
      </div>
    </div>
  );
}
