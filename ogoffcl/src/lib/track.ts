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
