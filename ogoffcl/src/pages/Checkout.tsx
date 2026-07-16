import { useMemo, useState, FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { supabase, PAYSTACK_PUBLIC_KEY } from "../lib/supabase";
import { money, CURRENCY } from "../lib/money";
import { useCart } from "../store/CartContext";
import Reveal from "../components/Reveal";

declare global {
  interface Window {
    PaystackPop?: { setup: (opts: Record<string, unknown>) => { openIframe: () => void } };
  }
}

interface Applied {
  code: string;
  percentage: number;
}

export default function Checkout() {
  const { items, subtotal, clear } = useCart();
  const navigate = useNavigate();
  const [form, setForm] = useState({ name: "", email: "", phone: "", address: "", city: "", note: "" });
  const [codeInput, setCodeInput] = useState("");
  const [applied, setApplied] = useState<Applied | null>(null);
  const [codeMsg, setCodeMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const discountAmount = useMemo(
    () => (applied ? Math.round(subtotal * (applied.percentage / 100) * 100) / 100 : 0),
    [applied, subtotal],
  );
  const total = Math.max(0, subtotal - discountAmount);

  const set = (k: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  const applyCode = async () => {
    const code = codeInput.trim().toUpperCase();
    if (!code) return;
    setCodeMsg(null);
    const { data, error } = await supabase
      .from("discount_codes")
      .select("*")
      .eq("code", code)
      .eq("is_active", true)
      .single();
    if (error || !data) {
      setApplied(null);
      setCodeMsg("Invalid or inactive code.");
      return;
    }
    if (data.expires_at && new Date(data.expires_at) < new Date()) {
      setApplied(null);
      setCodeMsg("This code has expired.");
      return;
    }
    const pct = Number(data.percentage ?? 0);
    if (!pct) {
      setApplied(null);
      setCodeMsg("This code has no discount attached.");
      return;
    }
    setApplied({ code, percentage: pct });
    setCodeMsg(`Applied — ${pct}% off.`);
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    if (items.length === 0) return;
    if (!form.name.trim() || !form.email.includes("@") || !form.phone.trim() || !form.address.trim()) {
      setErr("Fill in your name, a valid email, phone and delivery address.");
      return;
    }
    setBusy(true);
    try {
      const orderNumber = `OG-${Date.now().toString(36).toUpperCase()}${Math.floor(Math.random() * 90 + 10)}`;
      const shipping = [form.address.trim(), form.city.trim()].filter(Boolean).join(", ") + (form.note.trim() ? ` — Note: ${form.note.trim()}` : "");

      // upsert-ish customer by email (same table the old site used)
      let customerId: string | null = null;
      try {
        const { data: existing } = await supabase.from("customers").select("id").eq("email", form.email.trim()).limit(1);
        if (existing && existing[0]) {
          customerId = existing[0].id;
          await supabase.from("customers").update({ name: form.name.trim(), phone: form.phone.trim(), address: form.address.trim(), city: form.city.trim() || null }).eq("id", customerId);
        } else {
          const { data: created } = await supabase
            .from("customers")
            .insert({ name: form.name.trim(), full_name: form.name.trim(), email: form.email.trim(), phone: form.phone.trim(), address: form.address.trim(), city: form.city.trim() || null, country: "Ghana" })
            .select("id")
            .single();
          customerId = created?.id ?? null;
        }
      } catch { /* customer row is best-effort */ }

      const { data: order, error: orderErr } = await supabase
        .from("orders")
        .insert({
          order_number: orderNumber,
          customer_id: customerId,
          customer_name: form.name.trim(),
          customer_email: form.email.trim(),
          customer_phone: form.phone.trim(),
          shipping_address: shipping,
          status: "pending",
          payment_status: "pending",
          payment_method: "paystack",
          currency: CURRENCY,
          total_amount: total,
          discount_code: applied?.code ?? null,
          discount_amount: discountAmount || null,
        })
        .select("id, order_number")
        .single();
      if (orderErr || !order) throw new Error(orderErr?.message || "Could not create the order.");

      const itemRows = items.map((i) => ({
        order_id: order.id,
        product_id: i.productId,
        product_name: i.name,
        product_image: i.image || null,
        size: i.size,
        quantity: i.qty,
        unit_price: i.price,
        price: i.price,
        subtotal: Math.round(i.price * i.qty * 100) / 100,
      }));
      const { error: itemsErr } = await supabase.from("order_items").insert(itemRows);
      if (itemsErr) throw new Error(itemsErr.message);

      // Paystack inline
      if (!window.PaystackPop || !PAYSTACK_PUBLIC_KEY) {
        throw new Error("Payment library not loaded. Check your connection and retry.");
      }
      const handler = window.PaystackPop.setup({
        key: PAYSTACK_PUBLIC_KEY,
        email: form.email.trim(),
        amount: Math.round(total * 100),
        currency: CURRENCY,
        ref: order.order_number,
        metadata: { custom_fields: [{ display_name: "Order", variable_name: "order_number", value: order.order_number }] },
        callback: (resp: { reference: string }) => {
          (async () => {
            try {
              await supabase
                .from("orders")
                .update({ payment_status: "paid", status: "confirmed", payment_method: "paystack" })
                .eq("id", order.id);
              // best-effort stock decrement
              for (const i of items) {
                const { data: prow } = await supabase.from("products").select("id, stock").eq("id", i.productId).single();
                if (prow && prow.stock !== null && prow.stock !== undefined) {
                  await supabase.from("products").update({ stock: Math.max(0, Number(prow.stock) - i.qty) }).eq("id", i.productId);
                }
              }
            } catch {}
            clear();
            navigate(`/order-confirmation?ref=${encodeURIComponent(order.order_number)}&paid=1&reference=${encodeURIComponent(resp.reference)}`);
          })();
        },
        onClose: () => {
          setBusy(false);
          navigate(`/order-confirmation?ref=${encodeURIComponent(order.order_number)}&paid=0`);
        },
      });
      handler.openIframe();
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  };

  if (items.length === 0) {
    return (
      <div className="max-w-2xl mx-auto px-4 py-32 text-center">
        <p className="display-xl text-6xl text-bone/15">Empty bag</p>
        <p className="text-bone/50 uppercase tracking-widest text-sm mt-5">Add something to the cart before checkout.</p>
        <Link to="/shop" className="btn-og bg-acid text-ink px-8 py-4 text-sm mt-8 inline-flex hover:bg-bone">
          Back to the shop →
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-7xl mx-auto px-4 sm:px-6 py-12">
      <Reveal>
        <h1 className="display-xl text-6xl sm:text-7xl text-bone mb-12">
          Check<span className="text-stroke-acid">out</span>
        </h1>
      </Reveal>

      <div className="grid lg:grid-cols-[1fr_420px] gap-12">
        {/* form */}
        <form onSubmit={submit} className="space-y-5 order-2 lg:order-1">
          <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50">Delivery details</p>
          <div className="grid sm:grid-cols-2 gap-4">
            <input required placeholder="Full name *" value={form.name} onChange={set("name")} className="px-4 py-4 text-sm w-full" />
            <input required type="email" placeholder="Email *" value={form.email} onChange={set("email")} className="px-4 py-4 text-sm w-full" />
          </div>
          <input required type="tel" placeholder="Phone (for the rider) *" value={form.phone} onChange={set("phone")} className="px-4 py-4 text-sm w-full" />
          <input required placeholder="Delivery address *" value={form.address} onChange={set("address")} className="px-4 py-4 text-sm w-full" />
          <div className="grid sm:grid-cols-2 gap-4">
            <input placeholder="City / Area" value={form.city} onChange={set("city")} className="px-4 py-4 text-sm w-full" />
            <input placeholder="Delivery note (optional)" value={form.note} onChange={set("note")} className="px-4 py-4 text-sm w-full" />
          </div>

          {err && <p className="text-blood text-sm border border-blood/40 bg-blood/10 px-4 py-3">{err}</p>}

          <button type="submit" disabled={busy} className={`btn-og w-full py-5 text-sm ${busy ? "bg-ash text-bone/40" : "bg-acid text-ink hover:bg-bone"}`}>
            {busy ? "Opening secure payment…" : `Pay ${money(total)} with Paystack →`}
          </button>
          <p className="text-bone/30 text-[11px] uppercase tracking-widest text-center">
            Card · Mobile money · Bank — secured by Paystack
          </p>
        </form>

        {/* summary */}
        <aside className="order-1 lg:order-2">
          <div className="border border-ash bg-smoke/60 p-6 lg:sticky lg:top-24">
            <p className="font-display uppercase text-xs tracking-[0.3em] text-bone/50 mb-5">Order summary</p>
            <div className="space-y-4 max-h-72 overflow-y-auto pr-1">
              {items.map((i) => (
                <div key={i.productId + (i.size || "")} className="flex gap-3 items-center">
                  <div className="w-14 h-16 bg-ash overflow-hidden shrink-0">
                    {i.image && <img src={i.image} alt="" className="w-full h-full object-cover" />}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="font-display uppercase text-xs text-bone truncate">{i.name}</p>
                    <p className="text-bone/40 text-[11px] uppercase mt-0.5">{i.size ? `Size ${i.size} · ` : ""}Qty {i.qty}</p>
                  </div>
                  <span className="text-bone/80 text-sm">{money(i.price * i.qty)}</span>
                </div>
              ))}
            </div>

            {/* discount */}
            <div className="mt-6 flex">
              <input
                placeholder="DISCOUNT CODE"
                value={codeInput}
                onChange={(e) => setCodeInput(e.target.value)}
                className="flex-1 px-3 py-3 text-xs font-display uppercase tracking-[0.2em]"
              />
              <button type="button" onClick={applyCode} className="btn-og bg-bone text-ink px-5 text-xs hover:bg-acid">
                Apply
              </button>
            </div>
            {codeMsg && <p className={`text-xs mt-2 ${applied ? "text-acid" : "text-blood"}`}>{codeMsg}</p>}

            <div className="border-t border-ash mt-6 pt-4 space-y-2 text-sm">
              <div className="flex justify-between text-bone/60"><span>Subtotal</span><span>{money(subtotal)}</span></div>
              {applied && (
                <div className="flex justify-between text-acid"><span>{applied.code} (−{applied.percentage}%)</span><span>−{money(discountAmount)}</span></div>
              )}
              <div className="flex justify-between text-bone/60"><span>Delivery</span><span>Paid to rider / arranged</span></div>
              <div className="flex justify-between font-display uppercase text-bone text-lg pt-2">
                <span>Total</span><span className="text-acid">{money(total)}</span>
              </div>
            </div>
          </div>
        </aside>
      </div>
    </div>
  );
}
