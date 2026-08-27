"""The /receipts board — every transmission, its receipt, every party's contacts,
and where it has got to.

This is the full back-office page: the "krab issuer" transmissions list with the
things that list could not do — a status column the whole team can move (New, On
the way, Delivered, Paid), the receipt shown from the database rather than a
Telegram link that has expired, a contact block per party (Client, Driver,
Issuer, Dispatcher), and Send email / Send SMS buttons on every party that has
that contact.

Senders are REUSED, not reinvented:
  * email — ``utils/client_outreach.send_client_email`` (Resend first, SendGrid
    fallback; BCCs ``FOLLOWUP_EMAIL_COPY`` like every other client email).
  * SMS   — GoHighLevel via ``utils/ghl_client`` when GHL_API_KEY +
    GHL_LOCATION_ID are set; otherwise the existing Twilio sender
    (``utils/client_outreach.send_client_sms``).

Exposure: tristatetags.com is the speedy-tags Vercel project, whose catch-all
rewrite sends unknown paths to the storefront — and whose ``/api/*`` already
belongs to the quicktags checkout proxy. That is why EVERY route this board
needs also exists under ``/receipts/*`` (page, data, status, notify, image):
one Vercel rewrite pair ``/receipts(/:path*)`` → this Flask service exposes the
whole board without touching the checkout proxy. The original ``/api/…`` routes
stay put for anything already pointed at them.

Kept in its own module because admin_dashboard.py is already long, and because
the board is self-contained: it needs ``db`` and ``app`` and nothing else.
"""
import json
import logging
import os
import re
import time

import requests as _requests
from flask import Response, jsonify, request

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "New",
    "on_the_way": "On the way",
    "delivered": "Delivered",
    "paid": "Paid",
}
STATUS_ORDER = ("new", "on_the_way", "delivered", "paid")
PARTIES = ("client", "driver", "issuer", "dispatcher")

# Receipt bytes are served straight back from this origin — an allowlist, never
# the row's own claim (image/svg+xml is a script container).
RECEIPT_MIME = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/heic": "image/heic",
    "image/heif": "image/heif",
    "application/pdf": "application/pdf",
}

# The send buttons are one click on a shared board — a double-click must not
# text a client twice. Per (lead, party, channel), in-process.
_MIN_SECONDS_BETWEEN_SENDS = 15
_recent_sends = {}


def _agency() -> dict:
    """Branding for the message templates — same FOLLOWUP_* the bot uses."""
    try:
        from config import Config
        return {
            "name": Config.FOLLOWUP_AGENCY_NAME,
            "phone": Config.FOLLOWUP_PHONE,
            "website": Config.FOLLOWUP_WEBSITE,
        }
    except Exception:
        return {
            "name": (os.getenv("FOLLOWUP_AGENCY_NAME") or "Tri State Tags").strip(),
            "phone": (os.getenv("FOLLOWUP_PHONE") or "").strip(),
            "website": (os.getenv("FOLLOWUP_WEBSITE") or "tristatetags.com").strip(),
        }


def _email_copy_address() -> str:
    try:
        from config import Config
        return (getattr(Config, "FOLLOWUP_EMAIL_COPY", "") or "").strip()
    except Exception:
        return (os.getenv("FOLLOWUP_EMAIL_COPY") or "").strip()


def _mask_phone(p: str) -> str:
    digits = re.sub(r"\D", "", p or "")
    return ("…" + digits[-4:]) if len(digits) >= 4 else "…"


def _mask_email(e: str) -> str:
    e = (e or "").strip()
    if "@" not in e:
        return "…"
    user, _, domain = e.partition("@")
    return (user[:1] or "…") + "…@" + domain


def _client_name(lead: dict) -> str:
    lines = (lead.get("vehicle_details") or "").splitlines()
    return (lines[0] or "").strip() if lines else ""


