import { useEffect, useState, FormEvent } from "react";
import { supabase } from "../lib/supabase";
import { CURRENCY } from "../lib/money";
import { discountLabel } from "../lib/discounts";
import type { DiscountCode, DiscountKind, DiscountAudience } from "../lib/types";

const AUDIENCE_LABEL: Record<DiscountAudience, string> = {
  all: "Everyone",
  new: "New customers",
  returning: "Returning customers",
};

const blankForm = {
  code: "",
  discount_type: "percent" as DiscountKind,
  value: "",
  audience: "all" as DiscountAudience,
  min_subtotal: "",
  max_uses: "",
  expires_at: "",
};

export default function AdminDiscounts() {
  const [rows, setRows] = useState<DiscountCode[]>([]);
  const [form, setForm] = useState({ ...blankForm });
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data } = await supabase.from("discount_codes").select("*").order("created_at", { ascending: false });
    setRows((data || []) as DiscountCode[]);
  };
  useEffect(() => { load(); }, []);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setMsg(null);
    const code = form.code.trim().toUpperCase();
    const value = Number(form.value || 0);
    if (!code) { setMsg("A code is required."); return; }
    if (!value || value <= 0) { setMsg("Enter a discount amount greater than zero."); return; }
    if (form.discount_type === "percent" && value > 100) { setMsg("A percentage can't be over 100."); return; }

    const payload: Record<string, unknown> = {
      code,
      discount_type: form.discount_type,
      percentage: form.discount_type === "percent" ? value : null,
      amount_off: form.discount_type === "amount" ? value : null,
      audience: form.audience,
      min_subtotal: form.min_subtotal ? Number(form.min_subtotal) : 0,
      max_uses: form.max_uses ? Number(form.max_uses) : null,
      is_active: true,
    };
    if (form.expires_at) payload.expires_at = new Date(form.expires_at).toISOString();

    const { error } = await supabase.from("discount_codes").insert(payload);
    if (error) setMsg(error.message);
    else { setForm({ ...blankForm }); load(); }
  };

  const toggle = async (d: DiscountCode) => {
    await supabase.from("discount_codes").update({ is_active: !d.is_active }).eq("id", d.id);
    load();
  };
  const del = async (d: DiscountCode) => {
    if (!confirm(`Delete code ${d.code}?`)) return;
    await supabase.from("discount_codes").delete().eq("id", d.id);
    load();
  };

  const inputCls = "px-4 py-3 text-sm bg-transparent border-2 border-ash focus:border-acid focus:outline-none";
  const valueLabel = form.discount_type === "percent" ? "% off" : `${CURRENCY} off`;

  return (
    <div className="max-w-4xl">
      <form onSubmit={add} className="border border-ash p-4 mb-6 grid gap-3">
        <div className="grid sm:grid-cols-3 gap-3">
          <input
            placeholder="CODE" value={form.code}
            onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })}
            className={`${inputCls} font-display uppercase tracking-widest sm:col-span-1`}
          />
          <select
            value={form.discount_type}
            onChange={(e) => setForm({ ...form, discount_type: e.target.value as DiscountKind })}
            className={inputCls}
          >
            <option value="percent">Percent off (%)</option>
            <option value="amount">Amount off ({CURRENCY})</option>
          </select>
          <input
            type="number" min="1" step="any" placeholder={valueLabel} value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
            className={inputCls}
          />
        </div>

        <div className="grid sm:grid-cols-3 gap-3">
          <select
            value={form.audience}
            onChange={(e) => setForm({ ...form, audience: e.target.value as DiscountAudience })}
            className={inputCls}
          >
            <option value="all">Everyone</option>
            <option value="new">New customers only</option>
            <option value="returning">Returning customers only</option>
          </select>
          <input
            type="number" min="0" step="any" placeholder={`Min order (${CURRENCY}, optional)`}
            value={form.min_subtotal}
            onChange={(e) => setForm({ ...form, min_subtotal: e.target.value })}
            className={inputCls}
          />
          <input
            type="number" min="1" step="1" placeholder="Max uses (optional)" value={form.max_uses}
            onChange={(e) => setForm({ ...form, max_uses: e.target.value })}
            className={inputCls}
          />
        </div>

        <div className="grid sm:grid-cols-[1fr_auto] gap-3">
          <label className="flex items-center gap-3 text-xs uppercase tracking-widest text-bone/50">
            Expires
            <input
              type="date" value={form.expires_at}
              onChange={(e) => setForm({ ...form, expires_at: e.target.value })}
              className={`${inputCls} flex-1`}
            />
          </label>
          <button className="btn-og bg-acid text-ink text-xs py-3 px-8 hover:bg-bone">Create code</button>
        </div>
      </form>
      {msg && <p className="text-blood text-sm mb-4">{msg}</p>}

      <div className="border border-ash divide-y divide-ash">
        {rows.map((d) => {
          const aud = (d.audience as DiscountAudience) || "all";
          return (
            <div key={d.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
              <span className="font-display text-acid tracking-widest">{d.code}</span>
              <span className="text-bone">{discountLabel(d)}</span>
              {aud !== "all" && (
                <span className="text-[10px] uppercase tracking-widest px-2 py-1 bg-bone/10 text-bone/60">
                  {AUDIENCE_LABEL[aud]}
                </span>
              )}
              {Number(d.min_subtotal || 0) > 0 && (
                <span className="text-bone/40 text-xs">min {CURRENCY}{Number(d.min_subtotal)}</span>
              )}
              {d.max_uses != null && (
                <span className="text-bone/40 text-xs">{Number(d.used_count || 0)}/{d.max_uses} used</span>
              )}
              {d.expires_at && (
                <span className="text-bone/40 text-xs">until {new Date(d.expires_at).toLocaleDateString()}</span>
              )}
              <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${d.is_active ? "bg-acid/15 text-acid" : "bg-bone/10 text-bone/40"}`}>
                {d.is_active ? "active" : "off"}
              </span>
              <div className="ml-auto flex gap-2">
                <button onClick={() => toggle(d)} className="btn-og border-2 border-ash text-bone/70 px-3 py-1.5 text-[10px] hover:border-bone">{d.is_active ? "Disable" : "Enable"}</button>
                <button onClick={() => del(d)} className="btn-og border-2 border-blood/40 text-blood px-3 py-1.5 text-[10px] hover:bg-blood hover:text-bone">Del</button>
              </div>
            </div>
          );
        })}
        {rows.length === 0 && <p className="px-4 py-8 text-bone/40 text-sm">No discount codes yet.</p>}
      </div>
    </div>
  );
}
