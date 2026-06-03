(function () {
  const API = (window.KRAB_API_BASE_URL || "").replace(/\/$/, "");

  const loginSection = document.getElementById("login-section");
  const dashSection = document.getElementById("dashboard-section");
  const loginForm = document.getElementById("login-form");
  const logoutBtn = document.getElementById("logout-btn");
  const searchInput = document.getElementById("search");
  const tbody = document.getElementById("interviews-tbody");
  const pollHint = document.getElementById("poll-hint");

  let pollTimer = null;

  function api(path, opts) {
    return fetch(API + path, {
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      ...opts,
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || r.statusText);
      return data;
    });
  }

  function badgeClass(color) {
    if (color === "red") return "badge badge-red";
    if (color === "yellow") return "badge badge-yellow";
    if (color === "green") return "badge badge-green";
    return "badge badge-gray";
  }

  function fmtDate(iso) {
    if (!iso) return "-";
    try {
      return new Date(iso).toLocaleString();
    } catch (_) {
      return iso;
    }
  }

  function renderRows(items) {
    if (!tbody) return;
    tbody.innerHTML = "";
    (items || []).forEach((row) => {
      const tr = document.createElement("tr");
      const badge = row.badge || {};
      const lic = row.licenseUrl
        ? `<a href="${row.licenseUrl}" target="_blank" rel="noopener">License</a>`
        : "-";
      tr.innerHTML = `
        <td>${fmtDate(row.createdAt)}</td>
        <td>${escapeHtml(row.name)}</td>
        <td>${escapeHtml(row.telegramUsername)}</td>
        <td class="hide-mobile">${escapeHtml(row.phone)}</td>
        <td class="hide-mobile">${escapeHtml(row.email)}</td>
        <td>${lic}</td>
        <td><span class="${badgeClass(badge.color)}">${escapeHtml(badge.label || row.status)}</span></td>
        <td class="hide-mobile"><code>${escapeHtml(row.ipHashShort || "")}</code></td>
        <td>${fmtDate(row.lastSeenAt)}</td>
      `;
      tr.style.cursor = "pointer";
      tr.addEventListener("click", () => showDetail(row.draftId));
      tbody.appendChild(tr);
    });
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  async function showDetail(draftId) {
    try {
      const data = await api(`/api/admin/interviews/${draftId}`);
      const p = data.payload || {};
      const lines = Object.entries(p)
        .map(([k, v]) => `${k}: ${v}`)
        .join("\n");
      const lic = data.driversLicenseFileUrl || "(none)";
      alert(`Draft ${draftId}\n\n${lines}\n\nLicense: ${lic}`);
    } catch (e) {
      alert(e.message);
    }
  }

  async function loadTable() {
    const q = (searchInput && searchInput.value) || "";
    const data = await api(`/api/admin/interviews?q=${encodeURIComponent(q)}&limit=200`);
    renderRows(data.items);
    if (pollHint) pollHint.textContent = "Updated " + new Date().toLocaleTimeString();
  }

  function showDash() {
    if (loginSection) loginSection.hidden = true;
    if (dashSection) dashSection.hidden = false;
    loadTable();
    clearInterval(pollTimer);
    pollTimer = setInterval(loadTable, 5000);
  }

  function showLogin() {
    if (loginSection) loginSection.hidden = false;
    if (dashSection) dashSection.hidden = true;
    clearInterval(pollTimer);
  }

  async function checkSession() {
    try {
      const s = await api("/api/admin/session");
      if (s.authenticated) showDash();
      else showLogin();
    } catch (_) {
      showLogin();
    }
  }

  if (loginForm) {
    loginForm.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      const pw = document.getElementById("admin-password").value;
      try {
        await api("/api/admin/login", {
          method: "POST",
          body: JSON.stringify({ password: pw }),
        });
        showDash();
      } catch (e) {
        alert("Login failed");
      }
    });
  }

  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      await api("/api/admin/logout", { method: "POST" }).catch(() => {});
      showLogin();
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", () => {
      clearTimeout(searchInput._t);
      searchInput._t = setTimeout(loadTable, 400);
    });
  }

  document.addEventListener("DOMContentLoaded", checkSession);
})();
