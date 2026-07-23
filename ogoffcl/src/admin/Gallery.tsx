import { useEffect, useState } from "react";
import { supabase, uploadImage, publicImageUrl, STORAGE_BUCKET } from "../lib/supabase";

/**
 * Brand photo gallery (shoots, lookbook, lifestyle).
 *
 * Backed DIRECTLY by the storage bucket's gallery/ folder — the old
 * gallery_images table has no anon INSERT policy, so every upload stored the
 * file fine and then died on the row insert ("upload failed" with the image
 * actually in the bucket). Listing the folder needs no table and no new RLS.
 *
 * Images are uploaded as-is (no resizing, no re-encoding) — original quality
 * is preserved byte-for-byte. Tiles are square previews; tap opens full-res.
 */
interface Shot {
  name: string;
  url: string;
  size: number;
  createdAt: string;
}

export default function AdminGallery() {
  const [shots, setShots] = useState<Shot[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const load = async () => {
    const { data, error } = await supabase.storage
      .from(STORAGE_BUCKET)
      .list("gallery", { limit: 500, sortBy: { column: "created_at", order: "desc" } });
    if (error) { setMsg(`Could not load the gallery: ${error.message}`); return; }
    setShots(
      (data || [])
        .filter((o) => o.name && !o.name.startsWith(".") && (o.metadata as { mimetype?: string } | null)?.mimetype?.startsWith("image/"))
        .map((o) => ({
          name: o.name,
          url: publicImageUrl(`gallery/${o.name}`),
          size: Number((o.metadata as { size?: number } | null)?.size || 0),
          createdAt: String(o.created_at || ""),
        })),
    );
  };
  useEffect(() => { load(); }, []);

  const upload = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setBusy(true); setMsg(null);
    let done = 0, failed = 0;
    for (const f of Array.from(files)) {
      setProgress(`Uploading ${done + failed + 1}/${files.length} — ${f.name}`);
      try {
        // raw file upload — no compression or re-encode, full original quality
        await uploadImage(f, "gallery");
        done += 1;
      } catch (ex) {
        failed += 1;
        setMsg(`${f.name}: ${ex instanceof Error ? ex.message : "upload failed"}`);
      }
    }
    setProgress(null);
    setBusy(false);
    if (done > 0 && failed === 0) setMsg(`${done} photo${done === 1 ? "" : "s"} uploaded ✓`);
    load();
  };

  const del = async (s: Shot) => {
    if (!confirm(`Remove ${s.name} from the gallery?`)) return;
    const { error } = await supabase.storage.from(STORAGE_BUCKET).remove([`gallery/${s.name}`]);
    if (error) { setMsg(`Could not remove: ${error.message}`); return; }
    setShots((prev) => prev.filter((x) => x.name !== s.name));
  };

  const mb = (n: number) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB` : `${Math.round(n / 1024)} KB`);

  return (
    <div>
      <div className="border border-dashed border-ash p-8 text-center mb-8">
        <p className="text-bone/50 text-sm mb-4">Upload lifestyle / lookbook shots</p>
        <input
          type="file" accept="image/*" multiple disabled={busy}
          onChange={(e) => { upload(e.target.files); e.target.value = ""; }}
          className="w-full max-w-full text-sm text-bone/60 file:btn-og file:bg-acid file:text-ink file:border-0 file:px-5 file:py-2.5 file:mr-4 file:text-xs"
        />
        {progress && <p className="text-acid text-xs mt-3 animate-pulseSoft">{progress}</p>}
        {msg && <p className={`text-xs mt-3 ${/✓/.test(msg) ? "text-acid" : "text-blood"}`}>{msg}</p>}
        <p className="text-bone/25 text-[11px] mt-3 uppercase tracking-[0.2em]">Stored at original quality — no compression</p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        {shots.map((s) => (
          <div key={s.name} className="group relative aspect-square bg-smoke border border-ash overflow-hidden">
            {/* tile is a preview crop; the link opens the untouched full-res file */}
            <a href={s.url} target="_blank" rel="noreferrer" className="block w-full h-full">
              <img src={s.url} alt="" loading="lazy" className="w-full h-full object-cover" />
            </a>
            <span className="absolute top-0 right-0 bg-ink/70 text-bone/60 text-[9px] px-1.5 py-0.5">{mb(s.size)}</span>
            {/* visible by default on touch — hover-reveal made Remove unreachable on phones */}
            <button onClick={() => del(s)} className="absolute inset-x-0 bottom-0 bg-blood text-bone text-xs font-display uppercase py-2.5 sm:translate-y-full sm:group-hover:translate-y-0 transition-transform">
              Remove
            </button>
          </div>
        ))}
        {shots.length === 0 && <p className="col-span-full text-bone/40 text-sm py-6 text-center">Gallery is empty.</p>}
      </div>
    </div>
  );
}
