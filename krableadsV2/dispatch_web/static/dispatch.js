/* dispatch.js — shared client behavior for every /dispatch page.
 *
 * Vanilla JS, no deps, one file: the pages are server-rendered Jinja and this
 * script only adds three conveniences on top —
 *   1. board auto-refresh: poll /dispatch/data.json every 10s while a
 *      [data-autorefresh] table is on the page and the tab is visible,
 *      swapping only the <tbody> so the header/filters/scroll stay put;
 *   2. the New Lead "Parse" button: POST the pasted text to
 *      /dispatch/api/parse and pour the parsed state into the editable grid;
 *   3. confirm() before strike/restore submits (and any form that opts in
 *      with data-confirm).
 *
 * Every hook is optional — pages that ship none of these elements pay
 * nothing. Click/submit handlers are DELEGATED at the document, not bound
 * per-element, because the tbody swap replaces the strike/restore forms
 * every 10 seconds and per-element bindings would die with the old rows.
 * Never log page/payload values: lead text is client PII (Sentry rule).
 */
(function () {
  "use strict";

  // The blueprint mounts at /dispatch on the Flask app, but the PUBLIC url may
  // carry a proxy prefix (tristatetags.com/backend/dispatch/... — the Vercel
  // /backend proxy strips its prefix before forwarding, so the server never
  // sees it and url_for cannot emit it). Hard-coded "/dispatch/..." URLs would
  // resolve outside the proxy there and 404. Only the browser knows the real
  // prefix: everything up to and including the first "/dispatch" segment of
  // the page's own path.
  var BASE = (function () {
    var path = (window.location && window.location.pathname) || "";
    var m = path.match(/^(.*?\/dispatch)(?:\/|$)/);
    return m ? m[1] : "/dispatch";
  })();

  // Rebase one server-authored absolute "/dispatch/..." URL onto BASE.
  // Everything else (relative, external, already-prefixed) passes through.
  function rebase(u) {
    if (BASE === "/dispatch" || typeof u !== "string") return u;
    if (u === "/dispatch") return BASE;
    if (u.indexOf("/dispatch/") === 0) return BASE + u.slice("/dispatch".length);
    return u;
  }

  var REFRESH_URL = BASE + "/data.json";
  var REFRESH_MS = 10000;
  var PARSE_URL = BASE + "/api/parse";

  /* ---------------------------------------------------------------- utils */

  // Selector helpers swallow bad selectors: some are BUILT from payload keys.
  function qs(sel, root) {
    try { return (root || document).querySelector(sel); } catch (e) { return null; }
  }
  function qsa(sel, root) {
    try { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
    catch (e) { return []; }
  }
  function escAttr(v) { return String(v).replace(/["\\]/g, "\\$&"); }

  function fire(el, type) {
    // `new Event` throws on old engines; degrade rather than break the fill.
    var ev;
    try { ev = new Event(type, { bubbles: true }); }
    catch (e) {
      try { ev = document.createEvent("Event"); ev.initEvent(type, true, false); }
      catch (e2) { return; }
    }
    el.dispatchEvent(ev);
  }

  function dig(obj, path) {
    var cur = obj, parts = String(path).split(".");
    for (var i = 0; i < parts.length; i++) {
      if (cur == null || typeof cur !== "object") return undefined;
      cur = cur[parts[i]];
    }
    return (typeof cur === "string" || typeof cur === "number") ? cur : undefined;
  }

  /* ------------------------------------------------- 1. board auto-refresh */

  function initAutoRefresh() {
    if (!window.fetch) return; // no polyfill shipping for a nicety

    var hosts = qsa("[data-autorefresh]").map(function (host) {
      var tbody = (host.tBodies && host.tBodies[0]) || qs("tbody", host);
      return tbody ? { host: host, tbody: tbody } : null;
    }).filter(Boolean);
    if (!hosts.length) return;

    // The attribute can carry the URL itself (starts with "/") or a payload
    // key name; a separate data-autorefresh-url always wins. Both come from
    // the server (url_for emits "/dispatch/..."), so rebase them onto the
    // real public prefix — behind the /backend proxy the raw value 404s.
    var first = hosts[0].host;
    var attrVal = first.getAttribute("data-autorefresh") || "";
    var url = rebase(first.getAttribute("data-autorefresh-url")) ||
      (attrVal.charAt(0) === "/" ? rebase(attrVal) : REFRESH_URL);

    var period = REFRESH_MS;
    var iv = parseInt(first.getAttribute("data-autorefresh-interval") || "", 10);
    if (iv > 0) period = Math.max(2000, iv < 100 ? iv * 1000 : iv); // small = seconds

    var timer = null;
    var inflight = false;

    function schedule(ms) {
      clearTimeout(timer);
      timer = setTimeout(tick, ms);
    }

    function stamp(ok) {
      var el = qs("[data-refresh-stamp]");
      if (!el) return;
      var t = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      el.textContent = ok ? ("Updated " + t) : "Refresh failed — showing last data";
      el.dataset.state = ok ? "ok" : "err";
    }

    function tick() {
      clearTimeout(timer);
      if (document.hidden) return; // visibilitychange restarts the chain
      if (inflight) { schedule(period); return; }
      inflight = true;
      fetch(url, { cache: "no-store", credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (res) {
          // A redirect means the session died: keep the last good rows and
          // keep polling — logging back in from another tab revives us.
          if (!res.ok || res.redirected) throw new Error("HTTP " + res.status);
          var ct = res.headers.get("content-type") || "";
          return ct.indexOf("json") !== -1 ? res.json() : res.text();
        })
        .then(function (payload) {
          // ok:false is the server's own "database unreachable" envelope
          // (board_data answers it with HTTP 200, empty rows and a blank
          // tbody_html): keep the last good rows AND the last good counters.
          if (payload && typeof payload === "object" && payload.ok === false) {
            stamp(false);
            return;
          }
          hosts.forEach(function (h) { applyPayload(h.host, h.tbody, payload); });
          applyTextTargets(payload);
          stamp(true);
        })
        .catch(function () { stamp(false); }) // transient DB/network wobble: stay stale, retry
        .then(function () {
          inflight = false;
          if (!document.hidden) schedule(period);
        });
    }

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) tick(); // catch up the moment the tab returns
    });

    schedule(period); // page is fresh from the server; first poll waits a beat
  }

  // Don't yank rows out from under the pointer/keyboard: a swap between
  // mousedown and click on a strike button would eat the click.
  function interacting(tbody) {
    var a = document.activeElement;
    return !!(a && a !== document.body && tbody.contains(a));
  }

  // Tolerated payload shapes, in order of preference:
  //   {"tbody_html": "<tr>..."} (or tbody/rows_html/html) — server renders rows;
  //   {"rows": [[...], ...]} or [{...}, ...] — built here via textContent (XSS-safe);
  //   a bare "<tr>..." string. A host with data-autorefresh="<key>" unwraps
  //   payload[<key>] first so one data.json can feed several tables.
  function applyPayload(host, tbody, payload) {
    var p = payload;
    var key = host.getAttribute("data-autorefresh");
    if (p && typeof p === "object" && key && key !== "true" && key.charAt(0) !== "/" && p[key] != null) {
      p = p[key];
    }

    var html = null;
    if (typeof p === "string") html = p;
    else if (p && typeof p === "object" && !Array.isArray(p)) {
      var names = ["tbody_html", "tbody", "rows_html", "html"];
      for (var i = 0; i < names.length; i++) {
        if (typeof p[names[i]] === "string") { html = p[names[i]]; break; }
      }
    }
    if (html !== null) {
      // A login page, an error doc, or the error envelope's empty string is
      // not row markup; keep the last good rows. A legitimately empty table
      // still contains its server-rendered "no rows yet" <tr> (the board's
      // partial always emits one), so requiring <tr never rejects real data.
      if (html.indexOf("<tr") === -1) return;
      if (interacting(tbody)) return;
      tbody.innerHTML = html; // server-authored markup from our own session-gated endpoint
      return;
    }

    var rows = null;
    if (Array.isArray(p)) rows = p;
    else if (p && typeof p === "object") rows = p.rows || p.leads || p.items || p.data;
    if (!Array.isArray(rows)) return;
    if (interacting(tbody)) return;
    buildRows(host, tbody, rows);
  }

  function columnKeys(host, firstRow) {
    var attr = host.getAttribute("data-cols");
    if (attr) {
      return attr.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
    }
    var table = host.tagName === "TABLE" ? host : qs("table", host);
    var keyed = table ? qsa("thead [data-key]", table) : [];
    if (keyed.length) {
      return keyed.map(function (th) { return th.getAttribute("data-key"); });
    }
    return Object.keys(firstRow || {});
  }

  function countCols(host) {
    var table = host.tagName === "TABLE" ? host : qs("table", host);
    return table ? qsa("thead th", table).length : 0;
  }

  function cellText(v) {
    return (v == null || typeof v === "object") ? "" : String(v);
  }

  function buildRows(host, tbody, rows) {
    var frag = document.createDocumentFragment();
    if (!rows.length) {
      var tr0 = document.createElement("tr");
      var td0 = document.createElement("td");
      td0.colSpan = countCols(host) || 1;
      td0.className = "dw-empty";
      td0.textContent = "Nothing to show yet.";
      tr0.appendChild(td0);
      frag.appendChild(tr0);
    } else {
      var objectRows = !Array.isArray(rows[0]);
      var cols = objectRows ? columnKeys(host, rows[0]) : null;
      rows.forEach(function (r) {
        var tr = document.createElement("tr");
        if (objectRows && r && (r.id != null || r.lead_id != null)) {
          tr.setAttribute("data-id", String(r.id != null ? r.id : r.lead_id));
        }
        var values = objectRows
          ? cols.map(function (c) { return dig(r, c) != null ? dig(r, c) : (r ? r[c] : null); })
          : r;
        (values || []).forEach(function (v) {
          var td = document.createElement("td");
          td.textContent = cellText(v);
          tr.appendChild(td);
        });
        frag.appendChild(tr);
      });
    }
    tbody.textContent = ""; // drop old rows without an innerHTML parse
    tbody.appendChild(frag);
  }

  // Any element may subscribe to a scalar in the payload by dotted path
  // (e.g. <span data-refresh-text="total">) — used for counters next to the
  // table so they don't go stale while the tbody updates.
  function applyTextTargets(payload) {
    if (!payload || typeof payload !== "object") return;
    qsa("[data-refresh-text]").forEach(function (el) {
      var v = dig(payload, el.getAttribute("data-refresh-text"));
      if (v !== undefined) el.textContent = String(v);
    });
  }

  /* --------------------------------------------- 2. New Lead parse button */

  var PARSE_BTN_SEL = "[data-parse], [data-parse-btn], [data-action=\"parse\"], " +
    "#parse-btn, #dw-parse-btn, #btn-parse, .js-parse, " +
    "button[name=\"parse\"], button[value=\"parse\"]";

  // The parser's own state keys next to the names a form is likely to use;
  // first existing field wins. Everything not listed fills by its own name.
  var FILL_ALIASES = {
    pending_phone_number: ["pending_phone_number", "phone_number", "phone"],
    pending_price: ["pending_price", "price"],
    phone: ["phone", "phone_number", "pending_phone_number"],
    price: ["price", "pending_price"],
    name: ["name", "customer", "customer_name"],
    external_order_id: ["external_order_id", "order_id"],
    insurance_company: ["insurance_company", "insurance"],
    insurance_policy_number: ["insurance_policy_number", "policy_number", "policy"]
  };

  function onParseClick(e) {
    var t = e.target;
    var btn = t && t.closest ? t.closest(PARSE_BTN_SEL) : null;
    if (!btn) return;
    e.preventDefault(); // Parse must never submit the surrounding create form
    if (btn.disabled) return;
    runParse(btn);
  }

  function findSource(btn) {
    var sel = btn.getAttribute("data-parse-source");
    if (sel) {
      var el = qs(sel);
      if (el) return el;
    }
    var scopes = [btn.closest ? btn.closest("form") : null, document].filter(Boolean);
    var candidates = [
      "textarea[data-parse-source]", "#raw_text", "#lead_text", "#paste_text",
      "textarea[name=\"raw_text\"]", "textarea[name=\"lead_text\"]",
      "textarea[name=\"raw\"]", "textarea[name=\"text\"]", "textarea[name=\"paste\"]",
      "textarea"
    ];
    for (var s = 0; s < scopes.length; s++) {
      for (var c = 0; c < candidates.length; c++) {
        var found = qs(candidates[c], scopes[s]);
        if (found) return found;
      }
    }
    return null;
  }

  function feedbackEls() {
    var status = qs("[data-parse-status]") || qs("#parse-status");
    var errors = qs("[data-parse-errors]") || qs("[data-parse-error]") || qs("#parse-errors");
    return { status: status, errors: errors || status };
  }

  function say(el, text, isErr) {
    if (!el) { if (isErr && text) window.alert(text); return; }
    el.textContent = text || "";
    el.hidden = !text;
    el.dataset.state = isErr ? "err" : "ok";
  }

  // POST as JSON first; if the endpoint turns out to read request.form
  // instead (its author is another agent), retry once urlencoded. The same
  // value rides under three keys so either side's name choice lands.
  function postParse(url, text) {
    var jsonBody = JSON.stringify({ text: text, raw_text: text, raw: text });
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: jsonBody
    }).then(function (res) {
      return res.text().then(function (body) {
        try { return JSON.parse(body); } catch (e) { /* not JSON: fall through */ }
        var enc = encodeURIComponent(text);
        return fetch(url, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json" },
          body: "text=" + enc + "&raw_text=" + enc + "&raw=" + enc
        }).then(function (res2) { return res2.json(); });
      });
    });
  }

  function pickState(data) {
    if (!data || typeof data !== "object" || Array.isArray(data)) return null;
    var wrappers = ["state", "fields", "lead", "parsed", "result", "data"];
    for (var i = 0; i < wrappers.length; i++) {
      var v = data[wrappers[i]];
      if (v && typeof v === "object" && !Array.isArray(v)) return v;
    }
    return data; // flat payload: the parser's keys at top level
  }

  function pickErrors(data) {
    if (!data || typeof data !== "object") return [];
    var e = data.errors || data.error || data.problems;
    if (typeof e === "string" && e) return [e];
    if (Array.isArray(e)) return e.map(String);
    return [];
  }

  function findField(scope, nm) {
    var esc = escAttr(nm);
    var el = qs("[data-field=\"" + esc + "\"]", scope) || qs("[name=\"" + esc + "\"]", scope);
    if (el) return el;
    var byId = document.getElementById(nm);
    return (byId && scope.contains(byId)) ? byId : null;
  }

  function setField(el, value, source) {
    if (!el || el === source) return false; // never overwrite the paste box itself
    var tag = el.tagName;
    if (tag === "INPUT") {
      var type = (el.getAttribute("type") || "text").toLowerCase();
      if (type === "file" || type === "checkbox" || type === "radio" ||
          type === "button" || type === "submit") return false;
      el.value = value;
    } else if (tag === "TEXTAREA") {
      el.value = value;
    } else if (tag === "SELECT") {
      var matched = false;
      for (var i = 0; i < el.options.length; i++) {
        var o = el.options[i];
        if (o.value === value || o.text === value) { el.selectedIndex = i; matched = true; break; }
      }
      if (!matched) return false;
    } else if (el.isContentEditable) {
      el.textContent = value;
    } else {
      return false;
    }
    fire(el, "input");
    fire(el, "change");
    return true;
  }

  function fillGrid(scope, state, source) {
    var filled = 0;
    Object.keys(state).forEach(function (key) {
      var val = state[key];
      if (val == null || typeof val === "object") return;
      var names = FILL_ALIASES[key] || [key];
      for (var i = 0; i < names.length; i++) {
        var el = findField(scope, names[i]);
        if (el) {
          if (setField(el, String(val), source)) filled++;
          break;
        }
      }
    });
    return filled;
  }

  function runParse(btn) {
    var fb = feedbackEls();
    var source = findSource(btn);
    if (!source) { say(fb.errors, "No text box found to parse from.", true); return; }
    var text = (source.value || "").trim();
    if (!text) { say(fb.errors, "Paste the lead text first.", true); return; }

    var label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Parsing…";
    say(fb.status, "");
    if (fb.errors !== fb.status) say(fb.errors, ""); // one run, one message

    postParse(rebase(btn.getAttribute("data-parse-url")) || PARSE_URL, text)
      .then(function (data) {
        var errors = pickErrors(data);
        if (errors.length) {
          say(fb.errors, errors.join(" • "), true);
          return;
        }
        var state = pickState(data);
        if (!state) {
          say(fb.errors, "The parser returned nothing usable.", true);
          return;
        }
        // Fill inside the grid container/form first; if nothing matched
        // there (paste box lives in its own little form), go page-wide.
        var scope = qs("[data-parse-target]") ||
          (btn.closest ? btn.closest("form") : null) || document;
        var filled = fillGrid(scope, state, source);
        if (!filled && scope !== document) filled = fillGrid(document, state, source);
        if (filled) {
          say(fb.status, "Parsed — " + filled + " field" + (filled === 1 ? "" : "s") +
            " filled. Review, then submit.", false);
        } else {
          say(fb.errors, "Parsed, but no grid fields matched the result.", true);
        }
      })
      .catch(function (err) {
        // err.name only: the message could echo lead text (PII) back at us.
        say(fb.errors, "Parse failed — the server did not answer with JSON.", true);
        if (window.console && console.warn) console.warn("dispatch.js parse:", err && err.name);
      })
      .then(function () {
        btn.disabled = false;
        btn.textContent = label;
      });
  }

  /* -------------------------------------------- 3. strike/restore confirm */

  // Delegated so the forms swapped in by auto-refresh are covered too.
  // Opt-in via data-confirm (on the form or the submitting button) with an
  // optional custom message; strike/restore actions get a default even
  // without the attribute so the board can't lose a lead to a stray click.
  function onSubmit(e) {
    var form = e.target;
    if (!form || form.nodeName !== "FORM") return;
    // An inline onsubmit that returned false has already cancelled this
    // submit (return false prevents default but does NOT stop propagation) —
    // asking again about a dead submit is pure noise.
    if (e.defaultPrevented) return;
    var sub = e.submitter || null;

    var msg = null;
    if (sub && sub.hasAttribute && sub.hasAttribute("data-confirm")) {
      msg = sub.getAttribute("data-confirm") || "Are you sure?";
    } else if (form.hasAttribute("data-confirm")) {
      msg = form.getAttribute("data-confirm") || "Are you sure?";
    } else if (!form.hasAttribute("onsubmit")) {
      // Pattern-matched default only for forms with no confirm of their own:
      // lead.html's Strike form ships an inline onsubmit confirm — stacking
      // a second dialog on top of it double-prompts the operator.
      var action = (sub && sub.getAttribute && sub.getAttribute("formaction")) ||
        form.getAttribute("action") || "";
      if (/\b(strike|exclude)\b/i.test(action)) {
        msg = "Strike this lead from the board?";
      } else if (/\b(restore|unexclude|unstrike)\b/i.test(action)) {
        msg = "Restore this lead to the board?";
      }
    }
    if (!msg) return;
    if (!window.confirm(msg)) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  /* ------------------------------------------------------------- wire up */

  document.addEventListener("click", onParseClick);
  document.addEventListener("submit", onSubmit);

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAutoRefresh);
  } else {
    initAutoRefresh();
  }
})();
