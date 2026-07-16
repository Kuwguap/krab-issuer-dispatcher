import { useEffect, useState, FormEvent } from "react";
import { supabase } from "../lib/supabase";
import type { DiscountCode } from "../lib/types";

export default function AdminDiscounts() {
  const [rows, setRows] = useState<DiscountCode[]>([]);
  const [form, setForm] = useState({ code: "", percentage: "", expires_at: "" });
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data } = await supabase.from("discount_codes").select("*").order("created_at", { ascending: false });
    setRows((data || []) as DiscountCode[]);
  };
  useEffect(() => { load(); }, []);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setMsg(null);
    const payload: Record<string, unknown> = {
      code: form.code.trim().toUpperCase(),
      percentage: Number(form.percentage || 0),
      is_active: true,
    };
    if (form.expires_at) payload.expires_at = new Date(form.expires_at).toISOString();
    if (!payload.code || !payload.percentage) { setMsg("Code and percentage are required."); return; }
    const { error } = await supabase.from("discount_codes").insert(payload);
    if (error) setMsg(error.message);
    else { setForm({ code: "", percentage: "", expires_at: "" }); load(); }
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

  return (
    <div className="max-w-3xl">
      <form onSubmit={add} className="grid sm:grid-cols-4 gap-2 mb-6">
        <input placeholder="CODE" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value.toUpperCase() })} className="px-4 py-3 text-sm font-display uppercase tracking-widest" />
        <input type="number" min="1" max="100" placeholder="% off" value={form.percentage} onChange={(e) => setForm({ ...form, percentage: e.target.value })} className="px-4 py-3 text-sm" />
        <input type="date" value={form.expires_at} onChange={(e) => setForm({ ...form, expires_at: e.target.value })} className="px-4 py-3 text-sm" />
        <button className="btn-og bg-acid text-ink text-xs hover:bg-bone">Create</button>
      </form>
      {msg && <p className="text-blood text-sm mb-4">{msg}</p>}
      <div className="border border-ash divide-y divide-ash">
        {rows.map((d) => (
          <div key={d.id} className="flex flex-wrap items-center gap-3 px-4 py-3 text-sm">
            <span className="font-display text-acid tracking-widest">{d.code}</span>
            <span className="text-bone">{Number(d.percentage || 0)}% off</span>
            {d.expires_at && <span className="text-bone/40 text-xs">until {new Date(d.expires_at).toLocaleDateString()}</span>}
            <span className={`text-[10px] uppercase tracking-widest px-2 py-1 ${d.is_active ? "bg-acid/15 text-acid" : "bg-bone/10 text-bone/40"}`}>
              {d.is_active ? "active" : "off"}
            </span>
            <div className="ml-auto flex gap-2">
              <button onClick={() => toggle(d)} className="btn-og border-2 border-ash text-bone/70 px-3 py-1.5 text-[10px] hover:border-bone">{d.is_active ? "Disable" : "Enable"}</button>
              <button onClick={() => del(d)} className="btn-og border-2 border-blood/40 text-blood px-3 py-1.5 text-[10px] hover:bg-blood hover:text-bone">Del</button>
            </div>
          </div>
        ))}
        {rows.length === 0 && <p className="px-4 py-8 text-bone/40 text-sm">No discount codes yet.</p>}
      </div>
    </div>
  );
}
