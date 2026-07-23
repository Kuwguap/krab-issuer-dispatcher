import { useEffect, useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { isMissingTable } from "../lib/settings";

/**
 * Traffic dashboard over page_views (first-party tracking, src/lib/track.ts).
 * Reads client-side with the anon key like the rest of the admin.
 */
interface Row {
  path: string;
  referrer: string | null;
  source: string | null;
  campaign: string | null;
  session_id: string | null;
  screen_w: number | null;
  created_at: string;
}

const MIGRATION_HINT =
  "Run supabase/migration_analytics.sql once in the Supabase SQL editor (creates the page_views table), then reload. It only creates a new table — safe to run any time.";

const RANGES = [7, 30, 90] as const;

function refLabel(referrer: string | null): string {
  if (!referrer) return "Direct / none";
  try {
    const h = new URL(referrer).hostname.replace(/^www\./, "");
    if (/google\./.test(h)) return "Google";
    if (/bing\./.test(h)) return "Bing";
    if (/instagram\./.test(h) || h === "l.instagram.com") return "Instagram";
    if (/facebook\.|fb\./.test(h)) return "Facebook";
    if (/twitter\.|t\.co$|x\.com/.test(h)) return "X / Twitter";
    if (/tiktok\./.test(h)) return "TikTok";
    if (/whatsapp\./.test(h)) return "WhatsApp";
    if (/snapchat\./.test(h)) return "Snapchat";
    return h;
  } catch {
    return referrer.slice(0, 40);
  }
}

function topCounts(values: string[], limit = 8): [string, number][] {
  const m = new Map<string, number>();
  for (const v of values) m.set(v, (m.get(v) || 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, limit);
}

export default function AdminAnalytics() {
  const [rows, setRows] = useState<Row[]>([]);
  const [days, setDays] = useState<(typeof RANGES)[number]>(30);
  const [missing, setMissing] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    const since = new Date(Date.now() - days * 86400_000).toISOString();
    supabase
      .from("page_views")
      .select("path,referrer,source,campaign,session_id,screen_w,created_at")
      .gte("created_at", since)
      .order("created_at", { ascending: false })
      .limit(20000)
      .then(({ data, error }) => {
        if (error) { setMissing(isMissingTable(error)); setRows([]); }
        else { setMissing(false); setRows((data || []) as Row[]); }
        setLoading(false);
      });
  }, [days]);

  const stats = useMemo(() => {
    const sessions = new Set(rows.map((r) => r.session_id || "?")).size;
    const mobile = rows.filter((r) => (r.screen_w || 0) > 0 && (r.screen_w || 0) < 640).length;

    // views per day, oldest → newest
    const byDay = new Map<string, number>();
    for (let i = days - 1; i >= 0; i--) {
      byDay.set(new Date(Date.now() - i * 86400_000).toISOString().slice(0, 10), 0);
    }
    for (const r of rows) {
      const d = r.created_at.slice(0, 10);
      if (byDay.has(d)) byDay.set(d, (byDay.get(d) || 0) + 1);
    }
    const series = [...byDay.entries()];
    const max = Math.max(1, ...series.map(([, n]) => n));

    return {
      views: rows.length,
      sessions,
      mobilePct: rows.length ? Math.round((mobile / rows.length) * 100) : 0,
      series,
      max,
      pages: topCounts(rows.map((r) => r.path)),
      referrers: topCounts(rows.map((r) => refLabel(r.referrer))),
      sources: topCounts(rows.filter((r) => r.source).map((r) => `${r.source}${r.campaign ? ` · ${r.campaign}` : ""}`)),
    };
  }, [rows, days]);

  if (missing) {
    return (
      <div className="border border-ash bg-smoke/50 p-6 max-w-2xl text-sm text-bone/70 space-y-3">
        <p className="text-blood">The page_views table doesn't exist yet.</p>
        <p>{MIGRATION_HINT}</p>
      </div>
    );
  }

  return (
    <div className="space-y-10">
      {/* range + headline numbers */}
      <div className="flex flex-wrap items-center gap-3">
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Traffic</p>
        <div className="flex gap-2 ml-2">
          {RANGES.map((d) => (
            <button key={d} onClick={() => setDays(d)}
              className={`btn-og px-3 py-2.5 text-[10px] border-2 ${days === d ? "bg-acid border-acid text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
              {d}d
            </button>
          ))}
        </div>
        {loading && <span className="text-bone/40 text-xs animate-pulseSoft">Loading…</span>}
      </div>

      <div className="grid grid-cols-3 gap-px bg-ash border border-ash max-w-2xl">
        {[
          ["Views", String(stats.views)],
          ["Visits", String(stats.sessions)],
          ["Mobile", `${stats.mobilePct}%`],
        ].map(([label, value]) => (
          <div key={label} className="bg-ink p-4 sm:p-5">
            <p className="text-bone/40 text-[10px] uppercase tracking-[0.25em]">{label}</p>
            <p className="font-display text-2xl sm:text-3xl text-acid mt-1">{value}</p>
          </div>
        ))}
      </div>

      {/* views per day */}
      <div className="max-w-3xl">
        <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Views per day</p>
        <div className="flex items-end gap-px h-28 border-b border-ash">
          {stats.series.map(([d, n]) => (
            <div key={d} className="flex-1 min-w-0 group relative">
              <div
                className={`w-full ${n > 0 ? "bg-acid/70 group-hover:bg-acid" : "bg-bone/10"} transition-colors`}
                style={{ height: `${Math.max(2, (n / stats.max) * 100)}%` }}
                title={`${d}: ${n} view${n === 1 ? "" : "s"}`}
              />
            </div>
          ))}
        </div>
        <div className="flex justify-between text-bone/30 text-[10px] uppercase tracking-widest mt-2">
          <span>{stats.series[0]?.[0]}</span>
          <span>{stats.series[stats.series.length - 1]?.[0]}</span>
        </div>
      </div>

      {/* breakdowns */}
      <div className="grid md:grid-cols-3 gap-8">
        {([
          ["Top pages", stats.pages],
          ["Referrers", stats.referrers],
          ["UTM campaigns", stats.sources],
        ] as [string, [string, number][]][]).map(([title, list]) => (
          <div key={title}>
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-3">{title}</p>
            <div className="border border-ash divide-y divide-ash">
              {list.map(([label, n]) => (
                <div key={label} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                  <span className="text-bone truncate min-w-0 flex-1">{label}</span>
                  <span className="text-acid font-display">{n}</span>
                </div>
              ))}
              {list.length === 0 && <p className="px-3 py-4 text-bone/40 text-xs">Nothing in this window yet.</p>}
            </div>
          </div>
        ))}
      </div>

      <p className="text-bone/30 text-[11px] leading-relaxed max-w-2xl">
        First-party tracking — no cookies, no IPs, no consent banner needed. A random per-tab session id
        is the only identifier. Admin pages and obvious bots are excluded. Share links with{" "}
        <code className="text-bone/50">?utm_source=instagram&amp;utm_campaign=drop3</code> to see campaigns here.
      </p>
    </div>
  );
}
