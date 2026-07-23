import { useEffect, useState } from "react";
import { supabase, uploadImage, publicImageUrl } from "../lib/supabase";

interface GalleryRow { id: string; image_path: string; [key: string]: unknown; }

export default function AdminGallery() {
  const [rows, setRows] = useState<GalleryRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data } = await supabase.from("gallery_images").select("*");
    setRows((data || []) as GalleryRow[]);
  };
  useEffect(() => { load(); }, []);

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true); setMsg(null);
    try {
      for (const f of Array.from(files)) {
        const url = await uploadImage(f, "gallery");
        const { error } = await supabase.from("gallery_images").insert({ image_path: url });
        if (error) throw error;
      }
      load();
    } catch (ex) {
      setMsg(ex instanceof Error ? ex.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  };

  const del = async (r: GalleryRow) => {
    if (!confirm("Remove this image from the gallery?")) return;
    await supabase.from("gallery_images").delete().eq("id", r.id);
    load();
  };

  return (
    <div>
      <div className="border border-dashed border-ash p-8 text-center mb-8">
        <p className="text-bone/50 text-sm mb-4">Upload lifestyle / lookbook shots</p>
        <input type="file" accept="image/*" multiple disabled={busy} onChange={(e) => upload(e.target.files)} className="text-sm text-bone/60 file:btn-og file:bg-acid file:text-ink file:border-0 file:px-5 file:py-2.5 file:mr-4 file:text-xs" />
        {busy && <p className="text-acid text-xs mt-3 animate-pulseSoft">Uploading…</p>}
        {msg && <p className="text-blood text-xs mt-3">{msg}</p>}
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {rows.map((r) => (
          <div key={r.id} className="group relative aspect-square bg-smoke border border-ash overflow-hidden">
            <img src={publicImageUrl(String(r.image_path))} alt="" className="w-full h-full object-cover" />
            {/* visible by default on touch — hover-reveal made Remove unreachable on phones */}
            <button onClick={() => del(r)} className="absolute inset-x-0 bottom-0 bg-blood text-bone text-xs font-display uppercase py-2.5 sm:translate-y-full sm:group-hover:translate-y-0 transition-transform">
              Remove
            </button>
          </div>
        ))}
        {rows.length === 0 && <p className="col-span-full text-bone/40 text-sm py-6 text-center">Gallery is empty.</p>}
      </div>
    </div>
  );
}
