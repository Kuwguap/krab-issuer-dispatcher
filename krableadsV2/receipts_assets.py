"""Optional view modules for the /receipts board — served at
/receipts/asset/<name> by receipts_page.py.

Sheet (spreadsheet) view + CSV builder, Charts view, CRM (kanban) view, the
theme set, and the voice-command mic. Each is a self-contained classic script
(no imports, no frameworks) defining only its named globals; the board guards
every integration point with a ``typeof`` check, so deleting this file (or any
single entry) degrades that view to a friendly notice and nothing else.

The JS/CSS here IS the source — edit it directly.
"""

ASSETS = {
    'themes.css': r'''
/* themes.css — the receipts board's theme packs.
 *
 * Each [data-theme="…"] block on the root element overrides EVERY CSS variable
 * the page defines in :root — surfaces (--bg --card --ink --muted --line --soft
 * --accent), all status pill colors (--new --followup --issued --emailed
 * --printed --otw --del --uploaded --paid) and the toast/result colors
 * (--ok-bg --ok-ink --bad-bg --bad-ink) — so every component restyles itself
 * with no markup changes. `:root[data-theme=…]` (0,2,0) outranks both the bare
 * `:root` block and the `@media (prefers-color-scheme: dark) :root` block
 * (0,1,0 each), so an explicit theme always wins over the system preference;
 * removing the attribute ("auto") falls back to the page's own light/dark.
 *
 * Contrast: every --ink/--bg and --ink/--card pair holds WCAG AA (>= 4.5:1)
 * for body text; --muted stays >= 4.5:1 on its own surfaces.
 */

/* ── Soft landing when the theme flips ─────────────────────────────────── */
[data-theme] body,
[data-theme] header,
[data-theme] main,
[data-theme] footer,
[data-theme] .wrap,
[data-theme] .card,
[data-theme] .sheet,
[data-theme] .detail,
[data-theme] .tab,
[data-theme] .act,
[data-theme] .btn,
[data-theme] .pill,
[data-theme] .who,
[data-theme] th,
[data-theme] td,
[data-theme] input,
[data-theme] select,
[data-theme] textarea,
[data-theme] #cfg {
  transition: background-color .25s ease, color .25s ease, border-color .25s ease;
}

/* ── light — the default clean board ───────────────────────────────────── */
:root[data-theme="light"] {
  --bg:#f4f5f7; --card:#ffffff; --ink:#172b4d; --muted:#62708a;
  --line:#dfe1e6; --soft:#f8f9fb; --accent:#0065ff;
  --new:#8993a4; --followup:#b86e00; --issued:#00879e; --emailed:#8250df;
  --printed:#44546f; --otw:#0065ff; --del:#00875a; --uploaded:#ae2a6d;
  --paid:#6554c0;
  --ok-bg:#e3fcef; --ok-ink:#006644; --bad-bg:#ffebe6; --bad-ink:#bf2600;
}

/* ── dark — the current dark board, made explicit ──────────────────────── */
:root[data-theme="dark"] {
  --bg:#1d2125; --card:#22272b; --ink:#e6edf3; --muted:#9fadbc;
  --line:#2c333a; --soft:#1a1e22; --accent:#0065ff;
  --new:#8993a4; --followup:#b86e00; --issued:#00879e; --emailed:#8250df;
  --printed:#44546f; --otw:#0065ff; --del:#00875a; --uploaded:#ae2a6d;
  --paid:#6554c0;
  --ok-bg:#133527; --ok-ink:#7ee2b8; --bad-bg:#42221f; --bad-ink:#ff9c8f;
}

/* ── midnight — deep navy, cyan accents ────────────────────────────────── */
:root[data-theme="midnight"] {
  --bg:#0a1023; --card:#121a38; --ink:#dbe7ff; --muted:#93a5cc;
  --line:#243158; --soft:#0d1430; --accent:#22c8e6;
  --new:#55628a; --followup:#c47f16; --issued:#0e93b0; --emailed:#7d6cf0;
  --printed:#46618f; --otw:#2e77e6; --del:#129d6e; --uploaded:#c04a86;
  --paid:#8168ef;
  --ok-bg:#0d3230; --ok-ink:#6fe8c5; --bad-bg:#40182a; --bad-ink:#ff92a8;
}

/* ── matrix — near-black, phosphor green, terminal vibe ────────────────── */
:root[data-theme="matrix"] {
  --bg:#030906; --card:#08130c; --ink:#5cf28c; --muted:#3ba065;
  --line:#123a22; --soft:#050d08; --accent:#00ff6a;
  --new:#4e7a5f; --followup:#d6c22a; --issued:#2ee6a0; --emailed:#35d9e8;
  --printed:#8fce75; --otw:#33ff77; --del:#62ffa5; --uploaded:#b8f04a;
  --paid:#4fd6b8;
  --ok-bg:#06301a; --ok-ink:#5cff9d; --bad-bg:#331111; --bad-ink:#ff7a6b;
}
/* Phosphor pills glow light — the white pill text flips to terminal black. */
:root[data-theme="matrix"] .pill { color:#03170c; }

/* ── sunset — warm dark: plum ground, ember orange ─────────────────────── */
:root[data-theme="sunset"] {
  --bg:#251024; --card:#321a30; --ink:#ffe9d9; --muted:#cfa28f;
  --line:#4b2a44; --soft:#1e0d1d; --accent:#ff7a45;
  --new:#7c6577; --followup:#c96a12; --issued:#d1495b; --emailed:#9d64d6;
  --printed:#8a5a3c; --otw:#d95738; --del:#1f9d6b; --uploaded:#c93a8c;
  --paid:#8a63e0;
  --ok-bg:#1c3a2c; --ok-ink:#7fe0ae; --bad-bg:#4a1a1a; --bad-ink:#ff9d8a;
}

/* ── ocean — deep teal water, seafoam accents ──────────────────────────── */
:root[data-theme="ocean"] {
  --bg:#052430; --card:#0a323f; --ink:#def4f8; --muted:#8fc2ce;
  --line:#155263; --soft:#031c26; --accent:#2dd4bf;
  --new:#4f7683; --followup:#c78a1c; --issued:#1899b8; --emailed:#6e7ee6;
  --printed:#3f7aa0; --otw:#2b8fe6; --del:#17a673; --uploaded:#ba4f8f;
  --paid:#7a6ce6;
  --ok-bg:#0b3a30; --ok-ink:#79e6c3; --bad-bg:#3d1d24; --bad-ink:#ff98a0;
}

/* ── monday — the dark-CRM look: near-black neutral, vivid greens ──────── */
:root[data-theme="monday"] {
  --bg:#0f1117; --card:#181b22; --ink:#eceff4; --muted:#9a9db0;
  --line:#2b2e3a; --soft:#14161c; --accent:#00c875;
  --new:#797e93; --followup:#fdab3d; --issued:#579bfc; --emailed:#a25ddc;
  --printed:#784bd1; --otw:#0086c0; --del:#00c875; --uploaded:#037f4c;
  --paid:#7e3b8a;
  --ok-bg:#0e3524; --ok-ink:#5ee6a8; --bad-bg:#3d1a20; --bad-ink:#ff8f9d;
}
/* The one light pill in the monday set keeps its text legible. */
:root[data-theme="monday"] .pill.s-followup { color:#15171f; }

/* ── mono — pure grayscale; the pipeline darkens as it completes ───────── */
:root[data-theme="mono"] {
  --bg:#f2f2f2; --card:#ffffff; --ink:#171717; --muted:#5c5c5c;
  --line:#dcdcdc; --soft:#f7f7f7; --accent:#171717;
  --new:#858585; --followup:#757575; --issued:#666666; --emailed:#575757;
  --printed:#494949; --otw:#3b3b3b; --del:#2d2d2d; --uploaded:#1f1f1f;
  --paid:#111111;
  --ok-bg:#e9e9e9; --ok-ink:#1a1a1a; --bad-bg:#2b2b2b; --bad-ink:#f2f2f2;
}

/* ── bubblegum — light pink/purple, playful but readable ───────────────── */
:root[data-theme="bubblegum"] {
  --bg:#fdeff7; --card:#ffffff; --ink:#4d1949; --muted:#8a5482;
  --line:#f2d3e6; --soft:#fdf7fb; --accent:#d6336c;
  --new:#8d6b95; --followup:#e8590c; --issued:#0b7285; --emailed:#ae3ec9;
  --printed:#5c5f66; --otw:#3b5bdb; --del:#2f9e44; --uploaded:#d6336c;
  --paid:#9c36b5;
  --ok-bg:#dcf7e6; --ok-ink:#18774a; --bad-bg:#ffe3e8; --bad-ink:#c02545;
}

/* ── The theme picker itself — vars only, so it wears every theme ──────── */
.krab-theme-wrap { position:relative; display:inline-block; }
.krab-theme-btn {
  border:1px solid var(--line); background:transparent; color:var(--muted);
  border-radius:20px; padding:6px 11px; font-size:14px; line-height:1;
  cursor:pointer;
}
.krab-theme-btn:hover { border-color:var(--accent); color:var(--accent); }
.krab-theme-pop {
  position:absolute; right:0; top:calc(100% + 8px); z-index:70;
  background:var(--card); color:var(--ink); border:1px solid var(--line);
  border-radius:12px; padding:10px; box-shadow:0 14px 40px rgba(0,0,0,.3);
  display:grid; grid-template-columns:repeat(2, minmax(128px, 1fr)); gap:6px;
  min-width:280px;
}
.krab-theme-pop[hidden] { display:none; }
.krab-theme-chip {
  display:flex; align-items:center; gap:8px; width:100%;
  border:1px solid var(--line); background:var(--soft); color:var(--ink);
  border-radius:9px; padding:6px 8px; font:inherit; font-size:12.5px;
  font-weight:600; cursor:pointer; text-align:left;
}
.krab-theme-chip:hover { border-color:var(--accent); }
.krab-theme-chip.on { border-color:var(--accent); box-shadow:0 0 0 1px var(--accent) inset; }
.krab-theme-swatch {
  flex:0 0 auto; width:22px; height:22px; border-radius:6px;
  border:1px solid var(--line);
  display:flex; align-items:center; justify-content:center;
}
.krab-theme-dot { width:9px; height:9px; border-radius:50%; }
.krab-theme-label { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.krab-theme-check { flex:0 0 auto; color:var(--accent); visibility:hidden; }
.krab-theme-chip.on .krab-theme-check { visibility:visible; }
@media (max-width:860px) {
  .krab-theme-pop { position:fixed; left:12px; right:12px; top:auto; bottom:14px; min-width:0; }
}

''',
    'themes.js': r'''
/* themes.js — the theme picker for the receipts board.
 *
 * Classic script, no modules, no top-level side effects. Defines exactly three
 * globals:
 *
 *   THEMES            — [{id, label, emoji}] including "auto" (follow system)
 *   applyTheme(id)    — sets/removes data-theme on <html>, persists to
 *                       localStorage "krab_theme"; "auto" removes the attr
 *   initThemes(el)    — applies the persisted theme, then renders a compact
 *                       picker (🎨 button → popover grid of swatch chips) into
 *                       the given container element
 *
 * Swatch colors are never hardcoded here: initThemes probes each theme once by
 * flipping data-theme on the root and reading the computed --bg/--accent/--line
 * (transitions suppressed for the probe, restored before any paint), so the
 * chips always mirror whatever themes.css actually ships.
 */
(function () {
  "use strict";

  var STORE_KEY = "krab_theme";

  var THEMES = [
    { id: "auto",      label: "Auto",      emoji: "🌗" },
    { id: "light",     label: "Light",     emoji: "☀️" },
    { id: "dark",      label: "Dark",      emoji: "🌙" },
    { id: "midnight",  label: "Midnight",  emoji: "🌌" },
    { id: "matrix",    label: "Matrix",    emoji: "💻" },
    { id: "sunset",    label: "Sunset",    emoji: "🌅" },
    { id: "ocean",     label: "Ocean",     emoji: "🌊" },
    { id: "monday",    label: "Monday",    emoji: "🟩" },
    { id: "mono",      label: "Mono",      emoji: "⬜" },
    { id: "bubblegum", label: "Bubblegum", emoji: "🍬" }
  ];

  // Every rendered picker registers here so applyTheme can refresh its checks.
  var pickers = [];

  function knownTheme(id) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].id === id) return true;
    }
    return false;
  }

  function storedTheme() {
    var v = "";
    try { v = localStorage.getItem(STORE_KEY) || ""; } catch (e) { /* private mode */ }
    return knownTheme(v) ? v : "auto";
  }

  function currentTheme() {
    return document.documentElement.getAttribute("data-theme") || "auto";
  }

  function applyTheme(id) {
    var root = document.documentElement;
    if (!id || id === "auto" || !knownTheme(id)) {
      root.removeAttribute("data-theme");
      id = "auto";
    } else {
      root.setAttribute("data-theme", id);
    }
    try { localStorage.setItem(STORE_KEY, id); } catch (e) { /* best effort */ }
    for (var i = 0; i < pickers.length; i++) pickers[i].refresh();
  }

  // Flip through every theme once, reading its computed vars for the chip
  // swatches. All flips happen inside one synchronous task with transitions
  // force-disabled, so nothing intermediate ever paints.
  function probeSwatches() {
    var root = document.documentElement;
    var prev = root.getAttribute("data-theme");
    var kill = document.createElement("style");
    kill.textContent = "*{transition:none !important}";
    (document.head || root).appendChild(kill);
    var out = {};
    try {
      for (var i = 0; i < THEMES.length; i++) {
        var t = THEMES[i];
        if (t.id === "auto") root.removeAttribute("data-theme");
        else root.setAttribute("data-theme", t.id);
        var cs = getComputedStyle(root);
        out[t.id] = {
          bg: (cs.getPropertyValue("--bg") || "").trim(),
          accent: (cs.getPropertyValue("--accent") || "").trim(),
          line: (cs.getPropertyValue("--line") || "").trim()
        };
      }
    } finally {
      if (prev === null) root.removeAttribute("data-theme");
      else root.setAttribute("data-theme", prev);
      // Flush the restored state before re-arming transitions so the probe
      // never animates.
      void getComputedStyle(root).getPropertyValue("--bg");
      if (kill.parentNode) kill.parentNode.removeChild(kill);
    }
    return out;
  }

  function initThemes(containerEl) {
    if (!containerEl || containerEl.querySelector(".krab-theme-wrap")) return;

    applyTheme(storedTheme());          // boot whatever this browser last chose
    var swatches = probeSwatches();

    var wrap = document.createElement("span");
    wrap.className = "krab-theme-wrap";

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "krab-theme-btn";
    btn.textContent = "🎨";
    btn.title = "Theme";
    btn.setAttribute("aria-haspopup", "true");
    btn.setAttribute("aria-expanded", "false");

    var pop = document.createElement("div");
    pop.className = "krab-theme-pop";
    pop.hidden = true;
    pop.setAttribute("role", "menu");

    var chips = [];
    for (var i = 0; i < THEMES.length; i++) {
      (function (t) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "krab-theme-chip";
        chip.setAttribute("role", "menuitemradio");
        chip.dataset.theme = t.id;
        chip.title = t.label;

        var sw = document.createElement("span");
        sw.className = "krab-theme-swatch";
        var probed = swatches[t.id] || {};
        if (probed.bg) sw.style.background = probed.bg;
        if (probed.line) sw.style.borderColor = probed.line;
        var dot = document.createElement("span");
        dot.className = "krab-theme-dot";
        if (probed.accent) dot.style.background = probed.accent;
        sw.appendChild(dot);

        var label = document.createElement("span");
        label.className = "krab-theme-label";
        label.textContent = t.emoji + " " + t.label;

        var check = document.createElement("span");
        check.className = "krab-theme-check";
        check.textContent = "✓";

        chip.appendChild(sw);
        chip.appendChild(label);
        chip.appendChild(check);
        chip.addEventListener("click", function () {
          applyTheme(t.id);
          close();
        });
        pop.appendChild(chip);
        chips.push(chip);
      })(THEMES[i]);
    }

    function refresh() {
      var cur = currentTheme();
      for (var j = 0; j < chips.length; j++) {
        var on = chips[j].dataset.theme === cur;
        chips[j].classList.toggle("on", on);
        chips[j].setAttribute("aria-checked", on ? "true" : "false");
      }
    }

    function onDocClick(e) {
      if (!wrap.contains(e.target)) close();
    }
    function onKey(e) {
      if (e.key === "Escape") close();
    }
    function open() {
      refresh();
      pop.hidden = false;
      btn.setAttribute("aria-expanded", "true");
      document.addEventListener("click", onDocClick, true);
      document.addEventListener("keydown", onKey, true);
    }
    function close() {
      if (pop.hidden) return;
      pop.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      document.removeEventListener("click", onDocClick, true);
      document.removeEventListener("keydown", onKey, true);
    }

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      if (pop.hidden) open(); else close();
    });

    wrap.appendChild(btn);
    wrap.appendChild(pop);
    containerEl.appendChild(wrap);

    pickers.push({ refresh: refresh });
    refresh();
  }

  window.THEMES = THEMES;
  window.applyTheme = applyTheme;
  window.initThemes = initThemes;
})();

''',
    'sheet.js': r'''
/* sheet.js — the transmissions board as an actual spreadsheet.
 *
 * Defines exactly two globals:
 *   renderSheetView(rows, el)  — Excel-like grid: column letters, frozen header
 *                                and row-number column, gridlines, zebra,
 *                                totals row; scrolls both axes inside `el`.
 *   buildCsv(rows) -> string   — the same columns as RFC-4180 CSV (CRLF,
 *                                BOM-prefixed so Excel opens UTF-8 cleanly).
 *
 * Classic script, no frameworks, no top-level side effects beyond defining the
 * two globals. Uses the page's helpers (esc / when / moneyNum / monthKey /
 * monthLabel / LABELS) when present, with quiet local fallbacks so the module
 * never throws if loaded standalone. All colors are read from the page's CSS
 * variables at render time — nothing is hardcoded, light and dark both work.
 */
(function () {
  "use strict";

  /* ── page-helper bridges (resolved at call time, fallback if absent) ───── */

  function _esc(s) {
    if (typeof window.esc === "function") return window.esc(s);
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
    });
  }

  function _when(iso) {
    if (typeof window.when === "function") return window.when(iso);
    if (!iso) return "—";
    var d = new Date(iso);
    return isNaN(d) ? "—" : d.toLocaleDateString([], {month: "short", day: "numeric", year: "numeric"});
  }

  function _moneyNum(p) {
    if (typeof window.moneyNum === "function") return window.moneyNum(p);
    var n = parseFloat(String(p == null ? "" : p).replace(/[^0-9.\-]/g, ""));
    return isNaN(n) ? 0 : n;
  }

  function _monthKey(iso) {
    if (typeof window.monthKey === "function") return window.monthKey(iso);
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d)) return "";
    var m = d.getMonth() + 1;
    return d.getFullYear() + "-" + (m < 10 ? "0" + m : "" + m);
  }

  function _monthLabel(key) {
    if (typeof window.monthLabel === "function") return window.monthLabel(key);
    if (!key) return "";
    var parts = String(key).split("-");
    var names = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                 "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    var i = parseInt(parts[1], 10) - 1;
    return (names[i] || "") + " " + parts[0];
  }

  var FALLBACK_LABELS = {
    "new": "New Lead", "followup": "Followup", "tag_issued": "Tag issued",
    "tag_emailed": "Tag emailed", "tag_printed": "Tag printed",
    "on_the_way": "Driver on the way", "delivered": "Delivered",
    "receipt_uploaded": "Receipt uploaded"
  };

  function _label(status) {
    var L = (typeof window.LABELS === "object" && window.LABELS) || FALLBACK_LABELS;
    return L[status] || FALLBACK_LABELS[status] || String(status == null ? "" : status);
  }

  /* ── shared column model — one list drives both the grid and the CSV ───── */

  // Which CSS variable tints each status (dot + text in the grid).
  var STATUS_VAR = {
    "new": "--new", "followup": "--followup", "tag_issued": "--issued",
    "tag_emailed": "--emailed", "tag_printed": "--printed",
    "on_the_way": "--otw", "delivered": "--del", "receipt_uploaded": "--uploaded"
  };

  function renewalDays(r, now) {
    var base = r.issue_date || r.created_at;
    if (!base) return "";
    var t = new Date(base).getTime();
    if (isNaN(t)) return "";
    var d = 29 - Math.floor((now - t) / 86400000);
    return d < 0 ? 0 : d;
  }

  // The "#" column is the frozen row-number gutter; data columns get letters.
  // Each column: header label, align, and get(r, i, now) -> plain value
  // (string or number — never HTML; the grid escapes, the CSV quotes).
  var COLS = [
    {h: "#",             align: "right", num: true,
     get: function (r, i) { return i + 1; }},
    {h: "Month",         align: "left",
     get: function (r) { return _monthLabel(_monthKey(r.created_at)); }},
    {h: "Date",          align: "left",
     get: function (r) { return _when(r.created_at); }},
    {h: "Reference",     align: "left",
     get: function (r) { return r.reference_id || ""; }},
    {h: "Client",        align: "left",
     get: function (r) { return r.client_name || ""; }},
    {h: "Phone",         align: "left",
     get: function (r) { return r.client_phone || ""; }},
    {h: "Email",         align: "left", clip: 220,
     get: function (r) { return r.email || ""; }},
    {h: "Car",           align: "left", clip: 220,
     get: function (r) { return r.car || ""; }},
    {h: "Tags",          align: "right",
     get: function (r) { return r.tags || 1; }},
    {h: "Price",         align: "right",
     get: function (r) { return r.price || "—"; }},
    {h: "Receipt?",      align: "center",
     get: function (r) { return r.has_receipt ? "yes" : "no"; }},
    {h: "Receipt date",  align: "left",
     get: function (r) { return r.receipt_at ? _when(r.receipt_at) : ""; }},
    {h: "Status",        align: "left", status: true,
     get: function (r) { return _label(r.status); }},
    {h: "Driver",        align: "left",
     get: function (r) { return r.driver_name || ""; }},
    {h: "Driver phone",  align: "left",
     get: function (r) { return r.driver_phone || ""; }},
    {h: "Driver email",  align: "left", clip: 220,
     get: function (r) { return r.driver_email || ""; }},
    {h: "Issuer",        align: "left",
     get: function (r) { return r.issuer || ""; }},
    {h: "Dispatcher",    align: "left",
     get: function (r) { return r.group_name || ""; }},
    {h: "Renewal days",  align: "right", renew: true,
     get: function (r, i, now) { return renewalDays(r, now); }},
    {h: "Delivery",      align: "left", clip: 240,
     get: function (r) { return r.delivery || ""; }},
    {h: "Notes",         align: "left", clip: 280,
     get: function (r) { return r.notes || ""; }},
    {h: "Updated by",    align: "left",
     get: function (r) { return r.status_updated_by || ""; }}
  ];

  function colLetter(n) {           // 0 -> A, 25 -> Z, 26 -> AA …
    var s = "";
    n = n + 1;
    while (n > 0) {
      var rem = (n - 1) % 26;
      s = String.fromCharCode(65 + rem) + s;
      n = Math.floor((n - 1) / 26);
    }
    return s;
  }

  /* ── renderSheetView ───────────────────────────────────────────────────── */

  function renderSheetView(rows, el) {
    rows = Array.isArray(rows) ? rows : [];
    var now = Date.now();

    // Theme is read fresh on every render — never cached, never hardcoded.
    var cs = getComputedStyle(document.documentElement);
    function v(name, fb) {
      var x = (cs.getPropertyValue(name) || "").trim();
      return x || fb;
    }
    var C = {
      bg:     v("--bg", "#f4f5f7"),
      card:   v("--card", "#ffffff"),
      ink:    v("--ink", "#172b4d"),
      muted:  v("--muted", "#6b778c"),
      line:   v("--line", "#dfe1e6"),
      soft:   v("--soft", "#f8f9fb"),
      accent: v("--accent", "#0065ff"),
      okInk:  v("--ok-ink", "#006644"),
      badInk: v("--bad-ink", "#bf2600")
    };
    function statusColor(status) {
      return v(STATUS_VAR[status] || "--muted", C.muted);
    }

    // Totals.
    var sumPrice = 0, sumReceipted = 0, yes = 0, sumTags = 0;
    rows.forEach(function (r) {
      var p = _moneyNum(r.price);
      sumPrice += p;
      if (r.has_receipt) { yes += 1; sumReceipted += p; }
      sumTags += (r.tags || 1);
    });
    function money(n) {
      return "$" + n.toLocaleString(undefined, {maximumFractionDigits: 2});
    }

    // Scoped, resolved-at-render style block. Selectors are prefixed so this
    // cannot leak into the rest of the page.
    var mono = 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Cascadia Mono",monospace';
    var css = "" +
      ".shx-scroll{overflow:auto;max-height:100%;height:100%;background:" + C.card + ";" +
        "border:1px solid " + C.line + ";border-radius:10px;}" +
      ".shx-table{border-collapse:separate;border-spacing:0;min-width:max-content;" +
        "font:12px/1.5 " + mono + ";color:" + C.ink + ";" +
        "user-select:text;-webkit-user-select:text;}" +
      ".shx-table th,.shx-table td{border-right:1px solid " + C.line + ";" +
        "border-bottom:1px solid " + C.line + ";padding:4px 9px;white-space:nowrap;" +
        "background:" + C.card + ";background-clip:padding-box;}" +
      /* letters row (frozen, layer 1) */
      ".shx-letters th{position:sticky;top:0;z-index:4;height:20px;" +
        "background:" + C.soft + ";color:" + C.muted + ";font-weight:600;" +
        "font-size:10.5px;text-align:center;letter-spacing:.04em;padding:2px 9px;}" +
      /* header-label row (frozen just below the letters) */
      ".shx-head th{position:sticky;top:25px;z-index:4;background:" + C.soft + ";" +
        "color:" + C.ink + ";font-weight:700;font-size:11px;text-align:left;" +
        "border-bottom:2px solid " + C.line + ";}" +
      /* row-number gutter (frozen left) */
      ".shx-table .shx-num{position:sticky;left:0;z-index:3;background:" + C.soft + ";" +
        "color:" + C.muted + ";text-align:right;font-size:10.5px;min-width:34px;" +
        "border-right:2px solid " + C.line + ";user-select:none;-webkit-user-select:none;}" +
      /* the two frozen corners */
      ".shx-letters .shx-num,.shx-head .shx-num{z-index:5;}" +
      /* body */
      ".shx-table tbody tr:nth-child(even):not(.shx-total) td{background:" + C.soft + ";}" +
      ".shx-table tbody tr:hover td:not(.shx-num){box-shadow:inset 0 0 0 999px rgba(127,127,127,.06);}" +
      ".shx-c{text-align:center}.shx-r{text-align:right}" +
      ".shx-clip{max-width:var(--shx-clip,240px);overflow:hidden;text-overflow:ellipsis;}" +
      ".shx-dot{display:inline-block;width:7px;height:7px;border-radius:50%;" +
        "margin-right:6px;vertical-align:1px;}" +
      ".shx-dim{color:" + C.muted + ";}" +
      /* totals row — pinned to the bottom of the scroller */
      ".shx-total td{position:sticky;bottom:0;z-index:3;background:" + C.soft + ";" +
        "font-weight:700;border-top:2px solid " + C.line + ";border-bottom:0;}" +
      ".shx-total td.shx-num{z-index:5;}" +
      ".shx-empty{padding:26px 14px;color:" + C.muted + ";font:13px/1.5 " + mono + ";" +
        "text-align:center;}";

    var h = ['<style>' + css + '</style>', '<div class="shx-scroll"><table class="shx-table">'];

    // Row 0 — column letters. The "#" gutter gets the blank Excel corner.
    h.push('<thead><tr class="shx-letters"><th class="shx-num">&nbsp;</th>');
    for (var ci = 1; ci < COLS.length; ci++) {
      h.push("<th>" + colLetter(ci - 1) + "</th>");
    }
    h.push("</tr>");

    // Row 1 — header labels (frozen with the letters).
    h.push('<tr class="shx-head"><th class="shx-num">' + _esc(COLS[0].h) + "</th>");
    for (ci = 1; ci < COLS.length; ci++) {
      h.push("<th>" + _esc(COLS[ci].h) + "</th>");
    }
    h.push("</tr></thead><tbody>");

    if (!rows.length) {
      h.push('<tr><td class="shx-num">1</td><td colspan="' + (COLS.length - 1) +
             '"><div class="shx-empty">Nothing here yet.</div></td></tr>');
    }

    rows.forEach(function (r, i) {
      h.push("<tr>");
      COLS.forEach(function (col, k) {
        var val = col.get(r, i, now);
        var text = String(val == null ? "" : val);
        if (col.num) {                              // frozen row-number gutter
          h.push('<td class="shx-num">' + _esc(text) + "</td>");
          return;
        }
        var cls = [];
        if (col.align === "right") cls.push("shx-r");
        if (col.align === "center") cls.push("shx-c");
        if (col.clip) cls.push("shx-clip");
        var style = col.clip ? ' style="--shx-clip:' + col.clip + 'px"' : "";
        var inner;
        if (col.status) {                           // colored dot + label
          inner = '<span class="shx-dot" style="background:' +
                  _esc(statusColor(r.status)) + '"></span><span style="color:' +
                  _esc(statusColor(r.status)) + ';font-weight:600">' +
                  _esc(text) + "</span>";
        } else if (col.renew && text !== "") {      // renewal urgency tint
          var dcol = val <= 5 ? C.badInk : (val <= 10 ? C.accent : C.okInk);
          inner = '<span style="color:' + _esc(dcol) + ';font-weight:600">' +
                  _esc(text) + "</span>";
        } else if (text === "" || text === "—") {
          inner = '<span class="shx-dim">' + (text === "" ? "" : "—") + "</span>";
        } else {
          inner = _esc(text);
        }
        var title = col.clip && text.length > 24 ? ' title="' + _esc(text) + '"' : "";
        h.push("<td" + (cls.length ? ' class="' + cls.join(" ") + '"' : "") +
               style + title + ">" + inner + "</td>");
      });
      h.push("</tr>");
    });

    // Totals row — Σ price, Σ where a receipt is on file, and the counts.
    h.push('<tr class="shx-total"><td class="shx-num">&Sigma;</td>');
    COLS.forEach(function (col) {
      if (col.num) return;
      var cell = "";
      var cls = col.align === "right" ? ' class="shx-r"'
              : col.align === "center" ? ' class="shx-c"' : "";
      switch (col.h) {
        case "Client":       cell = rows.length + " row" + (rows.length === 1 ? "" : "s"); break;
        case "Tags":         cell = String(sumTags); break;
        case "Price":        cell = money(sumPrice); break;
        case "Receipt?":     cell = yes + " yes / " + (rows.length - yes) + " no"; break;
        case "Receipt date": cell = money(sumReceipted) + " receipted"; break;
      }
      h.push("<td" + cls + ">" + _esc(cell) + "</td>");
    });
    h.push("</tr></tbody></table></div>");

    el.innerHTML = h.join("");
  }

  /* ── buildCsv ──────────────────────────────────────────────────────────── */

  function csvField(val) {
    var s = String(val == null ? "" : val);
    // Neutralize spreadsheet formula injection: a leading = + - @ tab or CR
    // would make Excel/Sheets evaluate the cell. Prefix BEFORE quoting.
    if (/^[=+\-@\t\r]/.test(s)) s = "'" + s;
    if (/[",\r\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function buildCsv(rows) {
    rows = Array.isArray(rows) ? rows : [];
    var now = Date.now();
    var lines = [COLS.map(function (c) { return csvField(c.h); }).join(",")];
    rows.forEach(function (r, i) {
      lines.push(COLS.map(function (c) {
        return csvField(c.get(r, i, now));
      }).join(","));
    });
    // BOM so Excel opens the UTF-8 bytes cleanly; CRLF per RFC 4180.
    return "\uFEFF" + lines.join("\r\n") + "\r\n";
  }

  window.renderSheetView = renderSheetView;
  window.buildCsv = buildCsv;
})();

''',
    'charts.js': r'''
"use strict";
/* charts.js — the diagram view of all the numbers in the transactions.
 *
 * One global: renderChartView(rows, el).
 * Fully redraws into `el` on every call: a summary strip of stat chips, then a
 * grid of canvas cards — monthly revenue (total vs with-receipt) grouped bars,
 * a status donut with legend, receipts-per-month area line, and top-5 drivers /
 * issuers horizontal bars. All colors are read from the page's CSS variables at
 * render time (light + dark just work); canvases are DPR-scaled so they stay
 * crisp on retina. No libraries, no top-level side effects.
 */
function renderChartView(rows, el) {
  rows = Array.isArray(rows) ? rows : [];
  if (!el) return;

  /* ── page helpers (with safe fallbacks so the module never throws) ────── */
  var _esc = (typeof esc === "function") ? esc : function (s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var _money = (typeof moneyNum === "function") ? moneyNum : function (p) {
    var n = parseFloat(String(p == null ? "" : p).replace(/[^0-9.\-]/g, ""));
    return isNaN(n) ? 0 : n;
  };
  var _mkey = (typeof monthKey === "function") ? monthKey : function (iso) {
    return iso ? String(iso).slice(0, 7) : "";
  };
  var _statuses = (typeof STATUSES !== "undefined" && Array.isArray(STATUSES)) ? STATUSES
    : ["new", "followup", "tag_issued", "tag_emailed", "tag_printed", "on_the_way", "delivered", "receipt_uploaded"];
  var _labels = (typeof LABELS !== "undefined" && LABELS) ? LABELS : {};
  function statusLabel(s) { return _labels[s] || s; }

  /* ── theme, read live so light/dark both work ─────────────────────────── */
  var cs = getComputedStyle(document.documentElement);
  function cvar(name, fb) {
    var v = (cs.getPropertyValue(name) || "").trim();
    return v || fb;
  }
  var INK    = cvar("--ink", "#172b4d");
  var MUTED  = cvar("--muted", "#6b778c");
  var LINE   = cvar("--line", "#dfe1e6");
  var ACCENT = cvar("--accent", "#0065ff");
  var DEL    = cvar("--del", "#00875a");
  var PAID   = cvar("--paid", "#6554c0");
  var STATUS_COLOR = {
    new:              cvar("--new", "#8993a4"),
    followup:         cvar("--followup", "#ff991f"),
    tag_issued:       cvar("--issued", "#00b8d9"),
    tag_emailed:      cvar("--emailed", "#6554c0"),
    tag_printed:      cvar("--printed", "#403294"),
    on_the_way:       cvar("--otw", "#0065ff"),
    delivered:        cvar("--del", "#00875a"),
    receipt_uploaded: cvar("--uploaded", "#36b37e"),
    paid:             cvar("--paid", "#6554c0"),
  };
  function colorFor(status, i) {
    return STATUS_COLOR[status] ||
      [ACCENT, DEL, PAID, MUTED][i % 4];
  }
  var FONT = '-apple-system,system-ui,"Segoe UI",sans-serif';

  /* ── number / date formatting ─────────────────────────────────────────── */
  function fmtMoney(n) {
    return "$" + Math.round(n).toLocaleString("en-US");
  }
  function fmtCompact(n) {
    if (Math.abs(n) >= 1000) {
      var k = n / 1000;
      return "$" + (Math.abs(k) >= 100 ? Math.round(k) : Math.round(k * 10) / 10) + "k";
    }
    return "$" + Math.round(n);
  }
  var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function shortMonth(key) {           // "2026-05" -> "May"  ("Jan 26" at a year turn)
    var y = parseInt(key.slice(0, 4), 10), m = parseInt(key.slice(5, 7), 10) - 1;
    if (isNaN(m) || m < 0 || m > 11) return key;
    return MONTH_NAMES[m] + (m === 0 ? " " + String(y).slice(2) : "");
  }
  function lastMonths(n) {
    var out = [], now = new Date();
    for (var i = n - 1; i >= 0; i--) {
      var t = new Date(now.getFullYear(), now.getMonth() - i, 1);
      out.push(t.getFullYear() + "-" + ("0" + (t.getMonth() + 1)).slice(-2));
    }
    return out;
  }
  function niceMax(v) {
    if (v <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var d = v / p;
    var n = d <= 1 ? 1 : d <= 2 ? 2 : d <= 2.5 ? 2.5 : d <= 5 ? 5 : 10;
    return n * p;
  }
  function truncate(ctx, text, maxW) {
    text = String(text == null ? "" : text);
    if (ctx.measureText(text).width <= maxW) return text;
    while (text.length > 1 && ctx.measureText(text + "…").width > maxW) {
      text = text.slice(0, -1);
    }
    return text + "…";
  }

  /* ── aggregate the numbers ────────────────────────────────────────────── */
  var months = lastMonths(12);
  var mIndex = {};
  months.forEach(function (k, i) { mIndex[k] = i; });

  var revTotal = months.map(function () { return 0; });   // all $, by created month
  var revRecpt = months.map(function () { return 0; });   // $ of rows with a receipt
  var cntRecpt = months.map(function () { return 0; });   // receipts handed in, by month
  var sumAll = 0, sumWith = 0, sumThisMonth = 0;
  var nowKey = months[months.length - 1];
  var byStatus = {}, byDriver = {}, byIssuer = {};

  rows.forEach(function (r) {
    var amt = _money(r.price);
    sumAll += amt;
    if (r.has_receipt) sumWith += amt;

    var ck = _mkey(r.created_at);
    if (ck === nowKey) sumThisMonth += amt;
    if (ck in mIndex) {
      revTotal[mIndex[ck]] += amt;
      if (r.has_receipt) revRecpt[mIndex[ck]] += amt;
    }
    if (r.has_receipt) {
      var rk = _mkey(r.receipt_at || r.created_at);
      if (rk in mIndex) cntRecpt[mIndex[rk]] += 1;
    }
    var st = r.status || "new";
    byStatus[st] = (byStatus[st] || 0) + 1;
    var d = (r.driver_name || "").trim();
    if (d && d !== "—") byDriver[d] = (byDriver[d] || 0) + 1;
    var iss = (r.issuer || "").trim();
    if (iss && iss !== "—") byIssuer[iss] = (byIssuer[iss] || 0) + 1;
  });

  function top5(map) {
    return Object.keys(map)
      .map(function (k) { return { name: k, n: map[k] }; })
      .sort(function (a, b) { return b.n - a.n || a.name.localeCompare(b.name); })
      .slice(0, 5);
  }
  var drivers = top5(byDriver), issuers = top5(byIssuer);

  // Statuses in board order first, then anything the board does not know.
  var statusEntries = [];
  _statuses.forEach(function (s) {
    if (byStatus[s]) statusEntries.push({ status: s, n: byStatus[s] });
  });
  Object.keys(byStatus).sort().forEach(function (s) {
    if (_statuses.indexOf(s) === -1) statusEntries.push({ status: s, n: byStatus[s] });
  });

  /* ── one-time stylesheet (kc- prefix, page variables throughout) ──────── */
  if (!document.getElementById("kc-style")) {
    var st = document.createElement("style");
    st.id = "kc-style";
    st.textContent = [
      ".kc-wrap { display:flex; flex-direction:column; gap:12px; }",
      ".kc-chips { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }",
      ".kc-chip { background:var(--card); border:1px solid var(--line); border-radius:10px;",
      "  padding:12px 14px; min-width:0; }",
      ".kc-chip .kc-k { font-size:11px; letter-spacing:.06em; text-transform:uppercase;",
      "  color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }",
      ".kc-chip .kc-v { font-size:20px; font-weight:700; color:var(--ink); margin-top:3px;",
      "  font-variant-numeric:tabular-nums; white-space:nowrap; }",
      ".kc-chip .kc-s { font-size:11px; color:var(--muted); margin-top:2px; }",
      ".kc-grid { display:grid; grid-template-columns:repeat(12,1fr); gap:12px; }",
      ".kc-card { background:var(--card); border:1px solid var(--line); border-radius:10px;",
      "  padding:14px 16px 12px; min-width:0; }",
      ".kc-span12 { grid-column:span 12; } .kc-span6 { grid-column:span 6; }",
      "@media (max-width:900px) { .kc-span6 { grid-column:span 12; } }",
      ".kc-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; margin-bottom:10px; }",
      ".kc-title { font-size:11px; letter-spacing:.06em; text-transform:uppercase;",
      "  color:var(--muted); font-weight:700; }",
      ".kc-sub { font-size:11px; color:var(--muted); }",
      ".kc-legend { margin-left:auto; display:flex; gap:12px; flex-wrap:wrap; font-size:12px;",
      "  color:var(--muted); }",
      ".kc-legend .kc-dot, .kc-slices .kc-dot { display:inline-block; width:9px; height:9px;",
      "  border-radius:3px; margin-right:5px; vertical-align:baseline; }",
      ".kc-donutrow { display:flex; gap:16px; align-items:center; flex-wrap:wrap; }",
      ".kc-donutrow canvas { flex:0 0 auto; }",
      ".kc-slices { flex:1; min-width:170px; display:flex; flex-direction:column; gap:6px;",
      "  font-size:12.5px; }",
      ".kc-slices .kc-row { display:flex; align-items:center; gap:6px; min-width:0; }",
      ".kc-slices .kc-name { color:var(--ink); font-weight:600; white-space:nowrap;",
      "  overflow:hidden; text-overflow:ellipsis; }",
      ".kc-slices .kc-n { margin-left:auto; color:var(--ink); font-weight:700;",
      "  font-variant-numeric:tabular-nums; }",
      ".kc-slices .kc-pct { color:var(--muted); width:38px; text-align:right;",
      "  font-variant-numeric:tabular-nums; }",
      ".kc-canvasbox { position:relative; width:100%; }",
      ".kc-canvasbox canvas { display:block; width:100%; }",
      ".kc-empty { color:var(--muted); font-size:13px; padding:26px 0; text-align:center; }",
    ].join("\n");
    document.head.appendChild(st);
  }

  /* ── DPR-correct canvas plumbing ──────────────────────────────────────── */
  var dpr = Math.max(1, Math.min(3, window.devicePixelRatio || 1));
  function canvasIn(host, cssH) {
    var w = Math.max(240, host.clientWidth || el.clientWidth || 320);
    var cv = document.createElement("canvas");
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(cssH * dpr);
    cv.style.width = "100%";
    cv.style.height = cssH + "px";
    host.appendChild(cv);
    var ctx = cv.getContext("2d");
    ctx.scale(dpr, dpr);
    return { ctx: ctx, w: w, h: cssH };
  }
  function card(span, title, sub, legendHtml) {
    var c = document.createElement("div");
    c.className = "kc-card kc-span" + span;
    c.innerHTML = '<div class="kc-head"><span class="kc-title">' + _esc(title) + "</span>"
      + (sub ? '<span class="kc-sub">' + _esc(sub) + "</span>" : "")
      + (legendHtml ? '<span class="kc-legend">' + legendHtml + "</span>" : "")
      + '</div><div class="kc-canvasbox"></div>';
    return c;
  }
  function legendItem(color, label) {
    return '<span><span class="kc-dot" style="background:' + _esc(color) + '"></span>'
      + _esc(label) + "</span>";
  }
  function drawEmpty(ctx, w, h, msg) {
    ctx.fillStyle = MUTED;
    ctx.font = "13px " + FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(msg, w / 2, h / 2);
  }
  function roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, Math.abs(w) / 2, Math.abs(h) / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /* ── shared axis scaffolding for the monthly charts ───────────────────── */
  function monthAxes(ctx, w, h, maxVal, fmt) {
    var m = { l: 46, r: 10, t: 16, b: 22 };
    var pw = w - m.l - m.r, ph = h - m.t - m.b;
    var top = niceMax(maxVal);
    var steps = 4;
    ctx.font = "10px " + FONT;
    ctx.textBaseline = "middle";
    for (var i = 0; i <= steps; i++) {
      var v = top * i / steps;
      var y = m.t + ph - ph * i / steps;
      ctx.strokeStyle = LINE;
      ctx.lineWidth = 1;
      ctx.globalAlpha = i === 0 ? 1 : 0.6;
      ctx.beginPath();
      ctx.moveTo(m.l, y);
      ctx.lineTo(m.l + pw, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = MUTED;
      ctx.textAlign = "right";
      ctx.fillText(fmt(v), m.l - 6, y);
    }
    // month labels, thinned to what fits
    var slot = pw / months.length;
    var every = Math.max(1, Math.ceil(30 / slot));
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    months.forEach(function (k, i) {
      if (i % every !== 0 && i !== months.length - 1) return;
      if (i === months.length - 1 && (i % every !== 0) && slot * every < 34) return;
      ctx.fillStyle = MUTED;
      ctx.fillText(shortMonth(k), m.l + slot * i + slot / 2, h - 7);
    });
    return { m: m, pw: pw, ph: ph, top: top, slot: slot };
  }

  /* ── build the DOM ────────────────────────────────────────────────────── */
  el.innerHTML = "";
  var wrap = document.createElement("div");
  wrap.className = "kc-wrap";
  el.appendChild(wrap);

  // 5) summary chips first — the headline numbers
  var chips = document.createElement("div");
  chips.className = "kc-chips";
  var chipDefs = [
    ["Total, all time", fmtMoney(sumAll), rows.length + " transaction" + (rows.length === 1 ? "" : "s")],
    ["With receipts", fmtMoney(sumWith), sumAll > 0 ? Math.round(sumWith / sumAll * 100) + "% of total" : ""],
    ["Missing receipts", fmtMoney(sumAll - sumWith), rows.filter(function (r) { return !r.has_receipt; }).length + " without a receipt"],
    ["Transactions", String(rows.length), rows.filter(function (r) { return r.has_receipt; }).length + " with receipt"],
    ["This month", fmtMoney(sumThisMonth), shortMonth(nowKey)],
  ];
  chips.innerHTML = chipDefs.map(function (c) {
    return '<div class="kc-chip"><div class="kc-k">' + _esc(c[0]) + '</div>'
      + '<div class="kc-v">' + _esc(c[1]) + "</div>"
      + (c[2] ? '<div class="kc-s">' + _esc(c[2]) + "</div>" : "") + "</div>";
  }).join("");
  wrap.appendChild(chips);

  var grid = document.createElement("div");
  grid.className = "kc-grid";
  wrap.appendChild(grid);

  var hasAny = rows.length > 0;

  /* 1) monthly revenue — grouped bars, total vs with-receipt ─────────────── */
  (function () {
    var c = card(12, "Monthly revenue", "last 12 months",
      legendItem(ACCENT, "Total") + legendItem(DEL, "With receipt"));
    grid.appendChild(c);
    var box = c.querySelector(".kc-canvasbox");
    var cv = canvasIn(box, 240);
    var ctx = cv.ctx, w = cv.w, h = cv.h;
    var maxV = Math.max.apply(null, revTotal.concat(revRecpt, [0]));
    if (!hasAny || maxV <= 0) { drawEmpty(ctx, w, h, "No revenue in the last 12 months."); return; }
    var ax = monthAxes(ctx, w, h, maxV, fmtCompact);
    var group = ax.slot * 0.62;
    var bw = Math.max(3, Math.min(26, group / 2 - 1));
    ctx.font = "9.5px " + FONT;
    months.forEach(function (k, i) {
      var cx = ax.m.l + ax.slot * i + ax.slot / 2;
      [[revTotal[i], ACCENT, cx - bw - 1], [revRecpt[i], DEL, cx + 1]].forEach(function (bar) {
        var v = bar[0];
        var bh = ax.ph * (v / ax.top);
        var x = bar[2], y = ax.m.t + ax.ph - bh;
        if (v > 0) {
          ctx.fillStyle = bar[1];
          roundRect(ctx, x, y, bw, bh, 2.5);
          ctx.fill();
          if (bw >= 16) {                       // value labels only when they fit
            ctx.fillStyle = MUTED;
            ctx.textAlign = "center";
            ctx.textBaseline = "alphabetic";
            ctx.fillText(fmtCompact(v), x + bw / 2, y - 4);
          }
        }
      });
    });
  })();

  /* 2) status donut with legend + counts ─────────────────────────────────── */
  (function () {
    var c = card(6, "Status distribution", rows.length ? rows.length + " transactions" : "");
    grid.appendChild(c);
    var box = c.querySelector(".kc-canvasbox");
    if (!statusEntries.length) {
      box.innerHTML = '<div class="kc-empty">No transactions yet.</div>';
      return;
    }
    var row = document.createElement("div");
    row.className = "kc-donutrow";
    box.appendChild(row);

    var size = 190;
    var cvEl = document.createElement("canvas");
    cvEl.width = Math.round(size * dpr);
    cvEl.height = Math.round(size * dpr);
    cvEl.style.width = size + "px";
    cvEl.style.height = size + "px";
    row.appendChild(cvEl);
    var ctx = cvEl.getContext("2d");
    ctx.scale(dpr, dpr);

    var total = statusEntries.reduce(function (a, e) { return a + e.n; }, 0);
    var cx = size / 2, cy = size / 2, rOut = size / 2 - 4, rIn = rOut * 0.62;
    var a = -Math.PI / 2;
    var gap = statusEntries.length > 1 ? 0.02 : 0;
    statusEntries.forEach(function (e, i) {
      var sweep = (e.n / total) * Math.PI * 2;
      var a0 = a + gap / 2, a1 = a + sweep - gap / 2;
      if (a1 > a0) {
        ctx.beginPath();
        ctx.arc(cx, cy, rOut, a0, a1);
        ctx.arc(cx, cy, rIn, a1, a0, true);
        ctx.closePath();
        ctx.fillStyle = colorFor(e.status, i);
        ctx.fill();
      }
      a += sweep;
    });
    ctx.fillStyle = INK;
    ctx.font = "700 24px " + FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(String(total), cx, cy + 2);
    ctx.fillStyle = MUTED;
    ctx.font = "10px " + FONT;
    ctx.fillText("total", cx, cy + 16);

    var legend = document.createElement("div");
    legend.className = "kc-slices";
    legend.innerHTML = statusEntries.map(function (e, i) {
      var pct = Math.round(e.n / total * 100);
      return '<div class="kc-row"><span class="kc-dot" style="background:'
        + _esc(colorFor(e.status, i)) + '"></span>'
        + '<span class="kc-name">' + _esc(statusLabel(e.status)) + "</span>"
        + '<span class="kc-n">' + e.n + "</span>"
        + '<span class="kc-pct">' + pct + "%</span></div>";
    }).join("");
    row.appendChild(legend);
  })();

  /* 3) receipts handed in per month — area line ──────────────────────────── */
  (function () {
    var c = card(6, "Receipts per month", "handed in, last 12 months");
    grid.appendChild(c);
    var box = c.querySelector(".kc-canvasbox");
    var cv = canvasIn(box, 222);
    var ctx = cv.ctx, w = cv.w, h = cv.h;
    var maxV = Math.max.apply(null, cntRecpt.concat([0]));
    if (!hasAny || maxV <= 0) { drawEmpty(ctx, w, h, "No receipts in the last 12 months."); return; }
    var ax = monthAxes(ctx, w, h, maxV, function (v) { return String(Math.round(v)); });
    function px(i) { return ax.m.l + ax.slot * i + ax.slot / 2; }
    function py(v) { return ax.m.t + ax.ph - ax.ph * (v / ax.top); }

    ctx.beginPath();                                     // area fill
    ctx.moveTo(px(0), ax.m.t + ax.ph);
    cntRecpt.forEach(function (v, i) { ctx.lineTo(px(i), py(v)); });
    ctx.lineTo(px(cntRecpt.length - 1), ax.m.t + ax.ph);
    ctx.closePath();
    ctx.globalAlpha = 0.14;
    ctx.fillStyle = ACCENT;
    ctx.fill();
    ctx.globalAlpha = 1;

    ctx.beginPath();                                     // the line itself
    cntRecpt.forEach(function (v, i) {
      if (i === 0) ctx.moveTo(px(i), py(v)); else ctx.lineTo(px(i), py(v));
    });
    ctx.strokeStyle = ACCENT;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    ctx.font = "9.5px " + FONT;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    cntRecpt.forEach(function (v, i) {                   // points + count labels
      if (v <= 0) return;
      ctx.beginPath();
      ctx.arc(px(i), py(v), 3, 0, Math.PI * 2);
      ctx.fillStyle = ACCENT;
      ctx.fill();
      if (ax.slot >= 20) {
        ctx.fillStyle = MUTED;
        ctx.fillText(String(v), px(i), py(v) - 7);
      }
    });
  })();

  /* 4) top-5 drivers and issuers by lead count — horizontal bars ─────────── */
  function topCard(title, entries, color, emptyMsg) {
    var c = card(6, title, "by lead count");
    grid.appendChild(c);
    var box = c.querySelector(".kc-canvasbox");
    if (!entries.length) {
      box.innerHTML = '<div class="kc-empty">' + _esc(emptyMsg) + "</div>";
      return;
    }
    var rowH = 40, padT = 4;
    var cv = canvasIn(box, padT + entries.length * rowH);
    var ctx = cv.ctx, w = cv.w;
    var maxN = entries[0].n || 1;
    entries.forEach(function (e, i) {
      var y = padT + i * rowH;
      ctx.font = "600 12px " + FONT;
      ctx.textBaseline = "alphabetic";
      ctx.textAlign = "right";                            // count, right edge
      ctx.fillStyle = INK;
      var countTxt = String(e.n);
      ctx.fillText(countTxt, w - 2, y + 13);
      var countW = ctx.measureText(countTxt).width + 10;
      ctx.textAlign = "left";                             // name, truncated
      ctx.fillText(truncate(ctx, e.name, w - countW - 6), 2, y + 13);
      ctx.fillStyle = LINE;                               // track
      roundRect(ctx, 2, y + 20, w - 4, 8, 4);
      ctx.globalAlpha = 0.55;
      ctx.fill();
      ctx.globalAlpha = 1;
      var bw = Math.max(8, (w - 4) * (e.n / maxN));       // bar
      ctx.fillStyle = color;
      roundRect(ctx, 2, y + 20, bw, 8, 4);
      ctx.fill();
    });
  }
  topCard("Top drivers", drivers, ACCENT, "No drivers assigned yet.");
  topCard("Top issuers", issuers, PAID, "No issuers recorded yet.");

  /* ── keep it responsive: re-render (same rows) when the width changes ─── */
  el.__kcRows = rows;
  if (el.__kcRO) { el.__kcRO.disconnect(); el.__kcRO = null; }
  if (typeof ResizeObserver === "function") {
    var lastW = el.clientWidth;
    var pending = false;
    var ro = new ResizeObserver(function () {
      var cw = el.clientWidth;
      if (pending || !cw || Math.abs(cw - lastW) < 9) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        lastW = el.clientWidth;
        renderChartView(el.__kcRows, el);
      });
    });
    ro.observe(el);
    el.__kcRO = ro;
  }
}

''',
    'crm.js': r'''
/* crm.js — the CRM pipeline view: one column per status, cards drag between
 * them. Monday/Trello-shaped, but drawn with the receipts board's own palette:
 * every color is read from the page's CSS variables at render time, nothing
 * hardcoded, so light and dark themes both just work.
 *
 * Defines ONE global:
 *   renderCrmView(rows, el, api)
 *     rows — the board's transmissions array
 *     el   — container element to render into (fully re-rendered each call)
 *     api  — {LABELS, STATUSES, onStatusChange(lead_id, nextStatus), onOpen(lead_id)}
 *
 * Uses the page helpers: esc, when, moneyNum. No top-level side effects —
 * everything (CSS injection included) happens inside renderCrmView.
 */
function renderCrmView(rows, el, api) {
  "use strict";
  rows = Array.isArray(rows) ? rows : [];
  const LABELS = (api && api.LABELS) || {};
  const STATUSES = (api && api.STATUSES) || [];

  /* ── Theme: resolve the status palette from the page's CSS variables ───── */
  const cs = getComputedStyle(document.documentElement);
  const cssVar = (name) => (cs.getPropertyValue(name) || "").trim();
  const VARMAP = {
    new: "--new", followup: "--followup", tag_issued: "--issued",
    tag_emailed: "--emailed", tag_printed: "--printed",
    on_the_way: "--otw", delivered: "--del", receipt_uploaded: "--uploaded",
  };
  const fallbackColor = cssVar("--muted") || cssVar("--accent");
  const color = (s) => cssVar(VARMAP[s] || "") || fallbackColor;

  /* ── Small formatters ──────────────────────────────────────────────────── */
  const fmtMoney = (n) => "$" + Math.round(n).toLocaleString("en-US");
  function daysIn(r) {
    const iso = r.status_updated_at || r.created_at;
    if (!iso) return null;
    const d = new Date(iso);
    if (isNaN(d)) return null;
    return Math.max(0, Math.floor((Date.now() - d.getTime()) / 86400000));
  }

  /* ── Structural CSS, once. Layout colors stay as var(--…) so a theme flip
     restyles the board live; only the status accents are resolved inline. ── */
  if (!document.getElementById("crm-view-css")) {
    const st = document.createElement("style");
    st.id = "crm-view-css";
    st.textContent = `
      .crm-board { display:flex; gap:12px; overflow-x:auto; align-items:flex-start;
                   padding:4px 2px 16px; }
      .crm-col { flex:0 0 270px; width:270px; display:flex; flex-direction:column;
                 background:var(--soft); border:1px solid var(--line);
                 border-top:3px solid var(--line); border-radius:10px;
                 max-height:calc(100vh - 210px); min-height:180px; }
      .crm-col.crm-drop { border-color:var(--accent); background:var(--card); }
      .crm-colhead { display:flex; align-items:baseline; gap:7px;
                     padding:10px 12px 8px; }
      .crm-coltitle { font-size:11px; font-weight:700; letter-spacing:.06em;
                      text-transform:uppercase; color:var(--ink);
                      white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .crm-count { font-size:11px; font-weight:650; color:var(--muted);
                   background:var(--card); border:1px solid var(--line);
                   border-radius:10px; padding:0 7px; line-height:17px; }
      .crm-spacer { flex:1; }
      .crm-sum { font-size:12px; font-weight:700; color:var(--muted);
                 font-variant-numeric:tabular-nums; white-space:nowrap; }
      .crm-cards { flex:1; overflow-y:auto; display:flex; flex-direction:column;
                   gap:8px; padding:2px 8px 10px; scrollbar-width:thin; }
      .crm-card { background:var(--card); border:1px solid var(--line);
                  border-radius:8px; padding:9px 11px 8px; cursor:pointer;
                  box-shadow:0 1px 2px rgba(9,30,66,.08); user-select:none; }
      .crm-card:hover { border-color:var(--accent); }
      .crm-card.crm-dragging { opacity:.45; }
      .crm-c1 { display:flex; justify-content:space-between; gap:8px;
                align-items:baseline; }
      .crm-name { font-weight:650; color:var(--ink); min-width:0;
                  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .crm-price { font-weight:700; color:var(--ink); white-space:nowrap;
                   font-variant-numeric:tabular-nums; }
      .crm-price.crm-none, .crm-driver.crm-none { color:var(--muted); font-weight:600; }
      .crm-ref { font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
                 font-size:11px; color:var(--muted); margin-top:1px; }
      .crm-driver { font-size:12px; color:var(--muted); margin-top:5px;
                    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .crm-driver b { color:var(--ink); font-weight:600; }
      .crm-foot { display:flex; justify-content:space-between; align-items:center;
                  gap:8px; margin-top:8px; }
      .crm-chip { display:inline-flex; align-items:center; gap:5px;
                  border:1px solid var(--line); background:var(--soft);
                  color:var(--ink); border-radius:20px; padding:2px 9px;
                  font:inherit; font-size:11px; font-weight:650; cursor:pointer;
                  max-width:170px; }
      .crm-chip:hover { border-color:var(--accent); color:var(--accent); }
      .crm-chip .crm-lbl { overflow:hidden; text-overflow:ellipsis;
                           white-space:nowrap; }
      .crm-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto;
                 display:inline-block; }
      .crm-days { font-size:11px; font-weight:650; color:var(--muted);
                  white-space:nowrap; }
      .crm-days.crm-old { color:var(--bad-ink); }
      .crm-empty { border:1px dashed var(--line); border-radius:8px;
                   color:var(--muted); font-size:12px; text-align:center;
                   padding:22px 10px; margin-top:2px; }
      .crm-menu { position:fixed; z-index:70; min-width:180px; padding:4px;
                  background:var(--card); border:1px solid var(--line);
                  border-radius:9px; box-shadow:0 10px 30px rgba(0,0,0,.22);
                  display:flex; flex-direction:column; }
      .crm-menu button { display:flex; align-items:center; gap:8px;
                         border:0; background:transparent; color:var(--ink);
                         font:inherit; font-size:12.5px; font-weight:600;
                         padding:6px 9px; border-radius:6px; cursor:pointer;
                         text-align:left; }
      .crm-menu button:hover { background:var(--soft); }
      .crm-menu button.crm-cur { opacity:.5; cursor:default; }
      .crm-menu button.crm-cur:hover { background:transparent; }
    `;
    document.head.appendChild(st);
  }

  /* ── Group the rows: one bucket per status, unknown statuses fall into the
     first column so nothing silently disappears (dragging them out heals). ── */
  const byStatus = {};
  STATUSES.forEach((s) => { byStatus[s] = []; });
  rows.forEach((r) => {
    (byStatus[r.status] || byStatus[STATUSES[0]] || (byStatus._orphan = byStatus._orphan || [])).push(r);
  });

  /* ── Render ────────────────────────────────────────────────────────────── */
  function cardHtml(r) {
    const d = daysIn(r);
    const dLabel = d == null ? "" : (d === 0 ? "today" : d + "d");
    const since = r.status_updated_at || r.created_at;
    const dTitle = d == null ? "" :
      `${d === 0 ? "Entered this stage today" : d + " day" + (d === 1 ? "" : "s") + " in this stage"} (since ${when(since)})`;
    const driver = r.driver_name && r.driver_name !== "—" ? r.driver_name : "";
    const price = r.price && r.price !== "—" ? r.price : "";
    return `<div class="crm-card" draggable="true" data-id="${esc(r.lead_id)}"
        title="Click to open · drag to move">
      <div class="crm-c1">
        <span class="crm-name" title="${esc(r.client_name)}">${esc(r.client_name)}</span>
        <span class="crm-price ${price ? "" : "crm-none"}">${price ? esc(price) : "—"}</span>
      </div>
      <div class="crm-ref">${esc(r.reference_id)}</div>
      <div class="crm-driver ${driver ? "" : "crm-none"}">${
        driver ? `🚗 <b>${esc(driver)}</b>` : "🚗 no driver yet"}</div>
      <div class="crm-foot">
        <button class="crm-chip" data-id="${esc(r.lead_id)}" data-s="${esc(r.status)}"
            title="Change status">
          <span class="crm-dot" style="background:${esc(color(r.status))}"></span>
          <span class="crm-lbl">${esc(LABELS[r.status] || r.status)}</span>
        </button>
        ${dLabel ? `<span class="crm-days ${d >= 5 ? "crm-old" : ""}"
            title="${esc(dTitle)}">⏱ ${esc(dLabel)}</span>` : ""}
      </div>
    </div>`;
  }

  function colHtml(s) {
    const inCol = byStatus[s] || [];
    const sum = inCol.reduce((t, r) => t + moneyNum(r.price), 0);
    return `<div class="crm-col" data-status="${esc(s)}"
        style="border-top-color:${esc(color(s))}">
      <div class="crm-colhead">
        <span class="crm-coltitle" title="${esc(LABELS[s] || s)}">${esc(LABELS[s] || s)}</span>
        <span class="crm-count">${inCol.length}</span>
        <span class="crm-spacer"></span>
        <span class="crm-sum" title="Total value in this stage">${esc(fmtMoney(sum))}</span>
      </div>
      <div class="crm-cards">${
        inCol.length ? inCol.map(cardHtml).join("")
                     : `<div class="crm-empty">Nothing here —<br>drag a card in</div>`
      }</div>
    </div>`;
  }

  el.innerHTML = `<div class="crm-board">${STATUSES.map(colHtml).join("")}</div>`;

  /* ── Status menu (the no-mouse-drag fallback) ──────────────────────────── */
  function closeMenus() {
    el.querySelectorAll(".crm-menu").forEach((m) => m.remove());
  }
  function openMenu(chip) {
    closeMenus();
    const id = chip.dataset.id, cur = chip.dataset.s;
    const menu = document.createElement("div");
    menu.className = "crm-menu";
    menu.innerHTML = STATUSES.map((s) =>
      `<button data-id="${esc(id)}" data-s="${esc(s)}"
           class="${s === cur ? "crm-cur" : ""}">
         <span class="crm-dot" style="background:${esc(color(s))}"></span>
         ${esc(LABELS[s] || s)}${s === cur ? " ✓" : ""}
       </button>`).join("");
    el.appendChild(menu);
    const r = chip.getBoundingClientRect();
    const mw = menu.offsetWidth, mh = menu.offsetHeight;
    let left = Math.min(r.left, window.innerWidth - mw - 8);
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, r.top - mh - 4);
    menu.style.left = Math.max(8, left) + "px";
    menu.style.top = top + "px";
  }

  // One outside-close handler, replaced on every render so it never stacks.
  if (el.__crmDocClose) {
    document.removeEventListener("click", el.__crmDocClose, true);
    document.removeEventListener("scroll", el.__crmDocClose, true);
    document.removeEventListener("keydown", el.__crmDocClose, true);
  }
  el.__crmDocClose = (ev) => {
    if (ev.type === "keydown") { if (ev.key === "Escape") closeMenus(); return; }
    if (ev.type === "scroll") { closeMenus(); return; }
    if (!(ev.target.closest && ev.target.closest(".crm-menu, .crm-chip"))) closeMenus();
  };
  document.addEventListener("click", el.__crmDocClose, true);
  document.addEventListener("scroll", el.__crmDocClose, true);
  document.addEventListener("keydown", el.__crmDocClose, true);

  /* ── Clicks: menu option → status change; chip → menu; card body → open.
     Direct property assignment, so re-renders never double the listeners. ── */
  el.onclick = (e) => {
    const opt = e.target.closest(".crm-menu button");
    if (opt) {
      const id = opt.dataset.id, s = opt.dataset.s;
      const wasCurrent = opt.classList.contains("crm-cur");
      closeMenus();
      if (!wasCurrent && api.onStatusChange) api.onStatusChange(id, s);
      return;
    }
    const chip = e.target.closest(".crm-chip");
    if (chip) { openMenu(chip); return; }
    closeMenus();
    const card = e.target.closest(".crm-card");
    if (card && api.onOpen) api.onOpen(card.dataset.id);
  };

  /* ── Drag and drop between columns ─────────────────────────────────────── */
  el.ondragstart = (e) => {
    const card = e.target.closest ? e.target.closest(".crm-card") : null;
    if (!card) return;
    closeMenus();
    e.dataTransfer.setData("text/plain", card.dataset.id);
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("crm-dragging");
  };
  el.ondragend = () => {
    el.querySelectorAll(".crm-dragging").forEach((c) => c.classList.remove("crm-dragging"));
    el.querySelectorAll(".crm-drop").forEach((c) => c.classList.remove("crm-drop"));
  };
  el.ondragover = (e) => {
    const col = e.target.closest ? e.target.closest(".crm-col") : null;
    if (!col) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    if (!col.classList.contains("crm-drop")) {
      el.querySelectorAll(".crm-drop").forEach((c) => c.classList.remove("crm-drop"));
      col.classList.add("crm-drop");
    }
  };
  el.ondragleave = (e) => {
    const col = e.target.closest ? e.target.closest(".crm-col") : null;
    if (col && !(e.relatedTarget && col.contains(e.relatedTarget)))
      col.classList.remove("crm-drop");
  };
  el.ondrop = (e) => {
    const col = e.target.closest ? e.target.closest(".crm-col") : null;
    if (!col) return;
    e.preventDefault();
    col.classList.remove("crm-drop");
    const id = e.dataTransfer.getData("text/plain");
    if (!id) return;
    const row = rows.find((r) => String(r.lead_id) === String(id));
    const next = col.dataset.status;
    if (row && next && row.status !== next && api.onStatusChange)
      api.onStatusChange(row.lead_id, next);
  };
}

''',
    'voice.js': r'''
/* voice.js — floating voice-command control for the receipts board.
 *
 * One global: initVoice(opts)
 *   opts = {
 *     endpoint:   POST target, default "/receipts/api/voice"
 *     getContext: () => ({view, theme, ...}) merged into every request body
 *     onAction:   (action, args) => void — called IMMEDIATELY on a response
 *                 that carries an action, before any speech finishes
 *   }
 *
 * Classic script, no modules, no top-level side effects: nothing happens
 * until initVoice() is called. All colors are read from the page's CSS
 * variables (--accent, --card, --ink, --muted, --line, --soft, --bad-bg,
 * --bad-ink) via getComputedStyle at render time, so the control follows
 * the board's light/dark theme. All dynamic text lands via textContent —
 * never innerHTML — so row/transcript/server data cannot inject markup.
 */
function initVoice(opts) {
  "use strict";
  opts = opts || {};
  var endpoint = opts.endpoint || "/receipts/api/voice";
  var getContext = (typeof opts.getContext === "function") ? opts.getContext : function () { return {}; };
  var onAction = (typeof opts.onAction === "function") ? opts.onAction : function () {};

  // ── theme: every color is read from the page's variables at render time ──
  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      v = (v || "").trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  // ── structural styles (layout, sizes, animation only — no colors) ────────
  if (!document.getElementById("vc-style")) {
    var st = document.createElement("style");
    st.id = "vc-style";
    st.textContent = [
      ".vc-root{position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:70;",
      "  display:flex;flex-direction:column;align-items:center;gap:10px;",
      "  font:14px/1.45 -apple-system,system-ui,'Segoe UI',sans-serif;}",
      ".vc-mic{position:relative;width:52px;height:52px;border-radius:50%;border:0;cursor:pointer;",
      "  font-size:22px;display:flex;align-items:center;justify-content:center;",
      "  box-shadow:0 6px 18px rgba(0,0,0,.25);transition:transform .15s ease;}",
      ".vc-mic:hover{transform:scale(1.06);}",
      ".vc-mic:active{transform:scale(.96);}",
      ".vc-ring{position:absolute;inset:-4px;border-radius:50%;border:3px solid transparent;",
      "  opacity:0;pointer-events:none;}",
      ".vc-root.listening .vc-ring{opacity:1;animation:vc-pulse 1.4s ease-out infinite;}",
      "@keyframes vc-pulse{0%{transform:scale(.9);opacity:.9}70%{transform:scale(1.45);opacity:0}100%{opacity:0}}",
      ".vc-spin{width:20px;height:20px;border-radius:50%;border:3px solid transparent;",
      "  animation:vc-rot .8s linear infinite;display:none;}",
      ".vc-root.thinking .vc-spin{display:block;}",
      ".vc-root.thinking .vc-face{display:none;}",
      "@keyframes vc-rot{to{transform:rotate(360deg)}}",
      ".vc-root.speaking .vc-face{animation:vc-talk 1s ease-in-out infinite;}",
      "@keyframes vc-talk{0%,100%{transform:scale(1)}50%{transform:scale(1.14)}}",
      ".vc-bubble{position:relative;width:min(340px,calc(100vw - 32px));border-radius:12px;",
      "  padding:12px 34px 12px 14px;font-size:13.5px;box-shadow:0 10px 30px rgba(0,0,0,.22);}",
      ".vc-bubble[hidden]{display:none;}",
      ".vc-x{position:absolute;top:6px;right:6px;width:24px;height:24px;border:0;border-radius:6px;",
      "  background:transparent;cursor:pointer;font-size:13px;line-height:1;}",
      ".vc-interim{font-style:italic;}",
      ".vc-input{width:100%;margin-top:8px;padding:8px 11px;border-radius:8px;",
      "  font:inherit;box-sizing:border-box;}",
      ".vc-input[hidden]{display:none;}",
      ".vc-hist{width:min(300px,calc(100vw - 40px));border-radius:10px;padding:6px 0;",
      "  box-shadow:0 8px 24px rgba(0,0,0,.2);display:none;}",
      ".vc-root:hover .vc-hist.has{display:block;}",
      ".vc-root.listening .vc-hist,.vc-root.thinking .vc-hist{display:none!important;}",
      ".vc-hist-title{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;",
      "  padding:3px 12px 5px;font-weight:650;}",
      ".vc-hist-item{display:block;width:100%;text-align:left;border:0;background:transparent;",
      "  font:inherit;font-size:12.5px;padding:6px 12px;cursor:pointer;white-space:nowrap;",
      "  overflow:hidden;text-overflow:ellipsis;}",
      ".vc-kbd{font-size:10.5px;text-align:center;margin-top:2px;user-select:none;",
      "  pointer-events:none;opacity:0;transition:opacity .2s;}",
      ".vc-root:hover .vc-kbd{opacity:1;}",
      "@media (max-width:860px){.vc-root{bottom:14px;}.vc-kbd{display:none;}}",
    ].join("\n");
    document.head.appendChild(st);
  }

  // ── skeleton (static markup only; all dynamic text goes in via textContent)
  var prev = document.querySelector(".vc-root");
  if (prev && prev.__vcDestroy) prev.__vcDestroy();

  var root = document.createElement("div");
  root.className = "vc-root";
  root.innerHTML =
    '<div class="vc-hist" role="menu">' +
      '<div class="vc-hist-title">Recent commands</div>' +
      '<div class="vc-hist-items"></div>' +
    '</div>' +
    '<div class="vc-bubble" hidden>' +
      '<button class="vc-x" type="button" title="Dismiss" aria-label="Dismiss">✕</button>' +
      '<div class="vc-text" aria-live="polite"></div>' +
      '<input class="vc-input" type="text" hidden placeholder="type a command…" autocomplete="off">' +
    '</div>' +
    '<button class="vc-mic" type="button" title="Voice command (press v)" aria-label="Voice command">' +
      '<span class="vc-ring"></span>' +
      '<span class="vc-spin"></span>' +
      '<span class="vc-face">🎤</span>' +
    '</button>' +
    '<div class="vc-kbd">press <b>v</b> to talk</div>';
  document.body.appendChild(root);

  var mic = root.querySelector(".vc-mic");
  var ring = root.querySelector(".vc-ring");
  var spin = root.querySelector(".vc-spin");
  var face = root.querySelector(".vc-face");
  var bubble = root.querySelector(".vc-bubble");
  var bubbleText = root.querySelector(".vc-text");
  var closeBtn = root.querySelector(".vc-x");
  var input = root.querySelector(".vc-input");
  var hist = root.querySelector(".vc-hist");
  var histTitle = root.querySelector(".vc-hist-title");
  var histItems = root.querySelector(".vc-hist-items");
  var kbd = root.querySelector(".vc-kbd");

  // ── paint: applies theme colors, called at every render / theme flip ─────
  function paint() {
    var accent = cssVar("--accent", "#0065ff");
    var card = cssVar("--card", "#fff");
    var ink = cssVar("--ink", "#172b4d");
    var muted = cssVar("--muted", "#6b778c");
    var line = cssVar("--line", "#dfe1e6");
    var soft = cssVar("--soft", "#f8f9fb");
    mic.style.background = accent;
    mic.style.color = "#fff";                 // same as the page's .btn.primary
    ring.style.borderColor = accent;
    spin.style.borderColor = line;
    spin.style.borderTopColor = "#fff";
    bubble.style.background = card;
    bubble.style.border = "1px solid " + line;
    bubble.style.color = ink;
    closeBtn.style.color = muted;
    input.style.background = cssVar("--bg", "#f4f5f7");
    input.style.border = "1px solid " + line;
    input.style.color = ink;
    hist.style.background = card;
    hist.style.border = "1px solid " + line;
    histTitle.style.color = muted;
    histItems.querySelectorAll(".vc-hist-item").forEach(function (b) { b.style.color = ink; });
    kbd.style.color = muted;
    // hover affordance on history rows without a stylesheet color
    histItems.querySelectorAll(".vc-hist-item").forEach(function (b) {
      b.onmouseenter = function () { b.style.background = soft; };
      b.onmouseleave = function () { b.style.background = "transparent"; };
    });
  }

  // ── state machine ────────────────────────────────────────────────────────
  var state = "idle"; // idle | listening | thinking | speaking
  function setState(next) {
    state = next;
    root.classList.remove("idle", "listening", "thinking", "speaking");
    root.classList.add(next);
    face.textContent = next === "speaking" ? "🔊" : "🎤";
    mic.title = next === "listening" ? "Listening… (click or press v to stop)"
      : next === "thinking" ? "Working on it…"
      : next === "speaking" ? "Speaking — click to interrupt"
      : "Voice command (press v)";
    paint();
  }

  function showBubble(text, kind) {  // kind: "" | "interim" | "error" | "say"
    bubble.hidden = false;
    bubbleText.textContent = "";
    if (kind === "interim") {
      var i = document.createElement("span");
      i.className = "vc-interim";
      i.textContent = text;
      i.style.color = cssVar("--muted", "#6b778c");
      bubbleText.appendChild(i);
    } else {
      bubbleText.textContent = text;
    }
    if (kind === "error") {
      bubble.style.background = cssVar("--bad-bg", "#ffebe6");
      bubble.style.borderColor = cssVar("--bad-bg", "#ffebe6");
      bubbleText.style.color = cssVar("--bad-ink", "#bf2600");
    } else {
      paint();
      bubbleText.style.color = "";
    }
  }
  function showListening(finalPart, interimPart) {
    bubble.hidden = false;
    bubbleText.textContent = "";
    if (finalPart) {
      var f = document.createElement("span");
      f.textContent = finalPart + " ";
      bubbleText.appendChild(f);
    }
    var i = document.createElement("span");
    i.className = "vc-interim";
    i.style.color = cssVar("--muted", "#6b778c");
    i.textContent = interimPart || (finalPart ? "" : "Listening…");
    bubbleText.appendChild(i);
    paint();
  }
  function hideBubble() {
    bubble.hidden = true;
    input.hidden = true;
  }

  // ── command history (last 5, newest first) ───────────────────────────────
  var history = [];
  function pushHistory(text) {
    text = String(text || "").trim();
    if (!text) return;
    history = [text].concat(history.filter(function (t) { return t !== text; })).slice(0, 5);
    histItems.textContent = "";
    history.forEach(function (t) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "vc-hist-item";
      b.textContent = "“" + t + "”";
      b.title = "Run again";
      b.onclick = function () { submit(t); };
      histItems.appendChild(b);
    });
    hist.classList.toggle("has", history.length > 0);
    paint();
  }

  // ── speech synthesis ─────────────────────────────────────────────────────
  var cachedVoice = null;
  function pickVoice() {
    try {
      var voices = window.speechSynthesis.getVoices() || [];
      var en = voices.filter(function (v) { return /^en([-_]|$)/i.test(v.lang || ""); });
      var natural = en.find(function (v) { return /natural|neural|premium|enhanced|google|samantha|aria|zira/i.test(v.name || ""); });
      cachedVoice = natural || en[0] || null;
      return cachedVoice;
    } catch (e) { return null; }
  }
  function speak(text) {
    if (!("speechSynthesis" in window)) { setState("idle"); return; }
    try {
      window.speechSynthesis.cancel();     // never talk over ourselves
      var u = new SpeechSynthesisUtterance(text);
      u.rate = 1.02;
      u.lang = "en-US";
      var v = cachedVoice || pickVoice();
      if (v) u.voice = v;
      u.onstart = function () { setState("speaking"); };
      u.onend = function () { if (state === "speaking") setState("idle"); };
      u.onerror = function () { if (state === "speaking") setState("idle"); };
      setState("speaking");                // some browsers delay onstart
      window.speechSynthesis.speak(u);
    } catch (e) { setState("idle"); }
  }
  function stopSpeaking() {
    try { if ("speechSynthesis" in window) window.speechSynthesis.cancel(); } catch (e) {}
  }

  // ── the shared flow: text in → endpoint → say/action out ────────────────
  var inFlight = null;
  var inFlightCtrl = null;
  function abortInFlight() {
    var c = inFlightCtrl;
    inFlight = null;                       // drop the late response first…
    inFlightCtrl = null;
    if (c) { try { c.abort(); } catch (e) {} }  // …then cancel the request
  }
  function submit(text) {
    text = String(text || "").trim();
    if (!text) return;
    stopSpeaking();
    setState("thinking");
    showBubble("“" + text + "” …");
    pushHistory(text);
    var body = {};
    try {
      var ctx = getContext() || {};
      for (var k in ctx) if (Object.prototype.hasOwnProperty.call(ctx, k)) body[k] = ctx[k];
    } catch (e) { /* context is best-effort */ }
    body.text = text;
    var ctrl = (typeof AbortController === "function") ? new AbortController() : null;
    inFlightCtrl = ctrl;
    var timedOut = false;
    var timer = ctrl ? setTimeout(function () {
      timedOut = true;
      try { ctrl.abort(); } catch (e) {}
    }, 20000) : null;
    var mine = inFlight = fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ctrl ? ctrl.signal : undefined,
    }).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (t) {
          var msg = "HTTP " + res.status;
          try { var j = JSON.parse(t); if (j && j.error) msg = j.error; } catch (e2) {}
          throw new Error(msg);
        });
      }
      return res.json();
    }).then(function (data) {
      if (timer) clearTimeout(timer);
      if (mine !== inFlight) return;       // superseded or dismissed
      inFlight = null;
      inFlightCtrl = null;
      data = data || {};
      if (data.action) {
        // the board reacts NOW — speech is commentary, not a gate
        try { onAction(data.action, data.args); } catch (e) { /* board's problem, not ours */ }
      }
      if (data.say) {
        showBubble(data.say, "say");
        speak(data.say);
      } else {
        setState("idle");
        if (data.action) showBubble("Done — " + String(data.action).replace(/_/g, " "));
        else hideBubble();
      }
    }).catch(function (err) {
      if (timer) clearTimeout(timer);
      if (mine !== inFlight) return;       // superseded, dismissed, or user-aborted
      inFlight = null;
      inFlightCtrl = null;
      setState("idle");                    // never speak an error, only show it
      if (timedOut || (err && err.name === "AbortError")) {
        showBubble("That took too long — the request timed out. Try again.", "error");
      } else {
        showBubble("Couldn’t do that: " + (err && err.message ? err.message : "network error"), "error");
      }
    });
  }

  // ── speech recognition (with typed fallback) ─────────────────────────────
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  var rec = null;
  var micDenied = false;

  function showTypedFallback(note) {
    setState("idle");
    bubble.hidden = false;
    bubbleText.textContent = note || "Voice input isn’t available in this browser — type a command instead.";
    paint();
    input.hidden = false;
    input.value = "";
    input.focus();
  }

  function startListening() {
    stopSpeaking();
    if (!SR || micDenied) {
      showTypedFallback(micDenied
        ? "Microphone access is blocked — allow the mic in your browser’s site settings, or type a command:"
        : null);
      return;
    }
    try {
      rec = new SR();
      rec.lang = "en-US";
      rec.interimResults = true;
      rec.continuous = false;
      var finals = "";
      rec.onresult = function (ev) {
        var interim = "";
        for (var i = ev.resultIndex; i < ev.results.length; i++) {
          var chunk = ev.results[i][0] ? ev.results[i][0].transcript : "";
          if (ev.results[i].isFinal) finals += chunk;
          else interim += chunk;
        }
        showListening(finals.trim(), interim.trim());
      };
      rec.onerror = function (ev) {
        var code = ev && ev.error;
        if (code === "not-allowed" || code === "service-not-allowed") {
          micDenied = true;
          showTypedFallback("Microphone access is blocked — allow the mic in your browser’s site settings, or type a command:");
        } else if (code === "no-speech") {
          setState("idle");
          showBubble("Didn’t catch anything — tap the mic and try again.");
        } else if (code !== "aborted") {
          setState("idle");
          showBubble("Voice input error: " + (code || "unknown"), "error");
        } else {
          setState("idle");
        }
      };
      rec.onend = function () {
        var text = finals.trim();
        if (state === "listening") {
          if (text) submit(text);
          else { setState("idle"); hideBubble(); }
        }
      };
      setState("listening");
      showListening("", "");
      rec.start();
    } catch (e) {
      setState("idle");
      showBubble("Could not start the microphone: " + (e && e.message ? e.message : e), "error");
    }
  }

  function stopListening() {
    if (rec) { try { rec.stop(); } catch (e) {} }
    // onend fires and either submits the final transcript or goes idle
  }

  function toggle() {
    if (state === "listening") { stopListening(); return; }
    if (state === "thinking") {            // cancel the in-flight command
      abortInFlight();
      setState("idle");
      hideBubble();
      return;
    }
    if (state === "speaking") { stopSpeaking(); setState("idle"); return; }
    startListening();
  }

  // ── wiring ───────────────────────────────────────────────────────────────
  mic.addEventListener("click", toggle);
  closeBtn.addEventListener("click", function (ev) {
    ev.stopPropagation();
    stopSpeaking();
    if (state === "listening" && rec) { try { rec.abort(); } catch (e) {} }
    inFlight = null;                       // a late response must not fire onAction/speak
    setState("idle");
    hideBubble();
  });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") {
      var t = input.value.trim();
      input.value = "";
      input.hidden = true;
      if (t) submit(t);
      else hideBubble();
    } else if (ev.key === "Escape") {
      hideBubble();
    }
    ev.stopPropagation();
  });

  function isTyping(el) {
    if (!el) return false;
    var tag = (el.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }
  function onKey(ev) {
    if (ev.key !== "v" && ev.key !== "V") return;
    if (ev.ctrlKey || ev.metaKey || ev.altKey || ev.repeat) return;
    if (isTyping(ev.target)) return;
    ev.preventDefault();
    toggle();
  }
  document.addEventListener("keydown", onKey);

  // follow OS theme flips live (the page has no manual toggle)
  var mq = null, onTheme = function () { paint(); };
  try {
    mq = window.matchMedia("(prefers-color-scheme: dark)");
    if (mq.addEventListener) mq.addEventListener("change", onTheme);
    else if (mq.addListener) mq.addListener(onTheme);
  } catch (e) {}

  // follow the page's own theme attribute (the picker sets data-theme)
  var themeObserver = null;
  try {
    themeObserver = new MutationObserver(onTheme);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
  } catch (e) {}

  // voices often load async — warm the cached voice pick on availability
  var onVoices = function () { pickVoice(); };
  try {
    if ("speechSynthesis" in window && window.speechSynthesis.addEventListener) {
      window.speechSynthesis.addEventListener("voiceschanged", onVoices);
    }
  } catch (e) {}

  setState("idle");

  function destroy() {
    document.removeEventListener("keydown", onKey);
    try {
      if (mq) {
        if (mq.removeEventListener) mq.removeEventListener("change", onTheme);
        else if (mq.removeListener) mq.removeListener(onTheme);
      }
    } catch (e) {}
    try { if (themeObserver) themeObserver.disconnect(); } catch (e) {}
    try {
      if ("speechSynthesis" in window && window.speechSynthesis.removeEventListener) {
        window.speechSynthesis.removeEventListener("voiceschanged", onVoices);
      }
    } catch (e) {}
    stopSpeaking();
    if (rec) { try { rec.abort(); } catch (e) {} }
    if (root.parentNode) root.parentNode.removeChild(root);
  }
  root.__vcDestroy = destroy;

  return { toggle: toggle, submit: submit, destroy: destroy };
}

''',
    'tetris.js': r'''
/* tetris.js — pixel Tetris minigame for the /receipts board ("Video Game Mode").
 * Served alongside game_layer.js; the ambient sprite layer drives the
 * sprite-interference API (geometry / blast / nudge) while a human plays.
 *
 * CONTRACT:
 *   - defines exactly ONE global:
 *       window.krabTetris = { start(opts), stop(), pause(on), active(),
 *                            geometry(), blast(cx, cy), nudge(dir), state() }
 *     opts = { onClose?, onEvent?(type, payload) };  nothing else leaks;
 *   - ONE overlay wrapper (#kt-root), position:fixed, centered horizontally;
 *     vertically centered on viewports >= 700px tall, otherwise anchored
 *     96px above the bottom edge (the voice mic button lives bottom-center
 *     and must NEVER be overlapped);  z-index 22 (below the page's modals
 *     at z 50);  pointer-events:auto on the wrapper only — no backdrop;
 *   - SHORT viewports (innerHeight < 480 — phone landscape): the panel drops
 *     the centering entirely and anchors LEFT (left:12px, vertically
 *     centered) so the bottom-center mic stays clear;  the stats row is
 *     hidden and the touch controls become a vertical column to the RIGHT of
 *     the board inside the card, so the whole card fits vh - 16 with the
 *     header on-screen;  re-decided on every resize / orientationchange;
 *   - board 10x18 ALWAYS; cell size fits total width <= min(92vw, 340px) and
 *     total height <= 78vh (short: the vh-16 budget above), floor 8px;
 *     DPR-correct canvas, crisp integer-scaled pixels;
 *   - all colors come from the page theme at render time (--card --line
 *     --ink --muted --accent; blocks from --new --issued --followup
 *     --printed --del --paid --otw; clear/blast flash from --emailed) so
 *     every theme reskins the game;
 *   - fixed-timestep simulation (same accumulator discipline as
 *     game_layer.js); zero per-frame allocations in the render loop;
 *   - stop() is a FULL teardown: RAF, every listener (keyboard, pointer,
 *     resize, visibility, matchMedia coarse/reduced/dppx), timers, DOM —
 *     repeated start/stop leaks nothing;
 *   - prefers-reduced-motion: the 150ms line-clear flash is skipped
 *     (instant collapse) and blast/clear particles are not spawned;
 *   - events emitted via opts.onEvent: 'start' {}, 'lock' {colHeights},
 *     'clear' {lines}, 'over' {score}, 'blast' {cleared}, 'nudge' {dir}.
 *     Payload objects (and geometry()'s return) are REUSED between calls —
 *     read them synchronously, copy if you must keep them.
 */
(function () {
  'use strict';
  if (window.krabTetris) return;                 // double-include guard

  /* ── tuning constants (timing in ms, distances in CSS px) ─────────────── */
  const COLS = 10, ROWS = 18;
  const STEP = 1000 / 60;        // fixed simulation timestep
  const MAX_DT = 50;             // clamp a janky frame; never fast-forward
  const MAX_STEPS = 4;           // spiral-of-death guard
  const GRAV0 = 800;             // ms per row at level 1…
  const GRAV_DEC = 60;           // …minus this per level…
  const GRAV_MIN = 120;          // …floored here
  const SOFT_MS = 40;            // soft-drop gravity while held
  const LOCK_MS = 400;           // lock delay, refreshed by movement…
  const LOCK_CAP = 2000;         // …but total grounded time caps here
  const FLASH_MS = 150;          // line-clear flash before collapse
  const BLAST_MS = 220;          // blast cell flash
  const CLEAR_SCORE = [0, 100, 300, 500, 800];   // x level
  const KICKS = [0, -1, 1, -2, 2];               // simple wall-kick ladder
  const HS_KEY = 'krab_tetris_hs';
  const P_MAX = 48;              // particle pool hard cap
  const MIC_CLEAR = 96;          // bottom clearance for the voice mic button
  const NEXT_M = 9;              // next-preview mini-cell (CSS px)
  const Z_PANEL = 22;            // below the page's modals at z 50
  const CHROME_FINE = 108;       // panel chrome height minus board (est.)
  const CHROME_COARSE = 168;     // …with the touch control row
  const CHROME_SHORT = 66;       // landscape: padding 20 + border 2 + header 44
  const SIDE_COL = 52;           // landscape: 44px touch column + 8px gap
  const SHORT_VH = 480;          // below this the panel goes landscape mode

  function rand(a, b) { return a + Math.random() * (b - a); }

  /* ── theme — colors from the page's CSS variables, refreshed at most every
   * 500ms OUTSIDE the hot loops (getPropertyValue allocates strings).  The
   * block-tile atlas is rebuilt only when a color actually changed. ──────── */
  const THEME = { card: '#ffffff', line: '#dfe1e6', ink: '#172b4d',
                  muted: '#6b778c', accent: '#0065ff', flash: '#00b8d9' };
  // piece order: I J L O S T Z — one status color each, distinct fallbacks
  const PIECE_VARS = ['--new', '--issued', '--followup', '--printed', '--del', '--paid', '--otw'];
  const COLORS = ['#0065ff', '#8993a4', '#e2a33b', '#6554c0', '#00875a', '#e2447d', '#ff7452'];
  let themeAt = -1, tilesDirty = true;
  function cssVar(cs, n, fb) { const s = cs.getPropertyValue(n); const t = s && s.trim(); return t || fb; }
  function refreshTheme(now) {
    if (now - themeAt < 500) return;
    themeAt = now;
    let cs;
    try { cs = getComputedStyle(document.documentElement); } catch (e) { return; }
    const pl = THEME.line, pf = THEME.flash;
    THEME.card = cssVar(cs, '--card', THEME.card);
    THEME.line = cssVar(cs, '--line', THEME.line);
    THEME.ink = cssVar(cs, '--ink', THEME.ink);
    THEME.muted = cssVar(cs, '--muted', THEME.muted);
    THEME.accent = cssVar(cs, '--accent', THEME.accent);
    THEME.flash = cssVar(cs, '--emailed', THEME.flash);
    for (let i = 0; i < 7; i++) {
      const v = cssVar(cs, PIECE_VARS[i], COLORS[i]);
      if (v !== COLORS[i]) { COLORS[i] = v; tilesDirty = true; }
    }
    if (THEME.line !== pl || THEME.flash !== pf) tilesDirty = true;
  }

  /* ── the 7 tetrominoes — base cells in box coords, 4 rotations generated
   * once at boot ((x,y) -> (box-1-y, x), clockwise).  Flat Int8Arrays. ───── */
  const BASE = [
    { box: 4, cells: [0, 1, 1, 1, 2, 1, 3, 1] },   // I
    { box: 3, cells: [0, 0, 0, 1, 1, 1, 2, 1] },   // J
    { box: 3, cells: [2, 0, 0, 1, 1, 1, 2, 1] },   // L
    { box: 2, cells: [0, 0, 1, 0, 0, 1, 1, 1] },   // O
    { box: 3, cells: [1, 0, 2, 0, 0, 1, 1, 1] },   // S
    { box: 3, cells: [1, 0, 0, 1, 1, 1, 2, 1] },   // T
    { box: 3, cells: [0, 0, 1, 0, 1, 1, 2, 1] },   // Z
  ];
  const ROTS = [];                                 // ROTS[p][r] = Int8Array(8)
  const SPAWN_X = new Int8Array(7), SPAWN_Y = new Int8Array(7);
  (function buildRots() {
    for (let p = 0; p < 7; p++) {
      const box = BASE[p].box, rots = [];
      let c = Int8Array.from(BASE[p].cells);
      for (let r = 0; r < 4; r++) {
        rots.push(c);
        const nx = new Int8Array(8);
        for (let k = 0; k < 8; k += 2) { nx[k] = box - 1 - c[k + 1]; nx[k + 1] = c[k]; }
        c = nx;
      }
      ROTS.push(rots);
      let minY = 9;
      for (let k = 1; k < 8; k += 2) if (rots[0][k] < minY) minY = rots[0][k];
      SPAWN_Y[p] = -minY;                          // topmost cell spawns on row 0
      SPAWN_X[p] = ((COLS - box) / 2) | 0;
    }
  })();

  /* ── board state — everything preallocated at boot ──────────────────────── */
  const board = new Int8Array(COLS * ROWS);        // 0 empty, 1..7 = piece+1
  const blastF = new Float32Array(COLS * ROWS);    // per-cell blast flash timers
  const clearRows = new Int8Array(ROWS);           // rows marked for collapse
  const colHeights = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]; // settled stack height/col
  const cur = { p: 0, r: 0, x: 0, y: 0 };          // the falling piece
  let nextP = 0;
  const bag = new Int8Array(7);                    // 7-bag randomizer
  let bagIx = 7;
  function draw7() {
    if (bagIx >= 7) {
      for (let i = 0; i < 7; i++) bag[i] = i;
      for (let i = 6; i > 0; i--) {
        const j = (Math.random() * (i + 1)) | 0;
        const t = bag[i]; bag[i] = bag[j]; bag[j] = t;
      }
      bagIx = 0;
    }
    return bag[bagIx++];
  }

  /* ── runtime state ──────────────────────────────────────────────────────── */
  let activeFlag = false;
  let score = 0, lines = 0, level = 1, hi = 0;
  let over = false, paused = false;
  let phase = 0;                                   // 0 playing, 1 clearing, 2 over
  let gravIv = GRAV0, gravT = 0, lockT = 0, groundT = 0, clearT = 0, clearCount = 0;
  let kbSoft = false, btnSoft = false;
  function softOn() { return kbSoft || btnSoft; }
  let hudDirty = true, nextDirty = true;
  let REDUCED = false;
  let cbClose = null, cbEvent = null;

  // reused event payloads — read synchronously, copy to keep (see contract)
  const evStart = {};
  const evLock = { colHeights: colHeights };
  const evClear = { lines: 0 };
  const evOver = { score: 0 };
  const evBlast = { cleared: 0 };
  const evNudge = { dir: 0 };
  function emitEv(type, payload) {
    if (!cbEvent) return;
    try { cbEvent(type, payload); } catch (e) {}
  }

  /* ── DOM refs (created in start, destroyed in stop) ─────────────────────── */
  let root = null, boardCv = null, bctx = null, nextCv = null, nctx = null;
  let scoreEl = null, linesEl = null, levelEl = null, hiEl = null;
  let pauseBtn = null, ctrlRow = null, overWrap = null, finalEl = null;
  let statsRow = null, midWrap = null;             // for the landscape re-flow
  let cell = 20, cssW = COLS * 20, cssH = ROWS * 20, DPR = 1;
  let tileCv = null, tileCtx = null, tileD = 0;    // block atlas (kept across restarts)
  let baseCv = null, baseCtx = null;               // board bg + grid, prerendered
  let mqCoarse = null, mqReduced = null, mqDpr = null, armedDpr = 0;
  let raf = 0, lastTs = 0, acc = 0;
  let repTimer = 0;                                // touch-button hold-repeat

  /* ── high score (localStorage can throw; the game shrugs) ───────────────── */
  function readHi() {
    try { const v = parseInt(localStorage.getItem(HS_KEY), 10); return isFinite(v) && v > 0 ? v : 0; }
    catch (e) { return 0; }
  }
  function persistHi() {
    if (score > hi) { hi = score; try { localStorage.setItem(HS_KEY, String(hi)); } catch (e) {} }
  }

  /* ── core mechanics ─────────────────────────────────────────────────────── */
  function fits(p, r, x, y) {
    const t = ROTS[p][r];
    for (let k = 0; k < 8; k += 2) {
      const bx = x + t[k], by = y + t[k + 1];
      if (bx < 0 || bx >= COLS || by >= ROWS) return false;
      if (by >= 0 && board[by * COLS + bx]) return false;   // above the top is air
    }
    return true;
  }
  function recomputeHeights() {
    for (let c = 0; c < COLS; c++) {
      let h = 0;
      for (let r = 0; r < ROWS; r++) if (board[r * COLS + c]) { h = ROWS - r; break; }
      colHeights[c] = h;
    }
  }
  function tryMove(dx) {
    if (!activeFlag || over || paused || phase !== 0) return false;
    if (!fits(cur.p, cur.r, cur.x + dx, cur.y)) return false;
    cur.x += dx;
    if (!fits(cur.p, cur.r, cur.x, cur.y + 1)) lockT = 0;   // movement refreshes lock delay
    return true;
  }
  function tryRotate() {
    if (!activeFlag || over || paused || phase !== 0) return false;
    const nr = (cur.r + 1) & 3;
    for (let i = 0; i < 5; i++) {                  // simple wall kicks: 0 -1 +1 -2 +2
      const nx = cur.x + KICKS[i];
      if (fits(cur.p, nr, nx, cur.y)) {
        cur.r = nr; cur.x = nx;
        if (!fits(cur.p, cur.r, cur.x, cur.y + 1)) lockT = 0;
        return true;
      }
    }
    return false;
  }
  function hardDrop() {
    if (!activeFlag || over || paused || phase !== 0) return;
    let d = 0;
    while (fits(cur.p, cur.r, cur.x, cur.y + 1)) { cur.y++; d++; }
    if (d > 0) { score += 2 * d; hudDirty = true; }         // 2 points per cell
    lockNow();
  }
  function spawnPiece() {
    cur.p = nextP; nextP = draw7(); nextDirty = true;
    cur.r = 0; cur.x = SPAWN_X[cur.p]; cur.y = SPAWN_Y[cur.p];
    gravT = 0; lockT = 0; groundT = 0;
    ptrId = -1;                                    // a held drag never steers a NEW piece
    if (!fits(cur.p, cur.r, cur.x, cur.y)) doGameOver();    // spawn collides -> over
  }
  function lockNow() {
    const t = ROTS[cur.p][cur.r];
    let topOut = false;
    for (let k = 0; k < 8; k += 2) {
      const bx = cur.x + t[k], by = cur.y + t[k + 1];
      if (by < 0) { topOut = true; continue; }
      board[by * COLS + bx] = cur.p + 1;
    }
    recomputeHeights();
    clearCount = 0;                                // scan full rows BEFORE 'lock' fires so
    for (let r = 0; r < ROWS; r++) {               // a handler's synchronous blast() can
      let full = 1;                                // never void an earned line clear
      for (let c = 0; c < COLS; c++) if (!board[r * COLS + c]) { full = 0; break; }
      clearRows[r] = full;
      if (full) clearCount++;
    }
    emitEv('lock', evLock);                        // evLock.colHeights === colHeights
    if (topOut) { doGameOver(); return; }
    if (clearCount > 0) {
      if (REDUCED) { collapseRows(); afterClear(); spawnPiece(); }  // no flash
      else { phase = 1; clearT = 0; }
    } else spawnPiece();
  }
  function collapseRows() {                        // in-place, no allocation
    let w = ROWS - 1;
    for (let r = ROWS - 1; r >= 0; r--) {
      if (clearRows[r]) continue;
      if (w !== r) {
        for (let c = 0; c < COLS; c++) {
          board[w * COLS + c] = board[r * COLS + c];
          blastF[w * COLS + c] = blastF[r * COLS + c];
        }
      }
      w--;
    }
    for (; w >= 0; w--)
      for (let c = 0; c < COLS; c++) { board[w * COLS + c] = 0; blastF[w * COLS + c] = 0; }
    for (let r = 0; r < ROWS; r++) clearRows[r] = 0;
    recomputeHeights();
  }
  function afterClear() {
    score += CLEAR_SCORE[clearCount] * level;      // 100/300/500/800 x level
    lines += clearCount;
    const lv = 1 + ((lines / 10) | 0);
    if (lv !== level) { level = lv; gravIv = Math.max(GRAV_MIN, GRAV0 - GRAV_DEC * (level - 1)); }
    hudDirty = true;
    evClear.lines = clearCount;
    emitEv('clear', evClear);
    clearCount = 0;
  }
  function doGameOver() {
    over = true; phase = 2;
    persistHi(); hudDirty = true;
    if (finalEl) finalEl.textContent = 'SCORE ' + score;
    if (overWrap) overWrap.style.display = 'flex';
    evOver.score = score;
    emitEv('over', evOver);
  }
  function resetGame() {
    board.fill(0); blastF.fill(0);
    for (let r = 0; r < ROWS; r++) clearRows[r] = 0;
    for (let c = 0; c < COLS; c++) colHeights[c] = 0;
    resetParticles();
    score = 0; lines = 0; level = 1; gravIv = GRAV0;
    over = false; paused = false; phase = 0; clearCount = 0;
    gravT = 0; lockT = 0; groundT = 0; clearT = 0;
    kbSoft = false; btnSoft = false;
    bagIx = 7; nextP = draw7();
    spawnPiece();
    if (overWrap) overWrap.style.display = 'none';
    if (pauseBtn) pauseBtn.textContent = '⏸';
    hudDirty = true;
    emitEv('start', evStart);
  }

  /* ── particle pool — P_MAX objects preallocated; board-canvas space ────── */
  const pool = new Array(P_MAX);
  const free = new Int16Array(P_MAX);
  let freeTop = 0, liveParts = 0;
  (function () {
    for (let i = 0; i < P_MAX; i++) {
      pool[i] = { on: false, x: 0, y: 0, px: 0, py: 0, vx: 0, vy: 0, life: 0, ttl: 1, ci: 0, sz: 2 };
      free[i] = i;
    }
    freeTop = P_MAX;
  })();
  function resetParticles() {
    for (let i = 0; i < P_MAX; i++) { pool[i].on = false; free[i] = i; }
    freeTop = P_MAX; liveParts = 0;
  }
  function emitPart(x, y, vx, vy, ttl, ci, sz) {
    if (freeTop <= 0) return;
    const p = pool[free[--freeTop]];
    p.on = true; p.x = x; p.y = y; p.px = x; p.py = y; p.vx = vx; p.vy = vy;
    p.life = 0; p.ttl = ttl; p.ci = ci; p.sz = sz;
    liveParts++;
  }
  function spawnBurst(c, r, ci) {                  // pixel explosion on one cell
    const bx = (c + 0.5) * cell, by = (r + 0.5) * cell;
    const sz = Math.max(2, (cell / 6) | 0);
    for (let i = 0; i < 4; i++)
      emitPart(bx + rand(-3, 3), by + rand(-3, 3), rand(-90, 90), rand(-170, -20),
               rand(320, 620), ci, sz);
  }
  function updateParticles(dt) {
    const dts = dt / 1000;
    for (let i = 0; i < P_MAX; i++) {
      const p = pool[i];
      if (!p.on) continue;
      p.px = p.x; p.py = p.y;
      p.life += dt;
      if (p.life >= p.ttl) { p.on = false; free[freeTop++] = i; liveParts--; continue; }
      p.vy += 520 * dts;
      p.x += p.vx * dts; p.y += p.vy * dts;
    }
  }
  function renderParticles(g, alpha) {
    if (liveParts <= 0) return;
    for (let i = 0; i < P_MAX; i++) {
      const p = pool[i];
      if (!p.on) continue;
      const t = p.life / p.ttl;
      g.globalAlpha = t > 0.6 ? (1 - t) / 0.4 : 1;
      g.fillStyle = COLORS[p.ci];
      g.fillRect((p.px + (p.x - p.px) * alpha) | 0, (p.py + (p.y - p.py) * alpha) | 0, p.sz, p.sz);
    }
    g.globalAlpha = 1;
  }

  /* ── block tile atlas — each piece color drawn ONCE per theme/resize as a
   * 2-tone pixel block: 1u darker outline, light top bevel, left light,
   * bottom/right inner shade, one specular pixel.  Overlay-alpha shading
   * means no color parsing is ever needed. ───────────────────────────────── */
  function drawTile(g, ox, c, col) {
    const u = Math.max(1, (c / 8) | 0);            // the pixel unit
    g.fillStyle = col; g.fillRect(ox, 0, c, c);
    g.fillStyle = 'rgba(10,12,26,0.55)';           // outline ring
    g.fillRect(ox, 0, c, u); g.fillRect(ox, c - u, c, u);
    g.fillRect(ox, 0, u, c); g.fillRect(ox + c - u, 0, u, c);
    g.fillStyle = 'rgba(255,255,255,0.45)';        // top bevel light
    g.fillRect(ox + u, u, c - 2 * u, u);
    g.fillStyle = 'rgba(255,255,255,0.18)';        // left light
    g.fillRect(ox + u, 2 * u, u, c - 3 * u);
    g.fillStyle = 'rgba(0,0,0,0.30)';              // bottom inner shade
    g.fillRect(ox + u, c - 2 * u, c - 2 * u, u);
    g.fillStyle = 'rgba(0,0,0,0.18)';              // right inner shade
    g.fillRect(ox + c - 2 * u, 2 * u, u, c - 4 * u);
    g.fillStyle = 'rgba(255,255,255,0.6)';         // specular pixel
    g.fillRect(ox + 2 * u, 2 * u, u, u);
  }
  function buildTiles() {
    if (!tileCv) { tileCv = document.createElement('canvas'); tileCtx = tileCv.getContext('2d'); }
    tileD = Math.max(2, Math.round(cell * DPR));
    tileCv.width = 7 * tileD; tileCv.height = tileD;
    const g = tileCtx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    for (let i = 0; i < 7; i++) drawTile(g, i * tileD, tileD, COLORS[i]);
  }
  function buildBase() {                           // board well + grid, prerendered
    if (!baseCv) { baseCv = document.createElement('canvas'); baseCtx = baseCv.getContext('2d'); }
    const w = Math.max(1, Math.round(cssW * DPR)), h = Math.max(1, Math.round(cssH * DPR));
    baseCv.width = w; baseCv.height = h;
    const g = baseCtx;
    g.setTransform(1, 0, 0, 1, 0, 0);
    g.fillStyle = 'rgba(127,134,168,0.10)';        // neutral well tint, both themes
    g.fillRect(0, 0, w, h);
    g.globalAlpha = 0.45;
    g.fillStyle = THEME.line;
    for (let c = 1; c < COLS; c++) g.fillRect(Math.round(c * cell * DPR), 0, 1, h);
    for (let r = 1; r < ROWS; r++) g.fillRect(0, Math.round(r * cell * DPR), w, 1);
    g.globalAlpha = 1;
  }

  /* ── panel DOM ──────────────────────────────────────────────────────────── */
  function el(tag, css) {
    const e = document.createElement(tag);
    if (css) e.style.cssText = css;
    return e;
  }
  function mkIconBtn(txt, label) {                 // 44px touch target
    const b = el('button',
      'width:44px;height:44px;flex:0 0 auto;display:inline-flex;align-items:center;' +
      'justify-content:center;background:transparent;border:1px solid var(--line,#dfe1e6);' +
      'border-radius:10px;color:var(--ink,#172b4d);font:600 15px ui-monospace,SFMono-Regular,' +
      'Menlo,Consolas,monospace;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent;');
    b.type = 'button'; b.textContent = txt;
    b.setAttribute('aria-label', label);
    return b;
  }
  function mkWideBtn(txt, primary) {
    const b = el('button',
      'min-width:120px;min-height:44px;border-radius:10px;cursor:pointer;padding:0 16px;' +
      '-webkit-tap-highlight-color:transparent;' +
      'font:700 12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;letter-spacing:.12em;' +
      (primary
        ? 'background:var(--accent,#0065ff);border:1px solid var(--accent,#0065ff);color:#fff;'
        : 'background:transparent;border:1px solid rgba(255,255,255,.75);color:#fff;'));
    b.type = 'button'; b.textContent = txt;
    return b;
  }
  function buildPanel() {
    root = el('div',
      'position:fixed;left:50%;z-index:' + Z_PANEL + ';pointer-events:auto;box-sizing:content-box;' +
      'background:var(--card,#ffffff);border:1px solid var(--line,#dfe1e6);border-radius:12px;' +
      'box-shadow:0 14px 36px rgba(9,30,66,.28);padding:10px;touch-action:none;' +
      'user-select:none;-webkit-user-select:none;-webkit-touch-callout:none;' +
      '-webkit-tap-highlight-color:transparent;' +
      'color:var(--ink,#172b4d);' +
      'font:12px/1.3 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;');
    root.id = 'kt-root';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-label', 'Tetris');

    const head = el('div', 'display:flex;align-items:center;gap:6px;height:44px;');
    const title = el('div', 'flex:1;font-weight:800;font-size:13px;letter-spacing:.22em;');
    title.textContent = 'TETRIS';
    pauseBtn = mkIconBtn('⏸', 'Pause');
    const closeB = mkIconBtn('✕', 'Close');
    head.appendChild(title); head.appendChild(pauseBtn); head.appendChild(closeB);

    const stats = el('div', 'display:flex;align-items:flex-end;justify-content:space-between;' +
      'gap:8px;margin:2px 0 8px;');
    function statCol(label) {
      const w = el('div', 'min-width:0;');
      const l = el('div', 'font-size:9px;letter-spacing:.14em;color:var(--muted,#6b778c);');
      l.textContent = label;
      const v = el('div', 'font-size:13px;font-weight:800;');
      v.textContent = '0';
      w.appendChild(l); w.appendChild(v);
      stats.appendChild(w);
      return v;
    }
    statsRow = stats;
    scoreEl = statCol('SCORE');
    linesEl = statCol('LINES');
    levelEl = statCol('LVL');
    hiEl = statCol('HI');
    const nw = el('div', 'flex:0 0 auto;');
    const nl = el('div', 'font-size:9px;letter-spacing:.14em;color:var(--muted,#6b778c);text-align:right;');
    nl.textContent = 'NEXT';
    nextCv = document.createElement('canvas');
    nextCv.style.cssText = 'display:block;image-rendering:pixelated;';
    nw.appendChild(nl); nw.appendChild(nextCv);
    stats.appendChild(nw);

    const bw = el('div', 'position:relative;line-height:0;flex:0 0 auto;');
    boardCv = document.createElement('canvas');
    boardCv.style.cssText = 'display:block;image-rendering:pixelated;border-radius:4px;';
    bw.appendChild(boardCv);
    overWrap = el('div', 'position:absolute;inset:0;display:none;flex-direction:column;' +
      'align-items:center;justify-content:center;gap:10px;background:rgba(10,14,30,0.66);' +
      'border-radius:4px;text-align:center;line-height:1.3;');
    const goT = el('div', 'color:#fff;font-weight:800;font-size:16px;letter-spacing:.18em;');
    goT.textContent = 'GAME OVER';
    finalEl = el('div', 'color:#fff;font-size:12px;letter-spacing:.08em;');
    const restartB = mkWideBtn('RESTART', true);
    const closeB2 = mkWideBtn('CLOSE', false);
    overWrap.appendChild(goT); overWrap.appendChild(finalEl);
    overWrap.appendChild(restartB); overWrap.appendChild(closeB2);
    bw.appendChild(overWrap);

    ctrlRow = el('div', 'display:none;flex:0 0 auto;justify-content:center;align-items:center;' +
      'gap:10px;margin-top:8px;');
    function mkCtl(txt, label) {
      const b = el('button',
        'width:52px;height:44px;display:inline-flex;align-items:center;justify-content:center;' +
        'background:transparent;border:1px solid var(--line,#dfe1e6);border-radius:10px;' +
        'color:var(--ink,#172b4d);font:600 16px ui-monospace,monospace;cursor:pointer;padding:0;' +
        '-webkit-tap-highlight-color:transparent;');
      b.type = 'button'; b.textContent = txt;
      b.setAttribute('aria-label', label);
      ctrlRow.appendChild(b);
      return b;
    }
    const leftB = mkCtl('◀', 'Move left');
    const rightB = mkCtl('▶', 'Move right');
    const rotB = mkCtl('⟳', 'Rotate');
    const downB = mkCtl('⬇', 'Soft drop');

    // board + touch controls share one flex box: a column in portrait (controls
    // under the board), a row in landscape (controls beside it) — see layout()
    midWrap = el('div', 'display:flex;flex-direction:column;');
    midWrap.appendChild(bw);
    midWrap.appendChild(ctrlRow);

    root.appendChild(head);
    root.appendChild(stats);
    root.appendChild(midWrap);
    document.body.appendChild(root);

    // panel-local listeners (die with the DOM on stop(); no manual removal)
    root.addEventListener('contextmenu', preventEv);
    root.addEventListener('wheel', preventEv, { passive: false });
    boardCv.addEventListener('pointerdown', onPtrDown);
    boardCv.addEventListener('pointermove', onPtrMove);
    boardCv.addEventListener('pointerup', onPtrUp);
    boardCv.addEventListener('pointercancel', onPtrCancel);
    pauseBtn.addEventListener('click', togglePause);
    closeB.addEventListener('click', userClose);
    restartB.addEventListener('click', resetGame);
    closeB2.addEventListener('click', userClose);
    leftB.addEventListener('pointerdown', function (e) { e.preventDefault(); startRepeat(moveL); });
    rightB.addEventListener('pointerdown', function (e) { e.preventDefault(); startRepeat(moveR); });
    leftB.addEventListener('pointerup', stopRepeat);
    rightB.addEventListener('pointerup', stopRepeat);
    leftB.addEventListener('pointercancel', stopRepeat);
    rightB.addEventListener('pointercancel', stopRepeat);
    leftB.addEventListener('pointerleave', stopRepeat);
    rightB.addEventListener('pointerleave', stopRepeat);
    rotB.addEventListener('pointerdown', function (e) { e.preventDefault(); tryRotate(); });
    downB.addEventListener('pointerdown', function (e) { e.preventDefault(); btnSoft = true; });
    downB.addEventListener('pointerup', softOff);
    downB.addEventListener('pointercancel', softOff);
    downB.addEventListener('pointerleave', softOff);
  }
  function preventEv(e) { e.preventDefault(); }    // ONLY inside the panel
  function softOff() { btnSoft = false; }
  function moveL() { tryMove(-1); }
  function moveR() { tryMove(1); }
  function startRepeat(fn) {                       // touch hold-repeat: 260ms, then 110ms
    stopRepeat();
    fn();
    repTimer = setTimeout(function again() { fn(); repTimer = setTimeout(again, 110); }, 260);
  }
  function stopRepeat() { if (repTimer) { clearTimeout(repTimer); repTimer = 0; } }

  /* ── layout — cell size from min(92vw, 340px) x 78vh, DPR-exact canvases,
   * vertical anchoring (centered >= 700px tall, else 96px above the bottom
   * so the voice mic button is never overlapped).
   *
   * SHORT viewports (vh < 480 — a phone in landscape) get a different panel
   * entirely: portrait's math there yields a card taller than the screen and
   * shoves the header off the top (-20px at 812x375).  Instead the stats row
   * is hidden, the touch controls move to a vertical column RIGHT of the
   * board, and the card anchors to the LEFT edge (vertically centered) so the
   * bottom-center voice mic is never covered.  Only the 44px header sits
   * above the board there, so the whole card fits in vh - 16.  This runs on
   * every resize / orientationchange, so the two modes swap live. ────────── */
  function layout() {
    if (!root) return;
    DPR = window.devicePixelRatio || 1;
    const vw = window.innerWidth, vh = window.innerHeight;
    const coarse = !!(mqCoarse && mqCoarse.matches);
    const short = vh < SHORT_VH;                   // landscape / short viewport
    ctrlRow.style.display = coarse ? 'flex' : 'none';
    statsRow.style.display = short ? 'none' : 'flex';
    let maxW, availH;
    if (short) {
      // controls ride beside the board, so the ONLY chrome over the board is
      // the header: the height budget is vh - 16 minus padding+border+header
      const sideW = coarse ? SIDE_COL : 0;
      maxW = vw - 24 - 22 - sideW;                 // 12px gutters, padding + border
      availH = vh - 16 - CHROME_SHORT;
    } else {
      maxW = Math.min(vw * 0.92, 340) - 22;        // minus padding + border
      availH = Math.min(vh * 0.78, vh - MIC_CLEAR - 12) - (coarse ? CHROME_COARSE : CHROME_FINE);
    }
    cell = Math.max(8, Math.min(30, (maxW / COLS) | 0, (availH / ROWS) | 0));
    cssW = cell * COLS; cssH = cell * ROWS;        // 10 x 18, always
    root.style.width = (cssW + (short && coarse ? SIDE_COL : 0)) + 'px';
    boardCv.style.width = cssW + 'px'; boardCv.style.height = cssH + 'px';
    boardCv.width = Math.max(1, Math.round(cssW * DPR));
    boardCv.height = Math.max(1, Math.round(cssH * DPR));
    bctx = boardCv.getContext('2d');
    bctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    bctx.imageSmoothingEnabled = false;            // crisp pixels, always
    nextCv.style.width = (4 * NEXT_M) + 'px'; nextCv.style.height = (2 * NEXT_M) + 'px';
    nextCv.width = Math.max(1, Math.round(4 * NEXT_M * DPR));
    nextCv.height = Math.max(1, Math.round(2 * NEXT_M * DPR));
    nctx = nextCv.getContext('2d');
    nctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    nctx.imageSmoothingEnabled = false;
    // ── control re-flow: a row under the board, or a column beside it ──────
    const ctls = ctrlRow.children;
    if (short) {
      midWrap.style.flexDirection = 'row';
      midWrap.style.alignItems = 'center';
      midWrap.style.gap = '8px';
      ctrlRow.style.flexDirection = 'column';
      ctrlRow.style.gap = '6px';
      ctrlRow.style.marginTop = '0';
      ctrlRow.style.width = '44px';
      // 4 buttons + 3 gaps never taller than the board itself
      const bh = Math.max(30, Math.min(44, ((cssH - 18) / 4) | 0));
      for (let i = 0; i < ctls.length; i++) {
        ctls[i].style.width = '44px'; ctls[i].style.height = bh + 'px';
      }
    } else {
      midWrap.style.flexDirection = 'column';
      midWrap.style.alignItems = '';
      midWrap.style.gap = '';
      ctrlRow.style.flexDirection = 'row';
      ctrlRow.style.gap = '10px';
      ctrlRow.style.marginTop = '8px';
      ctrlRow.style.width = '';
      for (let i = 0; i < ctls.length; i++) {
        ctls[i].style.width = '52px'; ctls[i].style.height = '44px';
      }
    }
    // ── anchoring ─────────────────────────────────────────────────────────
    if (short) {                                   // LEFT edge, vertically centered:
      root.style.left = '12px';                    // the mic owns bottom-center
      root.style.top = '50%'; root.style.bottom = 'auto';
      root.style.transform = 'translateY(-50%)';
    } else if (vh >= 700) {
      root.style.left = '50%';
      root.style.top = '50%'; root.style.bottom = 'auto';
      root.style.transform = 'translate(-50%,-50%)';
    } else {
      root.style.left = '50%';
      root.style.top = 'auto'; root.style.bottom = MIC_CLEAR + 'px';
      root.style.transform = 'translateX(-50%)';
    }
    buildTiles(); buildBase();
    tilesDirty = false; nextDirty = true; rectAt = -1;
  }

  /* ── DPR watcher (zoom / monitor moves) — same pattern as game_layer ───── */
  function armDprWatch() {
    if (mqDpr) { try { mqDpr.removeEventListener('change', onDprChange); } catch (e) {} }
    try {
      armedDpr = window.devicePixelRatio || 1;
      mqDpr = window.matchMedia('(resolution: ' + armedDpr + 'dppx)');
      mqDpr.addEventListener('change', onDprChange);
    } catch (e) { mqDpr = null; }
  }
  function onDprChange() { layout(); armDprWatch(); }

  /* ── sprite-interference API ────────────────────────────────────────────── */
  const geomRect = { left: 0, top: 0, width: 0, height: 0, right: 0, bottom: 0 };
  const geom = {
    rect: geomRect, cell: 20, cols: COLS, rows: ROWS, colHeights: colHeights,
    topY: function (c) { return geomRect.top + (ROWS - (colHeights[c] || 0)) * geom.cell; },
  };
  let rectAt = -1;
  function geometry() {
    if (!activeFlag || !boardCv) return null;
    const now = performance.now();
    if (now - rectAt > 250) {                      // panel is fixed; refresh at most 4/s
      rectAt = now;
      const r = boardCv.getBoundingClientRect();
      geomRect.left = r.left; geomRect.top = r.top;
      geomRect.width = r.width; geomRect.height = r.height;
      geomRect.right = r.right; geomRect.bottom = r.bottom;
    }
    geom.cell = cell;
    return geom;
  }
  function blast(cx, cy) {                         // settled cells only, 3x3
    // phase 1 is the clear flash: those cells are already scored — never double-score
    if (!activeFlag || over || paused || phase !== 0) return 0;
    cx |= 0; cy |= 0;
    let cleared = 0;
    for (let r = cy - 1; r <= cy + 1; r++) {
      if (r < 0 || r >= ROWS) continue;
      for (let c = cx - 1; c <= cx + 1; c++) {
        if (c < 0 || c >= COLS) continue;
        const ix = r * COLS + c;
        if (!board[ix]) continue;                  // the falling piece never lives here
        if (!REDUCED) spawnBurst(c, r, board[ix] - 1);
        board[ix] = 0;
        blastF[ix] = BLAST_MS;
        cleared++;
      }
    }
    if (cleared > 0) {
      score += 40 * cleared; hudDirty = true;
      recomputeHeights();
      evBlast.cleared = cleared;
      emitEv('blast', evBlast);
    }
    return cleared;
  }
  function nudge(dir) {                            // shift the FALLING piece
    // Coerce FIRST: isFinite('') / isFinite(null) / isFinite([]) are all true,
    // so a strict `dir === 0` alone let those through and silently shifted
    // the piece right. One numeric conversion closes the whole family.
    const n = +dir;
    if (!n || !isFinite(n)) return false;
    const d = n < 0 ? -1 : 1;
    const ok = tryMove(d);
    if (ok) { evNudge.dir = d; emitEv('nudge', evNudge); }
    return ok;
  }
  const stateObj = { score: 0, lines: 0, level: 1, over: false, paused: false };
  function apiState() {
    stateObj.score = score; stateObj.lines = lines; stateObj.level = level;
    stateObj.over = over; stateObj.paused = paused;
    return stateObj;
  }

  /* ── input: keyboard (only while active; never over form fields, never
   * while a page overlay is open, never stealing keys from focused page
   * controls outside #kt-root) ───────────────────────────────────────────── */
  function onKey(e) {
    if (!activeFlag) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
              t.tagName === 'SELECT' || t.isContentEditable)) return;
    // a page overlay owns the keyboard (incl. Escape) — one cheap check
    if (document.querySelector('.overlay:not([hidden])')) return;
    // a focused page control (button/link/tabindex) outside the panel keeps
    // its keys — Space/Enter must activate it normally, no preventDefault
    if (t && t.nodeType === 1 &&
        (t.tagName === 'BUTTON' || t.tagName === 'A' || t.hasAttribute('tabindex')) &&
        !(t.closest && t.closest('#kt-root'))) return;
    switch (e.code) {
      case 'Escape':     e.preventDefault(); if (!e.repeat) userClose(); break;
      case 'KeyP':       e.preventDefault(); if (!e.repeat) togglePause(); break;
      case 'ArrowLeft':  e.preventDefault(); tryMove(-1); break;
      case 'ArrowRight': e.preventDefault(); tryMove(1); break;
      case 'ArrowDown':  e.preventDefault(); kbSoft = true; break;
      case 'ArrowUp':
      case 'KeyZ':       e.preventDefault(); if (!e.repeat) tryRotate(); break;
      case 'Space':      e.preventDefault(); if (!e.repeat) hardDrop(); break;
    }
  }
  function onKeyUp(e) {
    if (!activeFlag) return;
    if (e.code === 'ArrowDown') kbSoft = false;
  }

  /* ── input: pointer on the board canvas — drag steers to the pointer's
   * column (grab-relative), tap rotates, quick downward swipe hard-drops. ── */
  let ptrId = -1, ptrX0 = 0, ptrY0 = 0, ptrT0 = 0, ptrMovedX = false, grabOff = 0;
  function colAtX(clientX) {
    geometry();                                    // keeps geomRect fresh
    return Math.floor((clientX - geomRect.left) / cell);   // symmetric at the left edge
  }
  function onPtrDown(e) {
    if (!activeFlag) return;
    e.preventDefault();
    if (over) return;                              // the overlay owns game-over input
    if (paused || phase !== 0) return;             // no grabbing mid-flash or paused
    try { boardCv.setPointerCapture(e.pointerId); } catch (err) {}
    ptrId = e.pointerId;
    ptrX0 = e.clientX; ptrY0 = e.clientY; ptrT0 = performance.now();
    ptrMovedX = false;
    rectAt = -1;                                   // force one fresh rect read
    grabOff = cur.x - colAtX(e.clientX);
  }
  function onPtrMove(e) {
    if (ptrId !== e.pointerId || !activeFlag || over || paused || phase !== 0) return;
    e.preventDefault();
    if (Math.abs(e.clientX - ptrX0) > cell * 0.6) ptrMovedX = true;
    const want = colAtX(e.clientX) + grabOff;
    let guard = COLS;
    while (cur.x < want && guard-- > 0) { if (!tryMove(1)) break; }
    guard = COLS;
    while (cur.x > want && guard-- > 0) { if (!tryMove(-1)) break; }
  }
  function onPtrUp(e) {
    if (ptrId !== e.pointerId) return;
    ptrId = -1;
    if (!activeFlag || over) return;
    const dt = Math.max(1, performance.now() - ptrT0);
    const dx = e.clientX - ptrX0, dy = e.clientY - ptrY0;
    if (dy > 40 && dy / dt > 0.4 && dy > Math.abs(dx)) { hardDrop(); return; }  // swipe
    if (!ptrMovedX && Math.abs(dy) < 12 && dt < 350) tryRotate();               // tap
  }
  function onPtrCancel(e) { if (ptrId === e.pointerId) ptrId = -1; }

  /* ── pause / close ──────────────────────────────────────────────────────── */
  function togglePause() {
    if (!activeFlag || over) return;
    paused = !paused;
    if (pauseBtn) pauseBtn.textContent = paused ? '▶' : '⏸';
  }
  function apiPause(on) {                          // public: idempotent P-key twin
    if (!activeFlag || over) return;
    if (!!on === paused) return;
    togglePause();
  }
  function userClose() {
    const cb = cbClose;
    apiStop();
    if (cb) { try { cb(); } catch (e) {} }
  }

  /* ── fixed-timestep simulation ──────────────────────────────────────────── */
  function update(dt) {
    if (paused) return;                            // pause freezes everything
    updateParticles(dt);
    for (let i = 0; i < blastF.length; i++) {      // blast flash decay
      if (blastF[i] > 0) { blastF[i] -= dt; if (blastF[i] < 0) blastF[i] = 0; }
    }
    if (over) return;                              // the last explosion still settles
    if (phase === 1) {                             // line-clear flash, then collapse
      clearT += dt;
      if (clearT >= FLASH_MS) {
        if (!REDUCED) {
          for (let r = 0; r < ROWS; r++) {
            if (!clearRows[r]) continue;
            for (let c = 0; c < COLS; c += 2) {
              const v = board[r * COLS + c];
              if (v) spawnBurst(c, r, v - 1);
            }
          }
        }
        collapseRows(); afterClear(); phase = 0; spawnPiece();
      }
      return;
    }
    // gravity + lock delay
    const grounded = !fits(cur.p, cur.r, cur.x, cur.y + 1);
    if (grounded) {
      gravT = 0;
      lockT += dt; groundT += dt;                  // movement resets lockT, never groundT
      if (lockT >= LOCK_MS || groundT >= LOCK_CAP) lockNow();
    } else {
      lockT = 0;
      const iv = softOn() ? Math.min(SOFT_MS, gravIv) : gravIv;
      gravT += dt;
      while (gravT >= iv) {
        gravT -= iv;
        if (fits(cur.p, cur.r, cur.x, cur.y + 1)) {
          cur.y++;
          groundT = 0;                             // descending re-arms the lock cap
          if (softOn()) { score += 1; hudDirty = true; }   // 1 point per soft cell
        } else break;
      }
    }
  }

  /* ── render — no allocations: prerendered base, tile atlas drawImage,
   * overlay flashes, pooled particles.  alpha only interpolates particles
   * (piece motion is deliberately stepped — 8-bit law). ──────────────────── */
  function drawNext() {
    nextDirty = false;
    const g = nctx;
    if (!g || !tileCv) return;
    g.clearRect(0, 0, 4 * NEXT_M, 2 * NEXT_M);
    const t = ROTS[nextP][0];
    let minX = 9, minY = 9, maxX = -9, maxY = -9;
    for (let k = 0; k < 8; k += 2) {
      if (t[k] < minX) minX = t[k];
      if (t[k] > maxX) maxX = t[k];
      if (t[k + 1] < minY) minY = t[k + 1];
      if (t[k + 1] > maxY) maxY = t[k + 1];
    }
    const ox = (((4 - (maxX - minX + 1)) * NEXT_M) / 2) | 0;
    const oy = (((2 - (maxY - minY + 1)) * NEXT_M) / 2) | 0;
    for (let k = 0; k < 8; k += 2)
      g.drawImage(tileCv, nextP * tileD, 0, tileD, tileD,
        ox + (t[k] - minX) * NEXT_M, oy + (t[k + 1] - minY) * NEXT_M, NEXT_M, NEXT_M);
  }
  function render(alpha) {
    const g = bctx;
    if (!g) return;
    if (tilesDirty) { buildTiles(); buildBase(); tilesDirty = false; nextDirty = true; }
    if (nextDirty) drawNext();
    g.save();                                      // fractional DPR (1.25 etc): clear the
    g.setTransform(1, 0, 0, 1, 0, 0);              // FULL bitmap so no particle flecks
    g.clearRect(0, 0, boardCv.width, boardCv.height);   // survive at the right/bottom edge
    g.restore();
    g.drawImage(baseCv, 0, 0, baseCv.width, baseCv.height, 0, 0, cssW, cssH);
    for (let r = 0; r < ROWS; r++) {               // settled cells
      const ro = r * COLS;
      for (let c = 0; c < COLS; c++) {
        const v = board[ro + c];
        if (v) g.drawImage(tileCv, (v - 1) * tileD, 0, tileD, tileD,
                           c * cell, r * cell, cell, cell);
      }
    }
    if (phase === 1) {                             // line-clear flicker
      g.globalAlpha = (((clearT / 50) | 0) % 2 === 0) ? 0.85 : 0.35;
      g.fillStyle = THEME.flash;
      for (let r = 0; r < ROWS; r++) if (clearRows[r]) g.fillRect(0, r * cell, cssW, cell);
      g.globalAlpha = 1;
    }
    for (let i = 0; i < blastF.length; i++) {      // blast cell flashes
      const f = blastF[i];
      if (f <= 0) continue;
      g.globalAlpha = f / BLAST_MS;
      g.fillStyle = THEME.flash;
      g.fillRect((i % COLS) * cell, ((i / COLS) | 0) * cell, cell, cell);
    }
    g.globalAlpha = 1;
    if (phase === 0 && !over) {                    // the falling piece
      const t = ROTS[cur.p][cur.r], sx = cur.p * tileD;
      for (let k = 0; k < 8; k += 2) {
        const by = cur.y + t[k + 1];
        if (by < 0) continue;
        g.drawImage(tileCv, sx, 0, tileD, tileD,
                    (cur.x + t[k]) * cell, by * cell, cell, cell);
      }
    }
    renderParticles(g, alpha);
    if (paused && !over) {
      g.fillStyle = 'rgba(10,14,30,0.55)';
      g.fillRect(0, 0, cssW, cssH);
      g.fillStyle = '#ffffff';
      g.font = '700 16px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace';
      g.textAlign = 'center';
      g.fillText('PAUSED', cssW / 2, cssH / 2);
    }
  }
  function updateHud() {                           // DOM touched only on change
    hudDirty = false;
    if (!scoreEl) return;
    scoreEl.textContent = String(score);
    linesEl.textContent = String(lines);
    levelEl.textContent = String(level);
    hiEl.textContent = String(score > hi ? score : hi);
  }

  /* ── the loop — STEP accumulator, dt clamped, spiral guard; a hidden tab
   * freezes the clock (and auto-pauses so nobody dies off-screen). ───────── */
  function tick(ts) {
    if (!activeFlag) return;
    raf = requestAnimationFrame(tick);
    let dt = ts - lastTs; lastTs = ts;
    if (dt < 0) dt = 0;
    if (dt > MAX_DT) dt = MAX_DT;
    acc += dt;
    let n = 0;
    while (acc >= STEP && n < MAX_STEPS) { update(STEP); acc -= STEP; n++; }
    if (n === MAX_STEPS) acc = 0;                  // spiral guard: drop the debt
    refreshTheme(performance.now());
    render(acc / STEP);
    if (hudDirty) updateHud();
  }

  /* ── window-level listeners ─────────────────────────────────────────────── */
  function onResize() { layout(); }
  function onVis() {
    if (!activeFlag) return;
    if (document.hidden) {
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      if (!over && !paused) togglePause();         // don't die in a hidden tab
    } else if (!raf) {
      lastTs = performance.now(); acc = 0;
      raf = requestAnimationFrame(tick);
    }
  }
  function onCoarse() { layout(); }
  function onPrm() { REDUCED = !!(mqReduced && mqReduced.matches); }
  function onBlur() { kbSoft = false; btnSoft = false; }   // never a stuck soft-drop

  /* ── lifecycle ──────────────────────────────────────────────────────────── */
  function apiStart(opts) {
    if (activeFlag) return;
    if (!document.body) return;                    // included too early: no-op
    cbClose = (opts && typeof opts.onClose === 'function') ? opts.onClose : null;
    cbEvent = (opts && typeof opts.onEvent === 'function') ? opts.onEvent : null;
    try {
      mqCoarse = window.matchMedia('(pointer: coarse)');
      mqCoarse.addEventListener('change', onCoarse);
    } catch (e) { mqCoarse = null; }
    try {
      mqReduced = window.matchMedia('(prefers-reduced-motion: reduce)');
      mqReduced.addEventListener('change', onPrm);
      REDUCED = mqReduced.matches;
    } catch (e) { mqReduced = null; REDUCED = false; }
    hi = readHi();
    buildPanel();
    layout();
    armDprWatch();
    window.addEventListener('keydown', onKey);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('resize', onResize);
    window.addEventListener('orientationchange', onResize);
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onVis);
    activeFlag = true;
    themeAt = -1;                                  // force an immediate theme read
    resetGame();                                   // emits 'start'
    updateHud();
    lastTs = performance.now(); acc = 0;
    if (!document.hidden) raf = requestAnimationFrame(tick);
  }
  function apiStop() {
    if (!activeFlag) return;
    activeFlag = false;
    persistHi();
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    stopRepeat();
    window.removeEventListener('keydown', onKey);
    window.removeEventListener('keyup', onKeyUp);
    window.removeEventListener('resize', onResize);
    window.removeEventListener('orientationchange', onResize);
    window.removeEventListener('blur', onBlur);
    document.removeEventListener('visibilitychange', onVis);
    if (mqCoarse) { try { mqCoarse.removeEventListener('change', onCoarse); } catch (e) {} mqCoarse = null; }
    if (mqReduced) { try { mqReduced.removeEventListener('change', onPrm); } catch (e) {} mqReduced = null; }
    if (mqDpr) { try { mqDpr.removeEventListener('change', onDprChange); } catch (e) {} mqDpr = null; }
    if (root && root.parentNode) root.parentNode.removeChild(root);
    // element-attached listeners die with the removed subtree; drop the refs
    root = null; boardCv = null; bctx = null; nextCv = null; nctx = null;
    overWrap = null; finalEl = null; pauseBtn = null; ctrlRow = null;
    statsRow = null; midWrap = null;
    scoreEl = null; linesEl = null; levelEl = null; hiEl = null;
    kbSoft = false; btnSoft = false; ptrId = -1; rectAt = -1;
    resetParticles();
    cbClose = null; cbEvent = null;
  }

  /* ── the ONE global ─────────────────────────────────────────────────────── */
  window.krabTetris = {
    get element() { return root; },   // the panel card — the sprite layer's shake target
    start: apiStart,
    stop: apiStop,
    pause: apiPause,
    active: function () { return activeFlag; },
    geometry: geometry,
    blast: blast,
    nudge: nudge,
    state: apiState,
  };
})();

''',
}