def _party_contact(db, lead_id: str, party: str):
    """{"name","phone","email","reference_id"} for one party of one lead.

    Resolved fresh from the database on every send — the browser never chooses
    the destination address, only the party. Returns None when the lead is gone.
    """
    try:
        r = (
            db.client.table("leads")
            .select("id, reference_id, vehicle_details, phone_number, email, "
                    "telegram_username, user_id, group_id")
            .eq("id", str(lead_id)).limit(1).execute()
        )
        lead = (r.data or [None])[0]
    except Exception as e:
        logger.error("receipts board: lead lookup failed for %s: %s", lead_id, e)
        lead = None
    if not lead:
        return None
    ref = (lead.get("reference_id") or "").strip()

    if party == "client":
        return {
            "name": _client_name(lead) or "client",
            "phone": (lead.get("phone_number") or "").strip(),
            "email": (lead.get("email") or "").strip(),
            "reference_id": ref,
        }
    if party == "driver":
        drv = {}
        try:
            a = (
                db.client.table("lead_assignments")
                .select("lead_id, status, driver:drivers(driver_name, phone_number, email)")
                .eq("lead_id", str(lead_id)).eq("status", "accepted")
                .limit(1).execute()
            )
            drv = ((a.data or [{}])[0].get("driver") or {})
        except Exception as e:
            logger.warning("receipts board: driver lookup failed for %s: %s", lead_id, e)
        return {
            "name": (drv.get("driver_name") or "").strip() or "driver",
            "phone": (drv.get("phone_number") or "").strip(),
            "email": (drv.get("email") or "").strip(),
            "reference_id": ref,
        }
    if party == "issuer":
        # Issuers live in Telegram; no phone or email is stored for them, so the
        # buttons stay guarded until that day comes.
        return {
            "name": (lead.get("telegram_username") or "").strip() or "issuer",
            "phone": "",
            "email": "",
            "reference_id": ref,
        }
    # dispatcher — the team (group) the lead went to
    grp = {}
    try:
        gid = str(lead.get("group_id") or "")
        if gid:
            g = (
                db.client.table("groups")
                .select("id, group_name, group_telegram_id, supervisory_telegram_id")
                .eq("id", gid).limit(1).execute()
            )
            grp = (g.data or [{}])[0] or {}
    except Exception as e:
        logger.warning("receipts board: group lookup failed for %s: %s", lead_id, e)
    return {
        "name": (grp.get("group_name") or "").strip() or "dispatcher",
        "phone": "",
        "email": "",
        "reference_id": ref,
    }


