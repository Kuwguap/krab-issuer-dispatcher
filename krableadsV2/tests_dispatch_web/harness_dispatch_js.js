/* harness_dispatch_js.js — executes the REAL static/dispatch.js under a stub
 * DOM, once per scenario, and prints one JSON report on stdout for
 * test_frontend_dispatch.py to assert on. No dependencies: node + vm only.
 *
 *   node harness_dispatch_js.js <path-to-dispatch.js>
 *
 * Each scenario gets a fresh vm context (fresh window/document/timers/fetch
 * log), because dispatch.js is an IIFE that reads the DOM at load. The stubs
 * implement exactly the surface dispatch.js touches — anything it touches
 * beyond that throws, which is a finding, not a harness gap to paper over.
 * On any error the report is {error, stack} and the exit code is 1.
 */
"use strict";

const fs = require("fs");
const vm = require("vm");

const srcPath = process.argv[2];
if (!srcPath) {
  process.stderr.write("usage: node harness_dispatch_js.js <dispatch.js>\n");
  process.exit(2);
}
const SRC = fs.readFileSync(srcPath, "utf8");

/* ------------------------------------------------------------------ stubs */

function makeEl(tag, attrs, opts) {
  opts = opts || {};
  const node = {
    tagName: String(tag).toUpperCase(),
    nodeName: String(tag).toUpperCase(),
    _attrs: Object.assign({}, attrs || {}),
    _innerHTML: opts.innerHTML || "",
    _innerHTMLWrites: [],
    textContent: opts.textContent || "",
    value: opts.value || "",
    dataset: {},
    disabled: false,
    hidden: false,
    getAttribute(n) {
      return Object.prototype.hasOwnProperty.call(this._attrs, n) ? this._attrs[n] : null;
    },
    hasAttribute(n) {
      return Object.prototype.hasOwnProperty.call(this._attrs, n);
    },
    contains() { return false; },
    closest() { return null; },
    querySelector() { return null; },
    addEventListener() {},
    dispatchEvent() {},
  };
  Object.defineProperty(node, "innerHTML", {
    get() { return this._innerHTML; },
    set(v) { this._innerHTML = v; this._innerHTMLWrites.push(v); },
  });
  return node;
}

// selectors: {selectorString: element | [elements]} — the only lookups the
// stub answers; everything else is "not on this page" (null / []).
function makeDocument(selectors) {
  return {
    readyState: "complete", // makes the IIFE run initAutoRefresh synchronously
    hidden: false,
    activeElement: null,
    body: {},
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    querySelector(sel) {
      const v = selectors[sel];
      if (Array.isArray(v)) return v[0] || null;
      return v || null;
    },
    querySelectorAll(sel) {
      const v = selectors[sel];
      if (!v) return [];
      return Array.isArray(v) ? v.slice() : [v];
    },
    getElementById() { return null; },
    createEvent() { throw new Error("stub DOM: no createEvent"); },
    createElement(t) { return makeEl(t); },
    createDocumentFragment() { return { appendChild() {} }; },
  };
}

function makeRes(spec) {
  return {
    ok: spec.ok !== false,
    status: spec.status || 200,
    redirected: !!spec.redirected,
    headers: { get: () => (spec.contentType || "application/json") },
    json: () => Promise.resolve(spec.json),
    text: () => Promise.resolve(spec.text != null ? spec.text : JSON.stringify(spec.json)),
  };
}

