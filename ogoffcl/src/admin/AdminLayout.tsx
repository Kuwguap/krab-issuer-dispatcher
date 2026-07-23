import { useState, FormEvent, ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";

/**
 * Admin gate. Password from VITE_ADMIN_PASSWORD (recommended to set on Vercel).
 * Defaults to OGADMIN26 until the env var is added.
 */
const ADMIN_PASSWORD = String(import.meta.env.VITE_ADMIN_PASSWORD ?? "OGADMIN26").replace(/\\r|\\n/g, "").trim() || "OGADMIN26";
const KEY = "ogoffcl_admin_v1";
const PW_KEY = "ogoffcl_admin_pw";

/** The password the admin typed — sent as x-admin-password to /api/admin/* .*/
export function adminPassword(): string {
  try { return sessionStorage.getItem(PW_KEY) || ADMIN_PASSWORD; } catch { return ADMIN_PASSWORD; }
}

/** fetch wrapper for admin API calls. */
export async function adminApi(path: string, body: unknown): Promise<{ ok: boolean; [k: string]: unknown }> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-admin-password": adminPassword() },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok && j.ok !== false, status: r.status, ...j };
}

function Gate({ children }: { children: ReactNode }) {
  const [ok, setOk] = useState(() => {
    try { return sessionStorage.getItem(KEY) === "1"; } catch { return false; }
  });
  const [pw, setPw] = useState("");
  const [bad, setBad] = useState(false);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (pw === ADMIN_PASSWORD) {
      try {
        sessionStorage.setItem(KEY, "1");
        sessionStorage.setItem(PW_KEY, pw);
      } catch {}
      setOk(true);
    } else setBad(true);
  };

  if (ok) return <>{children}</>;
  return (
    <div className="min-h-[70vh] flex items-center justify-center px-4">
      <form onSubmit={submit} className="w-full max-w-sm border border-ash bg-smoke p-8">
        <p className="display-xl text-3xl text-bone mb-2">Admin<span className="text-acid">.</span></p>
        <p className="text-bone/40 text-xs uppercase tracking-widest mb-6">Staff only</p>
        <input
          autoFocus
          type="password"
          value={pw}
          onChange={(e) => { setPw(e.target.value); setBad(false); }}
          placeholder="Admin password"
          className="w-full px-4 py-3 text-sm mb-3"
        />
        {bad && <p className="text-blood text-xs mb-3">Wrong password.</p>}
        <button className="btn-og w-full bg-acid text-ink py-3 text-sm hover:bg-bone">Enter</button>
      </form>
    </div>
  );
}

const tabs = [
  { to: "/admin", label: "Dashboard", end: true },
  { to: "/admin/products", label: "Products" },
  { to: "/admin/orders", label: "Orders" },
  { to: "/admin/categories", label: "Categories" },
  { to: "/admin/discounts", label: "Discounts" },
  { to: "/admin/gallery", label: "Gallery" },
  { to: "/admin/site", label: "Site & Mail" },
  { to: "/admin/email", label: "Email Studio" },
  { to: "/admin/analytics", label: "Analytics" },
];

export default function AdminLayout() {
  return (
    <Gate>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10">
        <h1 className="display-xl text-5xl text-bone mb-8">OG <span className="text-stroke-acid">Admin</span></h1>
        <div className="flex gap-2 flex-wrap border-b border-ash pb-4 mb-8">
          {tabs.map((t) => (
            <NavLink
              key={t.to}
              to={t.to}
              end={t.end as boolean | undefined}
              className={({ isActive }) =>
                `btn-og px-4 py-3 sm:py-2 text-xs border-2 ${isActive ? "bg-acid border-acid text-ink" : "border-ash text-bone/70 hover:border-bone"}`
              }
            >
              {t.label}
            </NavLink>
          ))}
        </div>
        <Outlet />
      </div>
    </Gate>
  );
}
