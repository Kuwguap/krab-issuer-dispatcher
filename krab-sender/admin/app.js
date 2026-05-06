// Admin page auto-picks the correct API base for your environment:
//   • Served from localhost / 127.0.0.1 / private LAN → same origin (local FastAPI)
//   • Served from file:// (opened directly) → DEFAULT_API_BASE (Render)
//   • Served from any other host (e.g. Vercel) → DEFAULT_API_BASE (Render)
// Override (no code change):
//   • Add ?api=https%3A%2F%2Fyour-api.onrender.com to the URL
//   • Or set localStorage key `krab_api_base` to a full base URL (no trailing slash) and reload
//   • Clear override: ?api=reset
//
// Highkage handle split override:
//   ?highkage=kingkrab,haruhatsu
// or set localStorage key `krab_highkage_handles` to a comma-separated handle list
// (without @). Default highkage handle: haruhatsu
const DEFAULT_API_BASE = "https://krab-dispatch-api.onrender.com";

function _isLocalHost(hostname) {
  const h = String(hostname || "").toLowerCase();
  if (!h) return false;
  if (h === "localhost" || h === "127.0.0.1" || h === "0.0.0.0" || h === "::1") {
    return true;
  }
  // Private LAN ranges and Windows/Mac .local names
  if (h.endsWith(".local") || h.endsWith(".localhost")) return true;
  if (/^10\.\d+\.\d+\.\d+$/.test(h)) return true;
  if (/^192\.168\.\d+\.\d+$/.test(h)) return true;
  if (/^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$/.test(h)) return true;
  return false;
}

function resolveApiBase() {
  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = (params.get("api") || "").trim();
    if (fromQuery === "reset" || fromQuery === "clear") {
      try {
        localStorage.removeItem("krab_api_base");
      } catch {
        // ignore
      }
    } else if (
      fromQuery.startsWith("https://") ||
      fromQuery.startsWith("http://")
    ) {
      const normalized = fromQuery.replace(/\/+$/, "");
      try {
        localStorage.setItem("krab_api_base", normalized);
      } catch {
        // ignore
      }
      return normalized;
    }
  } catch {
    // ignore
  }
  try {
    const stored = (localStorage.getItem("krab_api_base") || "").trim();
    if (stored.startsWith("https://") || stored.startsWith("http://")) {
      return stored.replace(/\/+$/, "");
    }
  } catch {
    // ignore
  }
  // Auto-detect: when the admin page is served from localhost / private LAN,
  // call the API on the same origin so local data shows up.
  try {
    const loc = window.location;
    if (
      loc &&
      (loc.protocol === "http:" || loc.protocol === "https:") &&
      _isLocalHost(loc.hostname)
    ) {
      return loc.origin.replace(/\/+$/, "");
    }
  } catch {
    // ignore
  }
  return DEFAULT_API_BASE;
}

const API_BASE = resolveApiBase();

function issuerTabActive() {
  const issuerPanel = document.getElementById("panel-issuer");
  return !!(issuerPanel && issuerPanel.classList.contains("tab-panel-active"));
}

function transactionsTabActive() {
  const p = document.getElementById("panel-transactions");
  return !!(p && p.classList.contains("tab-panel-active"));
}

function setupAdminTabs() {
  const strip = document.querySelector(".tab-strip");
  const txnPanel = document.getElementById("panel-transactions");
  const dispatchPanel = document.getElementById("panel-dispatch");
  const issuerPanel = document.getElementById("panel-issuer");
  if (!strip || !dispatchPanel || !issuerPanel) {
    return;
  }
  const tabs = strip.querySelectorAll(".tab[data-tab]");

  const activate = (id) => {
    tabs.forEach((btn) => {
      const on = btn.getAttribute("data-tab") === id;
      btn.classList.toggle("tab-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (txnPanel) {
      txnPanel.classList.toggle("tab-panel-active", id === "transactions");
    }
    dispatchPanel.classList.toggle("tab-panel-active", id === "dispatch");
    issuerPanel.classList.toggle("tab-panel-active", id === "issuer");
    if (id === "issuer") {
      refreshIssuerAdmin();
    } else if (id === "transactions") {
      refreshUnifiedTransactions();
    } else if (id === "dispatch") {
      refreshDispatchDriversList();
    }
  };

  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-tab");
      if (id) {
        activate(id);
      }
    });
  });
}

function resolveHighkageHandleSet() {
  const parseList = (raw) => {
    return String(raw || "")
      .split(",")
      .map((h) => h.trim().toLowerCase().replace(/^@/, ""))
      .filter(Boolean);
  };

  try {
    const params = new URLSearchParams(window.location.search);
    const fromQuery = (params.get("highkage") || "").trim();
    if (fromQuery) {
      const handles = parseList(fromQuery);
      if (handles.length > 0) {
        localStorage.setItem("krab_highkage_handles", handles.join(","));
        return new Set(handles);
      }
    }
  } catch {
    // ignore
  }

  try {
    const stored = (localStorage.getItem("krab_highkage_handles") || "").trim();
    const handles = parseList(stored);
    if (handles.length > 0) {
      return new Set(handles);
    }
  } catch {
    // ignore
  }

  return new Set(["haruhatsu"]);
}

const HIGHKAGE_FALLBACK_HANDLES = resolveHighkageHandleSet();

function formatNy(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    const nyDate = new Date(
      d.toLocaleString("en-US", { timeZone: "America/New_York" })
    );
    const day = nyDate.getDate();
    const month = nyDate.toLocaleString("en-US", {
      month: "long",
    });
    const year = nyDate.getFullYear();

    let hours = nyDate.getHours();
    const minutes = nyDate.getMinutes().toString().padStart(2, "0");
    const ampm = hours >= 12 ? "PM" : "AM";
    hours = hours % 12;
    if (hours === 0) hours = 12;

    return `${month} ${day} ${year} ${hours}:${minutes}${ampm.toLowerCase()}`;
  } catch {
    return ts;
  }
}

function formatRevenueUsd(totalCount) {
  const n = Number(totalCount) || 0;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(n * 100);
}

function normalizeHandle(rawHandle) {
  return String(rawHandle || "").trim().toLowerCase().replace(/^@/, "");
}

function formatHandleWithAt(rawHandle) {
  const h = normalizeHandle(rawHandle);
  return h ? "@" + h : "";
}

function truncateNotes(text, maxLen) {
  const n = maxLen != null ? maxLen : 80;
  const s = String(text || "").replace(/\s+/g, " ").trim();
  if (!s) return "—";
  if (s.length <= n) return s;
  return s.slice(0, n) + "…";
}

function escapeHtmlAttr(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function receiptLinkHtml(url) {
  const u = String(url || "").trim();
  if (!u) return "—";
  return `<a href="${escapeHtmlAttr(u)}" target="_blank" rel="noopener noreferrer">View</a>`;
}

function issuerGroupFromHandle(rawHandle) {
  const h = normalizeHandle(rawHandle);
  return HIGHKAGE_FALLBACK_HANDLES.has(h) ? "highkage_group" : "sensei_group";
}

function getStoredPassword() {
  try {
    return String(localStorage.getItem("krab_admin_password") || "").trim();
  } catch {
    return "";
  }
}

function storePassword(pw) {
  try {
    localStorage.setItem("krab_admin_password", pw);
  } catch {
    // ignore
  }
}

// Increments whenever a login batch (initial or manual) successfully validates against the API.
// tryInitialLogin uses this so a stale in-flight failure cannot clear the password after the user unlocked.
let _adminAuthSuccessGeneration = 0;

function bumpAdminAuthSuccessGeneration() {
  _adminAuthSuccessGeneration += 1;
}

function hasAdminPassword() {
  return !!getStoredPassword();
}

function syncAdminPasswordInputs(value) {
  const v = String(value || "");
  ["admin-password-input", "txn-admin-password-input", "issuer-admin-password-input"].forEach(
    (id) => {
      const el = document.getElementById(id);
      if (el) el.value = v;
    }
  );
}

function clearAdminAuthErrorDisplays() {
  document.querySelectorAll("[data-auth-error]").forEach((e) => {
    if (e && e.style) e.style.display = "none";
  });
  ["auth-error", "txn-auth-error", "issuer-auth-error"].forEach((id) => {
    const el = document.getElementById(id);
    if (el && el.style) el.style.display = "none";
  });
}

async function fetchWithAdmin(path, opts = {}) {
  const pw = getStoredPassword();
  if (!pw) {
    throw new Error("NO_PASSWORD");
  }
  const headers = Object.assign({}, opts.headers || {}, {
    "X-Admin-Password": pw,
  });
  let res;
  try {
    res = await fetch(API_BASE + path, {
    ...opts,
    headers,
  });
  } catch (e) {
    const msg = (e && e.message) || String(e);
    throw new Error("NETWORK: " + msg);
  }
  if (res.status === 401) {
    throw new Error("UNAUTHORIZED");
  }
  if (!res.ok) {
    throw new Error("HTTP_" + res.status);
  }
  return res.json();
}

// Like fetchWithAdmin, but does not throw on non-2xx; callers can branch.
async function requestWithAdminJson(path, opts = {}) {
  const pw = getStoredPassword();
  if (!pw) {
    return { ok: false, status: 0, error: "NO_PASSWORD" };
  }
  const headers = Object.assign({}, opts.headers || {}, {
    "X-Admin-Password": pw,
  });
  let res;
  try {
    res = await fetch(API_BASE + path, { ...opts, headers });
  } catch (e) {
    const msg = (e && e.message) || String(e);
    return { ok: false, status: 0, error: "NETWORK: " + msg };
  }
  if (res.status === 401) {
    return { ok: false, status: res.status, error: "UNAUTHORIZED" };
  }
  if (!res.ok) {
    return { ok: false, status: res.status, error: "HTTP_" + res.status };
  }
  try {
    const data = await res.json();
    return { ok: true, status: res.status, data };
  } catch (e) {
    return { ok: false, status: res.status, error: "BAD_JSON" };
  }
}

function issuerFormatDetail(detail) {
  if (detail == null) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((x) => (x && (x.msg || x.message)) || JSON.stringify(x))
      .join("; ");
  }
  return String(detail);
}

async function issuerApiJson(path, opts = {}) {
  const pw = getStoredPassword();
  if (!pw) return { ok: false, error: "NO_PASSWORD" };
  const headers = Object.assign({}, opts.headers || {}, {
    "X-Admin-Password": pw,
  });
  if (opts.body !== undefined && opts.body !== null) {
    headers["Content-Type"] = headers["Content-Type"] || "application/json";
  }
  let res;
  try {
    res = await fetch(API_BASE + path, { ...opts, headers });
  } catch (e) {
    return { ok: false, error: "NETWORK: " + ((e && e.message) || String(e)) };
  }
  const ct = res.headers.get("content-type") || "";
  let body = null;
  if (ct.includes("application/json")) {
    try {
      body = await res.json();
    } catch {
      body = null;
    }
  }
  if (!res.ok) {
    const detail = body && (body.detail ?? body.message ?? body.error);
    return {
      ok: false,
      status: res.status,
      error: issuerFormatDetail(detail) || "HTTP_" + res.status,
      body,
    };
  }
  return { ok: true, data: body };
}