function loadDispatch(pathname, selectors) {
  const timers = [];
  const fetches = [];
  const fetchQueue = [];
  const confirms = [];
  const confirmAnswers = []; // shift per confirm(); default answer true
  const alerts = [];
  const doc = makeDocument(selectors);

  const winFetch = function (url, opts) {
    fetches.push({ url: String(url), opts: opts || {} });
    const next = fetchQueue.shift() || { json: {} };
    return Promise.resolve(makeRes(next));
  };
  const winConfirm = function (msg) {
    confirms.push(String(msg));
    return confirmAnswers.length ? confirmAnswers.shift() : true;
  };
  const winAlert = function (msg) { alerts.push(String(msg)); };
  const noConsole = { log() {}, warn() {}, error() {} };

  const sandbox = {
    document: doc,
    console: noConsole,
    fetch: winFetch,
    confirm: winConfirm,
    alert: winAlert,
    encodeURIComponent,
    setTimeout(fn, ms) {
      timers.push({ fn, ms, cleared: false, ran: false });
      return timers.length - 1;
    },
    clearTimeout(id) {
      if (typeof id === "number" && timers[id]) timers[id].cleared = true;
    },
  };
  sandbox.window = {
    location: { pathname },
    fetch: winFetch,
    confirm: winConfirm,
    alert: winAlert,
    console: noConsole,
  };
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: "dispatch.js" });

  return {
    doc, timers, fetches, confirms, confirmAnswers, alerts,
    queue(spec) { fetchQueue.push(spec); },
    // Run the next pending poll timer (dispatch.js keeps exactly one alive).
    fire() {
      const pending = timers.filter((t) => !t.cleared && !t.ran);
      if (!pending.length) throw new Error("no timer pending — the poll chain died");
      const t = pending[pending.length - 1];
      t.ran = true;
      t.fn();
    },
    pollAlive() { return timers.some((t) => !t.cleared && !t.ran); },
    listeners(type) { return doc._listeners[type] || []; },
  };
}

// Let the fetch → json/text → apply promise chain settle (microtasks only).
async function drain() { for (let i = 0; i < 60; i++) await null; }

/* -------------------------------------------------------------- scenarios */

// The board page's exact markup contract behind the tristatetags.com /backend
// proxy: bare data-autorefresh + interval, tbody swap target, total counter,
// stamp — then the full failure-shape journey against seeded rows.
async function boardProxy() {
  const tbody = makeEl("tbody", { id: "board-tbody" });
  tbody._innerHTML = "<tr><td>seed</td></tr>"; // pre-load, not a tracked write
  const table = makeEl("table", { "data-autorefresh": "", "data-autorefresh-interval": "10000" });
  table.tBodies = [tbody];
  const stamp = makeEl("span", { "data-refresh-stamp": "" });
  const total = makeEl("span", { "data-refresh-text": "total" });
  total.textContent = "1";

  const h = loadDispatch("/backend/dispatch/", {
    "[data-autorefresh]": [table],
    "[data-refresh-stamp]": stamp,
    "[data-refresh-text]": [total],
  });

  const pending = h.timers.filter((t) => !t.cleared && !t.ran);
  const out = {
    armed: pending.length > 0,
    period: pending.length ? pending[pending.length - 1].ms : null,
    cycles: [],
  };

  h.queue({ json: { ok: true, total: 3, tbody_html: "<tr data-row><td>fresh</td></tr>" } });
  h.queue({ json: { ok: false, rows: [], total: 0, tbody_html: "", error: "database unreachable" } });
  h.queue({ json: { ok: true, tbody_html: "" } }); // rowless-and-blank fragment
  h.queue({ contentType: "text/html", text: "<html><body>Login</body></html>" });
  h.queue({ status: 503, ok: false, json: { ok: false, tbody_html: "" } });

  for (let i = 0; i < 5; i++) {
    h.fire();
    await drain();
    out.cycles.push({
      url: h.fetches[i] ? h.fetches[i].url : null,
      tbody: tbody.innerHTML,
      total: total.textContent,
      stamp: stamp.dataset.state || null,
    });
  }
  out.poll_alive = h.pollAlive();
  return out;
}

// Same markup at the bare mount: the derived URL must stay /dispatch/data.json.
async function boardRoot() {
  const tbody = makeEl("tbody", {});
  const table = makeEl("table", { "data-autorefresh": "" });
  table.tBodies = [tbody];
  const h = loadDispatch("/dispatch/", { "[data-autorefresh]": [table] });
  h.queue({ json: { ok: true, tbody_html: "<tr><td>r</td></tr>" } });
  h.fire();
  await drain();
  return { url: h.fetches[0] ? h.fetches[0].url : null };
}

// Attribute-carried URLs are exactly what url_for would emit ("/dispatch/…")
// and MUST be rebased onto the real public prefix before fetching.
async function attrUrl(attrs) {
  const tbody = makeEl("tbody", {});
  const table = makeEl("table", attrs);
  table.tBodies = [tbody];
  const h = loadDispatch("/backend/dispatch/", { "[data-autorefresh]": [table] });
  h.queue({ json: { ok: true, tbody_html: "<tr><td>r</td></tr>" } });
  h.fire();
  await drain();
  return { url: h.fetches[0] ? h.fetches[0].url : null };
}

