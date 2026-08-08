import crypto from "crypto";

const COOKIE_MAX_AGE_MS = 12 * 60 * 60 * 1000; // 12 hours

function getSecret() {
  const secret = (process.env.ADMIN_COOKIE_SECRET || "").trim();
  if (!secret) throw new Error("ADMIN_COOKIE_SECRET missing");
  return secret;
}

function hmacHex(message, secret) {
  return crypto.createHmac("sha256", secret).update(String(message)).digest("hex");
}

// Constant-time compare of two strings; pads to equal length so
// timingSafeEqual never throws on length mismatch.
function safeEqual(a, b) {
  const bufA = Buffer.from(String(a), "utf8");
  const bufB = Buffer.from(String(b), "utf8");
  const len = Math.max(bufA.length, bufB.length, 1);
  const padA = Buffer.alloc(len, 0);
  const padB = Buffer.alloc(len, 0);
  bufA.copy(padA);
  bufB.copy(padB);
  return crypto.timingSafeEqual(padA, padB) && bufA.length === bufB.length;
}

export function signAdminCookie() {
  const expiresMs = Date.now() + COOKIE_MAX_AGE_MS;
  const sig = hmacHex(expiresMs, getSecret());
  return `${expiresMs}.${sig}`;
}

export function verifyAdminCookie(value) {
  if (!value || typeof value !== "string") return false;
  const dot = value.indexOf(".");
  if (dot <= 0) return false;
  const expiresPart = value.slice(0, dot);
  const sigPart = value.slice(dot + 1);
  if (!/^\d+$/.test(expiresPart) || !/^[0-9a-f]{64}$/i.test(sigPart)) return false;
  const expiresMs = Number(expiresPart);
  if (!Number.isFinite(expiresMs) || expiresMs < Date.now()) return false;
  const expected = hmacHex(expiresPart, getSecret());
  return safeEqual(sigPart, expected);
}

export function checkPasscode(input) {
  const expected = (process.env.TRACK_ADMIN_PASSCODE || "").trim();
  if (!expected) return false;
  return safeEqual(String(input || ""), expected);
}
