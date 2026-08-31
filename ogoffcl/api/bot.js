// /api/bot — the Telegram bot's admin API. One function, dispatch on ?action= (keeps the
// Vercel function count down, mirrors api/tournament.js). Auth: the shared bot secret in the
// x-bot-secret header, checked against BOT_SECRET (fails closed, same posture as adminPassword).
// No money moves here — there is no refund action; mark-paid only flips a flag.
import {
  env, sb, sbCount, getOrder, getOrderByNumber, getOrderItems, updateOrder, appendStatusHistory,
  sendEmail, emailShell,
} from "./_lib.js";

// Customer-facing copy for status emails (kept in sync with api/admin/notify-status.js).
const FRIENDLY = {
  confirmed: ["Order confirmed", "Your order is confirmed and getting packed."],
  processing: ["Being prepared", "Your pieces are being prepped and packed right now."],
  shipped: ["On the move", "Your order has been handed to the courier."],
  out_for_delivery: ["Out for delivery", "The rider is on the way — keep your phone close."],
  delivered: ["Delivered", "Your order has been delivered. Wear it loud. 🖤"],
  cancelled: ["Order cancelled", "Your order has been cancelled. If this is unexpected, reply to this email."],
};

const VALID_STATUSES = new Set([
  "pending", "confirmed", "processing", "shipped", "out_for_delivery",
  "delivered", "cancelled", "refunded",
]);

const q = (v) => encodeURIComponent(String(v ?? ""));

function authed(req) {
  return env.botSecret && String(req.headers["x-bot-secret"] || "") === env.botSecret;
}

export default async (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  const action = String((req.query && req.query.action) || (req.body && req.body.action) || "");
  const body = req.body || {};
  const query = req.query || {};
  try {
    switch (action) {
      case "orders": return await listOrders(query, res);
      case "order": return await orderDetail(query, res);
      case "order-status": return await setStatus(body, res);
      case "mark-paid": return await markPaid(body, res);
      case "waitlist": return await waitlist(query, res);
      case "analytics": return await analytics(query, res);
      case "overview": return await overview(res);
      case "discounts": return await discounts(res);
      case "discount-toggle": return await discountToggle(body, res);
      case "discount-create": return await discountCreate(body, res);
      case "site-lock":
        return req.method === "POST" ? await setSiteLock(body, res) : await getSiteLock(res);
      case "product":
        return req.method === "POST" ? await productWrite(body, res) : await product(query, res);
      default: return res.status(400).json({ error: `unknown action: ${action}` });
    }
  } catch (e) {
    return res.status(500).json({ error: e && e.message ? e.message : "bot api failed" });
  }
};

// ── orders ──────────────────────────────────────────────────────────────────
async function listOrders(query, res) {
  const status = String(query.status || "").trim();
  const limit = Math.min(Number(query.limit || 15) || 15, 50);
  let path = `orders?select=id,order_number,status,payment_status,total_amount,customer_name,created_at&order=created_at.desc&limit=${limit}`;
  if (status) path += `&status=eq.${q(status)}`;
  const { data } = await sb("GET", path);
  return res.json({ orders: Array.isArray(data) ? data : [] });
}

async function orderDetail(query, res) {
  const number = String(query.number || "").trim();
  const order = number ? await getOrderByNumber(number) : await getOrder(String(query.id || ""));
  if (!order) return res.status(404).json({ error: "not found" });
  const items = await getOrderItems(order.id);
  return res.json({ ...order, items });
}