// The New Lead Parse button behind the proxy: its data-parse-url attribute is
// the server-authored absolute path and must be rebased too.
async function parseRebase() {
  const textarea = makeEl("textarea", {}, { value: "RAW PASTED LEAD" });
  const form = makeEl("form", {});
  form.querySelector = (sel) => (String(sel).indexOf("textarea") === 0 ? textarea : null);
  const btn = makeEl("button", { "data-parse": "", "data-parse-url": "/dispatch/api/parse" },
    { textContent: "Parse" });
  btn.closest = (sel) => {
    if (String(sel) === "form") return form;
    return String(sel).indexOf("data-parse") !== -1 ? btn : null;
  };

  const h = loadDispatch("/backend/dispatch/new", {});
  const clicks = h.listeners("click");
  if (!clicks.length) throw new Error("dispatch.js registered no click listener");
  clicks[0]({ target: btn, preventDefault() { this.prevented = true; } });
  await drain();
  return {
    url: h.fetches[0] ? h.fetches[0].url : null,
    btn_disabled_after: btn.disabled,
    btn_label_after: btn.textContent,
  };
}

// The strike/restore confirm matrix from lead.html's real markup: an inline
// onsubmit confirm must not be doubled, a cancelled inline confirm must not
// resurrect the dialog, pattern defaults still cover bare forms, and an
// explicit data-confirm always wins.
async function confirmMatrix() {
  const h = loadDispatch("/dispatch/lead/x", {});
  const submits = h.listeners("submit");
  if (!submits.length) throw new Error("dispatch.js registered no submit listener");
  const onSubmit = submits[0];

  function fireSubmit(form, opts) {
    opts = opts || {};
    const ev = {
      target: form,
      submitter: null,
      defaultPrevented: !!opts.defaultPrevented,
      _prevented: false,
      _stopped: false,
      preventDefault() { this._prevented = true; },
      stopPropagation() { this._stopped = true; },
    };
    const before = h.confirms.length;
    onSubmit(ev);
    return { confirms: h.confirms.length - before, ev };
  }

  const out = {};

  // lead.html's Strike form: inline onsubmit confirm — dispatch.js must add 0.
  const strikeInline = makeEl("form", {
    action: "/dispatch/lead/x/strike",
    onsubmit: "return confirm('Strike this lead from every count (leaderboard, receipts owed, usage)?');",
  });
  out.strike_with_inline = fireSubmit(strikeInline).confirms;

  // The inline confirm was CANCELLED (return false prevents default but not
  // propagation): the dead submit must not pop a second dialog here.
  out.default_prevented = fireSubmit(strikeInline, { defaultPrevented: true }).confirms;

  // lead.html's Restore form ships no confirm of its own: pattern default = 1.
  const restorePlain = makeEl("form", { action: "/dispatch/lead/x/restore" });
  const r = fireSubmit(restorePlain);
  out.restore_plain = { confirms: r.confirms, msg: h.confirms[h.confirms.length - 1] || null };

  // Cancelling the pattern default must kill the submit.
  h.confirmAnswers.push(false);
  const c = fireSubmit(restorePlain);
  out.cancel_prevents = { confirms: c.confirms, prevented: c.ev._prevented, stopped: c.ev._stopped };

  // An explicit data-confirm beats the onsubmit-suppression: still exactly 1.
  const explicit = makeEl("form", {
    action: "/dispatch/lead/x/strike",
    onsubmit: "return confirm('inline');",
    "data-confirm": "Really strike?",
  });
  const e2 = fireSubmit(explicit);
  out.explicit_data_confirm = { confirms: e2.confirms, msg: h.confirms[h.confirms.length - 1] || null };

  return out;
}

/* ------------------------------------------------------------------- main */

(async function main() {
  try {
    const report = {
      board_proxy: await boardProxy(),
      board_root: await boardRoot(),
      attr_url: await attrUrl({ "data-autorefresh": "", "data-autorefresh-url": "/dispatch/data.json" }),
      attr_path: await attrUrl({ "data-autorefresh": "/dispatch/data.json" }),
      parse: await parseRebase(),
      confirm_matrix: await confirmMatrix(),
    };
    process.stdout.write(JSON.stringify(report));
  } catch (err) {
    process.stdout.write(JSON.stringify({ error: String(err && err.message), stack: String(err && err.stack) }));
    process.exit(1);
  }
})();