BOARD_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipts &amp; Transmissions</title>
<style>
 :root {
   --bg:#f4f5f7; --card:#fff; --ink:#172b4d; --muted:#6b778c; --line:#dfe1e6;
   --soft:#f8f9fb; --accent:#0065ff;
   --new:#8993a4; --otw:#0065ff; --del:#00875a; --paid:#6554c0;
   --ok-bg:#e3fcef; --ok-ink:#006644; --bad-bg:#ffebe6; --bad-ink:#bf2600;
 }
 @media (prefers-color-scheme: dark) {
   :root { --bg:#1d2125; --card:#22272b; --ink:#e6edf3; --muted:#9fadbc;
           --line:#2c333a; --soft:#1a1e22;
           --ok-bg:#133527; --ok-ink:#7ee2b8; --bad-bg:#42221f; --bad-ink:#ff9c8f; }
 }
 * { box-sizing:border-box; }
 body { margin:0; font:14px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;
        background:var(--bg); color:var(--ink); }
 header { background:var(--card); border-bottom:1px solid var(--line);
          padding:14px 20px 12px; display:flex; gap:14px; align-items:center;
          flex-wrap:wrap; }
 h1 { font-size:18px; margin:0; font-weight:700; }
 .sub { color:var(--muted); font-size:12px; }
 .grow { flex:1; }
 input[type=search] { padding:8px 12px; border:1px solid var(--line); border-radius:8px;
                      background:var(--bg); color:inherit; min-width:230px; }
 .tabs { display:flex; gap:6px; flex-wrap:wrap; }
 .tab { padding:6px 12px; border:1px solid var(--line); border-radius:20px;
        background:transparent; color:var(--muted); cursor:pointer; font-weight:600;
        font-size:13px; }
 .tab.on { background:var(--ink); color:var(--card); border-color:var(--ink); }
 .counts { color:var(--muted); font-size:12px; }
 .who { border:1px dashed var(--line); border-radius:20px; padding:6px 12px;
        background:transparent; color:var(--muted); cursor:pointer; font-size:13px; }
 #cfg { margin:10px 20px 0; padding:9px 14px; border-radius:8px; font-size:13px;
        background:var(--soft); border:1px solid var(--line); color:var(--muted);
        display:none; }
 .err { background:var(--bad-bg); color:var(--bad-ink); padding:10px 14px;
        border-radius:8px; margin:10px 20px; }
 main { padding:12px 20px 70px; }
 .wrap { overflow-x:auto; background:var(--card); border:1px solid var(--line);
         border-radius:10px; }
 table { width:100%; min-width:1340px; border-collapse:collapse; }
 th { position:sticky; top:0; z-index:3; background:var(--card);
      text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
      color:var(--muted); padding:10px 12px; border-bottom:1px solid var(--line);
      white-space:nowrap; }
 td { padding:11px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
 tr.row:hover { background:rgba(0,101,255,.05); }
 .ref { font-family:ui-monospace,monospace; font-size:12px; color:var(--muted); }
 .cname { font-weight:650; }
 .carline { color:var(--muted); font-size:12px; }
 .exp { cursor:pointer; user-select:none; color:var(--muted); width:26px; }
 .pill { display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px;
         font-weight:650; color:#fff; white-space:nowrap; }
 .s-new{background:var(--new)} .s-on_the_way{background:var(--otw)}
 .s-delivered{background:var(--del)} .s-paid{background:var(--paid)}
 select.status { margin-top:5px; padding:5px 8px; border-radius:8px;
                 border:1px solid var(--line); background:var(--bg); color:inherit;
                 font-weight:600; }
 .thumb { width:72px; height:52px; object-fit:cover; border-radius:8px;
          border:1px solid var(--line); cursor:zoom-in; display:block; background:var(--soft); }
 .nothumb { width:72px; height:52px; border:1px dashed var(--line); border-radius:8px;
            color:var(--muted); font-size:11px; display:flex; align-items:center;
            justify-content:center; text-align:center; }
 .rinfo { margin-top:5px; font-size:12px; color:var(--muted); }
 .rinfo b { color:var(--ink); }
 .phone a { font-weight:650; white-space:nowrap; }
 .party { min-width:158px; font-size:12.5px; }
 .pname { font-weight:650; margin-bottom:2px; }
 .cl { color:var(--ink); margin:1px 0; white-space:nowrap; overflow:hidden;
       text-overflow:ellipsis; max-width:210px; }
 .cl .ic { display:inline-block; width:15px; color:var(--muted); }
 .cl.none { color:var(--muted); }
 .acts { margin-top:6px; display:flex; gap:5px; flex-wrap:wrap; }
 .act { border:1px solid var(--line); background:var(--soft); color:var(--ink);
        border-radius:7px; padding:3px 8px; font-size:12px; font-weight:600;
        cursor:pointer; }
 .act:hover { border-color:var(--accent); color:var(--accent); }
 .act.dim { opacity:.45; }
 .detail { background:var(--soft); }
 .detail dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0; }
 .detail dt { color:var(--muted); font-size:12px; }
 .detail img { max-width:min(460px,100%); border-radius:8px; margin-top:10px;
               border:1px solid var(--line); cursor:zoom-in; }
 a { color:var(--otw); text-decoration:none; }
 a:hover { text-decoration:underline; }
 .none { color:var(--muted); }
 .saving { opacity:.5; }
 .overlay { position:fixed; inset:0; background:rgba(9,30,66,.55); z-index:50;
            display:flex; align-items:center; justify-content:center; padding:20px; }
 .overlay[hidden] { display:none; }
 .sheet { background:var(--card); color:var(--ink); border-radius:12px;
          width:min(560px,100%); max-height:92vh; overflow:auto; padding:20px 22px;
          box-shadow:0 18px 50px rgba(0,0,0,.35); }
 .sheet h2 { margin:0 0 4px; font-size:16px; }
 .c-to { color:var(--muted); font-size:13px; margin-bottom:12px; }
 .sheet label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
 .sheet input, .sheet textarea { width:100%; padding:9px 11px; border:1px solid var(--line);
        border-radius:8px; background:var(--bg); color:inherit; font:inherit; }
 .sheet textarea { resize:vertical; }
 .c-actions { display:flex; gap:8px; align-items:center; margin-top:14px; }
 .c-actions .spacer { flex:1; }
 .btn { border-radius:8px; padding:8px 16px; font-weight:650; cursor:pointer;
        border:1px solid var(--line); background:var(--soft); color:var(--ink); }
 .btn.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
 .btn[disabled] { opacity:.55; cursor:default; }
 #c-result { font-size:13px; }
 #c-result.ok { color:var(--ok-ink); }
 #c-result.bad { color:var(--bad-ink); }
 #lightbox img { max-width:94vw; max-height:92vh; border-radius:10px; cursor:zoom-out;
                 background:#fff; }
 #toasts { position:fixed; right:16px; bottom:16px; z-index:60; display:flex;
           flex-direction:column; gap:8px; }
 .toast { padding:10px 14px; border-radius:8px; font-size:13px; font-weight:600;
          box-shadow:0 6px 18px rgba(0,0,0,.25); }
 .toast.ok { background:var(--ok-bg); color:var(--ok-ink); }
 .toast.bad { background:var(--bad-bg); color:var(--bad-ink); }
 footer { position:fixed; left:0; right:0; bottom:0; padding:6px 20px;
          font-size:11px; color:var(--muted); background:var(--bg); text-align:right;
          pointer-events:none; }
 @media (max-width:760px) { .hide-sm { display:none; } }
</style></head><body>
<header>
  <div>
    <h1>🧾 Receipts &amp; Transmissions</h1>
    <div class="sub">Every transmission, its receipt, every party — and where it has got to.</div>
  </div>
  <div class="tabs" id="tabs"></div>
  <span class="grow"></span>
  <input type="search" id="q" placeholder="Search ref, client, driver, phone…">
  <button class="who" id="who" title="Shown next to everything you change or send">👤 …</button>
  <span class="counts" id="counts"></span>
</header>
<div id="cfg"></div>
<div id="err"></div>
<main>
  <div class="wrap">
  <table>
    <thead><tr>
      <th class="exp"></th>
      <th>Client</th>
      <th>Receipt</th>
      <th>Client phone</th>
      <th>Tags</th>
      <th>Client contact</th>
      <th>Driver</th>
      <th>Issuer</th>
      <th>Dispatcher</th>
      <th>Status</th>
      <th class="hide-sm">Updated</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="11" class="none">Loading…</td></tr></tbody>
  </table>
  </div>
</main>

