import { useEffect, useMemo, useState } from "react";
import { supabase } from "../lib/supabase";
import { money } from "../lib/money";
import { adminApi } from "./AdminLayout";

const PIPELINE = ["pending", "confirmed", "processing", "shipped", "out_for_delivery", "delivered"] as const;
const TERMINAL = ["cancelled", "refunded"] as const;
const STATUS_LABEL: Record<string, string> = {
  pending: "Pending", confirmed: "Confirmed", processing: "Preparing",
  shipped: "Shipped", out_for_delivery: "Out for delivery", delivered: "Delivered",
  cancelled: "Cancelled", refunded: "Refunded",
};

interface OrderRow {
  id: string; order_number: string; status: string; payment_status: string;
  payment_method?: string | null; payment_ref?: string | null; paid_at?: string | null;
  authenticity_code?: string | null;
  momo_phone?: string | null; momo_channel?: string | null;
  total_amount: number; customer_name: string | null; customer_email: string | null;
  customer_phone: string | null; shipping_address: string | null;
  discount_code?: string | null; discount_amount?: number | null;
  tracking_note?: string | null; status_history?: { status: string; note?: string | null; at: string }[] | null;
  refund_status?: string | null; refund_amount?: number | null; refunded_at?: string | null;
  created_at: string;
}
interface ItemRow { id: string; product_name: string; size: string | null; quantity: number; price: number | null; unit_price: number | null; }

