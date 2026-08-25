"""The /receipts board — every transmission, its receipt, and where it has got to.

This is the "krab issuer" transmissions list on a full page of its own, with the
things that list could not do: a status column the whole team can move (On the way,
Delivered, Paid), the receipt shown from the database rather than a Telegram link
that has expired, and a row that expands to the detail.

Kept in its own module because admin_dashboard.py is already long, and because the
board is self-contained: it needs `db` and `app` and nothing else.
"""
from flask import jsonify, render_template_string, request

STATUS_LABELS = {
    "new": "New",
    "on_the_way": "On the way",
    "delivered": "Delivered",
    "paid": "Paid",
}
STATUS_ORDER = ("new", "on_the_way", "delivered", "paid")


BOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Receipts &amp; Transmissions</title>
<style>
 :root {
   --bg:#f4f5f7; --card:#fff; --ink:#172b4d; --muted:#6b778c; --line:#dfe1e6;
   --new:#8993a4; --otw:#0065ff; --del:#00875a; --paid:#6554c0;
 }
 @media (prefers-color-scheme: dark) {
   :root { --bg:#1d2125; --card:#22272b; --ink:#e6edf3; --muted:#9fadbc; --line:#2c333a; }
 }
 * { box-sizing:border-box; }
 body { margin:0; font:14px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;
        background:var(--bg); color:var(--ink); }
 header { position:sticky; top:0; z-index:5; background:var(--card);
          border-bottom:1px solid var(--line); padding:14px 18px;
          display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
 h1 { font-size:17px; margin:0; font-weight:650; }
 .grow { flex:1; }
 input[type=search] { padding:8px 12px; border:1px solid var(--line); border-radius:8px;
                      background:var(--bg); color:inherit; min-width:200px; }
 .tabs { display:flex; gap:6px; flex-wrap:wrap; }
 .tab { padding:6px 12px; border:1px solid var(--line); border-radius:20px;
        background:transparent; color:var(--muted); cursor:pointer; font-weight:600; }
 .tab.on { background:var(--ink); color:var(--card); border-color:var(--ink); }
 .counts { color:var(--muted); font-size:12px; }
 main { padding:14px 18px 60px; }
 table { width:100%; border-collapse:collapse; background:var(--card);
         border:1px solid var(--line); border-radius:10px; overflow:hidden; }
 th { text-align:left; font-size:11px; letter-spacing:.06em; text-transform:uppercase;
      color:var(--muted); padding:10px 12px; border-bottom:1px solid var(--line);
      white-space:nowrap; }
 td { padding:10px 12px; border-bottom:1px solid var(--line); vertical-align:top; }
 tr.row:hover { background:rgba(0,101,255,.045); }
 .ref { font-family:ui-monospace,monospace; font-weight:650; }
 .exp { cursor:pointer; user-select:none; color:var(--muted); width:26px; }
 .pill { display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px;
         font-weight:650; color:#fff; white-space:nowrap; }
 .s-new{background:var(--new)} .s-on_the_way{background:var(--otw)}
 .s-delivered{background:var(--del)} .s-paid{background:var(--paid)}
 select.status { padding:5px 8px; border-radius:8px; border:1px solid var(--line);
                 background:var(--bg); color:inherit; font-weight:600; }
 .detail { background:var(--bg); }
 .detail dl { display:grid; grid-template-columns:max-content 1fr; gap:6px 16px; margin:0; }
 .detail dt { color:var(--muted); font-size:12px; }
 .detail img { max-width:min(420px,100%); border-radius:8px; margin-top:10px;
               border:1px solid var(--line); }
 a { color:var(--otw); }
 .none { color:var(--muted); }
 .err { background:#ffebe6; color:#bf2600; padding:10px 14px; border-radius:8px;
        margin:10px 18px; }
 .saving { opacity:.5; }
 @media (max-width:760px) { .hide-sm { display:none; } }
</style></head><body>
<header>
  <h1>🧾 Receipts &amp; Transmissions</h1>
  <div class="tabs" id="tabs"></div>
  <span class="grow"></span>
  <input type="search" id="q" placeholder="Search ref, client, driver…">
  <span class="counts" id="counts"></span>
</header>
<div id="err"></div>
<main>
  <table>
    <thead><tr>
      <th class="exp"></th><th>Ref</th><th>Client</th><th class="hide-sm">Car</th>
      <th>Driver</th><th class="hide-sm">Team</th><th>Price</th>
      <th>Receipt</th><th>Status</th><th class="hide-sm">Updated</th>
    </tr></thead>
    <tbody id="rows"><tr><td colspan="10" class="none">Loading…</td></tr></tbody>
  </table>
</main>
<script>
const STATUSES = __STATUSES__;
const LABELS = __LABELS__;
let ALL = [], filter = "", q = "";

const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function when(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return isNaN(d) ? "—" : d.toLocaleString([], {month:"short", day:"numeric",
                                               hour:"2-digit", minute:"2-digit"});
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

function draw() {
  tabs();
  const rows = visible();
  document.getElementById("counts").textContent =
    `${rows.length} of ${ALL.length}`;
  const tb = document.getElementById("rows");
  if (!rows.length) {
    tb.innerHTML = '<tr><td colspan="10" class="none">Nothing here yet.</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const opts = STATUSES.map(s =>
      `<option value="${s}" ${r.status === s ? "selected" : ""}>${esc(LABELS[s])}</option>`
    ).join("");
    const receipt = r.has_receipt
      ? `<a href="/receipt/${encodeURIComponent(r.lead_id)}" target="_blank">view</a>`
      : '<span class="none">—</span>';
    return `<tr class="row" data-id="${esc(r.lead_id)}">
      <td class="exp" data-x="${esc(r.lead_id)}">▸</td>
      <td class="ref">${esc(r.reference_id)}</td>
      <td>${esc(r.client_name)}</td>
      <td class="hide-sm">${esc(r.car)}</td>
      <td>${esc(r.driver_name)}</td>
      <td class="hide-sm">${esc(r.group_name)}</td>
      <td>${esc(r.price)}</td>
      <td>${receipt}</td>
      <td><span class="pill s-${esc(r.status)}">${esc(LABELS[r.status] || r.status)}</span><br>
          <select class="status" data-id="${esc(r.lead_id)}">${opts}</select></td>
      <td class="hide-sm">${esc(when(r.status_updated_at))}<br>
          <span class="counts">${esc(r.status_updated_by || "")}</span></td>
    </tr>
    <tr class="detail" id="d-${esc(r.lead_id)}" hidden><td colspan="10">
      <dl>
        <dt>Delivery</dt><dd>${esc(r.delivery) || "—"}</dd>
        <dt>Notes</dt><dd>${esc(r.notes) || "—"}</dd>
        <dt>Email</dt><dd>${esc(r.email) || "—"}</dd>
        <dt>Entered by</dt><dd>${esc(r.issuer)}</dd>
        <dt>Created</dt><dd>${esc(when(r.created_at))}</dd>
        <dt>Receipt</dt><dd>${r.receipt_in_db ? "stored here (never expires)"
                              : (r.has_receipt ? "external link" : "not handed in")}</dd>
      </dl>
      ${r.has_receipt ? `<img loading="lazy" src="/receipt/${encodeURIComponent(r.lead_id)}" alt="receipt">` : ""}
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
      const res = await fetch(`/api/transmissions/${encodeURIComponent(id)}/status`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({status: next, by: localStorage.getItem("krab_who") || ""}),
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
}

async function load() {
  try {
    const res = await fetch("/api/transmissions?limit=500");
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
load();
setInterval(load, 30000);   // the board is shared — keep it fresh
</script>
</body></html>"""


def register(app, db_provider):
    """Attach the board and its two endpoints to the dashboard app.

    `db_provider` is resolved on every request, not captured once: binding the
    client at registration time would leave the board talking to a stale handle if
    the dashboard ever rebuilds it (and would quietly ignore a swapped-in double)."""
    _resolve = db_provider if callable(db_provider) else (lambda: db_provider)

    @app.route("/receipts", methods=["GET"])
    def receipts_board():
        html = (BOARD_HTML
                .replace("__STATUSES__", repr(list(STATUS_ORDER)).replace("'", '"'))
                .replace("__LABELS__", repr(STATUS_LABELS).replace("'", '"')))
        return render_template_string(html)

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

    @app.route("/api/transmissions/<lead_id>/status", methods=["POST"])
    def api_set_transmission_status(lead_id):
        body = request.get_json(silent=True) or {}
        status = (body.get("status") or request.args.get("status") or "").strip()
        if status not in STATUS_ORDER:
            return jsonify({"error": f"status must be one of {list(STATUS_ORDER)}"}), 400
        who = (body.get("by") or "").strip()
        if not _resolve().set_lead_status(lead_id, status, who):
            return jsonify({"error": "could not save"}), 500
        return jsonify({"ok": True, "lead_id": lead_id, "status": status})