function escapeIssuerText(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// ==========================================================================
// Unified Transactions tab (joins Dispatcher → Issuer → Driver → Receipt).
// ==========================================================================

let _txnRows = [];
let _txnLoading = false;
const KRAB_TXN_PERIOD_KEY = "krab_txn_period";
let txnUnifiedZoomScale = 1;

function txnPeriodApiValue() {
  const sel = document.getElementById("txn-period-select");
  const v = sel ? String(sel.value || "all").trim().toLowerCase() : "all";
  const allowed = new Set(["1w", "2w", "3w", "1m", "3m", "6m", "12m", "all"]);
  return allowed.has(v) ? v : "all";
}

function txnTransactionsUrl(path, limitNum) {
  const p = new URLSearchParams();
  p.set("limit", String(limitNum));
  const period = txnPeriodApiValue();
  if (period && period !== "all") p.set("period", period);
  return `${path}?${p.toString()}`;
}

function _mapBasicTxToUnifiedRows(items) {
  const arr = Array.isArray(items) ? items : [];
  return arr.map((tx) => ({
    id: tx && tx.id,
    reference_id: (tx && tx.reference_id) || null,
    timestamp_ny: (tx && tx.timestamp_ny) || "",
    filename: (tx && tx.filename) || "",
    delivery_status: (tx && tx.delivery_status) || "",
    tag_name: (tx && tx.lead_client_name) || null,
    price: (tx && tx.price) || null,
    receipt_price: (tx && tx.receipt_price) || null,
    receipt_image_url: (tx && tx.receipt_image_url) || null,
    issuer_group: (tx && tx.issuer_group) || "",
    issuer_submitter_handle: null,
    issuer_submitter_telegram_id: null,
    dispatcher_name: (tx && tx.telegram_name) || "",
    dispatcher_handle: (tx && tx.telegram_handle) || "",
    driver_selected_name: (tx && tx.recipient_name) || "",
    driver_recipient_email: (tx && tx.recipient_email) || "",
    driver_accepted: null,
    driver_history: [],
  }));
}

function setTxnBanner(msg) {
  const el = document.getElementById("txn-banner");
  if (!el) return;
  el.textContent = msg || "";
  el.style.display = msg ? "block" : "none";
}

function setTxnStatus(msg) {
  const el = document.getElementById("txn-status");
  if (!el) return;
  el.textContent = msg || "";
}

function _txnDriverEmailSuffix(row) {
  const e = ((row && row.driver_recipient_email) || "").trim();
  if (!e) return "";
  return ` <span class="small muted">${escapeIssuerText(e)}</span>`;
}

function _txnDriverCell(row) {
  const parts = [];
  const accepted = row && row.driver_accepted;
  const selected = (row && row.driver_selected_name) || "";
  const history = Array.isArray(row && row.driver_history)
    ? row.driver_history
    : [];
  const emailSuf = _txnDriverEmailSuffix(row);

  if (accepted && accepted.driver_name) {
    parts.push(
      `<div><strong>${escapeIssuerText(accepted.driver_name)}</strong>${emailSuf}` +
        (accepted.accepted_at
          ? ` <span class="small muted">· accepted ${escapeIssuerText(
              formatNy(accepted.accepted_at)
            )}</span>`
          : "") +
        "</div>"
    );
  } else if (selected) {
    parts.push(
      `<div><strong>${escapeIssuerText(selected)}</strong>${emailSuf}</div>`
    );
  } else if (history.length > 0 && history[0].driver_name) {
    parts.push(
      `<div><strong>${escapeIssuerText(history[0].driver_name)}</strong>${emailSuf}</div>`
    );
  } else {
    parts.push('<div class="muted">—</div>');
  }

  const extras = history.filter((h) => {
    if (!h || !h.driver_name) return false;
    if (accepted && h.driver_name === accepted.driver_name) return false;
    return true;
  });
  if (extras.length > 0) {
    const lines = extras
      .slice(0, 5)
      .map((h) => {
        const st = (h.status || "").toLowerCase();
        const when = h.accepted_at || h.created_at;
        const whenLabel = when ? ` · ${escapeIssuerText(formatNy(when))}` : "";
        return `<div class="small muted">↳ ${escapeIssuerText(
          h.driver_name
        )}${st ? ` · ${escapeIssuerText(st)}` : ""}${whenLabel}</div>`;
      })
      .join("");
    parts.push(lines);
  }
  return parts.join("");
}

function _txnIssuerCell(row) {
  // Issuer = who actually sent the tag through Krab Dispatch.
  const senderName = ((row && row.dispatcher_name) || "").trim();
  const senderHandle = ((row && row.dispatcher_handle) || "").trim();

  if (senderName || senderHandle) {
    const parts = [];
    if (senderName) {
      parts.push(`<strong>${escapeIssuerText(senderName)}</strong>`);
    }
    if (senderHandle) {
      parts.push(`<span class="small muted">@${escapeIssuerText(senderHandle)}</span>`);
    }
    return parts.join(" ");
  }

  // Older/partial rows fallback to Issuer-side submitter identity.
  const issuerHandle = ((row && row.issuer_submitter_handle) || "").trim();
  if (issuerHandle && issuerHandle.toLowerCase() !== "unknown") {
    return `<strong>@${escapeIssuerText(issuerHandle)}</strong>`;
  }
  return '<span class="muted">—</span>';
}

function _txnDispatcherCell(row) {
  // Dispatcher = who created the lead in Krab Issuer.
  const issuerHandle = ((row && row.issuer_submitter_handle) || "").trim();
  const creatorName = ((row && row.dispatcher_name) || "").trim();
  const creatorHandle = ((row && row.dispatcher_handle) || "").trim();

  if (issuerHandle && issuerHandle.toLowerCase() !== "unknown") {
    return `<strong>@${escapeIssuerText(issuerHandle)}</strong>`;
  }

  // Fallback for old rows without Issuer lead creator fields.
  if (creatorName || creatorHandle) {
    const parts = [];
    if (creatorName) {
      parts.push(`<strong>${escapeIssuerText(creatorName)}</strong>`);
    }
    if (creatorHandle) {
      parts.push(`<span class="small muted">@${escapeIssuerText(creatorHandle)}</span>`);
    }
    return parts.join(" ");
  }

  return '<span class="muted">—</span>';
}

function _txnReceiptCell(row) {
  const url = (row && row.receipt_image_url) || "";
  if (!url) return '<span class="muted">—</span>';
  return receiptLinkHtml(url);
}

function _txnPriceCell(row) {
  const raw = (row && row.price) || "";
  if (!raw) return '<span class="muted">—</span>';
  return escapeIssuerText(raw);
}

function _txnReceiptPriceCell(row) {
  const raw = (row && row.receipt_price) || "";
  if (!raw) return '<span class="muted">—</span>';
  return escapeIssuerText(raw);
}

function _txnParsePrice(raw) {
  if (raw == null) return 0;
  const s = String(raw).trim();
  if (!s) return 0;
  // Strip anything that isn't a digit, dot, minus, or comma; normalize commas.
  const cleaned = s.replace(/[^0-9.\-,]/g, "").replace(/,/g, "");
  if (!cleaned || cleaned === "-" || cleaned === ".") return 0;
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? n : 0;
}

function _txnFormatUsd(n) {
  const v = Number.isFinite(n) ? n : 0;
  const hasCents = Math.abs(v - Math.round(v)) > 0.0001;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: hasCents ? 2 : 0,
    maximumFractionDigits: hasCents ? 2 : 0,
  }).format(v);
}

function _txnStatusCell(row) {
  const s = ((row && row.delivery_status) || "").toUpperCase();
  if (!s) return '<span class="muted">—</span>';
  const klass =
    s === "DELIVERED"
      ? "delivered"
      : s === "PENDING"
      ? "pending"
      : "failed";
  return `<span class="pill ${klass}">${escapeIssuerText(s)}</span>`;
}

function _txnMatches(row, qLower) {
  if (!qLower) return true;
  const fields = [
    row.reference_id,
    row.tag_name,
    row.price,
    row.receipt_price,
    row.issuer_group,
    row.issuer_submitter_handle,
    row.dispatcher_name,
    row.dispatcher_handle,
    row.driver_selected_name,
    row.driver_recipient_email,
    row.driver_accepted && row.driver_accepted.driver_name,
    row.filename,
  ];
  for (const h of row.driver_history || []) {
    fields.push(h && h.driver_name);
  }
  return fields.some((v) =>
    v && String(v).toLowerCase().includes(qLower)
  );
}

function renderUnifiedTransactions() {
  const tbody = document.getElementById("txn-tbody");
  if (!tbody) return;
  const q = (
    (document.getElementById("txn-search-input") || {}).value || ""
  )
    .trim()
    .toLowerCase();
  const rows = (_txnRows || []).filter((r) => _txnMatches(r, q));
  tbody.innerHTML = "";
  if (rows.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 11;
    td.className = "muted";
    td.textContent = _txnLoading
      ? "Loading…"
      : "No transactions yet. Send a document through Krab Dispatch.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  let priceSum = 0;
  let priceCount = 0;
  let receiptPriceSum = 0;
  let receiptPriceCount = 0;
  rows.forEach((r, idx) => {
    const tr = document.createElement("tr");
    const parsed = _txnParsePrice(r && r.price);
    if (parsed > 0) {
      priceSum += parsed;
      priceCount += 1;
    }
    const parsedReceipt = _txnParsePrice(r && r.receipt_price);
    if (parsedReceipt > 0) {
      receiptPriceSum += parsedReceipt;
      receiptPriceCount += 1;
    }
    tr.innerHTML =
      `<td>${idx + 1}</td>` +
      `<td class="small">${escapeIssuerText(formatNy(r.timestamp_ny))}</td>` +
      `<td style="text-align:left;max-width:12rem;white-space:normal;">${
        r.tag_name
          ? escapeIssuerText(r.tag_name)
          : `<span class="muted">${escapeIssuerText(r.filename || "—")}</span>`
      }</td>` +
      `<td style="text-align:left;max-width:14rem;white-space:normal;">${_txnIssuerCell(r)}</td>` +
      `<td style="text-align:left;max-width:14rem;white-space:normal;">${_txnDriverCell(r)}</td>` +
      `<td style="text-align:left;white-space:normal;">${_txnDispatcherCell(r)}</td>` +
      `<td>${
        r.reference_id
          ? `<code>${escapeIssuerText(r.reference_id)}</code>`
          : '<span class="muted">—</span>'
      }</td>` +
      `<td>${_txnPriceCell(r)}</td>` +
      `<td>${_txnReceiptPriceCell(r)}</td>` +
      `<td>${_txnReceiptCell(r)}</td>` +
      `<td>${_txnStatusCell(r)}</td>`;
    tbody.appendChild(tr);
  });

  // Spreadsheet-style totals row: sum lead Price and Receipt Price for visible rows.
  const totalTr = document.createElement("tr");
  totalTr.className = "txn-total-row";
  const countText =
    priceCount > 0 || receiptPriceCount > 0
      ? `${priceCount} lead price · ${receiptPriceCount} receipt price`
      : "0 priced";
  totalTr.innerHTML =
    `<td colspan="7" style="text-align:right;font-weight:600;letter-spacing:0.03em;">` +
    `TOTAL <span class="muted" style="font-weight:400;">(${escapeIssuerText(
      countText
    )} of ${rows.length} rows)</span></td>` +
    `<td style="font-weight:700;color:var(--success);">${escapeIssuerText(
      _txnFormatUsd(priceSum)
    )}</td>` +
    `<td style="font-weight:700;color:var(--accent);">${escapeIssuerText(
      _txnFormatUsd(receiptPriceSum)
    )}</td>` +
    `<td colspan="2"></td>`;
  tbody.appendChild(totalTr);

  const statusEl = document.getElementById("txn-status");
  if (statusEl && !_txnLoading) {
    const periodSel = document.getElementById("txn-period-select");
    const rangeLabel =
      periodSel && periodSel.selectedOptions && periodSel.selectedOptions[0]
        ? periodSel.selectedOptions[0].textContent.trim()
        : "";
    const shownLabel =
      rows.length === _txnRows.length
        ? `Showing ${rows.length} transactions`
        : `Showing ${rows.length} of ${_txnRows.length} transactions`;
    statusEl.textContent = `${shownLabel}${rangeLabel ? ` · ${rangeLabel}` : ""} · Lead price total: ${_txnFormatUsd(priceSum)} · Receipt price total: ${_txnFormatUsd(receiptPriceSum)}`;
  }
}

function updateTxnAuthGate() {
  const noAuth = document.getElementById("txn-no-auth");
  if (!noAuth) return true;
  const pw = getStoredPassword();
  noAuth.style.display = pw ? "none" : "block";
  return !pw;
}

async function refreshUnifiedTransactions() {
  setTxnBanner("");
  if (updateTxnAuthGate()) {
    setTxnStatus("");
    _txnRows = [];
    renderUnifiedTransactions();
    return;
  }
  _txnLoading = true;
  setTxnStatus("Loading unified transactions…");
  renderUnifiedTransactions();
  const res = await requestWithAdminJson(
    txnTransactionsUrl("/transactions/full", 2000)
  );
  if (!hasAdminPassword()) {
    _txnLoading = false;
    return;
  }
  _txnLoading = false;
  if (!res.ok) {
    const err = res.error || ("HTTP_" + (res.status || 0));
    setTxnBanner(
      err === "UNAUTHORIZED"
        ? "Unauthorized — re-enter admin password or unlock from Krab Dispatch."
        : err === "NO_PASSWORD"
        ? "Unlock on the Krab Dispatch tab first."
        : "Failed to load: " + err
    );
    setTxnStatus("");
    _txnRows = [];
    renderUnifiedTransactions();
    return;
  }
  _txnRows = Array.isArray(res.data) ? res.data : [];
  if (_txnRows.length === 0) {
    // Forward-step validation fallback: if unified join returns empty, retry with
    // base transactions so dashboard still shows latest activity.
    const basic = await requestWithAdminJson(
      txnTransactionsUrl("/transactions", 200)
    );
    if (!hasAdminPassword()) {
      return;
    }
    if (basic.ok) {
      _txnRows = _mapBasicTxToUnifiedRows(basic.data);
    }
  }
  if (!hasAdminPassword()) {
    return;
  }
  const periodSel = document.getElementById("txn-period-select");
  const rangeLabel =
    periodSel && periodSel.selectedOptions && periodSel.selectedOptions[0]
      ? periodSel.selectedOptions[0].textContent.trim()
      : "";
  setTxnStatus(
    _txnRows.length
      ? `Showing ${_txnRows.length} transactions${
          rangeLabel ? ` · ${rangeLabel}` : ""
        }.`
      : ""
  );
  renderUnifiedTransactions();
}

function doAdminLogout() {
  storePassword("");
  syncAdminPasswordInputs("");
  clearAdminAuthErrorDisplays();
  _txnLoading = false;
  _txnRows = [];
  setTxnBanner("");
  setTxnStatus("");
  renderUnifiedTransactions();
  setIssuerBanner("");
  applyLoggedInUI(false);
}

function maybeRefreshTxnTab() {
  if (transactionsTabActive()) {
    refreshUnifiedTransactions();
  }
}

function setupTxnEvents() {
  const periodSel = document.getElementById("txn-period-select");
  if (periodSel) {
    try {
      const saved = localStorage.getItem(KRAB_TXN_PERIOD_KEY);
      const allowed = new Set([
        "1w",
        "2w",
        "3w",
        "1m",
        "3m",
        "6m",
        "12m",
        "all",
      ]);
      if (saved && allowed.has(saved)) periodSel.value = saved;
    } catch (_) {}
    periodSel.addEventListener("change", () => {
      try {
        localStorage.setItem(KRAB_TXN_PERIOD_KEY, periodSel.value);
      } catch (_) {}
      refreshUnifiedTransactions();
    });
  }
  const refresh = document.getElementById("txn-refresh-btn");
  if (refresh) {
    refresh.addEventListener("click", () => refreshUnifiedTransactions());
  }
  const search = document.getElementById("txn-search-input");
  if (search) {
    search.addEventListener("input", () => renderUnifiedTransactions());
  }
}

function setIssuerBanner(msg) {
  const el = document.getElementById("issuer-banner");
  if (!el) return;
  el.textContent = msg || "";
  el.style.display = msg ? "block" : "none";
}

function updateIssuerAuthGate() {
  const noAuth = document.getElementById("issuer-no-auth");
  const main = document.getElementById("issuer-main-wrap");
  if (!noAuth || !main) return true;
  const pw = getStoredPassword();
  if (!pw) {
    noAuth.style.display = "block";
    main.style.display = "none";
    setIssuerBanner("");
    return true;
  }
  noAuth.style.display = "none";
  main.style.display = "block";
  return false;
}

function maybeRefreshIssuerTab() {
  if (issuerTabActive()) {
    refreshIssuerAdmin();
  }
}

function issuerFillAssignSelects(groups, drivers) {
  const sg = document.getElementById("issuer-assign-group");
  const sd = document.getElementById("issuer-assign-driver");
  if (!sg || !sd) return;
  const prevG = sg.value;
  const prevD = sd.value;
  sg.innerHTML = "";
  sd.innerHTML = "";
  (groups || []).forEach((g) => {
    const o = document.createElement("option");
    o.value = g.id;
    o.textContent = g.group_name || g.id;
    sg.appendChild(o);
  });
  (drivers || []).forEach((d) => {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = d.driver_name || d.id;
    sd.appendChild(o);
  });
  if (prevG && [...sg.options].some((o) => o.value === prevG)) {
    sg.value = prevG;
  }
  if (prevD && [...sd.options].some((o) => o.value === prevD)) {
    sd.value = prevD;
  }
}

function renderIssuerBotUsage(rows) {
  const tb = document.getElementById("issuer-bot-usage-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "muted";
    td.textContent = "No usage yet.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((u) => {
    const tr = document.createElement("tr");
    const when = document.createElement("td");
    const ca = u.created_at || "";
    when.textContent = ca.length >= 19 ? ca.slice(0, 19) : ca || "—";
    const user = document.createElement("td");
    const un = u.telegram_username ? "@" + u.telegram_username : u.user_telegram_id;
    user.textContent = un || "—";
    const grp = document.createElement("td");
    grp.textContent = u.group_name || "—";
    const drv = document.createElement("td");
    drv.textContent = u.driver_names || "—";
    drv.style.whiteSpace = "normal";
    tr.appendChild(when);
    tr.appendChild(user);
    tr.appendChild(grp);
    tr.appendChild(drv);
    tb.appendChild(tr);
  });
}

