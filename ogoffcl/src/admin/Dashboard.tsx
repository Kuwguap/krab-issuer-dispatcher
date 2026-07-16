import { useEffect, useState } from "react";
import { supabase } from "../lib/supabase";
import { money } from "../lib/money";
import type { OrderRow } from "../lib/types";

export default function Dashboard() {
  const [stats, setStats] = useState({ products: 0, orders: 0, revenue: 0, pending: 0 });
  const [recent, setRecent] = useState<OrderRow[]>([]);

  useEffect(() => {
    (async () => {
      const [{ count: pc }, orders] = await Promise.all([
        supabase.from("products").select("id", { count: "exact", head: true }),
        supabase.from("orders").select("id, order_number, status, payment_status, total_amount, customer_name, customer_email, customer_phone, shipping_address, created_at").order("created_at", { ascending: false }),
      ]);
      const rows = (orders.data || []) as OrderRow[];
      const paid = rows.filter((o) => o.payment_status === "paid");
      setStats({
        products: pc || 0,
        orders: rows.length,
        revenue: paid.reduce((a, o) => a + Number(o.total_amount || 0), 0),
        pending: rows.filter((o) => o.payment_status !== "paid").length,
      });
      setRecent(rows.slice(0, 8));
    })();
  }, []);

  return (
    <div>
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-10">
        {[
          ["Products", String(stats.products)],
          ["Orders", String(stats.orders)],
          ["Paid revenue", money(stats.revenue)],
          ["Unpaid orders", String(stats.pending)],
        ].map(([label, val]) => (
          <div key={label} className="border border-ash bg-smoke p-5">
            <p className="text-bone/40 text-[11px] uppercase tracking-[0.25em]">{label}</p>
            <p className="font-display text-3xl text-acid mt-2 break-all">{val}</p>
          </div>
        ))}
      </div>

      <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-4">Recent orders</p>
      <div className="border border-ash divide-y divide-ash">
        {recent.map((o) => (
          <div key={o.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
            <span className="font-display text-acid">{o.order_number}</span>
            <span className="text-bone/70">{o.customer_name || "—"}</span>
            <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${o.payment_status === "paid" ? "bg-acid/15 text-acid" : "bg-blood/15 text-blood"}`}>
              {o.payment_status}
            </span>
            <span className="ml-auto text-bone">{money(o.total_amount)}</span>
            <span className="text-bone/30 text-xs">{new Date(o.created_at).toLocaleDateString()}</span>
          </div>
        ))}
        {recent.length === 0 && <p className="px-4 py-8 text-bone/40 text-sm">No orders yet.</p>}
      </div>
    </div>
  );
}