<div class="overlay" id="compose" hidden>
  <div class="sheet">
    <h2 id="c-title">Send</h2>
    <div class="c-to" id="c-to"></div>
    <label id="c-sublabel">Subject
      <input id="c-subject" autocomplete="off">
    </label>
    <label>Message
      <textarea id="c-msg" rows="9"></textarea>
    </label>
    <div class="c-actions">
      <span id="c-result"></span>
      <span class="spacer"></span>
      <button class="btn" id="c-cancel">Cancel</button>
      <button class="btn primary" id="c-send">Send</button>
    </div>
  </div>
</div>
<div class="overlay" id="lightbox" hidden><img id="lb-img" alt="receipt"></div>
<div id="toasts"></div>
<footer>refreshes every 30s</footer>

<script>
const STATUSES = __STATUSES__;
const LABELS = __LABELS__;
const AGENCY = __AGENCY__;
const API = "/receipts/api";
const IMG = "/receipts/receipt/";
let ALL = [], filter = "", q = "";
let CFG = {email: true, sms: "unknown"};   // refreshed from /receipts/api/sendconfig
let COMPOSE = null;                        // {row, party, channel}

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleString([], {month:"short", day:"numeric",
                                               hour:"2-digit", minute:"2-digit"});
}
const digits = s => String(s || "").replace(/\D/g, "");
const telHref = p => { const d = digits(p); return d.length >= 10 ? "tel:+1" + d.slice(-10) : ""; };
const firstName = n => (String(n || "").trim().split(/\s+/)[0]) || "there";

function whoAmI(ask) {
  let w = localStorage.getItem("krab_who") || "";
  if (!w && ask) {
    w = (prompt("Your name (shown next to what you change or send):") || "").trim();
    if (w) localStorage.setItem("krab_who", w);
  }
  document.getElementById("who").textContent = "👤 " + (localStorage.getItem("krab_who") || "who am I?");
  return localStorage.getItem("krab_who") || "";
}
document.getElementById("who").onclick = () => {
  const w = (prompt("Your name (shown next to what you change or send):",
                    localStorage.getItem("krab_who") || "") || "").trim();
  if (w) localStorage.setItem("krab_who", w);
  whoAmI(false);
};

function toast(text, ok) {
  const t = document.createElement("div");
  t.className = "toast " + (ok ? "ok" : "bad");
  t.textContent = text;
  document.getElementById("toasts").appendChild(t);
  setTimeout(() => t.remove(), 6500);
}

function tabs() {
  const el = document.getElementById("tabs");
  const counts = {};
  ALL.forEach(r => { counts[r.status] = (counts[r.status] || 0) + 1; });
  el.innerHTML = [["", "All"]].concat(STATUSES.map(s => [s, LABELS[s]]))
    .map(([v, label]) => {
      const n = v ? (counts[v] || 0) : ALL.length;
      return `<button class="tab ${filter === v ? "on" : ""}" data-f="${v}">`
           + `${esc(label)} <span class="counts">${n}</span></button>`;
    }).join("");
  el.querySelectorAll(".tab").forEach(b => b.onclick = () => {
    filter = b.dataset.f; draw();
  });
}

function visible() {
  const needle = q.trim().toLowerCase();
  return ALL.filter(r => (!filter || r.status === filter)
    && (!needle || JSON.stringify(r).toLowerCase().includes(needle)));
}

// Every party's contacts, in the one consistent shape the blocks render.
function contacts(r) {
  return {
    client: {
      label: "Client", name: r.client_name === "—" ? "" : r.client_name,
      tg: "", tgHref: "",
      phone: r.client_phone || "", email: r.email || "",
    },
    driver: {
      label: "Driver", name: r.driver_name === "—" ? "" : r.driver_name,
      tg: r.driver_tg_id ? "id " + r.driver_tg_id : "", tgHref: "",
      phone: r.driver_phone || "", email: r.driver_email || "",
    },
    issuer: {
      label: "Issuer",
      name: r.issuer_username ? "@" + r.issuer_username : (r.issuer === "—" ? "" : r.issuer),
      tg: r.issuer_tg_id ? "id " + r.issuer_tg_id : "",
      tgHref: r.issuer_username ? "https://t.me/" + r.issuer_username : "",
      phone: "", email: "",
    },
    dispatcher: {
      label: "Dispatcher", name: r.group_name === "—" ? "" : r.group_name,
      tg: r.dispatcher_tg_id ? "sup " + r.dispatcher_tg_id
          : (r.group_tg_id ? "grp " + r.group_tg_id : ""),
      tgHref: "",
      phone: "", email: "",
    },
  };
}

function contactLine(ic, val, href) {
  if (!val) return `<div class="cl none"><span class="ic">${ic}</span>—</div>`;
  const inner = href ? `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(val)}</a>` : esc(val);
  return `<div class="cl" title="${esc(val)}"><span class="ic">${ic}</span>${inner}</div>`;
}

