(function () {
  const API = (window.KRAB_API_BASE_URL || "").replace(/\/$/, "");

  const FIELDS = [
    { key: "full_name", label: "Full Name", required: true },
    { key: "work_commitment", label: "Work Commitment (hours / availability)", required: true },
    { key: "phone_number", label: "Phone Number", required: true, type: "tel" },
    { key: "email", label: "Email", required: true, type: "email" },
    { key: "mailing_address", label: "Mailing Address", required: true, textarea: true },
    { key: "drivers_license_id", label: "Driver License Number", required: true },
    {
      key: "telegram_username",
      label: "Telegram Username",
      required: true,
      highlight: true,
      hint: 'Required — we contact you on Telegram. <a href="/how-to-telegram" target="_blank">How to get Telegram & set @username</a>',
    },
    { key: "emergency_contact", label: "Emergency Contact (name + phone)", required: true },
    { key: "referral", label: "Referral (who referred you, or —)", required: false },
    { key: "payment_method", label: "Payment Method (CashApp / Venmo / Zelle)", required: true },
    { key: "profession_skill", label: "Profession / Skills", required: true },
    { key: "telegram_id", label: "Telegram ID (numeric, optional)", required: false },
  ];

  let draftId = null;
  let saveTimer = null;
  let frozen = false;

  const formEl = document.getElementById("application-form");
  const statusEl = document.getElementById("save-status");
  const frozenEl = document.getElementById("frozen-screen");
  const submitBtn = document.getElementById("submit-btn");
  const licenseInput = document.getElementById("license-file");

  function api(path, opts) {
    const url = API + path;
    return fetch(url, {
      credentials: "include",
      ...opts,
      headers: {
        ...(opts && opts.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
        ...(opts && opts.headers ? opts.headers : {}),
      },
    }).then(async (r) => {
      const data = await r.json().catch(() => ({}));
      if (!r.ok) {
        const err = new Error(data.detail || data.message || r.statusText);
        err.status = r.status;
        err.data = data;
        throw err;
      }
      return data;
    });
  }

  function setStatus(msg) {
    if (statusEl) statusEl.textContent = msg || "";
  }

  function buildForm() {
    if (!formEl) return;
    formEl.innerHTML = "";
    FIELDS.forEach((f) => {
      const wrap = document.createElement("div");
      if (f.highlight) wrap.className = "field-highlight";
      const lab = document.createElement("label");
      lab.htmlFor = f.key;
      lab.innerHTML = f.label + (f.required ? ' <span class="req">*</span>' : "");
      wrap.appendChild(lab);
      let input;
      if (f.textarea) {
        input = document.createElement("textarea");
        input.rows = 3;
      } else {
        input = document.createElement("input");
        input.type = f.type || "text";
      }
      input.id = input.name = f.key;
      input.dataset.key = f.key;
      input.addEventListener("input", queueSave);
      wrap.appendChild(input);
      if (f.hint) {
        const hint = document.createElement("p");
        hint.className = "field-hint";
        hint.innerHTML = f.hint;
        wrap.appendChild(hint);
      }
      formEl.appendChild(wrap);
    });
  }

  function collectPayload() {
    const payload = {};
    FIELDS.forEach((f) => {
      const el = document.getElementById(f.key);
      if (el) payload[f.key] = (el.value || "").trim();
    });
    return payload;
  }

  function fillForm(payload) {
    FIELDS.forEach((f) => {
      const el = document.getElementById(f.key);
      if (el && payload && payload[f.key] != null) el.value = payload[f.key];
    });
  }

  function showFrozen() {
    frozen = true;
    if (formEl) formEl.style.display = "none";
    if (submitBtn) submitBtn.style.display = "none";
    if (licenseInput && licenseInput.parentElement) licenseInput.parentElement.style.display = "none";
    if (frozenEl) frozenEl.hidden = false;
    setStatus("");
  }

  function queueSave() {
    if (frozen || !draftId) return;
    setStatus("Saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveDraft, 800);
  }

  async function saveDraft() {
    if (!draftId) return;
    try {
      const res = await api(`/api/interview/draft/${draftId}`, {
        method: "PATCH",
        body: JSON.stringify({ payload: collectPayload() }),
      });
      setStatus("Saved " + new Date().toLocaleTimeString());
      if (res.payload) fillForm(res.payload);
    } catch (e) {
      setStatus("Save failed");
      console.error(e);
    }
  }

  async function uploadLicense(file) {
    if (!draftId || !file) return;
    setStatus("Uploading license…");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api(`/api/interview/draft/${draftId}/license`, {
        method: "POST",
        body: fd,
      });
      setStatus("License uploaded");
      if (res.driversLicenseFileUrl) {
        const link = document.getElementById("license-link");
        if (link) {
          link.href = res.driversLicenseFileUrl;
          link.textContent = "View uploaded license";
          link.hidden = false;
        }
      }
    } catch (e) {
      setStatus("License upload failed");
      alert(e.message || "Upload failed");
    }
  }

  async function initDraft() {
    const data = await api("/api/interview/draft", { method: "POST" });
    draftId = data.draftId;
    if (data.alreadySubmitted) {
      showFrozen();
      return;
    }
    fillForm(data.payload || {});
    if (data.driversLicenseFileUrl) {
      const link = document.getElementById("license-link");
      if (link) {
        link.href = data.driversLicenseFileUrl;
        link.textContent = "View uploaded license";
        link.hidden = false;
      }
    }
    setStatus("Draft ready");
  }

  async function submitForm(ev) {
    ev.preventDefault();
    if (frozen || !draftId) return;
    submitBtn.disabled = true;
    setStatus("Submitting…");
    try {
      await saveDraft();
      const res = await api(`/api/interview/submit/${draftId}`, { method: "POST" });
      showFrozen();
    } catch (e) {
      submitBtn.disabled = false;
      const msg =
        typeof e.data?.detail === "string"
          ? e.data.detail
          : e.data?.detail?.message || e.message || "Submit failed";
      setStatus("");
      alert(msg);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    buildForm();
    if (licenseInput) {
      licenseInput.addEventListener("change", () => {
        const f = licenseInput.files && licenseInput.files[0];
        if (f) uploadLicense(f);
      });
    }
    const pageForm = document.getElementById("page-form");
    if (pageForm) pageForm.addEventListener("submit", submitForm);
    initDraft().catch((e) => {
      setStatus("Could not connect to server");
      console.error(e);
    });
  });
})();
