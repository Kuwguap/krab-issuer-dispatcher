import { useEffect, useState } from "react";
import { supabase } from "../lib/supabase";
import { money } from "../lib/money";
import type { OrderRow } from "../lib/types";

const STATUSES = ["pending", "confirmed", "processing", "shipped", "delivered", "cancelled"];

interface ItemRow { id: string; product_name: string; size: string | null; quantity: number; price: number | null; unit_price: number | null; }

export default function AdminOrders() {
  const [rows, setRows] = useState<OrderRow[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [items, setItems] = useState<Record<string, ItemRow[]>>({});
  const [filter, setFilter] = useState<"all" | "paid" | "unpaid">("all");

  const load = async () => {
    const { data } = await supabase
      .from("orders")
      .select("id, order_number, status, payment_status, payment_method, total_amount, currency, customer_name, customer_email, customer_phone, shipping_address, discount_code, discount_amount, created_at")
      .order("created_at", { ascending: false });
    setRows((data || []) as OrderRow[]);
  };
  useEffect(() => { load(); }, []);

  const toggle = async (id: string) => {
    if (openId === id) { setOpenId(null); return; }
    setOpenId(id);
    if (!items[id]) {
      const { data } = await supabase.from("order_items").select("id, product_name, size, quantity, price, unit_price").eq("order_id", id);
      setItems((m) => ({ ...m, [id]: (data || []) as ItemRow[] }));
    }
  };

  const setStatus = async (id: string, status: string) => {
    await supabase.from("orders").update({ status }).eq("id", id);
    load();
  };
  const setPay = async (id: string, payment_status: string) => {
    await supabase.from("orders").update({ payment_status }).eq("id", id);
    load();
  };

  const visible = rows.filter((o) =>
    filter === "all" ? true : filter === "paid" ? o.payment_status === "paid" : o.payment_status !== "paid",
  );

  return (
    <div>
      <div className="flex gap-2 mb-6">
        {(["all", "paid", "unpaid"] as const).map((f) => (
          <button key={f} onClick={() => setFilter(f)} className={`btn-og px-4 py-2 text-xs border-2 ${filter === f ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`}>
            {f.toUpperCase()}
          </button>
        ))}
        <span className="ml-auto text-bone/40 text-sm self-center">{visible.length} orders</span>
      </div>

      <div className="space-y-3">
        {visible.map((o) => (
          <div key={o.id} className="border border-ash bg-smoke/50">
            <button onClick={() => toggle(o.id)} className="w-full flex flex-wrap items-center gap-3 px-4 py-3 text-left text-sm">
              <span className="font-display text-acid">{o.order_number}</span>
              <span className="text-bone">{o.customer_name || "—"}</span>
              <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${o.payment_status === "paid" ? "bg-acid/15 text-acid" : "bg-blood/15 text-blood"}`}>{o.payment_status}</span>
              <span className="text-[10px] uppercase tracking-widest px-2 py-1 bg-bone/10 text-bone/70">{o.status}</span>
              <span className="ml-auto text-bone">{money(o.total_amount)}</span>
              <span className="text-bone/30 text-xs">{new Date(o.created_at).toLocaleString()}</span>
            </button>
            {openId === o.id && (
              <div className="border-t border-ash px-4 py-4 grid gap-4 md:grid-cols-2 text-sm">
                <div className="space-y-1.5 text-bone/70">
                  <p><span className="text-bone/40">Email:</span> {o.customer_email || "—"}</p>
                  <p><span className="text-bone/40">Phone:</span> {o.customer_phone || "—"}</p>
                  <p><span className="text-bone/40">Ship to:</span> {o.shipping_address || "—"}</p>
                  {o.discount_code && <p><span className="text-bone/40">Discount:</span> {o.discount_code} (−{money(o.discount_amount || 0)})</p>}
                  <div className="pt-2">
                    <p className="text-bone/40 mb-1">Items:</p>
                    {(items[o.id] || []).map((it) => (
                      <p key={it.id} className="text-bone">• {it.product_name} {it.size ? `(${it.size})` : ""} × {it.quantity} — {money(Number(it.price ?? it.unit_price ?? 0) * it.quantity)}</p>
                    ))}
                  </div>
                </div>
                <div className="space-y-3">
                  <div>
                    <p className="text-bone/40 text-xs uppercase tracking-widest mb-1.5">Order status</p>
                    <div className="flex flex-wrap gap-1.5">
                      {STATUSES.map((s) => (
                        <button key={s} onClick={() => setStatus(o.id, s)} className={`btn-og px-3 py-1.5 text-[10px] border-2 ${o.status === s ? "bg-bone border-bone text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
                          {s}
                        </button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="text-bone/40 text-xs uppercase tracking-widest mb-1.5">Payment</p>
                    <div className="flex gap-1.5">
                      {["pending", "paid", "refunded"].map((s) => (
                        <button key={s} onClick={() => setPay(o.id, s)} className={`btn-og px-3 py-1.5 text-[10px] border-2 ${o.payment_status === s ? "bg-acid border-acid text-ink" : "border-ash text-bone/60 hover:border-bone"}`}>
                          {s}
                        </button>
                      ))}
                    </div>
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