function block(r, party) {
  const c = contacts(r)[party];
  const btns = [];
  if (c.email) btns.push(
    `<button class="act ${CFG.email ? "" : "dim"}" data-id="${esc(r.lead_id)}"
      data-party="${party}" data-ch="email"
      title="${CFG.email ? "Send an email to the " + party : "Email sender not configured on this service"}">✉ Email</button>`);
  if (c.phone) btns.push(
    `<button class="act ${CFG.sms ? "" : "dim"}" data-id="${esc(r.lead_id)}"
      data-party="${party}" data-ch="sms"
      title="${CFG.sms ? "Send an SMS to the " + party + " via " + CFG.sms : "SMS sender not configured on this service"}">💬 SMS</button>`);
  return `<div class="party">`
    + `<div class="pname">${c.name ? esc(c.name) : '<span class="none">—</span>'}</div>`
    + contactLine("✈", c.tg, c.tgHref)
    + contactLine("☎", c.phone, telHref(c.phone))
    + contactLine("✉", c.email, c.email ? "mailto:" + c.email : "")
    + (btns.length ? `<div class="acts">${btns.join("")}</div>` : "")
    + `</div>`;
}

function receiptCell(r) {
  const img = r.has_receipt
    ? `<img class="thumb" loading="lazy" src="${IMG + encodeURIComponent(r.lead_id)}"
         data-full="${IMG + encodeURIComponent(r.lead_id)}" alt="receipt"
         onerror="this.outerHTML='<div class=nothumb>no image</div>'">`
    : `<div class="nothumb">no receipt</div>`;
  const date = r.receipt_at ? when(r.receipt_at) : (r.has_receipt ? "on file" : "—");
  return `${img}<div class="rinfo"><b>${esc(r.price)}</b> · ${esc(date)}<br>${esc(LABELS[r.status] || r.status)}</div>`;
}

function draw() {
  tabs();
  const rows = visible();
  document.getElementById("counts").textContent = `${rows.length} of ${ALL.length}`;
  const tb = document.getElementById("rows");
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="11" class="none">Nothing here yet.</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const opts = STATUSES.map(s =>
      `<option value="${s}" ${r.status === s ? "selected" : ""}>${esc(LABELS[s])}</option>`
    ).join("");
    const phone = r.client_phone
      ? `<a href="${esc(telHref(r.client_phone) || "#")}">${esc(r.client_phone)}</a>`
      : '<span class="none">—</span>';
    return `<tr class="row" data-id="${esc(r.lead_id)}">
      <td class="exp" data-x="${esc(r.lead_id)}">▸</td>
      <td><div class="cname">${esc(r.client_name)}</div>
          <div class="ref">${esc(r.reference_id)}</div>
          <div class="carline">${esc(r.car)}</div></td>
      <td>${receiptCell(r)}</td>
      <td class="phone">${phone}</td>
      <td>${(r.tags || 1) > 1 ? `<b title="one tag per car">${esc(r.tags)}×</b>` : ""}</td>
      <td>${block(r, "client")}</td>
      <td>${block(r, "driver")}</td>
      <td>${block(r, "issuer")}</td>
      <td>${block(r, "dispatcher")}</td>
      <td><span class="pill s-${esc(r.status)}">${esc(LABELS[r.status] || r.status)}</span><br>
          <select class="status" data-id="${esc(r.lead_id)}">${opts}</select></td>
      <td class="hide-sm">${esc(when(r.status_updated_at))}<br>
          <span class="counts">${esc(r.status_updated_by || "")}</span></td>
    </tr>
    <tr class="detail" id="d-${esc(r.lead_id)}" hidden><td colspan="11">
      <dl>
        <dt>Reference</dt><dd class="ref">${esc(r.reference_id)}</dd>
        <dt>Car</dt><dd>${esc(r.car)} ${(r.tags || 1) > 1 ? `— <b>${esc(r.tags)} tags owed</b>` : ""}</dd>
        <dt>Price</dt><dd>${esc(r.price)}</dd>
        <dt>Delivery</dt><dd>${esc(r.delivery) || "—"}</dd>
        <dt>Notes</dt><dd>${esc(r.notes) || "—"}</dd>
        <dt>Entered by</dt><dd>${esc(r.issuer)}</dd>
        <dt>Created</dt><dd>${esc(when(r.created_at))}</dd>
        <dt>Receipt</dt><dd>${r.receipt_in_db ? "stored here (never expires)"
                              : (r.has_receipt ? "external link" : "not handed in")}</dd>
      </dl>
      ${r.has_receipt ? `<img loading="lazy" src="${IMG + encodeURIComponent(r.lead_id)}"
                           data-full="${IMG + encodeURIComponent(r.lead_id)}" alt="receipt">` : ""}
    </td></tr>`;
  }).join("");

  tb.querySelectorAll(".exp").forEach(td => td.onclick = () => {
    const d = document.getElementById("d-" + td.dataset.x);
    d.hidden = !d.hidden;
    td.textContent = d.hidden ? "▸" : "▾";
  });
  tb.querySelectorAll("select.status").forEach(sel => sel.onchange = async () => {
    const id = sel.dataset.id, next = sel.value;
    const tr = sel.closest("tr");
    tr.classList.add("saving");
    try {
      const res = await fetch(`${API}/transmissions/${encodeURIComponent(id)}/status`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({status: next, by: whoAmI(true)}),
      });
      if (!res.ok) throw new Error(await res.text());
      const row = ALL.find(r => r.lead_id === id);
      if (row) {
        row.status = next;
        row.status_updated_at = new Date().toISOString();
        row.status_updated_by = localStorage.getItem("krab_who") || "";
      }
      draw();
    } catch (e) {
      document.getElementById("err").innerHTML =
        `<div class="err">Could not save that status: ${esc(e.message)}</div>`;
      tr.classList.remove("saving");
    }
  });
  tb.querySelectorAll(".act").forEach(b => b.onclick = () => {
    const row = ALL.find(r => r.lead_id === b.dataset.id);
    if (row) openCompose(row, b.dataset.party, b.dataset.ch);
  });
  tb.querySelectorAll("img[data-full]").forEach(img => img.onclick = () => {
    document.getElementById("lb-img").src = img.dataset.full;
    document.getElementById("lightbox").hidden = false;
  });
}