async function setStatus(body, res) {
  const { id, status, note } = body;
  if (!VALID_STATUSES.has(status)) return res.status(400).json({ error: "invalid status" });
  const order = await getOrder(id);
  if (!order) return res.status(404).json({ error: "not found" });
  await updateOrder(order.id, { status, ...(note ? { tracking_note: note } : {}) });
  await appendStatusHistory(order, { status, note: note || null });
  let emailed = false;
  const email = (order.customer_email || "").trim();
  const f = FRIENDLY[status];
  if (email && f) {
    const r = await sendEmail({
      to: email,
      subject: `${f[0]} — ${order.order_number}`,
      html: emailShell(f[0], `
        <p>${f[1]}</p>
        <p>Order: <strong style="color:#C8FF00;">${order.order_number}</strong></p>
        ${note ? `<p style="color:#8b877e;">${String(note)}</p>` : ""}`),
    });
    emailed = !!r.ok;
  }
  return res.json({ ok: true, emailed });
}

async function markPaid(body, res) {
  const order = await getOrder(body.id);
  if (!order) return res.status(404).json({ error: "not found" });
  await updateOrder(order.id, {
    payment_status: "paid",
    status: order.status === "pending" ? "confirmed" : order.status,
  });
  return res.json({ ok: true });
}

// ── waitlist ─────────────────────────────────────────────────────────────────
async function waitlist(query, res) {
  const source = String(query.source || "").trim();
  const limit = Math.min(Number(query.limit || 20) || 20, 100);
  const filter = source ? `source=eq.${q(source)}` : "";
  let path = `subscribers?select=id,email,source,created_at&order=created_at.desc&limit=${limit}`;
  if (source) path += `&${filter}`;
  const { data } = await sb("GET", path);
  const count = await sbCount("subscribers", filter);
  return res.json({ rows: Array.isArray(data) ? data : [], count });
}

// ── analytics (page_views) ───────────────────────────────────────────────────
function refSource(ref) {
  if (!ref) return "direct";
  let h = "";
  try { h = new URL(ref).hostname.replace(/^www\./, ""); } catch { return "direct"; }
  if (/google/.test(h)) return "Google";
  if (/instagram/.test(h)) return "Instagram";
  if (/facebook|fb\.com/.test(h)) return "Facebook";
  if (/t\.co|twitter|x\.com/.test(h)) return "X/Twitter";
  if (/tiktok/.test(h)) return "TikTok";
  if (/whatsapp|wa\.me/.test(h)) return "WhatsApp";
  if (/snapchat/.test(h)) return "Snapchat";
  if (/bing/.test(h)) return "Bing";
  return h || "direct";
}

async function analytics(query, res) {
  const days = Math.min(Number(query.days || 7) || 7, 90);
  const sinceIso = new Date(Date.now() - days * 86400000).toISOString();
  const filter = `created_at=gte.${sinceIso}`;
  const views = await sbCount("page_views", filter);
  const { data } = await sb("GET",
    `page_views?select=path,referrer,session_id,screen_w&${filter}&order=created_at.desc&limit=1000`);
  const rows = Array.isArray(data) ? data : [];
  const sessions = new Set(rows.map((r) => r.session_id).filter(Boolean));
  const mobile = rows.filter((r) => Number(r.screen_w) && Number(r.screen_w) < 640).length;
  const tally = (arr, keyFn) => {
    const m = {};
    for (const r of arr) { const k = keyFn(r); m[k] = (m[k] || 0) + 1; }
    return Object.entries(m).sort((a, b) => b[1] - a[1]);
  };
  return res.json({
    days,
    views,
    visits: sessions.size,
    mobile_pct: rows.length ? Math.round((mobile * 100) / rows.length) : 0,
    top_pages: tally(rows, (r) => r.path || "/").slice(0, 8).map(([path, count]) => ({ path, count })),
    referrers: tally(rows, (r) => refSource(r.referrer)).slice(0, 8).map(([source, count]) => ({ source, count })),
  });
}

// ── overview (dashboard KPIs) ────────────────────────────────────────────────
async function overview(res) {
  const [products, orders, unpaid] = await Promise.all([
    sbCount("products"),
    sbCount("orders"),
    sbCount("orders", "payment_status=neq.paid"),
  ]);
  const { data } = await sb("GET", "orders?select=total_amount&payment_status=eq.paid&limit=1000");
  const revenue = (Array.isArray(data) ? data : []).reduce((s, o) => s + Number(o.total_amount || 0), 0);
  return res.json({ products, orders, unpaid, revenue });
}

