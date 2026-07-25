import { useEffect } from "react";
import { useLocation } from "react-router-dom";
import { supabase } from "./supabase";

/**
 * First-party page tracking — one row per route view into page_views.
 * No cookies, no IPs, no fingerprinting: a random per-tab session id in
 * sessionStorage is the only identifier, so no consent banner is needed.
 * Fails silently (including before migration_analytics.sql has been run).
 */

const SID_KEY = "og_sid_v1";
const GTAG_ID = "AW-18347528486";

declare global {
  interface Window { gtag?: (...args: unknown[]) => void }
}

/** Safe wrapper — no-ops if gtag.js is blocked or still loading. */
export function gtagEvent(name: string, params?: Record<string, unknown>) {
  try { window.gtag?.("event", name, params || {}); } catch { /* ad-blocked */ }
}

/** Fire a conversion-style event exactly once per key (per browser session). */
export function gtagOnce(key: string, name: string, params?: Record<string, unknown>) {
  try {
    const k = `og_gtag_${key}`;
    if (sessionStorage.getItem(k)) return;
    sessionStorage.setItem(k, "1");
  } catch { /* still fire */ }
  gtagEvent(name, params);
}

function sid(): string {
  try {
    let v = sessionStorage.getItem(SID_KEY);
    if (!v) {
      v = Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
      sessionStorage.setItem(SID_KEY, v);
    }
    return v;
  } catch {
    return "na";
  }
}

let lastKey = "";

export function usePageTracking() {
  const loc = useLocation();

  useEffect(() => {
    const key = loc.pathname + loc.search;
    if (key === lastKey) return; // strict-mode double-mount / same-route guard
    lastKey = key;

    if (loc.pathname.startsWith("/admin")) return;
    if (/bot|crawl|spider|preview|lighthouse|headless/i.test(navigator.userAgent)) return;

    // Google tag: SPA route changes don't reload the page, so re-config with
    // the new path — this is what makes page-based conversion rules work.
    try { window.gtag?.("config", GTAG_ID, { page_path: loc.pathname + loc.search }); } catch { /* ad-blocked */ }

    const q = new URLSearchParams(loc.search);
    let referrer: string | null = null;
    try {
      if (document.referrer && new URL(document.referrer).host !== location.host) referrer = document.referrer.slice(0, 300);
    } catch { /* malformed referrer */ }

    supabase
      .from("page_views")
      .insert({
        path: loc.pathname,
        query: loc.search ? loc.search.slice(0, 200) : null,
        referrer,
        source: q.get("utm_source"),
        medium: q.get("utm_medium"),
        campaign: q.get("utm_campaign"),
        session_id: sid(),
        screen_w: window.innerWidth,
        lang: navigator.language || null,
      })
      .then(() => {}, () => {}); // best-effort — never surfaces to the user
  }, [loc.pathname, loc.search]);
}