// ── Compose ────────────────────────────────────────────────────────────────
function statusLine(r) {
  return {
    new: "We have received your order and are getting it ready.",
    on_the_way: "Your temporary tag is on the way to you now.",
    delivered: "Your temporary tag has been delivered.",
    paid: "Payment received — thank you! Your transaction is complete.",
  }[r.status] || "Here is an update on your temporary tag.";
}

function prefill(r, party, channel) {
  if (party === "client") {
    if (channel === "sms")
      return {subject: "", message:
        `Hi ${firstName(r.client_name)}, it's ${AGENCY.name}. ${statusLine(r)} (ref ${r.reference_id}).`
        + (AGENCY.phone ? ` Questions? Call/text ${AGENCY.phone}.` : "")};
    return {
      subject: `Update on your temporary tag — ${r.reference_id}`,
      message: `Hi ${firstName(r.client_name)},

${statusLine(r)}

Reference: ${r.reference_id}
Vehicle: ${r.car}

Questions? Reply to this email` + (AGENCY.phone ? ` or call/text ${AGENCY.phone}` : "") + `.

Thank you,
${AGENCY.name}
${AGENCY.website}`,
    };
  }
  const who = localStorage.getItem("krab_who") || AGENCY.name;
  const line = `${r.reference_id} — ${r.client_name}, ${r.car}: status "${LABELS[r.status] || r.status}".`;
  if (channel === "sms")
    return {subject: "", message: `From the receipts board (${who}): ${line}`};
  return {
    subject: `Transmission ${r.reference_id} — ${LABELS[r.status] || r.status}`,
    message: `Hi,

${line}

Sent from the receipts board by ${who}.

${AGENCY.name}`,
  };
}

function openCompose(row, party, channel) {
  COMPOSE = {row, party, channel};
  const c = contacts(row)[party];
  const to = channel === "email" ? c.email : c.phone;
  if (!to) { toast(`No ${channel === "email" ? "email" : "phone number"} on file for the ${party}.`, false); return; }
  const t = prefill(row, party, channel);
  document.getElementById("c-title").textContent =
    (channel === "email" ? "✉ Email" : "💬 SMS") + " → " + c.label.toLowerCase();
  document.getElementById("c-to").textContent =
    `To ${c.name || party}: ${to}` + (channel === "sms" && CFG.sms ? `  (via ${CFG.sms})` : "");
  document.getElementById("c-sublabel").style.display = channel === "email" ? "" : "none";
  document.getElementById("c-subject").value = t.subject;
  document.getElementById("c-msg").value = t.message;
  const res = document.getElementById("c-result");
  res.textContent = ""; res.className = "";
  document.getElementById("c-send").disabled = false;
  document.getElementById("compose").hidden = false;
  document.getElementById("c-msg").focus();
}

document.getElementById("c-cancel").onclick = () => {
  document.getElementById("compose").hidden = true; COMPOSE = null;
};
document.getElementById("c-send").onclick = async () => {
  if (!COMPOSE) return;
  const {row, party, channel} = COMPOSE;
  const btn = document.getElementById("c-send");
  const res = document.getElementById("c-result");
  const message = document.getElementById("c-msg").value.trim();
  if (!message) { res.textContent = "Write a message first."; res.className = "bad"; return; }
  btn.disabled = true; btn.textContent = "Sending…";
  res.textContent = ""; res.className = "";
  try {
    const r = await fetch(`${API}/transmissions/${encodeURIComponent(row.lead_id)}/notify`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        party, channel, message,
        subject: document.getElementById("c-subject").value.trim(),
        by: whoAmI(true),
      }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
    res.textContent = `Sent ✓ via ${body.provider || channel} to ${body.to || "recipient"}`;
    res.className = "ok";
    toast(`${channel === "email" ? "Email" : "SMS"} sent to the ${party} (${row.reference_id})`, true);
    setTimeout(() => { document.getElementById("compose").hidden = true; COMPOSE = null; }, 1400);
  } catch (e) {
    res.textContent = e.message; res.className = "bad";
    toast(`Send failed: ${e.message}`, false);
  } finally {
    btn.disabled = false; btn.textContent = "Send";
  }
};

