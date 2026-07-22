// Shared server-side helpers for the OG OFFCL Vercel functions.
// Secrets live in (non-VITE) Vercel env vars and never reach the browser.

const clean = (v) => String(v ?? "").replace(/\\r|\\n/g, "").trim();
const jwt = (v) => {
  const m = String(v || "").match(/eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/);
  return m ? m[0] : clean(v);
};

// ── Env ────────────────────────────────────────────────────────────────────
const env = {
  supabaseUrl: (clean(process.env.SUPABASE_URL || process.env.VITE_SUPABASE_URL).match(/https:\/\/[a-z0-9-]+\.supabase\.co/i) || [""])[0],
  supabaseKey: jwt(process.env.SUPABASE_SERVICE_ROLE_KEY || process.env.SUPABASE_KEY || process.env.VITE_SUPABASE_ANON_KEY),
  moolreUser: clean(process.env.MOOLRE_API_USER),
  moolrePrivateKey: clean(process.env.MOOLRE_API_KEY || process.env.MOOLRE_PRIVATE_KEY),
  moolrePubKey: clean(process.env.MOOLRE_PUBKEY || process.env.MOOLRE_PUBLIC_KEY),
  moolreAccount: clean(process.env.MOOLRE_ACCOUNT_NUMBER),
  moolreBase: /^(1|true|sandbox)$/i.test(clean(process.env.MOOLRE_SANDBOX))
    ? "https://sandbox.moolre.com"
    : "https://api.moolre.com",
  resendKey: clean(process.env.RESEND_API_KEY),
  resendFrom: clean(process.env.RESEND_FROM) || "OG OFFCL <onboarding@resend.dev>",
  storeNotify: clean(process.env.STORE_NOTIFY_EMAIL),
  adminPassword: clean(process.env.ADMIN_API_PASSWORD || process.env.VITE_ADMIN_PASSWORD) || "OGADMIN26",
  siteUrl: clean(process.env.SITE_URL) || "https://ogoffcl.store",
};

function moolreConfigured() {
  return !!(env.moolreUser && env.moolreAccount && (env.moolrePrivateKey || env.moolreBase.includes("sandbox")));
}

// ── Supabase REST (PostgREST) ──────────────────────────────────────────────
async function sb(method, path, body, headers = {}) {
  const r = await fetch(`${env.supabaseUrl}/rest/v1/${path}`, {
    method,
    headers: {
      apikey: env.supabaseKey,
      Authorization: `Bearer ${env.supabaseKey}`,
      "Content-Type": "application/json",
      ...headers,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  return { ok: r.ok, status: r.status, data };
}

async function getOrder(orderId) {
  const { data } = await sb("GET", `orders?id=eq.${encodeURIComponent(orderId)}&select=*&limit=1`);
  return Array.isArray(data) && data[0] ? data[0] : null;
}

async function getOrderByNumber(orderNumber) {
  const { data } = await sb("GET", `orders?order_number=eq.${encodeURIComponent(orderNumber)}&select=*&limit=1`);
  return Array.isArray(data) && data[0] ? data[0] : null;
}

async function getOrderItems(orderId) {
  const { data } = await sb("GET", `order_items?order_id=eq.${encodeURIComponent(orderId)}&select=*`);
  return Array.isArray(data) ? data : [];
}

/** Update an order; retries without unknown columns so the API works even
 *  before migration_moolre_email.sql has been run. */
async function updateOrder(orderId, patch) {
  let res = await sb("PATCH", `orders?id=eq.${encodeURIComponent(orderId)}`, patch);
  if (!res.ok && res.status === 400 && typeof res.data === "object") {
    const msg = JSON.stringify(res.data);
    const bad = (msg.match(/'([a-z_]+)' column/i) || msg.match(/column "?([a-z_]+)"? of/i) || [])[1];
    if (bad && bad in patch) {
      const { [bad]: _drop, ...rest } = patch;
      if (Object.keys(rest).length) return updateOrder(orderId, rest);
      return { ok: true, status: 200, data: null };
    }
  }
  return res;
}

async function appendStatusHistory(order, entry) {
  const prev = Array.isArray(order.status_history) ? order.status_history : [];
  await updateOrder(order.id, {
    status_history: [...prev, { ...entry, at: new Date().toISOString() }],
  });
}

// ── Moolre ─────────────────────────────────────────────────────────────────
const PAY_CHANNELS = { mtn: "13", telecel: "6", at: "7" };       // collections
const TRANSFER_CHANNELS = { mtn: "1", telecel: "6", at: "7" };   // disbursements/refunds

// Moolre requires local Ghana format: starts with 0, no country code.
function ghPhone(raw) {
  let d = String(raw || "").replace(/\D/g, "");
  if (d.startsWith("233")) d = "0" + d.slice(3);
  else if (d.length === 9) d = "0" + d;      // missing leading 0
  return d;
}

async function moolre(path, payload, keyType) {
  const headers = {
    "Content-Type": "application/json",
    "X-API-USER": env.moolreUser,
  };
  if (keyType === "private") headers["X-API-KEY"] = env.moolrePrivateKey;
  if (keyType === "public") headers["X-API-PUBKEY"] = env.moolrePubKey;
  const r = await fetch(`${env.moolreBase}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
  });
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }
  return { http: r.status, ...((data && typeof data === "object") ? data : { data }) };
}

/** Initiate a MoMo collection. Returns Moolre envelope {status, code, message, data}.
 *  Collections authenticate with the PUBLIC key (X-API-PUBKEY) — confirmed
 *  against the live API; the private key returns AIN01 Authentication Error. */
function moolrePay({ channel, phone, amount, externalref, reference, otpcode }) {
  const payload = {
    type: 1,
    channel: PAY_CHANNELS[channel] || channel,
    currency: "GHS",
    payer: ghPhone(phone),
    amount: String(amount),
    externalref,
    reference: reference || "OG OFFCL order",
    accountnumber: env.moolreAccount,
  };
  if (otpcode) payload.otpcode = String(otpcode).trim();
  return moolre("/open/transact/payment", payload, "public");
}

/** Check a transaction by external reference. */
function moolreStatus(externalref) {
  return moolre("/open/transact/status", {
    type: 1,
    idtype: 1,
    id: externalref,
    accountnumber: env.moolreAccount,
  }, "public");
}

/** Send money out (refund to customer momo). */
function moolreTransfer({ channel, phone, amount, externalref, reference }) {
  return moolre("/open/transact/transfer", {
    type: 1,
    channel: TRANSFER_CHANNELS[channel] || channel,
    currency: "GHS",
    amount: String(amount),
    receiver: ghPhone(phone),
    externalref,
    reference: reference || "OG OFFCL refund",
    accountnumber: env.moolreAccount,
  }, "private");
}

// ── Resend ─────────────────────────────────────────────────────────────────
async function sendEmail({ to, subject, html, text }) {
  if (!env.resendKey) return { ok: false, skipped: true, error: "RESEND_API_KEY not set" };
  const r = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.resendKey}`, "Content-Type": "application/json" },
    body: JSON.stringify({ from: env.resendFrom, to: Array.isArray(to) ? to : [to], subject, html, text }),
  });
  const data = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, data, error: r.ok ? undefined : (data?.message || `HTTP ${r.status}`) };
}

