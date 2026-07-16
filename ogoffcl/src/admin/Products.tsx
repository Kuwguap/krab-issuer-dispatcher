import { useEffect, useState, FormEvent } from "react";
import { supabase, uploadImage, publicImageUrl } from "../lib/supabase";
import type { Category, Product } from "../lib/types";
import { money } from "../lib/money";

const EMPTY = {
  name: "", description: "", price: "", product_category: "", category_id: "",
  sizes: "", stock: "", sku: "", is_active: true,
};

export default function AdminProducts() {
  const [rows, setRows] = useState<Product[]>([]);
  const [cats, setCats] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Product | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ ...EMPTY });
  const [files, setFiles] = useState<FileList | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data } = await supabase.from("products").select("*").order("created_at", { ascending: false });
    setRows((data || []) as Product[]);
  };
  useEffect(() => {
    load();
    supabase.from("categories").select("*").order("name").then(({ data }) => setCats((data || []) as Category[]));
  }, []);

  const openCreate = () => { setCreating(true); setEditing(null); setForm({ ...EMPTY }); setFiles(null); setMsg(null); };
  const openEdit = (p: Product) => {
    setEditing(p); setCreating(false); setFiles(null); setMsg(null);
    setForm({
      name: p.name || "", description: p.description || "", price: String(p.price ?? ""),
      product_category: p.product_category || "", category_id: p.category_id || "",
      sizes: (p.sizes || []).join(", "), stock: p.stock == null ? "" : String(p.stock),
      sku: p.sku || "", is_active: !!p.is_active,
    });
  };
  const close = () => { setEditing(null); setCreating(false); };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true); setMsg(null);
    try {
      let imageUrls: string[] = [];
      if (files && files.length > 0) {
        for (const f of Array.from(files)) imageUrls.push(await uploadImage(f, "product-images"));
      }
      const payload: Record<string, unknown> = {
        name: form.name.trim(),
        description: form.description.trim() || null,
        price: Number(form.price || 0),
        product_category: form.product_category.trim() || null,
        category_id: form.category_id || null,
        sizes: form.sizes.split(",").map((s) => s.trim().toUpperCase()).filter(Boolean),
        stock: form.stock === "" ? null : Number(form.stock),
        sku: form.sku.trim() || null,
        is_active: form.is_active,
      };
      if (imageUrls.length > 0) {
        payload.image = imageUrls[0];
        const prevExtra = editing ? (editing.images || []) : [];
        payload.images = [...imageUrls, ...prevExtra.filter((u) => !imageUrls.includes(String(u)))];
      }
      if (editing) {
        const { error } = await supabase.from("products").update(payload).eq("id", editing.id);
        if (error) throw error;
      } else {
        const { error } = await supabase.from("products").insert(payload);
        if (error) throw error;
      }
      await load();
      close();
    } catch (ex) {
      setMsg(ex instanceof Error ? ex.message : "Save failed");
    } finally {
      setBusy(false);
    }
  };

  const toggleActive = async (p: Product) => {
    await supabase.from("products").update({ is_active: !p.is_active }).eq("id", p.id);
    load();
  };

  const del = async (p: Product) => {
    if (!confirm(`Delete "${p.name}"? This cannot be undone.`)) return;
    const { error } = await supabase.from("products").delete().eq("id", p.id);
    if (error) alert("Delete failed: " + error.message + "\n(Tip: products referenced by orders may be protected — deactivate instead.)");
    load();
  };

  const showForm = creating || !!editing;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <p className="text-bone/50 text-sm">{rows.length} products</p>
        <button onClick={openCreate} className="btn-og bg-acid text-ink px-5 py-2.5 text-xs hover:bg-bone">+ New product</button>
      </div>

      {showForm && (
        <form onSubmit={save} className="border border-acid/40 bg-smoke p-6 mb-8 grid gap-4 sm:grid-cols-2">
          <p className="sm:col-span-2 font-display uppercase text-sm text-acid tracking-widest">
            {editing ? `Editing: ${editing.name}` : "New product"}
          </p>
          <input required placeholder="Name *" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="px-3 py-3 text-sm" />
          <input required type="number" step="0.01" placeholder="Price (GHS) *" value={form.price} onChange={(e) => setForm({ ...form, price: e.target.value })} className="px-3 py-3 text-sm" />
          <select value={form.category_id} onChange={(e) => setForm({ ...form, category_id: e.target.value })} className="px-3 py-3 text-sm">
            <option value="">— Collection (category) —</option>
            {cats.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
          <input placeholder="Type label (e.g. T-Shirt, Hoodie)" value={form.product_category} onChange={(e) => setForm({ ...form, product_category: e.target.value })} className="px-3 py-3 text-sm" />
          <input placeholder="Sizes, comma separated (S, M, L, XL)" value={form.sizes} onChange={(e) => setForm({ ...form, sizes: e.target.value })} className="px-3 py-3 text-sm" />
          <input type="number" placeholder="Stock (blank = untracked)" value={form.stock} onChange={(e) => setForm({ ...form, stock: e.target.value })} className="px-3 py-3 text-sm" />
          <input placeholder="SKU" value={form.sku} onChange={(e) => setForm({ ...form, sku: e.target.value })} className="px-3 py-3 text-sm" />
          <label className="flex items-center gap-3 text-sm text-bone/70 px-1">
            <input type="checkbox" checked={form.is_active} onChange={(e) => setForm({ ...form, is_active: e.target.checked })} className="w-4 h-4 accent-[#C8FF00]" />
            Visible in shop
          </label>
          <textarea placeholder="Description" rows={3} value={form.description} onChange={(e) => setForm({ ...form, description: e.target.value })} className="px-3 py-3 text-sm sm:col-span-2" />
          <div className="sm:col-span-2">
            <p className="text-bone/40 text-xs uppercase tracking-widest mb-2">Images (first = main){editing ? " — leave empty to keep current" : ""}</p>
            <input type="file" accept="image/*" multiple onChange={(e) => setFiles(e.target.files)} className="text-sm text-bone/60 file:btn-og file:bg-bone file:text-ink file:border-0 file:px-4 file:py-2 file:mr-4 file:text-xs" />
          </div>
          {msg && <p className="sm:col-span-2 text-blood text-sm">{msg}</p>}
          <div className="sm:col-span-2 flex gap-3">
            <button type="submit" disabled={busy} className="btn-og bg-acid text-ink px-6 py-3 text-xs hover:bg-bone">{busy ? "Saving…" : "Save product"}</button>
            <button type="button" onClick={close} className="btn-og border-2 border-ash text-bone/60 px-6 py-3 text-xs hover:border-bone">Cancel</button>
          </div>
        </form>
      )}

      <div className="grid gap-3">
        {rows.map((p) => (
          <div key={p.id} className="flex items-center gap-4 border border-ash bg-smoke/50 p-3">
            <div className="w-14 h-16 bg-ash overflow-hidden shrink-0">
              {p.image && <img src={publicImageUrl(String(p.image))} alt="" className="w-full h-full object-cover" />}
            </div>
            <div className="flex-1 min-w-0">
              <p className="font-display uppercase text-sm text-bone truncate">{p.name}</p>
              <p className="text-bone/40 text-xs mt-0.5">
                {money(p.price)} · {p.product_category || "—"} · stock {p.stock ?? "∞"}
                {!p.is_active && <span className="text-blood ml-2">HIDDEN</span>}
              </p>
            </div>
            <button onClick={() => toggleActive(p)} className="btn-og border-2 border-ash text-bone/70 px-3 py-1.5 text-[10px] hover:border-bone">
              {p.is_active ? "Hide" : "Show"}
            </button>
            <button onClick={() => openEdit(p)} className="btn-og bg-bone text-ink px-3 py-1.5 text-[10px] hover:bg-acid">Edit</button>
            <button onClick={() => del(p)} className="btn-og border-2 border-blood/40 text-blood px-3 py-1.5 text-[10px] hover:bg-blood hover:text-bone">Del</button>
          </div>
        ))}
      </div>
    </div>
  );
}
