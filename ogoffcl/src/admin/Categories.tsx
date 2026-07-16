import { useEffect, useState, FormEvent } from "react";
import { supabase } from "../lib/supabase";
import type { Category } from "../lib/types";

const slugify = (s: string) => s.toLowerCase().trim().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");

export default function AdminCategories() {
  const [rows, setRows] = useState<Category[]>([]);
  const [name, setName] = useState("");
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data } = await supabase.from("categories").select("*").order("name");
    setRows((data || []) as Category[]);
  };
  useEffect(() => { load(); }, []);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setMsg(null);
    const n = name.trim();
    if (!n) return;
    const { error } = await supabase.from("categories").insert({ name: n, slug: slugify(n) });
    if (error) setMsg(error.message);
    else { setName(""); load(); }
  };

  const del = async (c: Category) => {
    if (!confirm(`Delete category "${c.name}"?`)) return;
    const { error } = await supabase.from("categories").delete().eq("id", c.id);
    if (error) alert("Delete failed: " + error.message + "\n(Categories with products attached are protected.)");
    load();
  };

  return (
    <div className="max-w-2xl">
      <form onSubmit={add} className="flex gap-2 mb-6">
        <input placeholder="New collection name" value={name} onChange={(e) => setName(e.target.value)} className="flex-1 px-4 py-3 text-sm" />
        <button className="btn-og bg-acid text-ink px-6 text-xs hover:bg-bone">Add</button>
      </form>
      {msg && <p className="text-blood text-sm mb-4">{msg}</p>}
      <div className="border border-ash divide-y divide-ash">
        {rows.map((c) => (
          <div key={c.id} className="flex items-center gap-3 px-4 py-3">
            <span className="font-display uppercase text-sm text-bone">{c.name}</span>
            <span className="text-bone/30 text-xs">/{c.slug}</span>
            <button onClick={() => del(c)} className="ml-auto btn-og border-2 border-blood/40 text-blood px-3 py-1.5 text-[10px] hover:bg-blood hover:text-bone">Del</button>
          </div>
        ))}
      </div>
    </div>
  );
}