export default function AdminOrders() {
  const [rows, setRows] = useState<OrderRow[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [items, setItems] = useState<Record<string, ItemRow[]>>({});
  const [filter, setFilter] = useState<string>("all");
  const [search, setSearch] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState<Record<string, string>>({});

  const load = async () => {
    const { data } = await supabase.from("orders").select("*").order("created_at", { ascending: false });
    setRows((data || []) as OrderRow[]);
  };
  useEffect(() => { load(); }, []);

  const say = (m: string) => { setFlash(m); setTimeout(() => setFlash(null), 3500); };

  const toggle = async (id: string) => {
    if (openId === id) { setOpenId(null); return; }
    setOpenId(id);
    if (!items[id]) {
      const { data } = await supabase.from("order_items").select("id, product_name, size, quantity, price, unit_price").eq("order_id", id);
      setItems((m) => ({ ...m, [id]: (data || []) as ItemRow[] }));
    }
  };

  const setStatus = async (o: OrderRow, status: string, notify: boolean) => {
    setBusyId(o.id);
    try {
      if (notify) {
        const r = await adminApi("/api/admin/notify-status", { orderId: o.id, status, note: noteDraft[o.id] || undefined });
        if (!r.ok) throw new Error(String(r.error || "Status update failed"));
        say(r.emailed ? `Status → ${STATUS_LABEL[status] || status} · customer emailed ✓` : `Status → ${STATUS_LABEL[status] || status} (no email sent)`);
      } else {
        await supabase.from("orders").update({ status }).eq("id", o.id);
        say(`Status → ${STATUS_LABEL[status] || status}`);
      }
      await load();
    } catch (e) {
      say(e instanceof Error ? e.message : "Failed");
    } finally { setBusyId(null); }
  };

  const markPaid = async (o: OrderRow) => {
    if (!confirm(`Mark ${o.order_number} as PAID manually? Use only if money actually arrived.`)) return;
    await supabase.from("orders").update({ payment_status: "paid", status: o.status === "pending" ? "confirmed" : o.status }).eq("id", o.id);
    say("Marked paid (manual)");
    load();
  };

  const refund = async (o: OrderRow) => {
    const amtRaw = prompt(
      `Refund for ${o.order_number}\nPaid: GH₵${o.total_amount}\nMoMo: ${o.momo_phone || "—"} (${o.momo_channel || "?"})\n\nAmount to refund (GH₵):`,
      String(o.total_amount),
    );
    if (amtRaw === null) return;
    const amount = Number(amtRaw);
    if (!(amount > 0)) { say("Invalid amount"); return; }
    const reason = prompt("Reason (goes to the customer email, optional):") || undefined;
    if (!confirm(`Send GH₵${amount} back to ${o.momo_phone || "their momo"}? This moves real money.`)) return;
    setBusyId(o.id);
    try {
      const r = await adminApi("/api/admin/refund", { orderId: o.id, amount, reason });
      if (!r.ok) throw new Error(String(r.error || "Refund failed"));
      say(`Refunded GH₵${amount} ✓`);
      await load();
    } catch (e) {
      say(e instanceof Error ? e.message : "Refund failed");
    } finally { setBusyId(null); }
  };

  const exportCsv = () => {
    const cols = ["order_number", "created_at", "status", "payment_status", "total_amount", "customer_name", "customer_email", "customer_phone", "momo_phone", "shipping_address", "payment_ref", "refund_status", "refund_amount"];
    const csv = [cols.join(",")].concat(
      visible.map((o) => cols.map((c) => `"${String((o as Record<string, unknown>)[c] ?? "").replace(/"/g, '""')}"`).join(",")),
    ).join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `og-orders-${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  };

  const counts = useMemo(() => {
    const c: Record<string, number> = { all: rows.length, unpaid: 0 };
    for (const s of [...PIPELINE, ...TERMINAL]) c[s] = 0;
    for (const o of rows) {
      c[o.status] = (c[o.status] || 0) + 1;
      if (o.payment_status !== "paid") c.unpaid += 1;
    }
    return c;
  }, [rows]);

  const visible = useMemo(() => {
    let list = rows;
    if (filter === "unpaid") list = list.filter((o) => o.payment_status !== "paid");
    else if (filter !== "all") list = list.filter((o) => o.status === filter);
    const q = search.trim().toLowerCase();
    if (q) {
      list = list.filter((o) =>
        [o.order_number, o.customer_name, o.customer_email, o.customer_phone, o.momo_phone, o.payment_ref]
          .filter(Boolean).join(" ").toLowerCase().includes(q),
      );
    }
    return list;
  }, [rows, filter, search]);

  const wa = (phone?: string | null) => {
    const d = String(phone || "").replace(/\D/g, "");
    if (!d) return null;
    return `https://wa.me/${d.startsWith("0") ? "233" + d.slice(1) : d}`;
  };

  return (
    <div>
      {flash && (
        <div className="fixed top-4 inset-x-4 sm:inset-x-auto sm:right-4 z-50 bg-acid text-ink font-display uppercase text-xs px-4 py-3 shadow-lg">{flash}</div>
      )}

      {/* pipeline filters */}
      <div className="flex gap-2 flex-wrap mb-4">
        {["all", "unpaid", ...PIPELINE, ...TERMINAL].map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`btn-og px-3 py-2.5 text-[11px] border-2 ${filter === f ? "bg-acid border-acid text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
            {f === "all" ? "All" : f === "unpaid" ? "⚠ Unpaid" : STATUS_LABEL[f] || f}
            <span className="ml-1 opacity-60">({counts[f] ?? 0})</span>
          </button>
        ))}
      </div>
      <div className="flex gap-2 mb-6">
        <input placeholder="Search order #, name, phone, email…" value={search} onChange={(e) => setSearch(e.target.value)} className="flex-1 px-4 py-2.5 text-sm" />
        <button onClick={exportCsv} className="btn-og bg-bone text-ink px-4 text-[11px] hover:bg-acid">Export CSV</button>
      </div>

      <div className="space-y-3">
        {visible.map((o) => (
          <div key={o.id} className={`border bg-smoke/50 ${o.payment_status !== "paid" && o.status === "pending" ? "border-blood/40" : "border-ash"}`}>
            <button onClick={() => toggle(o.id)} className="w-full flex flex-wrap items-center gap-3 px-4 py-3 text-left text-sm">
              <span className="font-display text-acid">{o.order_number}</span>
              <span className="text-bone">{o.customer_name || "—"}</span>
              <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${o.payment_status === "paid" ? "bg-acid/15 text-acid" : "bg-blood/15 text-blood"}`}>
                {o.payment_status}{o.refund_status === "refunded" ? " · refunded" : ""}
              </span>
              <span className="text-[10px] uppercase tracking-widest px-2 py-1 bg-bone/10 text-bone/70">{STATUS_LABEL[o.status] || o.status}</span>
              <span className="ml-auto text-bone">{money(o.total_amount)}</span>
              <span className="text-bone/30 text-xs">{new Date(o.created_at).toLocaleString()}</span>
            </button>

            {openId === o.id && (
              <div className="border-t border-ash px-4 py-4 grid gap-6 lg:grid-cols-2 text-sm">
                {/* left: customer + payment + items + timeline
                    (min-w-0: long emails otherwise inflate the mobile column) */}
                <div className="space-y-4 min-w-0">
                  <div className="space-y-1.5 text-bone/70">
                    <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40">Customer</p>
                    <p className="text-bone">{o.customer_name || "—"}</p>
                    <p>
                      {o.customer_phone && <a className="text-acid hover:underline" href={`tel:${o.customer_phone}`}>{o.customer_phone}</a>}
                      {wa(o.customer_phone) && <a className="ml-3 text-acid hover:underline" target="_blank" rel="noreferrer" href={wa(o.customer_phone)!}>WhatsApp ↗</a>}
                      {o.customer_email && <a className="ml-3 text-acid hover:underline break-all" href={`mailto:${o.customer_email}`}>{o.customer_email}</a>}
                    </p>
                    <p><span className="text-bone/40">Ship to:</span> {o.shipping_address || "—"}</p>
                    {o.discount_code && <p><span className="text-bone/40">Discount:</span> {o.discount_code} (−{money(o.discount_amount || 0)})</p>}
                  </div>

                  <div className="space-y-1.5 text-bone/70">
                    <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40">Payment</p>
                    <p>
                      {o.payment_method === "moolre_momo" ? "Moolre MoMo" : o.payment_method || "—"}
                      {o.momo_phone ? ` · ${o.momo_phone} (${(o.momo_channel || "").toUpperCase()})` : ""}
                    </p>
                    {o.payment_ref && <p className="text-bone/40 text-xs">Ref: {o.payment_ref}</p>}
                    {o.paid_at && <p className="text-bone/40 text-xs">Paid {new Date(o.paid_at).toLocaleString()}</p>}
                    {o.authenticity_code && (
                      <p className="text-bone/40 text-xs flex flex-wrap items-center gap-2 mt-1">
                        <span>Auth: <span className="text-acid break-all">{o.authenticity_code}</span></span>
                        <a href={`/authenticity-certificate?code=${encodeURIComponent(o.authenticity_code)}`} target="_blank" rel="noreferrer"
                          className="btn-og border border-acid/50 text-acid px-2.5 py-1 text-[10px] hover:bg-acid hover:text-ink">⎙ Certificate</a>
                      </p>
                    )}
                    {o.refund_status === "refunded" && (
                      <p className="text-blood text-xs">Refunded {money(o.refund_amount || 0)} · {o.refunded_at ? new Date(o.refunded_at).toLocaleString() : ""}</p>
                    )}
                  </div>

                  <div>
                    <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40 mb-1">Items</p>
                    {(items[o.id] || []).map((it) => (
                      <p key={it.id} className="text-bone">• {it.product_name} {it.size ? `(${it.size})` : ""} × {it.quantity} — {money(Number(it.price ?? it.unit_price ?? 0) * it.quantity)}</p>
                    ))}
                  </div>

                  {Array.isArray(o.status_history) && o.status_history.length > 0 && (
                    <div>
                      <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40 mb-1">Timeline</p>
                      <div className="border-l-2 border-ash pl-3 space-y-1.5">
                        {o.status_history.slice().reverse().map((h, i) => (
                          <p key={i} className="text-xs text-bone/60">
                            <span className="text-bone">{STATUS_LABEL[h.status] || h.status}</span>
                            {h.note ? ` — ${h.note}` : ""}
                            <span className="text-bone/30"> · {new Date(h.at).toLocaleString()}</span>
                          </p>
                        ))}
                      </div>
                    </div>
                  )}
                </div>

                {/* right: actions */}
                <div className="space-y-4 min-w-0">
                  <div>
                    <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40 mb-1.5">
                      Update status <span className="normal-case tracking-normal">(emails the customer)</span>
                    </p>
                    <input
                      placeholder="Tracking / rider note for the customer (optional)"
                      value={noteDraft[o.id] ?? o.tracking_note ?? ""}
                      onChange={(e) => setNoteDraft((m) => ({ ...m, [o.id]: e.target.value }))}
                      className="w-full px-3 py-2.5 text-xs mb-2"
                    />
                    <div className="flex flex-wrap gap-2">
                      {PIPELINE.map((s) => (
                        <button key={s} disabled={busyId === o.id} onClick={() => setStatus(o, s, true)}
                          className={`btn-og px-3 py-2.5 text-[10px] border-2 ${o.status === s ? "bg-bone border-bone text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
                          {STATUS_LABEL[s]}
                        </button>
                      ))}
                      <button disabled={busyId === o.id}
                        onClick={() => { if (confirm(`Cancel order ${o.order_number}? The customer gets a cancellation email.`)) setStatus(o, "cancelled", true); }}
                        className="btn-og px-3 py-2.5 text-[10px] border-2 border-blood/40 text-blood hover:bg-blood hover:text-bone">
                        Cancel order
                      </button>
                    </div>
                  </div>

                  <div>
                    <p className="font-display uppercase text-[10px] tracking-[0.3em] text-bone/40 mb-1.5">Money</p>
                    <div className="flex flex-wrap gap-2">
                      {o.payment_status !== "paid" && (
                        <button onClick={() => markPaid(o)} className="btn-og px-3 py-2.5 text-[10px] border-2 border-acid/50 text-acid hover:bg-acid hover:text-ink">
                          Mark paid (manual)
                        </button>
                      )}
                      {o.payment_status === "paid" && o.refund_status !== "refunded" && (
                        <button disabled={busyId === o.id} onClick={() => refund(o)}
                          className="btn-og px-3 py-2.5 text-[10px] border-2 border-blood/40 text-blood hover:bg-blood hover:text-bone">
                          {busyId === o.id ? "Working…" : "Refund via MoMo"}
                        </button>
                      )}
                    </div>
                    <p className="text-bone/30 text-[11px] mt-2">
                      Refunds send real money back to the customer's MoMo via Moolre and email them automatically.
                    </p>
                  </div>
                </div>
              </div>
            )}
          </div>
        ))}
        {visible.length === 0 && <p className="text-bone/40 py-10 text-center text-sm">No orders in this view.</p>}
      </div>
    </div>
  );
}