document.getElementById("lightbox").onclick = () => {
  document.getElementById("lightbox").hidden = true;
};
document.addEventListener("keydown", e => {
  if (e.key === "Escape") {
    document.getElementById("lightbox").hidden = true;
    document.getElementById("compose").hidden = true;
    COMPOSE = null;
  }
});

// ── Data ───────────────────────────────────────────────────────────────────
async function loadConfig() {
  try {
    const res = await fetch(`${API}/sendconfig`);
    if (!res.ok) return;
    CFG = await res.json();
    const notes = [];
    if (!CFG.email) notes.push("email (set RESEND_API_KEY + RESEND_FROM, or SENDGRID_API_KEY + SENDGRID_FROM)");
    if (!CFG.sms) notes.push("SMS (set GHL_API_KEY + GHL_LOCATION_ID for GoHighLevel, or TWILIO_*)");
    let text = notes.length
      ? "⚠ Sending not configured for " + notes.join(" and ") + " — buttons will explain when clicked."
      : "";
    if (CFG.status_column === false)
      text += (text ? "   " : "") + "⚠ Statuses cannot be saved yet — run "
            + "database/migration_lead_delivery_status.sql in the Supabase SQL editor once.";
    const cfg = document.getElementById("cfg");
    cfg.textContent = text;
    cfg.style.display = text ? "block" : "none";
  } catch (e) { /* the board still works read-only */ }
}

async function load() {
  try {
    const res = await fetch(`${API}/transmissions?limit=500`);
    if (!res.ok) throw new Error(await res.text());
    ALL = await res.json();
    document.getElementById("err").innerHTML = "";
  } catch (e) {
    document.getElementById("err").innerHTML =
      `<div class="err">Could not load the board: ${esc(e.message)}</div>`;
    ALL = [];
  }
  draw();
}