/** Batch send (Resend /emails/batch caps at 100 per call). */
async function sendEmailBatch(messages) {
  if (!env.resendKey) return { ok: false, sent: 0, failed: messages.length, error: "RESEND_API_KEY not set" };
  let sent = 0, failed = 0;
  for (let i = 0; i < messages.length; i += 100) {
    const chunk = messages.slice(i, i + 100).map((m) => ({ from: env.resendFrom, ...m }));
    const r = await fetch("https://api.resend.com/emails/batch", {
      method: "POST",
      headers: { Authorization: `Bearer ${env.resendKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(chunk),
    });
    if (r.ok) sent += chunk.length;
    else failed += chunk.length;
  }
  return { ok: failed === 0, sent, failed };
}

// ── Email HTML (shared shell) ──────────────────────────────────────────────
function emailShell(title, bodyHtml) {
  return `<!doctype html><html><body style="margin:0;background:#0A0A0A;padding:24px 12px;font-family:Arial,Helvetica,sans-serif;">
  <div style="max-width:560px;margin:0 auto;background:#141414;border:1px solid #2a2a2a;">
    <div style="padding:20px 24px;border-bottom:2px solid #C8FF00;">
      <span style="font-size:22px;font-weight:900;color:#F5F2EA;letter-spacing:1px;">OG<span style="color:#C8FF00;">.</span>OFFCL</span>
    </div>
    <div style="padding:24px;color:#d8d5cc;font-size:14px;line-height:1.65;">
      <h1 style="margin:0 0 14px;font-size:20px;color:#F5F2EA;text-transform:uppercase;letter-spacing:1px;">${title}</h1>
      ${bodyHtml}
    </div>
    <div style="padding:16px 24px;border-top:1px solid #2a2a2a;color:#6b675e;font-size:11px;text-transform:uppercase;letter-spacing:2px;">
      Original Gangster Official — Accra · <a href="${env.siteUrl}" style="color:#C8FF00;text-decoration:none;">ogoffcl.store</a>
    </div>
  </div></body></html>`;
}

function orderItemsHtml(items) {
  return items.map((i) =>
    `<tr>
      <td style="padding:8px 10px;border-bottom:1px solid #2a2a2a;color:#F5F2EA;">${i.product_name}${i.size ? ` <span style="color:#8b877e;">(${i.size})</span>` : ""}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #2a2a2a;color:#8b877e;">×${i.quantity}</td>
      <td style="padding:8px 10px;border-bottom:1px solid #2a2a2a;color:#C8FF00;text-align:right;">GH₵${Number(i.price ?? i.unit_price ?? 0) * Number(i.quantity || 1)}</td>
    </tr>`).join("");
}

export {
  env, moolreConfigured, sb, getOrder, getOrderByNumber, getOrderItems, updateOrder,
  appendStatusHistory, moolrePay, moolreStatus, moolreTransfer, PAY_CHANNELS,
  sendEmail, sendEmailBatch, emailShell, orderItemsHtml,
};
