import { supabase } from "./supabase";

/**
 * Site lock + mailing list, stored in Supabase so the admin panel can flip the
 * lock live (no redeploy):
 *   site_settings (key text pk, value jsonb)  -> key 'site_locked', value {"locked": bool}
 *   subscribers   (id, email unique, source, created_at)
 * Run supabase/migration_waitlist.sql once to create them. Until then:
 *   - lock state falls back to the VITE_SITE_LOCKED env var
 *   - waitlist emails fall back into the existing `customers` table (metadata tag)
 */

const CACHE_KEY = "ogoffcl_lock_cache_v1";

export function isMissingTable(err: unknown): boolean {
  const e = err as { code?: string; message?: string } | null;
  if (!e) return false;
  return e.code === "PGRST205" || /could not find the table|relation .* does not exist/i.test(e.message || "");
}

export function cachedLocked(): boolean | null {
  try {
    const v = localStorage.getItem(CACHE_KEY);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch {}
  return null;
}

export async function fetchSiteLocked(): Promise<{ locked: boolean; lockedAt: number; tableMissing: boolean }> {
  try {
    const { data, error } = await supabase.from("site_settings").select("value").eq("key", "site_locked").maybeSingle();
    if (error) {
      if (isMissingTable(error)) return { locked: false, lockedAt: 0, tableMissing: true };
      return { locked: false, lockedAt: 0, tableMissing: false };
    }
    const v = (data?.value || {}) as { locked?: boolean; locked_at?: string | number };
    const locked = !!v.locked;
    const lockedAt = v.locked_at ? new Date(v.locked_at).getTime() : 0;
    try { localStorage.setItem(CACHE_KEY, locked ? "1" : "0"); } catch {}
    return { locked, lockedAt, tableMissing: false };
  } catch {
    return { locked: false, lockedAt: 0, tableMissing: false };
  }
}

export async function setSiteLocked(locked: boolean): Promise<{ ok: boolean; error?: string; tableMissing?: boolean }> {
  // locked_at stamps each NEW lock: staff bypasses granted before this moment
  // become invalid, so re-locking always locks everyone out again.
  const value = locked ? { locked: true, locked_at: new Date().toISOString() } : { locked: false };
  const { error } = await supabase
    .from("site_settings")
    .upsert({ key: "site_locked", value, updated_at: new Date().toISOString() }, { onConflict: "key" });
  if (error) {
    if (isMissingTable(error)) return { ok: false, tableMissing: true, error: error.message };
    return { ok: false, error: error.message };
  }
  try { localStorage.setItem(CACHE_KEY, locked ? "1" : "0"); } catch {}
  return { ok: true };
}

export type SubscribeSource = "waitlist" | "newsletter";

export async function subscribe(emailRaw: string, source: SubscribeSource): Promise<{ ok: boolean; already?: boolean; error?: string }> {
  const email = emailRaw.trim().toLowerCase();
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email)) return { ok: false, error: "Enter a valid email address." };

  // Welcome/confirmation email — server-side via Resend, best-effort. Sent on
  // every join (new OR already-on-list) so people always get the confirmation.
  const fireWelcome = () => {
    fetch("/api/email/welcome", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, source }),
    }).catch(() => {});
  };

  const { error } = await supabase.from("subscribers").insert({ email, source });
  if (!error) {
    fireWelcome();
    return { ok: true };
  }

  // duplicate — already subscribed is a success for the user; still confirm.
  if (error.code === "23505" || /duplicate key/i.test(error.message || "")) {
    fireWelcome();
    return { ok: true, already: true };
  }

  if (isMissingTable(error)) {
    // Fallback so signups are never lost before the migration runs:
    // stash in the existing customers table, tagged in metadata.
    try {
      const { data: existing } = await supabase.from("customers").select("id").eq("email", email).limit(1);
      if (existing && existing[0]) return { ok: true, already: true };
      const { error: cErr } = await supabase.from("customers").insert({
        email,
        name: null,
        metadata: { subscriber: true, source, subscribed_at: new Date().toISOString() },
      });
      if (!cErr) return { ok: true };
      return { ok: false, error: cErr.message };
    } catch (ex) {
      return { ok: false, error: ex instanceof Error ? ex.message : "Could not save your email." };
    }
  }
  return { ok: false, error: error.message };
}

export interface SubscriberRow {
  id: string;
  email: string;
  source: string | null;
  created_at: string;
}

export async function listSubscribers(): Promise<{ rows: SubscriberRow[]; tableMissing: boolean }> {
  const { data, error } = await supabase.from("subscribers").select("*").order("created_at", { ascending: false });
  if (error) return { rows: [], tableMissing: isMissingTable(error) };
  return { rows: (data || []) as SubscriberRow[], tableMissing: false };
}

export async function deleteSubscriber(id: string): Promise<void> {
  await supabase.from("subscribers").delete().eq("id", id);
}