// ── discounts ─────────────────────────────────────────────────────────────────
async function discounts(res) {
  const { data } = await sb("GET",
    "discount_codes?select=id,code,percentage,is_active,expires_at&order=created_at.desc&limit=50");
  return res.json({ codes: Array.isArray(data) ? data : [] });
}

async function discountToggle(body, res) {
  const { data } = await sb("GET", `discount_codes?id=eq.${q(body.id)}&select=is_active&limit=1`);
  const cur = Array.isArray(data) && data[0] ? !!data[0].is_active : false;
  await sb("PATCH", `discount_codes?id=eq.${q(body.id)}`, { is_active: !cur });
  return res.json({ ok: true, is_active: !cur });
}

async function discountCreate(body, res) {
  const code = String(body.code || "").toUpperCase().trim();
  const percentage = Math.max(1, Math.min(Number(body.percentage || 0) || 0, 100));
  if (!code) return res.status(400).json({ error: "code required" });
  const { data } = await sb("POST", "discount_codes",
    { code, percentage, is_active: true }, { Prefer: "return=representation" });
  return res.json({ ok: true, code: Array.isArray(data) ? data[0] : data });
}

// ── site lock ─────────────────────────────────────────────────────────────────
async function getSiteLock(res) {
  const { data } = await sb("GET", "site_settings?key=eq.site_locked&select=value&limit=1");
  const value = (Array.isArray(data) && data[0] && data[0].value) || {};
  return res.json({ locked: !!value.locked, value });
}

async function setSiteLock(body, res) {
  const locked = !!body.locked;
  const { data } = await sb("GET", "site_settings?key=eq.site_locked&select=value&limit=1");
  const prev = (Array.isArray(data) && data[0] && data[0].value) || {};
  const value = { ...prev, locked, locked_at: locked ? new Date().toISOString() : (prev.locked_at || null) };
  await sb("POST", "site_settings?on_conflict=key",
    { key: "site_locked", value }, { Prefer: "resolution=merge-duplicates" });
  return res.json({ ok: true, locked });
}

// ── products (stock / visibility only — no image fields) ──────────────────────
const PRODUCT_COLS = "id,name,price,stock,is_active";

async function product(query, res) {
  const id = String(query.id || "");
  if (id) {
    const { data } = await sb("GET", `products?id=eq.${q(id)}&select=${PRODUCT_COLS}&limit=1`);
    const p = Array.isArray(data) && data[0] ? data[0] : null;
    return p ? res.json(p) : res.status(404).json({ error: "not found" });
  }
  const search = String(query.q || "").trim();
  const limit = Math.min(Number(query.limit || 20) || 20, 50);
  let path = `products?select=${PRODUCT_COLS}&order=created_at.desc&limit=${limit}`;
  if (search) path += `&name=ilike.*${q(search)}*`;
  const { data } = await sb("GET", path);
  return res.json({ products: Array.isArray(data) ? data : [] });
}

async function productWrite(body, res) {
  const id = String(body.id || "");
  if (!id) return res.status(400).json({ error: "id required" });
  const { data } = await sb("GET", `products?id=eq.${q(id)}&select=${PRODUCT_COLS}&limit=1`);
  const p = Array.isArray(data) && data[0] ? data[0] : null;
  if (!p) return res.status(404).json({ error: "not found" });
  const patch = {};
  if (body.stock_delta !== undefined) {
    const base = (p.stock === null || p.stock === undefined) ? 0 : Number(p.stock);
    patch.stock = Math.max(0, base + Number(body.stock_delta || 0));
  }
  if (body.toggle_active) patch.is_active = !p.is_active;
  else if (body.is_active !== undefined) patch.is_active = !!body.is_active;
  if (Object.keys(patch).length) await sb("PATCH", `products?id=eq.${q(id)}`, patch);
  return res.json({ ...p, ...patch });
}
