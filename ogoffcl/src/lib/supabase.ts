import { createClient } from "@supabase/supabase-js";

/**
 * The Vercel env values for this project historically contain paste artifacts
 * (a literal trailing "\r\n" on the anon key). Sanitize everything so the
 * stored env vars can stay exactly as they are and the site still works.
 */
function cleanUrl(raw: string | undefined): string {
  const m = String(raw || "").match(/https:\/\/[a-z0-9-]+\.supabase\.co/i);
  return m ? m[0] : String(raw || "").trim();
}

function cleanKey(raw: string | undefined): string {
  // Supabase anon keys are JWTs — extract the JWT and drop any junk around it.
  const m = String(raw || "").match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
  return m ? m[0] : String(raw || "").trim();
}

function cleanPlain(raw: string | undefined, fallback = ""): string {
  return String(raw ?? fallback).replace(/\\r|\\n/g, "").trim() || fallback;
}

export const SUPABASE_URL = cleanUrl(import.meta.env.VITE_SUPABASE_URL);
export const SUPABASE_ANON_KEY = cleanKey(import.meta.env.VITE_SUPABASE_ANON_KEY);
export const STORAGE_BUCKET = cleanPlain(import.meta.env.VITE_SUPABASE_STORAGE_BUCKET, "images");
export const PAYSTACK_PUBLIC_KEY = cleanPlain(import.meta.env.VITE_PAYSTACK_PUBLIC_KEY);

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

export function publicImageUrl(path: string): string {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  return `${SUPABASE_URL}/storage/v1/object/public/${STORAGE_BUCKET}/${path.replace(/^\/+/, "")}`;
}

/** Upload a file to the images bucket, returns its public URL. */
export async function uploadImage(file: File, folder = "product-images"): Promise<string> {
  const ext = (file.name.split(".").pop() || "jpg").toLowerCase();
  const path = `${folder}/${Date.now()}-${Math.random().toString(36).slice(2, 8)}.${ext}`;
  const { error } = await supabase.storage.from(STORAGE_BUCKET).upload(path, file, {
    contentType: file.type || "image/jpeg",
    upsert: false,
  });
  if (error) throw error;
  const { data } = supabase.storage.from(STORAGE_BUCKET).getPublicUrl(path);
  return data.publicUrl;
}

/** Upload a background/lock-screen video, returns its public URL. Long cache
 *  so the CDN serves repeat visits fast. onProgress is best-effort. */
export async function uploadVideo(file: File, folder = "site"): Promise<string> {
  const ext = (file.name.split(".").pop() || "mp4").toLowerCase();
  const path = `${folder}/lock-${Date.now()}.${ext}`;
  const { error } = await supabase.storage.from(STORAGE_BUCKET).upload(path, file, {
    contentType: file.type || "video/mp4",
    upsert: true,
    cacheControl: "31536000",
  });
  if (error) throw error;
  const { data } = supabase.storage.from(STORAGE_BUCKET).getPublicUrl(path);
  return data.publicUrl;
}