function renderIssuerStats(stats) {
  const sumEl = document.getElementById("issuer-stats-summary");
  const tb = document.getElementById("issuer-stats-tbody");
  if (!sumEl || !tb) return;
  const total = stats && stats.total_leads != null ? stats.total_leads : 0;
  sumEl.textContent = "Total leads sent: " + total;
  tb.innerHTML = "";
  const drivers = (stats && stats.drivers) || [];
  if (drivers.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = "No drivers.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  drivers.forEach((d) => {
    const tr = document.createElement("tr");
    const a = document.createElement("td");
    a.textContent = d.driver_name || "—";
    const b = document.createElement("td");
    b.textContent = String(d.leads_accepted ?? 0);
    const c = document.createElement("td");
    c.textContent = String(d.receipts_submitted ?? 0);
    tr.appendChild(a);
    tr.appendChild(b);
    tr.appendChild(c);
    tb.appendChild(tr);
  });
}

function renderIssuerContactSources(sources) {
  const tb = document.getElementById("issuer-contact-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(sources) ? sources : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = "No sources yet.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((s) => {
    const tr = document.createElement("tr");
    const lab = document.createElement("td");
    lab.textContent = s.label || "—";
    lab.style.textAlign = "left";
    const st = document.createElement("td");
    st.textContent = s.is_active === false ? "Inactive" : "Active";
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = s.is_active === false ? "Activate" : "Deactivate";
    btn.dataset.toggleContactSource = s.id;
    act.appendChild(btn);
    tr.appendChild(lab);
    tr.appendChild(st);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderIssuerAssignments(rows, groups, drivers) {
  const tb = document.getElementById("issuer-assignments-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  const gmap = Object.fromEntries((groups || []).map((g) => [g.id, g.group_name]));
  const dmap = Object.fromEntries((drivers || []).map((d) => [d.id, d.driver_name]));
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 3;
    td.className = "muted";
    td.textContent = "No assignments.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((a) => {
    const tr = document.createElement("tr");
    const g = document.createElement("td");
    g.textContent = a.group_name || gmap[a.group_id] || "—";
    const d = document.createElement("td");
    d.textContent = a.driver_name || dmap[a.driver_id] || "—";
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = "Remove";
    btn.dataset.removeAssignment = a.id;
    act.appendChild(btn);
    tr.appendChild(g);
    tr.appendChild(d);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderIssuerGroups(groups) {
  const tb = document.getElementById("issuer-groups-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(groups) ? groups : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "No groups.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((g) => {
    const tr = document.createElement("tr");
    const nm = document.createElement("td");
    nm.textContent = g.group_name || "—";
    nm.style.textAlign = "left";
    const tg = document.createElement("td");
    tg.innerHTML = "<code>" + escapeIssuerText(g.group_telegram_id || "") + "</code>";
    const sup = document.createElement("td");
    sup.innerHTML = "<code>" + escapeIssuerText(g.supervisory_telegram_id || "") + "</code>";
    const st = document.createElement("td");
    st.textContent = g.is_active === false ? "Inactive" : "Active";
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = g.is_active === false ? "Activate" : "Deactivate";
    btn.dataset.toggleGroup = g.id;
    act.appendChild(btn);
    tr.appendChild(nm);
    tr.appendChild(tg);
    tr.appendChild(sup);
    tr.appendChild(st);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderIssuerDrivers(drivers) {
  const tb = document.getElementById("issuer-drivers-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(drivers) ? drivers : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "No drivers.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((d) => {
    const tr = document.createElement("tr");
    const nm = document.createElement("td");
    nm.textContent = d.driver_name || "—";
    nm.style.textAlign = "left";
    const tg = document.createElement("td");
    tg.innerHTML = "<code>" + escapeIssuerText(d.driver_telegram_id || "") + "</code>";
    const ph = document.createElement("td");
    ph.textContent = d.phone_number || "—";
    const st = document.createElement("td");
    st.textContent = d.is_active === false ? "Inactive" : "Active";
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = d.is_active === false ? "Activate" : "Deactivate";
    btn.dataset.toggleDriver = d.id;
    act.appendChild(btn);
    tr.appendChild(nm);
    tr.appendChild(tg);
    tr.appendChild(ph);
    tr.appendChild(st);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderDispatchDrivers(drivers) {
  const tb = document.getElementById("dispatch-drivers-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(drivers) ? drivers : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "No drivers.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((d) => {
    const tr = document.createElement("tr");
    const nm = document.createElement("td");
    nm.textContent = d.driver_name || "—";
    nm.style.textAlign = "left";
    const tg = document.createElement("td");
    tg.innerHTML = "<code>" + escapeIssuerText(d.driver_telegram_id || "") + "</code>";
    const ph = document.createElement("td");
    ph.textContent = d.phone_number || "—";
    const st = document.createElement("td");
    st.textContent = d.is_active === false ? "Inactive" : "Active";
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = d.is_active === false ? "Activate" : "Deactivate";
    btn.dataset.toggleDispatchDriver = d.id;
    act.appendChild(btn);
    tr.appendChild(nm);
    tr.appendChild(tg);
    tr.appendChild(ph);
    tr.appendChild(st);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

async function refreshDispatchDriversList() {
  const tb = document.getElementById("dispatch-drivers-tbody");
  if (!tb) return;
  if (!hasAdminPassword()) {
    tb.innerHTML =
      '<tr><td colspan="5" class="muted">Unlock to view drivers.</td></tr>';
    return;
  }
  try {
    const res = await fetch(API_BASE + "/dispatch-drivers/ui");
    if (res.status === 503) {
      tb.innerHTML =
        '<tr><td colspan="5" class="muted">Issuer database not configured on API.</td></tr>';
      return;
    }
    if (!res.ok) {
      tb.innerHTML =
        '<tr><td colspan="5" class="muted">Could not load drivers.</td></tr>';
      return;
    }
    const drivers = await res.json();
    renderDispatchDrivers(drivers);
  } catch (e) {
    console.error(e);
    tb.innerHTML =
      '<tr><td colspan="5" class="muted">Failed to load drivers.</td></tr>';
  }
}

function renderIssuerAssistants(groups, assistantsByGroup) {
  const host = document.getElementById("issuer-assistants-host");
  if (!host) return;
  host.innerHTML = "";
  const list = Array.isArray(groups) ? groups : [];
  if (list.length === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No groups yet.";
    host.appendChild(p);
    return;
  }
  list.forEach((g) => {
    const card = document.createElement("div");
    card.className = "assistant-card";
    const title = document.createElement("div");
    const strong = document.createElement("strong");
    strong.textContent = g.group_name || g.id;
    title.appendChild(strong);
    title.appendChild(document.createTextNode(" — assistants"));
    const ul = document.createElement("ul");
    const tids = assistantsByGroup[g.id] || [];
    if (tids.length === 0) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "None yet";
      ul.appendChild(li);
    } else {
      tids.forEach((tid) => {
        const li = document.createElement("li");
        const code = document.createElement("code");
        code.style.fontSize = "0.78rem";
        code.textContent = tid;
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn-danger-issuer";
        btn.style.marginLeft = "0.35rem";
        btn.textContent = "Remove";
        btn.dataset.removeAssistantGroup = g.id;
        btn.dataset.removeAssistantTid = tid;
        li.appendChild(code);
        li.appendChild(btn);
        ul.appendChild(li);
      });
    }
    const row = document.createElement("div");
    row.className = "field-row";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "Assistant Telegram ID";
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.textContent = "Add assistant";
    addBtn.dataset.addAssistantGroup = g.id;
    row.appendChild(inp);
    row.appendChild(addBtn);
    card.appendChild(title);
    card.appendChild(ul);
    card.appendChild(row);
    host.appendChild(card);
  });
}

function renderIssuerDebts(summary) {
  const tb = document.getElementById("issuer-debts-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const drivers = (summary && summary.drivers) || [];
  const withDebt = drivers.filter((x) => (Number(x.owed_receipts) || 0) > 0);
  if (withDebt.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.className = "muted";
    td.textContent = "No pending receipt debts.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  withDebt.forEach((d) => {
    const tr = document.createElement("tr");
    const name = document.createElement("td");
    name.style.textAlign = "left";
    name.textContent = (d.driver_name || "—") + (d.is_active === false ? " (inactive)" : "");
    const owed = document.createElement("td");
    owed.textContent = String(d.owed_receipts ?? 0);
    const refs = document.createElement("td");
    refs.style.whiteSpace = "normal";
    refs.style.textAlign = "left";
    refs.style.fontSize = "0.74rem";
    (d.pending_references || []).forEach((pr) => {
      const wrap = document.createElement("span");
      wrap.style.display = "inline-block";
      wrap.style.margin = "0.1rem 0.35rem 0 0";
      const code = document.createElement("code");
      code.textContent = pr.reference_id || "—";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-danger-issuer";
      btn.textContent = "×";
      btn.title = "Remove assignment";
      btn.dataset.removeDebtAssignment = pr.assignment_id;
      wrap.appendChild(code);
      wrap.appendChild(document.createTextNode(" "));
      wrap.appendChild(btn);
      refs.appendChild(wrap);
    });
    const act = document.createElement("td");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-danger-issuer";
    btn.textContent = "Clear all pending";
    btn.dataset.clearDebtsDriver = d.driver_id;
    act.appendChild(btn);
    tr.appendChild(name);
    tr.appendChild(owed);
    tr.appendChild(refs);
    tr.appendChild(act);
    tb.appendChild(tr);
  });
}

function renderIssuerSubmitted(rows) {
  const tb = document.getElementById("issuer-submitted-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "No submitted receipts.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((r) => {
    const tr = document.createElement("tr");
    const ref = document.createElement("td");
    ref.textContent = r.reference_id || "—";
    const dr = document.createElement("td");
    dr.textContent = r.driver_name || "—";
    const gr = document.createElement("td");
    gr.textContent = r.group_name || "—";
    const upd = document.createElement("td");
    const u = r.updated_at || "";
    upd.textContent = u.length >= 19 ? u.slice(0, 19) : u || "—";
    const link = document.createElement("td");
    link.innerHTML = receiptLinkHtml(r.receipt_image_url);
    tr.appendChild(ref);
    tr.appendChild(dr);
    tr.appendChild(gr);
    tr.appendChild(upd);
    tr.appendChild(link);
    tb.appendChild(tr);
  });
}

function renderIssuerRenewals(rows) {
  const tb = document.getElementById("issuer-renewals-tbody");
  if (!tb) return;
  tb.innerHTML = "";
  const list = Array.isArray(rows) ? rows : [];
  if (list.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.className = "muted";
    td.textContent = "No upcoming renewals.";
    tr.appendChild(td);
    tb.appendChild(tr);
    return;
  }
  list.forEach((r) => {
    const tr = document.createElement("tr");
    const due = document.createElement("td");
    const ds = r.renewal_due_at || "";
    due.textContent = ds.length >= 19 ? ds.slice(0, 19) : ds || "—";
    const ref = document.createElement("td");
    ref.textContent = r.reference_id || "—";
    const cl = document.createElement("td");
    cl.textContent = r.client_name || "—";
    cl.style.maxWidth = "8rem";
    cl.style.whiteSpace = "normal";
    const gr = document.createElement("td");
    gr.textContent = r.group_name || "—";
    const dr = document.createElement("td");
    dr.textContent = r.driver_name || "—";
    const st = document.createElement("td");
    st.textContent = r.status || "—";
    const days = document.createElement("td");
    days.textContent = r.days_left != null ? String(r.days_left) : "—";
    tr.appendChild(due);
    tr.appendChild(ref);
    tr.appendChild(cl);
    tr.appendChild(gr);
    tr.appendChild(dr);
    tr.appendChild(st);
    tr.appendChild(days);
    tb.appendChild(tr);
  });
}

function applyIssuerSettingsUi(settings) {
  const choose = !!(settings && settings.assistants_choose_group);
  const mode =
    settings && settings.receipt_detection_mode === "strict" ? "strict" : "lax";
  const flowLabel = document.getElementById("issuer-lead-flow-label");
  const btnMode = document.getElementById("issuer-toggle-assistants-mode");
  const recLabel = document.getElementById("issuer-receipt-mode-label");
  if (flowLabel) {
    flowLabel.textContent = choose
      ? "Current: assistants choose group."
      : "Current: assigned groups only.";
  }
  if (btnMode) {
    btnMode.textContent = choose
      ? "Use assigned groups only"
      : "Allow assistants to choose group";
  }
  if (recLabel) {
    recLabel.textContent =
      mode === "strict"
        ? "Current: strict ($ on receipt)."
        : "Current: lax (match amount).";
  }
  const stIn = document.getElementById("issuer-st-telegram-id");
  if (stIn && settings) {
    stIn.value = settings.st_telegram_id || "";
  }
}

async function refreshIssuerAdmin() {
  setIssuerBanner("");
  if (updateIssuerAuthGate()) {
    return;
  }
  const gRes = await issuerApiJson("/issuer-admin/groups");
  if (!hasAdminPassword()) {
    return;
  }
  if (!gRes.ok) {
    setIssuerBanner(
      gRes.status === 503
        ? "Configure SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY on the API server."
        : gRes.error || "Could not load Issuer data."
    );
    return;
  }
  const groups = Array.isArray(gRes.data) ? gRes.data : [];
  const [
    dRes,
    setRes,
    aRes,
    cRes,
    bRes,
    stRes,
    rdRes,
    subRes,
    renRes,
  ] = await Promise.all([
    issuerApiJson("/issuer-admin/drivers"),
    issuerApiJson("/issuer-admin/settings"),
    issuerApiJson("/issuer-admin/assignments"),
    issuerApiJson("/issuer-admin/contact-sources"),
    issuerApiJson("/issuer-admin/bot-usage?limit=100"),
    issuerApiJson("/issuer-admin/stats"),
    issuerApiJson("/issuer-admin/receipt-debts/summary"),
    issuerApiJson("/issuer-admin/receipts/submitted?limit=80"),
    issuerApiJson("/issuer-admin/renewals/upcoming"),
  ]);
  if (!hasAdminPassword()) {
    return;
  }
  const softErr = [dRes, setRes, aRes, cRes, bRes, stRes, rdRes, subRes, renRes].find(
    (x) => !x.ok && x.status !== 503
  );
  if (softErr && softErr.status === 401) {
    setIssuerBanner("Session expired. Re-enter password on the Dispatch tab.");
    return;
  }
  const drivers = dRes.ok && Array.isArray(dRes.data) ? dRes.data : [];
  const settings = setRes.ok && setRes.data ? setRes.data : {};
  const assignments = aRes.ok && Array.isArray(aRes.data) ? aRes.data : [];
  const contacts = cRes.ok && Array.isArray(cRes.data) ? cRes.data : [];
  const botUsage = bRes.ok && Array.isArray(bRes.data) ? bRes.data : [];
  const stats = stRes.ok && stRes.data ? stRes.data : { total_leads: 0, drivers: [] };
  const debts = rdRes.ok && rdRes.data ? rdRes.data : { drivers: [] };
  const submitted = subRes.ok && Array.isArray(subRes.data) ? subRes.data : [];
  const renewals = renRes.ok && Array.isArray(renRes.data) ? renRes.data : [];

  if (!hasAdminPassword()) {
    return;
  }
  applyIssuerSettingsUi(settings);
  issuerFillAssignSelects(groups, drivers);
  renderIssuerBotUsage(botUsage);
  renderIssuerStats(stats);
  renderIssuerContactSources(contacts);
  renderIssuerAssignments(assignments, groups, drivers);
  renderIssuerGroups(groups);
  renderIssuerDrivers(drivers);
  renderIssuerDebts(debts);
  renderIssuerSubmitted(submitted);
  renderIssuerRenewals(renewals);

  const asstResults = await Promise.all(
    groups.map((g) => issuerApiJson("/issuer-admin/groups/" + g.id + "/assistants"))
  );
  if (!hasAdminPassword()) {
    return;
  }
  const assistantsByGroup = {};
  groups.forEach((g, i) => {
    const r = asstResults[i];
    assistantsByGroup[g.id] = r.ok && Array.isArray(r.data) ? r.data : [];
  });
  renderIssuerAssistants(groups, assistantsByGroup);

  const warn = [dRes, setRes, aRes, cRes, bRes, stRes, rdRes, subRes, renRes].filter(
    (x) => !x.ok
  );
  if (warn.length) {
    setIssuerBanner(
      "Some sections failed to load (" +
        warn.map((w) => w.error || "error").join("; ") +
        "). Check API logs."
    );
  }
}

function setupIssuerAdminEvents() {
  const refreshBtn = document.getElementById("issuer-refresh-btn");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => refreshIssuerAdmin());
  }

  const btnAssist = document.getElementById("issuer-toggle-assistants-mode");
  if (btnAssist) {
    btnAssist.addEventListener("click", async () => {
      const cur = await issuerApiJson("/issuer-admin/settings");
      if (!cur.ok) {
        alert(cur.error || "Could not read settings");
        return;
      }
      const next = !cur.data.assistants_choose_group;
      const res = await issuerApiJson("/issuer-admin/settings", {
        method: "POST",
        body: JSON.stringify({ assistants_choose_group: next }),
      });
      if (!res.ok) {
        alert(res.error || "Could not update");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const rs = document.getElementById("issuer-set-receipt-strict");
  const rl = document.getElementById("issuer-set-receipt-lax");
  async function postReceiptMode(mode) {
    const res = await issuerApiJson("/issuer-admin/settings", {
      method: "POST",
      body: JSON.stringify({ receipt_detection_mode: mode }),
    });
    if (!res.ok) {
      alert(res.error || "Could not update");
      return;
    }
    await refreshIssuerAdmin();
  }
  if (rs) rs.addEventListener("click", () => postReceiptMode("strict"));
  if (rl) rl.addEventListener("click", () => postReceiptMode("lax"));

  const saveSt = document.getElementById("issuer-save-st-btn");
  if (saveSt) {
    saveSt.addEventListener("click", async () => {
      const v = document.getElementById("issuer-st-telegram-id");
      const raw = v ? v.value.trim() : "";
      const res = await issuerApiJson("/issuer-admin/settings", {
        method: "POST",
        body: JSON.stringify({ st_telegram_id: raw }),
      });
      if (!res.ok) {
        alert(res.error || "Could not save");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const addG = document.getElementById("issuer-add-group-btn");
  if (addG) {
    addG.addEventListener("click", async () => {
      const name = document.getElementById("issuer-group-name").value.trim();
      const gtg = document.getElementById("issuer-group-tg-id").value.trim();
      const sup = document.getElementById("issuer-group-supervisory-id").value.trim();
      if (!name || !gtg || !sup) {
        alert("Fill group name, group Telegram ID, and supervisory ID.");
        return;
      }
      const res = await issuerApiJson("/issuer-admin/groups", {
        method: "POST",
        body: JSON.stringify({
          group_name: name,
          group_telegram_id: gtg,
          supervisory_telegram_id: sup,
        }),
      });
      if (!res.ok) {
        alert(res.error || "Could not add group");
        return;
      }
      document.getElementById("issuer-group-name").value = "";
      document.getElementById("issuer-group-tg-id").value = "";
      document.getElementById("issuer-group-supervisory-id").value = "";
      await refreshIssuerAdmin();
    });
  }

  const addD = document.getElementById("issuer-add-driver-btn");
  if (addD) {
    addD.addEventListener("click", async () => {
      const name = document.getElementById("issuer-driver-name").value.trim();
      const tg = document.getElementById("issuer-driver-tg-id").value.trim();
      const phone = document.getElementById("issuer-driver-phone").value.trim();
      if (!name || !tg) {
        alert("Driver name and Telegram ID are required.");
        return;
      }
      const body = { driver_name: name, driver_telegram_id: tg };
      if (phone) body.phone_number = phone;
      const res = await issuerApiJson("/issuer-admin/drivers", {
        method: "POST",
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        alert(res.error || "Could not add driver");
        return;
      }
      document.getElementById("issuer-driver-name").value = "";
      document.getElementById("issuer-driver-tg-id").value = "";
      document.getElementById("issuer-driver-phone").value = "";
      await refreshIssuerAdmin();
    });
  }

  const addC = document.getElementById("issuer-add-contact-btn");
  if (addC) {
    addC.addEventListener("click", async () => {
      const lab = document.getElementById("issuer-contact-label").value.trim();
      if (!lab) {
        alert("Enter a label.");
        return;
      }
      const res = await issuerApiJson("/issuer-admin/contact-sources", {
        method: "POST",
        body: JSON.stringify({ label: lab, sort_order: 0 }),
      });
      if (!res.ok) {
        alert(res.error || "Could not add");
        return;
      }
      document.getElementById("issuer-contact-label").value = "";
      await refreshIssuerAdmin();
    });
  }

  const assignBtn = document.getElementById("issuer-assign-btn");
  if (assignBtn) {
    assignBtn.addEventListener("click", async () => {
      const gid = document.getElementById("issuer-assign-group").value;
      const did = document.getElementById("issuer-assign-driver").value;
      if (!gid || !did) {
        alert("Choose a group and driver.");
        return;
      }
      const res = await issuerApiJson("/issuer-admin/assignments", {
        method: "POST",
        body: JSON.stringify({ group_id: gid, driver_id: did }),
      });
      if (!res.ok) {
        alert(res.error || "Could not assign (duplicate?)");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const gtb = document.getElementById("issuer-groups-tbody");
  if (gtb) {
    gtb.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-toggle-group]");
      if (!btn) return;
      const id = btn.getAttribute("data-toggle-group");
      const res = await issuerApiJson("/issuer-admin/groups/" + id + "/toggle", {
        method: "POST",
      });
      if (!res.ok) {
        alert(res.error || "Toggle failed");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const dtb = document.getElementById("issuer-drivers-tbody");
  if (dtb) {
    dtb.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-toggle-driver]");
      if (!btn) return;
      const id = btn.getAttribute("data-toggle-driver");
      const res = await issuerApiJson("/issuer-admin/drivers/" + id + "/toggle", {
        method: "POST",
      });
      if (!res.ok) {
        alert(res.error || "Toggle failed");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const ctb = document.getElementById("issuer-contact-tbody");
  if (ctb) {
    ctb.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-toggle-contact-source]");
      if (!btn) return;
      const id = btn.getAttribute("data-toggle-contact-source");
      const res = await issuerApiJson(
        "/issuer-admin/contact-sources/" + id + "/toggle",
        { method: "POST" }
      );
      if (!res.ok) {
        alert(res.error || "Toggle failed");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const atb = document.getElementById("issuer-assignments-tbody");
  if (atb) {
    atb.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-remove-assignment]");
      if (!btn) return;
      const id = btn.getAttribute("data-remove-assignment");
      if (!confirm("Remove this driver from the group?")) return;
      const res = await issuerApiJson("/issuer-admin/assignments/" + id, {
        method: "DELETE",
      });
      if (!res.ok) {
        alert(res.error || "Remove failed");
        return;
      }
      await refreshIssuerAdmin();
    });
  }

  const debtb = document.getElementById("issuer-debts-tbody");
  if (debtb) {
    debtb.addEventListener("click", async (ev) => {
      const rm = ev.target.closest("[data-remove-debt-assignment]");
      if (rm) {
        const aid = rm.getAttribute("data-remove-debt-assignment");
        if (!confirm("Delete this assignment row? Lead stays in DB.")) return;
        const res = await issuerApiJson(
          "/issuer-admin/receipt-debts/assignments/" + aid,
          { method: "DELETE" }
        );
        if (!res.ok) {
          alert(res.error || "Could not remove");
          return;
        }
        await refreshIssuerAdmin();
        return;
      }
      const cl = ev.target.closest("[data-clear-debts-driver]");
      if (cl) {
        const did = cl.getAttribute("data-clear-debts-driver");
        if (!confirm("Clear ALL pending receipt assignments for this driver?")) return;
        const res = await issuerApiJson(
          "/issuer-admin/receipt-debts/drivers/" + did + "/pending",
          { method: "DELETE" }
        );
        if (!res.ok) {
          alert(res.error || "Could not clear");
          return;
        }
        await refreshIssuerAdmin();
      }
    });
  }

  const host = document.getElementById("issuer-assistants-host");
  if (host) {
    host.addEventListener("click", async (ev) => {
      const add = ev.target.closest("[data-add-assistant-group]");
      if (add) {
        const gid = add.getAttribute("data-add-assistant-group");
        const row = add.closest(".field-row");
        const inp = row ? row.querySelector("input[type=text]") : null;
        const tid = inp ? inp.value.trim() : "";
        if (!tid) {
          alert("Enter a Telegram ID.");
          return;
        }
        const res = await issuerApiJson("/issuer-admin/groups/" + gid + "/assistants", {
          method: "POST",
          body: JSON.stringify({ telegram_id: tid }),
        });
        if (!res.ok) {
          alert(res.error || "Could not add assistant");
          return;
        }
        inp.value = "";
        await refreshIssuerAdmin();
        return;
      }
      const rm = ev.target.closest("[data-remove-assistant-group]");
      if (rm) {
        const gid = rm.getAttribute("data-remove-assistant-group");
        const tid = rm.getAttribute("data-remove-assistant-tid");
        if (!confirm("Remove assistant " + tid + "?")) return;
        const res = await issuerApiJson(
          "/issuer-admin/groups/" +
            gid +
            "/assistants/" +
            encodeURIComponent(tid),
          { method: "DELETE" }
        );
        if (!res.ok) {
          alert(res.error || "Could not remove");
          return;
        }
        await refreshIssuerAdmin();
      }
    });
  }
}

async function checkHealth() {
  const pill = document.getElementById("status-pill");
  const text = document.getElementById("status-text");
  try {
    const res = await fetch(API_BASE + "/health");
    const data = await res.json();
    if (data.status === "ok") {
      pill.querySelector(".dot").classList.remove("dot-bad");
      pill.querySelector(".dot").classList.add("dot-ok");
      text.textContent = "API online · " + API_BASE;
    } else {
      text.textContent = "API up but health check is not OK";
    }
  } catch (e) {
    pill.querySelector(".dot").classList.remove("dot-ok");
    pill.querySelector(".dot").classList.add("dot-bad");
    text.textContent = "API unreachable (check Render) · " + API_BASE;
  }
}

function renderTransactions(items) {
  const body = document.getElementById("tx-body");
  body.innerHTML = "";
  if (!items || items.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 10;
    td.className = "muted";
    td.textContent = "No transmissions yet.";
    tr.appendChild(td);
    body.appendChild(tr);
    return;
  }

  const sortedItems = [...items].sort((a, b) => parseItemTimeMs(b) - parseItemTimeMs(a));
  sortedItems.forEach((tx, index) => {
    const tr = document.createElement("tr");

    const tdNum = document.createElement("td");
    tdNum.textContent = String(index + 1);
    tr.appendChild(tdNum);

    const tdTime = document.createElement("td");
    tdTime.textContent = formatNy(tx.timestamp_ny);
    tr.appendChild(tdTime);

    const tdClient = document.createElement("td");
    tdClient.innerHTML = `<strong>${tx.filename}</strong>`;
    tr.appendChild(tdClient);

    const tdTelegram = document.createElement("td");
    tdTelegram.textContent = tx.telegram_name || "—";
    tr.appendChild(tdTelegram);

    const tdDriver = document.createElement("td");
    if (tx.recipient_name) {
      tdDriver.textContent = tx.recipient_name;
    } else {
      tdDriver.textContent = "Not recorded";
    }
    tr.appendChild(tdDriver);

    const tdRef = document.createElement("td");
    tdRef.textContent = (tx.reference_id && String(tx.reference_id).trim()) || "—";
    tr.appendChild(tdRef);

    const tdStatus = document.createElement("td");
    tdStatus.className = "status";
    const pill = document.createElement("span");
    const status = (tx.delivery_status || "").toUpperCase();
    pill.classList.add("pill");
    if (status === "DELIVERED") {
      pill.classList.add("delivered");
    } else if (status === "PENDING") {
      pill.classList.add("pending");
    } else {
      pill.classList.add("failed");
    }
    pill.textContent = status || "UNKNOWN";
    tdStatus.appendChild(pill);
    tr.appendChild(tdStatus);

    body.appendChild(tr);
  });
}

async function refreshTransactions() {
  const body = document.getElementById("tx-body");
  try {
    const data = await fetchWithAdmin("/transactions");
    if (!hasAdminPassword()) {
      return;
    }
    renderTransactions(data);
  } catch (e) {
    console.error(e);
    if (!body) {
      return;
    }
    body.innerHTML = "";
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 10;
    td.className = "muted";
    td.textContent =
      (e && e.message && String(e.message).startsWith("NETWORK:")
        ? "API unreachable. Check Render service and network, or point admin to the correct API. Base: " +
          API_BASE
        : "Failed to load data. " + (e && e.message ? e.message : "")) +
      "";
    tr.appendChild(td);
    body.appendChild(tr);
  }
}

async function refreshLatest() {
  const el = document.getElementById("latest-tx");
  try {
    const data = await fetchWithAdmin("/transactions/latest");
    if (!hasAdminPassword()) {
      return;
    }
    if (!data) {
      el.textContent = "No transmissions yet.";
      return;
    }
    const refLine =
      data.reference_id && String(data.reference_id).trim()
        ? `<div class="small">Reference: ${String(data.reference_id).trim()}</div>`
        : "";
    const leadLine =
      data.lead_client_name && String(data.lead_client_name).trim()
        ? `<div class="small">Lead: ${String(data.lead_client_name)
            .trim()
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")}</div>`
        : "";
    const recLine = data.receipt_image_url
      ? `<div class="small">Receipt: ${receiptLinkHtml(data.receipt_image_url)}</div>`
      : "";
    el.innerHTML = `
      <div><strong>${data.filename}</strong></div>
      ${refLine}
      ${leadLine}
      ${recLine}
      <div class="small">
        ${data.telegram_name || "—"} · ${formatNy(data.timestamp_ny)}
      </div>
      <div class="small">Driver: ${data.recipient_name || "Not recorded"}</div>
      <div class="small">Status: ${data.delivery_status}</div>
    `;
  } catch (e) {
    console.error(e);
    if (el) {
      el.textContent =
        (e && e.message && String(e.message).startsWith("NETWORK:")
          ? "API unreachable. Base: " + API_BASE
          : "Failed to load latest. " + (e && e.message ? e.message : ""));
    }
  }
}

let lastSummary = null;
let summaryZoomScale = 1;
let txZoomScale = 1;
const summaryAiHistory = [];
const SUMMARY_AI_HISTORY_KEY = "krab_summary_ai_history";

function loadSummaryAiHistory() {
  try {
    const raw = sessionStorage.getItem(SUMMARY_AI_HISTORY_KEY);
    if (!raw) return;
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      summaryAiHistory.length = 0;
      for (const m of parsed.slice(-20)) {
        if (!m || !m.role || !m.content) continue;
        summaryAiHistory.push({
          role: String(m.role),
          content: String(m.content),
        });
      }
    }
  } catch {
    // ignore
  }
}

function saveSummaryAiHistory() {
  try {
    sessionStorage.setItem(
      SUMMARY_AI_HISTORY_KEY,
      JSON.stringify(summaryAiHistory.slice(-20))
    );
  } catch {
    // ignore
  }
}

function renderSummaryAiLog() {
  const log = document.getElementById("summary-ai-log");
  if (!log) return;
  log.innerHTML = "";
  if (!summaryAiHistory.length) {
    const div = document.createElement("div");
    div.className = "summary-ai-msg assistant";
    div.textContent = "Ask questions about current summary data.";
    log.appendChild(div);
    return;
  }
  for (const m of summaryAiHistory.slice(-16)) {
    const div = document.createElement("div");
    div.className =
      "summary-ai-msg " + (m.role === "user" ? "user" : "assistant");
    div.textContent = m.content;
    log.appendChild(div);
  }
  log.scrollTop = log.scrollHeight;
}

function clampSummaryZoom(next) {
  return Math.max(0.05, Math.min(2.5, next));
}

function applySummaryZoom(scale) {
  summaryZoomScale = clampSummaryZoom(scale);
  const table = document.querySelector("#summary-table table");
  if (table) {
    // `zoom` gives practical pinch-like resizing in Chromium-based browsers.
    table.style.zoom = String(summaryZoomScale);
  }
  const resetBtn = document.getElementById("summary-zoom-reset-btn");
  if (resetBtn) {
    resetBtn.textContent = `${Math.round(summaryZoomScale * 100)}%`;
  }
  const resetBtnExpanded = document.getElementById("summary-expanded-zoom-reset-btn");
  if (resetBtnExpanded) {
    resetBtnExpanded.textContent = `${Math.round(summaryZoomScale * 100)}%`;
  }
}

function clampTxZoom(next) {
  return Math.max(0.05, Math.min(2.5, next));
}

function applyTxZoom(scale) {
  txZoomScale = clampTxZoom(scale);
  const table = document.querySelector("#tx-table table");
  if (table) {
    table.style.zoom = String(txZoomScale);
  }
  const resetBtn = document.getElementById("tx-zoom-reset-btn");
  if (resetBtn) {
    resetBtn.textContent = `${Math.round(txZoomScale * 100)}%`;
  }
  const resetBtnExpanded = document.getElementById("tx-expanded-zoom-reset-btn");
  if (resetBtnExpanded) {
    resetBtnExpanded.textContent = `${Math.round(txZoomScale * 100)}%`;
  }
}

function clampTxnUnifiedZoom(next) {
  return Math.max(0.05, Math.min(2.5, next));
}

function applyTxnUnifiedZoom(scale) {
  txnUnifiedZoomScale = clampTxnUnifiedZoom(scale);
  const table = document.querySelector("#txn-ledger-wrapper table");
  if (table) {
    table.style.zoom = String(txnUnifiedZoomScale);
  }
  const resetBtn = document.getElementById("txn-ledger-zoom-reset-btn");
  if (resetBtn) {
    resetBtn.textContent = `${Math.round(txnUnifiedZoomScale * 100)}%`;
  }
  const resetBtnExpanded = document.getElementById(
    "txn-ledger-expanded-zoom-reset-btn"
  );
  if (resetBtnExpanded) {
    resetBtnExpanded.textContent = `${Math.round(txnUnifiedZoomScale * 100)}%`;
  }
}

function renderSummaryTable(summary) {
  const tbody = document.getElementById("summary-tbody");
  if (!tbody) return;

  tbody.innerHTML = "";

  const items = (summary && summary.items) || [];
  if (items.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 13;
    td.className = "muted";
    td.textContent = "No transmissions in this summary window.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const sorted = [...items].sort((a, b) => parseItemTimeMs(b) - parseItemTimeMs(a));
  const issuerHandleCounts = {};
  for (const it of sorted) {
    const handle = normalizeHandle(it.telegram_handle) || "__unknown__";
    issuerHandleCounts[handle] = (issuerHandleCounts[handle] || 0) + 1;
  }

  for (let i = 0; i < sorted.length; i += 1) {
    const it = sorted[i];
    const tr = document.createElement("tr");

    const tdNum = document.createElement("td");
    tdNum.textContent = String(i + 1);
    tr.appendChild(tdNum);

    const tdTime = document.createElement("td");
    tdTime.textContent = formatNy(it.timestamp_ny);
    tr.appendChild(tdTime);

    const tdPdf = document.createElement("td");
    tdPdf.textContent = it.filename || "—";
    tr.appendChild(tdPdf);

    const tdIssuerName = document.createElement("td");
    tdIssuerName.textContent = it.telegram_name || "—";
    tr.appendChild(tdIssuerName);

    const tdDriverName = document.createElement("td");
    tdDriverName.textContent = it.recipient_name || "—";
    tr.appendChild(tdDriverName);

    const tdSuccess = document.createElement("td");
    tdSuccess.textContent =
      (it.delivery_status || "").toUpperCase() === "DELIVERED" ? "YES" : "NO";
    tr.appendChild(tdSuccess);

    const tdStatus = document.createElement("td");
    tdStatus.className = "status";
    const statusPill = document.createElement("span");
    const status = (it.delivery_status || "").toUpperCase();
    statusPill.classList.add("pill");
    if (status === "DELIVERED") {
      statusPill.classList.add("delivered");
    } else if (status === "PENDING") {
      statusPill.classList.add("pending");
    } else {
      statusPill.classList.add("failed");
    }
    statusPill.textContent = status || "UNKNOWN";
    tdStatus.appendChild(statusPill);
    tr.appendChild(tdStatus);

    const tdCount = document.createElement("td");
    const handleKey = normalizeHandle(it.telegram_handle) || "__unknown__";
    tdCount.textContent = String(issuerHandleCounts[handleKey] || 0);
    tr.appendChild(tdCount);

    const tdIssuerHandle = document.createElement("td");
    tdIssuerHandle.textContent = formatHandleWithAt(it.telegram_handle) || "—";
    tr.appendChild(tdIssuerHandle);

    const tdDriverEmail = document.createElement("td");
    tdDriverEmail.textContent = it.recipient_email || "—";
    tr.appendChild(tdDriverEmail);

    const tdRef = document.createElement("td");
    tdRef.textContent = (it.reference_id && String(it.reference_id).trim()) || "—";
    tr.appendChild(tdRef);

    const tdPrice = document.createElement("td");
    const p = it.price != null && String(it.price).trim() !== "" ? String(it.price).trim() : "";
    tdPrice.textContent = p || "—";
    tr.appendChild(tdPrice);

    const tdReceiptPrice = document.createElement("td");
    const rp =
      it.receipt_price != null && String(it.receipt_price).trim() !== ""
        ? String(it.receipt_price).trim()
        : "";
    tdReceiptPrice.textContent = rp || "—";
    tr.appendChild(tdReceiptPrice);

    const tdReceipt = document.createElement("td");
    tdReceipt.className = "small";
    tdReceipt.innerHTML = receiptLinkHtml(it.receipt_image_url);
    tr.appendChild(tdReceipt);

    tbody.appendChild(tr);
  }
}

function renderSummaryIssuerTable(summary) {
  const tbody = document.getElementById("summary-issuer-tbody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const items = (summary && summary.items) || [];
  if (items.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "No issuer data in this summary window.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  const grouped = {};
  for (const it of items) {
    const issuerName = it.telegram_name || "Unknown";
    const issuerHandle = formatHandleWithAt(it.telegram_handle) || "—";
    const key = `${issuerName}||${issuerHandle}`;
    if (!grouped[key]) {
      grouped[key] = {
        issuerName,
        issuerHandle,
        total: 0,
        delivered: 0,
        pending: 0,
        failed: 0,
        drivers: new Set(),
      };
    }
    const g = grouped[key];
    g.total += 1;
    const status = (it.delivery_status || "").toUpperCase();
    if (status === "DELIVERED") g.delivered += 1;
    else if (status === "PENDING") g.pending += 1;
    else g.failed += 1;
    if (it.recipient_name) g.drivers.add(it.recipient_name);
  }

  const rows = Object.values(grouped).sort((a, b) => b.total - a.total);
  for (const row of rows) {
    const tr = document.createElement("tr");
    const cells = [
      row.issuerName,
      row.issuerHandle,
      String(row.delivered),
      String(row.pending),
      row.drivers.size > 0 ? Array.from(row.drivers).join(", ") : "—",
    ];
    for (const c of cells) {
      const td = document.createElement("td");
      td.textContent = c;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function getNyDateKey(ts) {
  try {
    const d = new Date(ts);
    return d.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  } catch {
    return "";
  }
}

function answerSummaryQuestion(question) {
  const q = String(question || "").trim().toLowerCase();
  if (!q) return "Please ask a question.";
  if (!lastSummary || !Array.isArray(lastSummary.items)) {
    return "Generate summary first so I can analyze the data.";
  }
  const items = lastSummary.items;
  if (!items.length) return "There are no rows in the current summary window.";

  // Forward-step validation: only answer from loaded rows and clear patterns.
  if (q.includes("how many issuers") && q.includes("today")) {
    const todayNy = getNyDateKey(new Date().toISOString());
    const issuerSet = new Set();
    for (const it of items) {
      if (getNyDateKey(it.timestamp_ny) === todayNy) {
        issuerSet.add((it.telegram_name || "").trim().toLowerCase());
      }
    }
    return `${issuerSet.size} issuer(s) made transactions today (NJ time).`;
  }

  const m = q.match(/how many(?:\s+does)?\s+(@?[a-z0-9_ ]+?)\s+(?:have|made|did)/i);
  if (m && m[1]) {
    const targetRaw = m[1].trim().toLowerCase().replace(/^@/, "");
    let count = 0;
    for (const it of items) {
      const name = (it.telegram_name || "").trim().toLowerCase();
      const handle = normalizeHandle(it.telegram_handle);
      if (name === targetRaw || handle === targetRaw) count += 1;
    }
    return `${targetRaw} has ${count} transaction(s) in the current summary window.`;
  }

  if (q.includes("total")) {
    return `Total transactions in this summary: ${items.length}.`;
  }

  if (q.includes("how are you") || q.includes("how you doing")) {
    return "I am doing great and ready to help with your dashboard data. 😊";
  }

  if (
    (q.includes("which issuer") || q.includes("who")) &&
    (q.includes("most") || q.includes("highest") || q.includes("top"))
  ) {
    const counts = {};
    for (const it of items) {
      const issuer = (it.telegram_name || "Unknown").trim();
      counts[issuer] = (counts[issuer] || 0) + 1;
    }
    let topIssuer = "Unknown";
    let topCount = 0;
    for (const [issuer, n] of Object.entries(counts)) {
      if (n > topCount) {
        topIssuer = issuer;
        topCount = n;
      }
    }
    return `${topIssuer} has the most transactions: ${topCount}.`;
  }

  return "I can answer questions like: 'how many issuers made transactions today?' or 'how many does haru have?'";
}

async function askSummaryWithGpt(question) {
  if (!lastSummary || !Array.isArray(lastSummary.items)) {
    return "Generate summary first so I can analyze the data.";
  }
  const q = String(question || "").trim();
  if (!q) {
    return "Please ask a question.";
  }
  const payload = {
    question: q,
    history: summaryAiHistory.slice(-12),
    window:
      (document.getElementById("summary-window") &&
        document.getElementById("summary-window").value) ||
      "1w",
    summary: {
      period_start_ny: lastSummary.period_start_ny,
      period_end_ny: lastSummary.period_end_ny,
      total_transactions: lastSummary.total_transactions,
      delivered: lastSummary.delivered,
      pending: lastSummary.pending,
      failed: lastSummary.failed,
      items: (lastSummary.items || []).slice(0, 300),
    },
  };
  const res = await requestWithAdminJson("/ai/summary-ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    return `AI unavailable (${res.error || "request failed"}).`;
  }
  return (res.data && res.data.answer) || "No answer returned.";
}

function downloadSummaryCsv() {
  if (!lastSummary || !lastSummary.items || lastSummary.items.length === 0) {
    alert("No summary data to download. Generate a summary first.");
    return;
  }

  const rows = [
    [
      "Row",
      "TimeDate",
      "ClientPdfName",
      "Notes",
      "LeadIssuer",
      "IssuerName",
      "DriverName",
      "Success",
      "Status",
      "Count",
      "IssuerUsername",
      "DriverEmail",
      "Reference",
      "Price",
      "ReceiptPrice",
      "Receipt",
    ],
  ];

  const issuerHandleCounts = {};
  for (const it of lastSummary.items) {
    const handle = normalizeHandle(it.telegram_handle) || "__unknown__";
    issuerHandleCounts[handle] = (issuerHandleCounts[handle] || 0) + 1;
  }

  for (let i = 0; i < lastSummary.items.length; i += 1) {
    const it = lastSummary.items[i];
    const receiptUrl = (it.receipt_image_url && String(it.receipt_image_url).trim()) || "";
    const receiptCsvValue = receiptUrl
      ? `=HYPERLINK("${receiptUrl.replace(/"/g, '""')}","View")`
      : "";
    const priceStr =
      it.price != null && String(it.price).trim() !== ""
        ? String(it.price).trim()
        : "";
    const receiptPriceStr =
      it.receipt_price != null && String(it.receipt_price).trim() !== ""
        ? String(it.receipt_price).trim()
        : "";
    rows.push([
      i + 1,
      formatNy(it.timestamp_ny || ""),
      it.filename || "",
      (() => {
        const s = String(it.client_details || "")
          .replace(/\s+/g, " ")
          .trim();
        if (!s) return "";
        return s.length > 500 ? s.slice(0, 500) + "…" : s;
      })(),
      (it.lead_client_name && String(it.lead_client_name).trim()) || "",
      it.telegram_name || "",
      it.recipient_name || "Not recorded",
      (it.delivery_status || "").toUpperCase() === "DELIVERED" ? "YES" : "NO",
      (it.delivery_status || "").toUpperCase() || "UNKNOWN",
      issuerHandleCounts[normalizeHandle(it.telegram_handle) || "__unknown__"] || 0,
      formatHandleWithAt(it.telegram_handle),
      it.recipient_email || "",
      (it.reference_id && String(it.reference_id).trim()) || "",
      priceStr,
      receiptPriceStr,
      receiptCsvValue,
    ]);
  }

  const csv = rows
    .map((r) =>
      r
        .map((field) => {
          const v = String(field ?? "");
          if (v.includes(",") || v.includes('"') || v.includes("\n")) {
            return `"${v.replace(/"/g, '""')}"`;
          }
          return v;
        })
        .join(",")
    )
    .join("\n");

  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);

  const a = document.createElement("a");
  a.href = url;
  a.download = "krab_dispatch_summary.csv";
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * Issuer team totals for the dashboard.
 *
 * Important: do NOT trust `issuer_group` alone — production data may label
 * highkage senders (e.g. @haruhatsu) as sensei. Telegram handle list wins.
 */
function deriveGroupCountsForDashboard(data) {
  const items = (data && data.items) || [];
  const counts = {
    sensei_group: { issued: 0, sent: 0 },
    highkage_group: { issued: 0, sent: 0 },
  };

  for (const it of items) {
    const status = (it.delivery_status || "").toUpperCase();
    const handle = String(it.telegram_handle || "")
      .trim()
      .toLowerCase()
      .replace(/^@/, "");
    const bucket = HIGHKAGE_FALLBACK_HANDLES.has(handle)
      ? "highkage_group"
      : "sensei_group";
    counts[bucket].issued += 1;
    if (status === "DELIVERED") {
      counts[bucket].sent += 1;
    }
  }

  return counts;
}

function windowKeyToDays(windowKey) {
  const k = (windowKey || "1w").toLowerCase();
  const map = {
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
    all: null,
  };
  return map[k] === undefined ? 7 : map[k];
}

function parseItemTimeMs(item) {
  const raw = (item && item.timestamp_ny) || "";
  if (!raw) {
    return NaN;
  }
  const t = new Date(raw);
  const ms = t.getTime();
  return Number.isNaN(ms) ? NaN : ms;
}

async function fetchAllAdminTransactions() {
  const all = [];
  const pageSize = 200;
  let offset = 0;
  const maxItems = 20000;
  while (all.length < maxItems) {
    const pageRes = await requestWithAdminJson(
      "/transactions?limit=" + pageSize + "&offset=" + offset
    );
    if (!pageRes.ok) {
      throw new Error(pageRes.error || "FAILED_PAGE");
    }
    const page = pageRes.data;
    if (!page || page.length === 0) {
      break;
    }
    for (const row of page) {
      all.push(row);
    }
    if (page.length < pageSize) {
      break;
    }
    offset += pageSize;
  }
  return all;
}

function buildClientWindowSummary(allTx, windowKey) {
  const nowMs = Date.now();
  const days = windowKeyToDays(windowKey);
  const startMs = days == null ? null : nowMs - days * 24 * 60 * 60 * 1000;

  const filtered = [];
  for (const it of allTx) {
    const tms = parseItemTimeMs(it);
    if (Number.isNaN(tms)) {
      continue;
    }
    if (startMs == null || tms >= startMs) {
      filtered.push(it);
    }
  }
  filtered.sort((a, b) => parseItemTimeMs(a) - parseItemTimeMs(b));

  let delivered = 0;
  let pending = 0;
  let failed = 0;
  for (const it of filtered) {
    const status = (it.delivery_status || "").toUpperCase();
    if (status === "DELIVERED") {
      delivered += 1;
    } else if (status === "PENDING") {
      pending += 1;
    } else {
      failed += 1;
    }
  }

  const firstItemMs = filtered.length > 0 ? parseItemTimeMs(filtered[0]) : NaN;
  const lastItemMs =
    filtered.length > 0
      ? parseItemTimeMs(filtered[filtered.length - 1])
      : NaN;

  let periodStartMs = nowMs;
  if (filtered.length > 0 && !Number.isNaN(firstItemMs)) {
    periodStartMs = firstItemMs;
  } else if (startMs != null) {
    periodStartMs = startMs;
  } else {
    // "all" and no data: use now as a harmless anchor for formatting
    periodStartMs = nowMs;
  }

  let periodEndMs = nowMs;
  if (filtered.length > 0 && !Number.isNaN(lastItemMs)) {
    periodEndMs = lastItemMs;
  }

  return {
    period_start_ny: new Date(periodStartMs).toISOString(),
    period_end_ny: new Date(periodEndMs).toISOString(),
    total_transactions: filtered.length,
    delivered,
    pending,
    failed,
    items: filtered,
    _client_window: true,
  };
}

async function refreshSummary() {
  const windowEl = document.getElementById("summary-window");
  const periodEl = document.getElementById("summary-period");
  const totalEl = document.getElementById("summary-total");
  const revenueEl = document.getElementById("summary-revenue");
  const deliveredEl = document.getElementById("summary-delivered");
  const pfEl = document.getElementById("summary-pending-failed");
  const senseiEl = document.getElementById("summary-sensei");
  const highkageEl = document.getElementById("summary-highkage");
  const statusEl = document.getElementById("summary-status");

  try {
    const windowKey = (windowEl && windowEl.value) || "1w";
    if (statusEl) {
      statusEl.textContent = "Loading summary (NJ)...";
    }

    // Primary path: server-side rolling window summary (avoids large /transactions scans).
    const rollRes = await requestWithAdminJson(
      "/summaries/rolling?window=" + encodeURIComponent(windowKey)
    );
    if (!hasAdminPassword()) {
      return;
    }
    let data = null;
    if (rollRes.ok) {
      data = rollRes.data;
    } else {
      if (statusEl) {
        statusEl.textContent =
          "Rolling summary API unavailable, building summary locally (can be slow)...";
      }
      const allTx = await fetchAllAdminTransactions();
      if (!hasAdminPassword()) {
        return;
      }
      data = buildClientWindowSummary(allTx, windowKey);
    }
    if (!hasAdminPassword()) {
      return;
    }
    lastSummary = data;
    periodEl.textContent =
      data.period_start_ny && data.period_end_ny
        ? `${formatNy(data.period_start_ny)} → ${formatNy(data.period_end_ny)}`
        : `All time → ${formatNy(data.period_end_ny)}`;
    totalEl.textContent = `${data.total_transactions} total`;
    if (revenueEl) {
      revenueEl.textContent = formatRevenueUsd(data.total_transactions);
    }
    deliveredEl.textContent = data.delivered;
    pfEl.textContent = `${data.pending} / ${data.failed}`;
    const fallbackCounts = deriveGroupCountsForDashboard(data);
    const apiGc = data.group_counts;
    const useApiGroupCounts =
      apiGc &&
      apiGc.sensei_group &&
      apiGc.highkage_group &&
      !data._client_window;
    const sensei = useApiGroupCounts
      ? apiGc.sensei_group
      : fallbackCounts.sensei_group;
    const highkage = useApiGroupCounts
      ? apiGc.highkage_group
      : fallbackCounts.highkage_group;
    senseiEl.textContent = `${sensei.issued} / ${sensei.sent}`;
    highkageEl.textContent = `${highkage.issued} / ${highkage.sent}`;
    if (statusEl) {
      if (data.total_transactions === 0) {
        statusEl.textContent = "No transmissions in the selected window.";
      } else {
        const omitted = Number(data.items_omitted) || 0;
        const extra =
          omitted > 0
            ? ` Table lists the latest ${
                (data.items && data.items.length) || 0
              } rows; ${omitted} older rows are omitted from the table.`
            : "";
        statusEl.textContent = "Summary generated successfully." + extra;
      }
    }

    renderSummaryTable(data);
    renderSummaryIssuerTable(data);
  } catch (e) {
    console.error(e);
    const revenueOnErr = document.getElementById("summary-revenue");
    if (revenueOnErr) {
      revenueOnErr.textContent = "—";
    }
    if (statusEl) {
      statusEl.textContent =
        e && e.message && String(e.message).startsWith("NETWORK:")
          ? "API unreachable. Check Render service, then try again. Base: " + API_BASE
          : "Failed to load summary. " + (e && e.message ? e.message : "");
    }
  }
}

function applyLoggedInUI(loggedIn) {
  const authArea = document.getElementById("auth-area");
  const dashArea = document.getElementById("dashboard-area");
  const tabLogoutIds = [
    "txn-tab-logout-btn",
    "dispatch-tab-logout-btn",
    "issuer-tab-logout-btn",
  ];
  const dispatchHeaderWrap = document.getElementById("dispatch-header-wrap");
  const dispatchTxPanel = document.getElementById("dispatch-transmissions-panel");
  const txnPrivateWrap = document.getElementById("txn-private-wrap");
  const txnToolbarPrivate = document.getElementById("txn-toolbar-private");
  const txnPageTitle = document.getElementById("txn-page-title");
  const txnAuthArea = document.getElementById("txn-auth-area");
  const issuerPageTitle = document.getElementById("issuer-page-title");
  const issuerAddDriverWrap = document.getElementById("issuer-add-driver-wrap");
  const issuerPrivateWrap = document.getElementById("issuer-private-wrap");
  const dispatchDriversListWrap = document.getElementById(
    "dispatch-drivers-list-wrap"
  );
  const issuerToolbarPrivate = document.getElementById("issuer-toolbar-private");
  const issuerAuthArea = document.getElementById("issuer-auth-area");

  if (authArea) authArea.style.display = loggedIn ? "none" : "block";
  if (dashArea) dashArea.style.display = loggedIn ? "block" : "none";
  tabLogoutIds.forEach((id) => {
    const b = document.getElementById(id);
    if (b) b.style.display = loggedIn ? "inline-flex" : "none";
  });

  // Krab Dispatch tab: when locked, show ONLY the Access panel.
  if (dispatchHeaderWrap) dispatchHeaderWrap.style.display = loggedIn ? "flex" : "none";
  if (dispatchTxPanel) dispatchTxPanel.style.display = loggedIn ? "block" : "none";

  // Transactions tab: when locked, show ONLY the Unlock controls in header.
  if (txnPageTitle) txnPageTitle.style.display = loggedIn ? "block" : "none";
  if (txnAuthArea) txnAuthArea.style.display = loggedIn ? "none" : "block";
  if (txnPrivateWrap) txnPrivateWrap.style.display = loggedIn ? "block" : "none";
  if (txnToolbarPrivate) txnToolbarPrivate.style.display = loggedIn ? "contents" : "none";

  // Krab Issuer tab: when locked, show Access + Add driver.
  if (issuerPageTitle) issuerPageTitle.style.display = loggedIn ? "block" : "none";
  if (issuerAuthArea) issuerAuthArea.style.display = loggedIn ? "none" : "block";
  if (issuerPrivateWrap) issuerPrivateWrap.style.display = loggedIn ? "block" : "none";
  if (issuerToolbarPrivate) issuerToolbarPrivate.style.display = loggedIn ? "contents" : "none";
  // Keep add-driver visible even when locked (list stays hidden until unlocked).
  if (issuerAddDriverWrap) issuerAddDriverWrap.style.display = "block";

  // Krab Dispatch: Telegram drivers list mirrors Issuer (private until unlocked).
  if (dispatchDriversListWrap) {
    dispatchDriversListWrap.style.display = loggedIn ? "block" : "none";
    dispatchDriversListWrap.setAttribute(
      "aria-hidden",
      loggedIn ? "false" : "true"
    );
  }
}

async function tryInitialLogin() {
  const genAtStart = _adminAuthSuccessGeneration;
  if (!hasAdminPassword()) return;
  try {
    await refreshTransactions();
    await refreshLatest();
    await refreshSummary();
    bumpAdminAuthSuccessGeneration();
    applyLoggedInUI(true);
  } catch {
    if (_adminAuthSuccessGeneration !== genAtStart) {
      return;
    }
    storePassword("");
    applyLoggedInUI(false);
  }
}

function setupEvents() {
  const loginBtn = document.getElementById("login-btn");
  const input = document.getElementById("admin-password-input");
  const err = document.getElementById("auth-error");
  const txnLoginBtn = document.getElementById("txn-login-btn");
  const txnInput = document.getElementById("txn-admin-password-input");
  const txnErr = document.getElementById("txn-auth-error");
  const issuerLoginBtn = document.getElementById("issuer-login-btn");
  const issuerInput = document.getElementById("issuer-admin-password-input");
  const issuerErr = document.getElementById("issuer-auth-error");
  const refreshTableBtn = document.getElementById("refresh-table-btn");
  const summaryBtn = document.getElementById("summary-btn");
  const summaryDownloadBtn = document.getElementById(
    "summary-download-btn"
  );
  const summaryExpandBtn = document.getElementById("summary-expand-btn");
  const summaryZoomInBtn = document.getElementById("summary-zoom-in-btn");
  const summaryZoomOutBtn = document.getElementById("summary-zoom-out-btn");
  const summaryZoomResetBtn = document.getElementById("summary-zoom-reset-btn");
  const summaryExpandedZoomInBtn = document.getElementById(
    "summary-expanded-zoom-in-btn"
  );
  const summaryExpandedZoomOutBtn = document.getElementById(
    "summary-expanded-zoom-out-btn"
  );
  const summaryExpandedZoomResetBtn = document.getElementById(
    "summary-expanded-zoom-reset-btn"
  );
  const summaryExpandedCloseBtn = document.getElementById(
    "summary-expanded-close-btn"
  );
  const txExpandBtn = document.getElementById("tx-expand-btn");
  const txZoomInBtn = document.getElementById("tx-zoom-in-btn");
  const txZoomOutBtn = document.getElementById("tx-zoom-out-btn");
  const txZoomResetBtn = document.getElementById("tx-zoom-reset-btn");
  const txExpandedZoomInBtn = document.getElementById("tx-expanded-zoom-in-btn");
  const txExpandedZoomOutBtn = document.getElementById("tx-expanded-zoom-out-btn");
  const txExpandedZoomResetBtn = document.getElementById("tx-expanded-zoom-reset-btn");
  const txExpandedCloseBtn = document.getElementById("tx-expanded-close-btn");
  const txnLedgerExpandBtn = document.getElementById("txn-ledger-expand-btn");
  const txnLedgerZoomInBtn = document.getElementById("txn-ledger-zoom-in-btn");
  const txnLedgerZoomOutBtn = document.getElementById("txn-ledger-zoom-out-btn");
  const txnLedgerZoomResetBtn = document.getElementById("txn-ledger-zoom-reset-btn");
  const txnLedgerExpandedZoomInBtn = document.getElementById(
    "txn-ledger-expanded-zoom-in-btn"
  );
  const txnLedgerExpandedZoomOutBtn = document.getElementById(
    "txn-ledger-expanded-zoom-out-btn"
  );
  const txnLedgerExpandedZoomResetBtn = document.getElementById(
    "txn-ledger-expanded-zoom-reset-btn"
  );
  const txnLedgerExpandedCloseBtn = document.getElementById(
    "txn-ledger-expanded-close-btn"
  );
  const summaryAiInput = document.getElementById("summary-ai-input");
  const summaryAiAskBtn = document.getElementById("summary-ai-ask-btn");
  const summaryAiAnswer = document.getElementById("summary-ai-answer");
  loadSummaryAiHistory();
  renderSummaryAiLog();

  async function doLoginWithPassword(pwRaw, errEl) {
    const pw = String(pwRaw || "").trim();
    if (!pw) return;
    storePassword(pw);
    syncAdminPasswordInputs(pw);
    clearAdminAuthErrorDisplays();
    try {
      await refreshTransactions();
      await refreshLatest();
      await refreshSummary();
      bumpAdminAuthSuccessGeneration();
      applyLoggedInUI(true);
      // Recipients will be refreshed by the modified applyLoggedInUI
    } catch (e) {
      console.error(e);
      storePassword("");
      syncAdminPasswordInputs("");
      if (errEl && errEl.style) {
        errEl.style.display = "block";
      } else if (err) {
        err.style.display = "block";
      }
    }
  }

  async function doLoginFromMain() {
    return doLoginWithPassword(input && input.value, err);
  }
  async function doLoginFromTxn() {
    return doLoginWithPassword(txnInput && txnInput.value, txnErr);
  }
  async function doLoginFromIssuer() {
    return doLoginWithPassword(issuerInput && issuerInput.value, issuerErr);
  }

  if (loginBtn) loginBtn.addEventListener("click", doLoginFromMain);
  if (input) {
    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") doLoginFromMain();
    });
  }
  if (txnLoginBtn) txnLoginBtn.addEventListener("click", doLoginFromTxn);
  if (txnInput) {
    txnInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") doLoginFromTxn();
    });
  }
  if (issuerLoginBtn) issuerLoginBtn.addEventListener("click", doLoginFromIssuer);
  if (issuerInput) {
    issuerInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") doLoginFromIssuer();
    });
  }

  ["txn-tab-logout-btn", "dispatch-tab-logout-btn", "issuer-tab-logout-btn"].forEach(
    (id) => {
      const b = document.getElementById(id);
      if (b) b.addEventListener("click", () => doAdminLogout());
    }
  );

  const dispatchAddDriverBtn = document.getElementById("dispatch-add-driver-btn");
  if (dispatchAddDriverBtn) {
    dispatchAddDriverBtn.addEventListener("click", async () => {
      const name = (document.getElementById("dispatch-driver-name") || {}).value || "";
      const tid = (document.getElementById("dispatch-driver-chat-id") || {}).value || "";
      const phoneRaw =
        (document.getElementById("dispatch-driver-phone") || {}).value || "";
      const msg = document.getElementById("dispatch-add-driver-msg");
      const driver_name = String(name).trim();
      const driver_telegram_id = String(tid).trim();
      const phone_number = String(phoneRaw || "").trim();
      if (!driver_name || !driver_telegram_id) {
        if (msg) msg.textContent = "Driver name and Telegram chat/user ID are required.";
        return;
      }
      if (msg) msg.textContent = "Adding…";
      try {
        const body = { driver_name, driver_telegram_id };
        if (phone_number) body.phone_number = phone_number;
        const res = await fetch(API_BASE + "/dispatch-drivers/ui", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (res.status === 409) {
          if (msg) msg.textContent = "Driver already exists for this Telegram ID.";
          return;
        }
        if (!res.ok) {
          const txt = await res.text().catch(() => "");
          throw new Error("HTTP_" + res.status + (txt ? ": " + txt : ""));
        }
        (document.getElementById("dispatch-driver-name") || {}).value = "";
        (document.getElementById("dispatch-driver-chat-id") || {}).value = "";
        (document.getElementById("dispatch-driver-phone") || {}).value = "";
        if (msg) msg.textContent = "Driver added.";
        refreshDispatchDriversList();
      } catch (e) {
        console.error(e);
        if (msg) msg.textContent = "Failed to add driver. Please try again.";
      }
    });
  }

  const dispatchDriversTb = document.getElementById("dispatch-drivers-tbody");
  if (dispatchDriversTb) {
    dispatchDriversTb.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-toggle-dispatch-driver]");
      if (!btn) return;
      const id = btn.getAttribute("data-toggle-dispatch-driver");
      if (!id) return;
      try {
        const res = await fetch(
          API_BASE + "/dispatch-drivers/ui/" + encodeURIComponent(id) + "/toggle",
          { method: "POST" }
        );
        if (!res.ok) {
          alert("Could not toggle driver.");
          return;
        }
        await refreshDispatchDriversList();
      } catch (e) {
        console.error(e);
        alert("Could not toggle driver.");
      }
    });
  }

  refreshTableBtn.addEventListener("click", () => {
    refreshTransactions();
    refreshLatest();
  });

  summaryBtn.addEventListener("click", () => {
    refreshSummary();
  });

  if (summaryDownloadBtn) {
    summaryDownloadBtn.addEventListener("click", () => {
      downloadSummaryCsv();
    });
  }

  if (summaryExpandBtn) {
    summaryExpandBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("summary-table-wrapper");
      if (!wrapper) return;
      const expanded = wrapper.classList.toggle("expanded");
      summaryExpandBtn.innerHTML = expanded
        ? "🗕<span>Collapse</span>"
        : "⤢<span>Expand</span>";
    });
  }

  if (summaryExpandedCloseBtn) {
    summaryExpandedCloseBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("summary-table-wrapper");
      if (!wrapper) return;
      wrapper.classList.remove("expanded");
      if (summaryExpandBtn) {
        summaryExpandBtn.innerHTML = "⤢<span>Expand</span>";
      }
    });
  }

  if (txExpandBtn) {
    txExpandBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("tx-table-wrapper");
      if (!wrapper) return;
      const expanded = wrapper.classList.toggle("expanded");
      txExpandBtn.innerHTML = expanded ? "🗕<span>Collapse</span>" : "⤢<span>Expand</span>";
    });
  }

  if (txExpandedCloseBtn) {
    txExpandedCloseBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("tx-table-wrapper");
      if (!wrapper) return;
      wrapper.classList.remove("expanded");
      if (txExpandBtn) {
        txExpandBtn.innerHTML = "⤢<span>Expand</span>";
      }
    });
  }

  if (txnLedgerExpandBtn) {
    txnLedgerExpandBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("txn-ledger-wrapper");
      if (!wrapper) return;
      const expanded = wrapper.classList.toggle("expanded");
      txnLedgerExpandBtn.innerHTML = expanded
        ? "🗕<span>Collapse</span>"
        : "⤢<span>Expand</span>";
    });
  }

  if (txnLedgerExpandedCloseBtn) {
    txnLedgerExpandedCloseBtn.addEventListener("click", () => {
      const wrapper = document.getElementById("txn-ledger-wrapper");
      if (!wrapper) return;
      wrapper.classList.remove("expanded");
      if (txnLedgerExpandBtn) {
        txnLedgerExpandBtn.innerHTML = "⤢<span>Expand</span>";
      }
    });
  }

  if (summaryZoomInBtn) {
    summaryZoomInBtn.addEventListener("click", () => {
      applySummaryZoom(summaryZoomScale + 0.1);
    });
  }
  if (summaryZoomOutBtn) {
    summaryZoomOutBtn.addEventListener("click", () => {
      applySummaryZoom(summaryZoomScale - 0.1);
    });
  }
  if (summaryZoomResetBtn) {
    summaryZoomResetBtn.addEventListener("click", () => {
      applySummaryZoom(1);
    });
  }
  if (summaryExpandedZoomInBtn) {
    summaryExpandedZoomInBtn.addEventListener("click", () => {
      applySummaryZoom(summaryZoomScale + 0.1);
    });
  }
  if (summaryExpandedZoomOutBtn) {
    summaryExpandedZoomOutBtn.addEventListener("click", () => {
      applySummaryZoom(summaryZoomScale - 0.1);
    });
  }
  if (summaryExpandedZoomResetBtn) {
    summaryExpandedZoomResetBtn.addEventListener("click", () => {
      applySummaryZoom(1);
    });
  }

  if (txZoomInBtn) {
    txZoomInBtn.addEventListener("click", () => {
      applyTxZoom(txZoomScale + 0.1);
    });
  }
  if (txZoomOutBtn) {
    txZoomOutBtn.addEventListener("click", () => {
      applyTxZoom(txZoomScale - 0.1);
    });
  }
  if (txZoomResetBtn) {
    txZoomResetBtn.addEventListener("click", () => {
      applyTxZoom(1);
    });
  }
  if (txExpandedZoomInBtn) {
    txExpandedZoomInBtn.addEventListener("click", () => {
      applyTxZoom(txZoomScale + 0.1);
    });
  }
  if (txExpandedZoomOutBtn) {
    txExpandedZoomOutBtn.addEventListener("click", () => {
      applyTxZoom(txZoomScale - 0.1);
    });
  }
  if (txExpandedZoomResetBtn) {
    txExpandedZoomResetBtn.addEventListener("click", () => {
      applyTxZoom(1);
    });
  }

  if (txnLedgerZoomInBtn) {
    txnLedgerZoomInBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(txnUnifiedZoomScale + 0.1);
    });
  }
  if (txnLedgerZoomOutBtn) {
    txnLedgerZoomOutBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(txnUnifiedZoomScale - 0.1);
    });
  }
  if (txnLedgerZoomResetBtn) {
    txnLedgerZoomResetBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(1);
    });
  }
  if (txnLedgerExpandedZoomInBtn) {
    txnLedgerExpandedZoomInBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(txnUnifiedZoomScale + 0.1);
    });
  }
  if (txnLedgerExpandedZoomOutBtn) {
    txnLedgerExpandedZoomOutBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(txnUnifiedZoomScale - 0.1);
    });
  }
  if (txnLedgerExpandedZoomResetBtn) {
    txnLedgerExpandedZoomResetBtn.addEventListener("click", () => {
      applyTxnUnifiedZoom(1);
    });
  }

  const summaryTableWrap = document.querySelector("#summary-table");
  if (summaryTableWrap) {
    let pinchStartDistance = 0;
    let pinchStartScale = 1;
    summaryTableWrap.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches.length === 2) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          pinchStartDistance = Math.hypot(dx, dy);
          pinchStartScale = summaryZoomScale;
        }
      },
      { passive: true }
    );
    summaryTableWrap.addEventListener(
      "touchmove",
      (ev) => {
        if (ev.touches.length === 2 && pinchStartDistance > 0) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          const distance = Math.hypot(dx, dy);
          const ratio = distance / pinchStartDistance;
          applySummaryZoom(pinchStartScale * ratio);
          ev.preventDefault();
        }
      },
      { passive: false }
    );
  }

  const txTableWrap = document.querySelector("#tx-table");
  if (txTableWrap) {
    let pinchStartDistance = 0;
    let pinchStartScale = 1;
    txTableWrap.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches.length === 2) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          pinchStartDistance = Math.hypot(dx, dy);
          pinchStartScale = txZoomScale;
        }
      },
      { passive: true }
    );
    txTableWrap.addEventListener(
      "touchmove",
      (ev) => {
        if (ev.touches.length === 2 && pinchStartDistance > 0) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          const distance = Math.hypot(dx, dy);
          const ratio = distance / pinchStartDistance;
          applyTxZoom(pinchStartScale * ratio);
          ev.preventDefault();
        }
      },
      { passive: false }
    );
  }

  const txnLedgerTableWrap = document.querySelector("#txn-table-wrap");
  if (txnLedgerTableWrap) {
    let pinchStartDistance = 0;
    let pinchStartScale = 1;
    txnLedgerTableWrap.addEventListener(
      "touchstart",
      (ev) => {
        if (ev.touches.length === 2) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          pinchStartDistance = Math.hypot(dx, dy);
          pinchStartScale = txnUnifiedZoomScale;
        }
      },
      { passive: true }
    );
    txnLedgerTableWrap.addEventListener(
      "touchmove",
      (ev) => {
        if (ev.touches.length === 2 && pinchStartDistance > 0) {
          const dx = ev.touches[0].clientX - ev.touches[1].clientX;
          const dy = ev.touches[0].clientY - ev.touches[1].clientY;
          const distance = Math.hypot(dx, dy);
          const ratio = distance / pinchStartDistance;
          applyTxnUnifiedZoom(pinchStartScale * ratio);
          ev.preventDefault();
        }
      },
      { passive: false }
    );
  }

  async function handleAiAsk() {
    if (!summaryAiAnswer) return;
    const q = summaryAiInput ? summaryAiInput.value : "";
    if (!String(q || "").trim()) {
      summaryAiAnswer.textContent = "Please enter a question.";
      return;
    }
    // Forward-step validation: if summary is missing, try to load it first.
    if (!lastSummary || !Array.isArray(lastSummary.items)) {
      try {
        await refreshSummary();
      } catch {
        // continue to fallback messaging below
      }
    }
    summaryAiAnswer.textContent = "Thinking...";
    summaryAiHistory.push({ role: "user", content: String(q) });
    saveSummaryAiHistory();
    renderSummaryAiLog();
    try {
      const ai = await askSummaryWithGpt(q);
      summaryAiAnswer.textContent = ai;
      summaryAiHistory.push({ role: "assistant", content: String(ai) });
      saveSummaryAiHistory();
      renderSummaryAiLog();
    } catch {
      const msg = "AI unavailable right now. Please try again.";
      summaryAiAnswer.textContent = msg;
      summaryAiHistory.push({ role: "assistant", content: msg });
      saveSummaryAiHistory();
      renderSummaryAiLog();
    }
    if (summaryAiInput) {
      summaryAiInput.value = "";
    }
  }
  if (summaryAiAskBtn) {
    summaryAiAskBtn.addEventListener("click", handleAiAsk);
  }
  if (summaryAiInput) {
    summaryAiInput.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        handleAiAsk();
      }
    });
  }

  // Recipient management
  const addRecipientBtn = document.getElementById("add-recipient-btn");
  const recipientForm = document.getElementById("recipient-form");
  const recipientNameInput = document.getElementById("recipient-name-input");
  const recipientEmailInput = document.getElementById("recipient-email-input");
  const saveRecipientBtn = document.getElementById("save-recipient-btn");
  const cancelRecipientBtn = document.getElementById("cancel-recipient-btn");
  const recipientError = document.getElementById("recipient-error");
  const recipientListWrap = document.getElementById("recipient-list-wrap");
  const recipientsBody = document.getElementById("recipients-body");

  function updateRecipientConfidentialUI() {
    const hasPw = !!getStoredPassword();
    if (recipientListWrap) {
      recipientListWrap.style.display = hasPw ? "block" : "none";
    }
    if (!hasPw && recipientsBody) {
      recipientsBody.innerHTML = `
        <tr>
          <td colspan="3" class="muted">Unlock to view saved driver emails.</td>
        </tr>
      `;
    }
  }

  async function refreshRecipients() {
    // Confidentiality: allow adding without password, but hide the list until unlocked.
    if (!getStoredPassword()) {
      updateRecipientConfidentialUI();
      return;
    }
    try {
      const res = await fetch(API_BASE + "/recipients/ui");
      if (!res.ok) {
        throw new Error("HTTP_" + res.status);
      }
      const recipients = await res.json();
      if (!hasAdminPassword()) {
        updateRecipientConfidentialUI();
        return;
      }
      renderRecipients(recipients);
    } catch (e) {
      console.error("Failed to fetch recipients:", e);
      recipientsBody.innerHTML = `
          <tr>
            <td colspan="3" class="muted">Failed to load drivers. Is the API updated?</td>
          </tr>
        `;
    }
  }

  function renderRecipients(recipients) {
    updateRecipientConfidentialUI();
    if (!getStoredPassword()) return;
    recipientsBody.innerHTML = "";
    if (!recipients || recipients.length === 0) {
      recipientsBody.innerHTML = `
        <tr>
          <td colspan="3" class="muted">No drivers yet. Click Add to create one.</td>
        </tr>
      `;
      return;
    }

    for (const r of recipients) {
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      tdName.textContent = r.name;
      tr.appendChild(tdName);

      const tdEmail = document.createElement("td");
      tdEmail.textContent = r.email;
      tr.appendChild(tdEmail);

      const tdActions = document.createElement("td");
      const deleteBtn = document.createElement("button");
      deleteBtn.className = "secondary";
      deleteBtn.style.fontSize = "0.75rem";
      deleteBtn.textContent = "Delete";
      deleteBtn.addEventListener("click", () => deleteRecipient(r.id));
      tdActions.appendChild(deleteBtn);
      tr.appendChild(tdActions);

      recipientsBody.appendChild(tr);
    }
  }

  async function deleteRecipient(id) {
    if (!confirm("Are you sure you want to delete this driver?")) {
      return;
    }
    try {
      const res = await fetch(
        API_BASE + "/recipients/ui/" + encodeURIComponent(id),
        { method: "DELETE" }
      );
      if (!res.ok) {
        throw new Error("HTTP_" + res.status);
      }
      await refreshRecipients();
    } catch (e) {
      console.error("Failed to delete recipient:", e);
      alert("Failed to delete driver. Please try again.");
    }
  }

  async function saveRecipient() {
    const name = recipientNameInput.value.trim();
    const email = recipientEmailInput.value.trim();

    if (!name || !email) {
      recipientError.textContent = "Name and email are required.";
      recipientError.style.display = "block";
      return;
    }

    if (!email.includes("@")) {
      recipientError.textContent = "Please enter a valid email address.";
      recipientError.style.display = "block";
      return;
    }

    recipientError.style.display = "none";

    try {
      const res = await fetch(API_BASE + "/recipients/ui", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email }),
      });
      if (!res.ok) {
        throw new Error("HTTP_" + res.status);
      }
      recipientNameInput.value = "";
      recipientEmailInput.value = "";
      recipientForm.style.display = "none";
      await refreshRecipients();
    } catch (e) {
      console.error("Failed to save recipient:", e);
      recipientError.textContent = "Failed to save driver. Please try again.";
      recipientError.style.display = "block";
    }
  }

  addRecipientBtn.addEventListener("click", () => {
    recipientForm.style.display = "block";
    recipientNameInput.focus();
  });

  cancelRecipientBtn.addEventListener("click", () => {
    recipientForm.style.display = "none";
    recipientNameInput.value = "";
    recipientEmailInput.value = "";
    recipientError.style.display = "none";
  });

  saveRecipientBtn.addEventListener("click", saveRecipient);

  recipientNameInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      recipientEmailInput.focus();
    }
  });

  recipientEmailInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      saveRecipient();
    }
  });

  // Refresh recipients when logged in; Add driver card also loads without password.
  const originalApplyLoggedInUI = applyLoggedInUI;
  applyLoggedInUI = (loggedIn) => {
    originalApplyLoggedInUI(loggedIn);
    if (loggedIn) {
      refreshRecipients();
      maybeRefreshIssuerTab();
      maybeRefreshTxnTab();
      refreshDispatchDriversList();
    } else {
      updateIssuerAuthGate();
      updateTxnAuthGate();
      updateRecipientConfidentialUI();
      refreshDispatchDriversList();
    }
  };

  setupIssuerAdminEvents();
  setupTxnEvents();

  applySummaryZoom(1);
  applyTxZoom(1);
  applyTxnUnifiedZoom(1);
  updateRecipientConfidentialUI();

  // Initial lock layout: without this, first paint shows tables before any login attempt.
  applyLoggedInUI(!!String(getStoredPassword() || "").trim());
}

window.addEventListener("DOMContentLoaded", async () => {
  setupAdminTabs();
  checkHealth();
  setupEvents();
  updateTxnAuthGate();
  renderUnifiedTransactions();
  refreshDispatchDriversList();
  await tryInitialLogin();
  // If the Transactions tab is active on first load and we already have a
  // stored password, kick off the joined fetch immediately so the spreadsheet
  // is populated without the user switching tabs.
  if (transactionsTabActive() && getStoredPassword()) {
    refreshUnifiedTransactions();
  }
});


