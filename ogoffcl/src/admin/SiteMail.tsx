import { useEffect, useMemo, useState } from "react";
import {
  fetchSiteLocked, setSiteLocked, setTournamentOpen, listSubscribers, deleteSubscriber,
  getLockVideo, setLockVideo, SubscriberRow,
} from "../lib/settings";
import { uploadVideo } from "../lib/supabase";

const MIGRATION_HINT =
  "Run supabase/migration_waitlist.sql once in the Supabase SQL editor (creates site_settings + subscribers), then reload this page.";

export default function AdminSiteMail() {
  const [locked, setLocked] = useState<boolean | null>(null);
  const [tournamentOpen, setTournamentOpenState] = useState(false);
  const [settingsMissing, setSettingsMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const [subs, setSubs] = useState<SubscriberRow[]>([]);
  const [subsMissing, setSubsMissing] = useState(false);
  const [filter, setFilter] = useState<"all" | "waitlist" | "newsletter" | "tournament">("all");
  const [copied, setCopied] = useState(false);

  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [vidBusy, setVidBusy] = useState(false);
  const [vidMsg, setVidMsg] = useState<string | null>(null);

  const load = async () => {
    const s = await fetchSiteLocked();
    setLocked(s.locked);
    setTournamentOpenState(s.tournamentOpen);
    setSettingsMissing(s.tableMissing);
    getLockVideo().then(setVideoUrl).catch(() => {});
    const l = await listSubscribers();
    setSubs(l.rows);
    setSubsMissing(l.tableMissing);
  };

  const onVideo = async (file: File | null) => {
    if (!file) return;
    if (!/^video\//.test(file.type)) { setVidMsg("Please choose a video file (MP4 or WebM)."); return; }
    const mb = file.size / 1048576;
    if (mb > 45) { setVidMsg(`That's ${mb.toFixed(0)}MB — too large. Compress to a short web loop (aim under ~15MB) and try again.`); return; }
    setVidBusy(true); setVidMsg(mb > 15 ? "Uploading… (large file — trim it for faster visitor loads)" : "Uploading…");
    try {
      const url = await uploadVideo(file);
      const r = await setLockVideo(url);
      if (!r.ok) throw new Error(r.error);
      setVideoUrl(url);
      setVidMsg("Video live on the lock screen ✓");
    } catch (ex) {
      setVidMsg(`Upload failed: ${ex instanceof Error ? ex.message : "try again"}`);
    } finally {
      setVidBusy(false);
    }
  };

  const removeVideo = async () => {
    if (!confirm("Remove the lock-screen video? It reverts to the moving-type background.")) return;
    setVidBusy(true);
    const r = await setLockVideo(null);
    setVidBusy(false);
    if (r.ok) { setVideoUrl(null); setVidMsg("Video removed."); }
    else setVidMsg(`Could not remove: ${r.error}`);
  };
  useEffect(() => { load(); }, []);

  const toggle = async () => {
    if (locked === null) return;
    setBusy(true); setMsg(null);
    const res = await setSiteLocked(!locked, tournamentOpen);
    if (res.ok) {
      setLocked(!locked);
      if (!locked) {
        // Locking: invalidate THIS browser's staff bypass too, so what you see
        // is what visitors see. Re-enter the staff code to browse while locked.
        try {
          localStorage.removeItem("ogoffcl_unlocked_v2");
          localStorage.removeItem("ogoffcl_unlocked_v1");
        } catch {}
        setMsg("Site is now LOCKED — everyone (including this browser) sees the waitlist. Use the staff code to browse.");
      } else {
        setMsg("Site is now OPEN.");
      }
    } else {
      setMsg(res.tableMissing ? MIGRATION_HINT : `Could not save: ${res.error}`);
      if (res.tableMissing) setSettingsMissing(true);
    }
    setBusy(false);
  };

  const toggleTournamentOpen = async () => {
    const next = !tournamentOpen;
    setTournamentOpenState(next);
    if (locked) {
      // live change while locked — preserves locked_at, staff bypasses stay valid
      const r = await setTournamentOpen(next);
      setMsg(r.ok
        ? next ? "Tournament pages are now LIVE while the store stays locked." : "Tournament pages are now locked too."
        : `Could not save: ${r.error}`);
      if (!r.ok) setTournamentOpenState(!next);
    }
  };

  const visible = useMemo(
    () => subs.filter((s) => (filter === "all" ? true : (s.source || "newsletter") === filter)),
    [subs, filter],
  );

  const copyEmails = async () => {
    try {
      await navigator.clipboard.writeText(visible.map((s) => s.email).join(", "));
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {}
  };

  const downloadCsv = () => {
    const rows = [["email", "source", "subscribed_at"], ...visible.map((s) => [s.email, s.source || "", s.created_at])];
    const csv = rows.map((r) => r.map((f) => `"${String(f).replace(/"/g, '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `ogoffcl-subscribers-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const del = async (s: SubscriberRow) => {
    if (!confirm(`Remove ${s.email} from the list?`)) return;
    await deleteSubscriber(s.id);
    load();
  };

  return (
    <div className="space-y-10">
      {/* ── Lock control ─────────────────────────────────────── */}
      <section className="border border-ash bg-smoke/50 p-6 max-w-2xl">
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Site lock</p>
        {settingsMissing ? (
          <div className="text-sm text-bone/70 space-y-3">
            <p className="text-blood">The settings table doesn't exist yet.</p>
            <p>{MIGRATION_HINT}</p>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-5">
            <div className={`px-4 py-2 font-display uppercase text-sm tracking-widest ${locked ? "bg-blood/15 text-blood" : "bg-acid/15 text-acid"}`}>
              {locked === null ? "…" : locked ? "Locked — waitlist showing" : "Open — store live"}
            </div>
            <button
              onClick={toggle}
              disabled={busy || locked === null}
              className={`btn-og px-6 py-3 text-xs ${locked ? "bg-acid text-ink hover:bg-bone" : "bg-blood text-bone hover:bg-bone hover:text-ink"}`}
            >
              {busy ? "Saving…" : locked ? "Unlock the store" : "Lock the store"}
            </button>
          </div>
        )}
        {!settingsMissing && (
          <label className={`flex items-start gap-3 mt-5 p-4 border-2 cursor-pointer transition-colors ${tournamentOpen ? "border-acid bg-acid/5" : "border-ash hover:border-bone/40"}`}>
            <input type="checkbox" checked={tournamentOpen} onChange={toggleTournamentOpen} className="mt-0.5 accent-[#C8FF00] w-4 h-4" />
            <span className="min-w-0">
              <span className="font-display uppercase text-sm text-bone block">Keep the FC26 tournament live while locked</span>
              <span className="text-bone/50 text-xs leading-relaxed block mt-1">
                When locked, /tournament (registration, fixtures, pay links) stays open and the waitlist
                screen shows an "enter the tournament" button. Untick to lock everything.
                {locked ? " Changes apply immediately." : " Applies when you lock the store."}
              </span>
            </span>
          </label>
        )}
        {msg && <p className="text-bone/60 text-xs mt-4">{msg}</p>}
        <p className="text-bone/30 text-[11px] mt-4 leading-relaxed">
          Locked = every visitor sees the waitlist screen and can drop their email. Staff bypass with the
          staff code (VITE_SITE_PASSWORD) or <code className="text-bone/50">?unlock=&lt;code&gt;</code>. Takes effect on next page load — no redeploy.
        </p>

        {/* ── Lock-screen background video ─────────────────────── */}
        {!settingsMissing && (
          <div className="mt-6 pt-6 border-t border-ash">
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-3">Lock-screen background video</p>
            {videoUrl ? (
              <div className="flex flex-wrap items-start gap-4">
                <video src={videoUrl} muted loop autoPlay playsInline className="w-48 h-28 object-cover border border-ash bg-ink" />
                <div className="flex flex-col gap-2">
                  <span className="text-acid text-xs uppercase tracking-widest">● Live on the waitlist screen</span>
                  <div className="flex gap-2">
                    <label className="btn-og border-2 border-ash text-bone/70 px-4 py-2 text-[10px] hover:border-bone cursor-pointer">
                      Replace
                      <input type="file" accept="video/mp4,video/webm,video/*" className="hidden" disabled={vidBusy}
                        onChange={(e) => { onVideo(e.target.files?.[0] || null); e.currentTarget.value = ""; }} />
                    </label>
                    <button onClick={removeVideo} disabled={vidBusy} className="btn-og border-2 border-blood/40 text-blood px-4 py-2 text-[10px] hover:bg-blood hover:text-bone">Remove</button>
                  </div>
                </div>
              </div>
            ) : (
              <label className="flex items-center gap-4 border border-dashed border-ash hover:border-acid transition-colors p-5 cursor-pointer max-w-md">
                <div className="w-14 h-14 border border-ash bg-ink flex items-center justify-center text-bone/30 text-2xl shrink-0">▶</div>
                <div className="min-w-0">
                  <p className="text-bone/80 text-sm font-display uppercase tracking-wide">{vidBusy ? "Uploading…" : "Upload a background video"}</p>
                  <p className="text-bone/40 text-xs mt-1">MP4 or WebM · it loops muted behind the waitlist form.</p>
                </div>
                <input type="file" accept="video/mp4,video/webm,video/*" className="hidden" disabled={vidBusy}
                  onChange={(e) => { onVideo(e.target.files?.[0] || null); e.currentTarget.value = ""; }} />
              </label>
            )}
            {vidMsg && <p className={`text-xs mt-3 ${/✓|live/i.test(vidMsg) ? "text-acid" : "text-bone/60"}`}>{vidMsg}</p>}
            <p className="text-bone/30 text-[11px] mt-3 leading-relaxed max-w-xl">
              Autoplay needs a <strong className="text-bone/50">muted</strong> video (browsers block sound on load). For fast loading keep it a short
              5–10s loop, 1080p, H.264 MP4 (or VP9 WebM), compressed to <strong className="text-bone/50">~5–15MB</strong> — true lossless files are hundreds of MB and won't stream smoothly on mobile.
            </p>
          </div>
        )}
      </section>

      {/* ── Mailing list ─────────────────────────────────────── */}
      <section>
        <div className="flex flex-wrap items-center gap-3 mb-5">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">
            Mailing list <span className="text-acid">({visible.length})</span>
          </p>
          <div className="flex gap-2 ml-2">
            {(["all", "waitlist", "newsletter", "tournament"] as const).map((f) => (
              <button key={f} onClick={() => setFilter(f)} className={`btn-og px-3 py-2.5 text-[10px] border-2 ${filter === f ? "bg-acid border-acid text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
                {f}
              </button>
            ))}
          </div>
          <div className="ml-auto flex gap-2">
            <button onClick={copyEmails} disabled={visible.length === 0} className="btn-og border-2 border-ash text-bone/70 px-4 py-2 text-[10px] hover:border-bone">
              {copied ? "✓ Copied" : "Copy emails"}
            </button>
            <button onClick={downloadCsv} disabled={visible.length === 0} className="btn-og bg-bone text-ink px-4 py-2 text-[10px] hover:bg-acid">
              Download CSV
            </button>
          </div>
        </div>

        {subsMissing ? (
          <p className="border border-ash px-4 py-6 text-sm text-bone/60">
            The subscribers table doesn't exist yet — {MIGRATION_HINT}
            <br />
            <span className="text-bone/40 text-xs">
              (Waitlist signups made before the migration are stored in the customers table, tagged as subscribers.)
            </span>
          </p>
        ) : (
          <div className="border border-ash divide-y divide-ash">
            {visible.map((s) => (
              <div key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
                <span className="text-bone break-all min-w-0">{s.email}</span>
                <span className={`text-[10px] uppercase tracking-widest px-2 py-0.5 ${s.source === "waitlist" ? "bg-acid/15 text-acid" : "bg-bone/10 text-bone/60"}`}>
                  {s.source || "newsletter"}
                </span>
                <span className="ml-auto text-bone/30 text-xs">{new Date(s.created_at).toLocaleString()}</span>
                <button onClick={() => del(s)} className="btn-og border-2 border-blood/40 text-blood px-3 py-2.5 text-[10px] hover:bg-blood hover:text-bone">
                  Remove
                </button>
              </div>
            ))}
            {visible.length === 0 && <p className="px-4 py-8 text-bone/40 text-sm">No subscribers in this view yet.</p>}
          </div>
        )}

        <p className="text-bone/30 text-[11px] mt-4 leading-relaxed max-w-2xl">
          Sending: export the CSV (or copy the emails) into your mail tool of choice — Gmail BCC for small
          sends, or import into Mailchimp / Brevo / Resend for proper campaigns. When you're ready for
          automated sends from the site itself, that needs a small email backend — say the word.
        </p>
      </section>
    </div>
  );
}