document.getElementById("q").oninput = e => { q = e.target.value; draw(); };
whoAmI(false);
loadConfig();
load();
setInterval(() => {                     // the board is shared — keep it fresh,
  if (document.querySelector(".overlay:not([hidden])")) return;   // but never under a compose
  load();
}, 30000);
</script>
</body></html>"""


def register(app, db_provider):
    """Attach the board and its endpoints to the dashboard app.

    `db_provider` is resolved on every request, not captured once: binding the
    client at registration time would leave the board talking to a stale handle if
    the dashboard ever rebuilds it (and would quietly ignore a swapped-in double).

    Every endpoint is ALSO mounted under /receipts/* so a single Vercel rewrite
    (tristatetags.com/receipts →  this service) carries the whole board —
    tristatetags.com/api/* already belongs to the checkout proxy and must not
    be fought over."""
    _resolve = db_provider if callable(db_provider) else (lambda: db_provider)

    @app.route("/receipts", methods=["GET"])
    def receipts_board():
        html = (BOARD_HTML
                .replace("__STATUSES__", json.dumps(list(STATUS_ORDER)))
                .replace("__LABELS__", json.dumps(STATUS_LABELS))
                .replace("__AGENCY__", json.dumps(_agency())))
        return Response(html, mimetype="text/html")

    @app.route("/receipts/api/transmissions", methods=["GET"])
    @app.route("/api/transmissions", methods=["GET"])
    def api_transmissions():
        try:
            raw = request.args.get("limit", "300")
            limit = int(raw) if str(raw).isdigit() else 300
            return jsonify(_resolve().get_transmissions(
                limit=limit,
                status=(request.args.get("status") or "").strip(),
                search=(request.args.get("q") or "").strip(),
            ))
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/receipts/api/transmissions/<lead_id>/status", methods=["POST"])
    @app.route("/api/transmissions/<lead_id>/status", methods=["POST"])
    def api_set_transmission_status(lead_id):
        body = request.get_json(silent=True) or {}
        status = (body.get("status") or request.args.get("status") or "").strip()
        if status not in STATUS_ORDER:
            return jsonify({"error": f"status must be one of {list(STATUS_ORDER)}"}), 400
        who = (body.get("by") or "").strip()
        if not _resolve().set_lead_status(lead_id, status, who):
            return jsonify({"error": (
                "could not save — if this keeps happening, check that "
                "database/migration_lead_delivery_status.sql has been run"
            )}), 500
        return jsonify({"ok": True, "lead_id": lead_id, "status": status})

    @app.route("/receipts/api/transmissions/<lead_id>/notify", methods=["POST"])
    @app.route("/api/transmissions/<lead_id>/notify", methods=["POST"])
    def api_notify_party(lead_id):
        """Send an email or SMS to one party of one transmission.

        The browser names the party; the ADDRESS is resolved from the database
        here, so the endpoint can never be pointed at an arbitrary recipient."""
        body = request.get_json(silent=True) or {}
        party = (body.get("party") or "").strip().lower()
        channel = (body.get("channel") or "").strip().lower()
        if party not in PARTIES:
            return jsonify({"error": f"party must be one of {list(PARTIES)}"}), 400
        if channel not in ("email", "sms"):
            return jsonify({"error": "channel must be email or sms"}), 400
        message = (body.get("message") or "").strip()
        if not message:
            return jsonify({"error": "message is required"}), 400
        who = (body.get("by") or "").strip()[:64]

        key = (str(lead_id), party, channel)
        now = time.time()
        if now - _recent_sends.get(key, 0) < _MIN_SECONDS_BETWEEN_SENDS:
            return jsonify({"error": "Just sent — give it a few seconds before retrying."}), 429

        contact = _party_contact(_resolve(), lead_id, party)
        if contact is None:
            return jsonify({"error": "lead not found"}), 404

        if channel == "email":
            to = (contact.get("email") or "").strip()
            if not to:
                return jsonify({"error": f"No email on file for the {party}."}), 400
            subject = (body.get("subject") or "").strip() \
                or f"Update on your transmission — {contact.get('reference_id') or lead_id}"
            from utils.client_outreach import send_client_email
            ok, err = send_client_email(to, subject, message, copy_to=_email_copy_address())
            provider, shown = "resend/sendgrid", _mask_email(to)
        else:
            to = (contact.get("phone") or "").strip()
            if not to:
                return jsonify({"error": f"No phone number on file for the {party}."}), 400
            from utils.client_outreach import send_client_sms, sms_configured
            from utils.ghl_client import ghl_configured, send_ghl_sms
            if ghl_configured():
                ok, err = send_ghl_sms(to, message)
                provider = "gohighlevel"
            elif sms_configured():
                ok, err = send_client_sms(to, message)
                provider = "twilio"
            else:
                return jsonify({"error": (
                    "SMS is not configured — set GHL_API_KEY + GHL_LOCATION_ID "
                    "(GoHighLevel), or TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / "
                    "TWILIO_FROM_NUMBER."
                )}), 503
            shown = _mask_phone(to)

        logger.info("receipts board: %s to %s for lead %s by %s -> %s%s",
                    channel, party, lead_id, who or "board",
                    "ok" if ok else "FAILED", "" if ok else f" ({err})")
        if not ok:
            return jsonify({"error": err or "send failed"}), 502
        _recent_sends[key] = now
        return jsonify({"ok": True, "party": party, "channel": channel,
                        "provider": provider, "to": shown})

    @app.route("/receipts/api/sendconfig", methods=["GET"])
    def receipts_send_config():
        """Which senders this deployment can actually use — booleans only, so the
        page can label its buttons honestly. No secrets leave the process."""
        email_ok, sms = False, None
        try:
            from utils.client_outreach import _sendgrid_config, sms_configured
            from utils.resend_client import get_resend_client, get_resend_from_address
            email_ok = bool(get_resend_client() and get_resend_from_address()) \
                or bool(_sendgrid_config())
            from utils.ghl_client import ghl_configured
            if ghl_configured():
                sms = "gohighlevel"
            elif sms_configured():
                sms = "twilio"
        except Exception as e:
            logger.warning("receipts board: sendconfig probe failed: %s", e)
        # Whether statuses can persist at all — the migration adds the column,
        # and a board that quietly cannot save is how this shipped broken once.
        status_col = True
        try:
            _resolve().client.table("leads").select("delivery_status").limit(1).execute()
        except Exception:
            status_col = False
        return jsonify({"email": email_ok, "sms": sms, "status_column": status_col})

    @app.route("/receipts/receipt/<lead_id>", methods=["GET"])
    def receipts_receipt_image(lead_id):
        """The receipt for the board's thumbnails — database first (never
        expires), else the lead's external URL fetched server-side (a redirect
        would be dropped by the Vercel rewrite)."""
        got = None
        try:
            got = _resolve().get_receipt_file(lead_id)
        except Exception as e:
            logger.warning("receipts board: stored receipt read failed for %s: %s", lead_id, e)
        if got:
            safe = RECEIPT_MIME.get((got.get("content_type") or "").lower(), "image/jpeg")
            return Response(got["data"], mimetype=safe, headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'",
            })
        url = ""
        try:
            r = (_resolve().client.table("leads").select("receipt_image_url")
                 .eq("id", str(lead_id)).limit(1).execute())
            url = ((r.data or [{}])[0].get("receipt_image_url") or "").strip()
        except Exception:
            url = ""
        # "/receipt/" URLs point back at this very service — nothing external
        # to fetch, and no reason to call ourselves.
        if url.startswith("http") and "/receipt/" not in url:
            try:
                resp = _requests.get(url.split("#", 1)[0], timeout=25)
                ct = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if resp.ok and resp.content and ct in RECEIPT_MIME:
                    return Response(resp.content, mimetype=RECEIPT_MIME[ct], headers={
                        "Cache-Control": "private, max-age=300",
                        "X-Content-Type-Options": "nosniff",
                        "Content-Security-Policy": "default-src 'none'",
                    })
            except Exception as e:
                logger.warning("receipts board: external receipt fetch failed for %s: %s",
                               lead_id, e)
        return jsonify({"error": "no receipt stored for this lead"}), 404
