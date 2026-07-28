import { useEffect, useState, FormEvent, ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";

/**
 * Admin gate. The password lives ONLY in the server env (ADMIN_API_PASSWORD
 * on Vercel) — the typed password is validated by the API, so nothing about
 * it ever ships in the client bundle.
 *
 * Session hygiene: the stored password is re-validated on entry, every admin
 * API 401 kills the session on the spot, and sessions expire after 12h — a
 * stale session always lands back on the login form, never on a half-broken
 * admin full of "unauthorized" errors.
 */
const KEY = "ogoffcl_admin_v1";
const PW_KEY = "ogoffcl_admin_pw";
const AT_KEY = "ogoffcl_admin_at";
const SESSION_MAX_MS = 12 * 60 * 60 * 1000; // 12h

/** The password the admin typed — sent as x-admin-password to /api/admin/* .*/
export function adminPassword(): string {
  try { return sessionStorage.getItem(PW_KEY) || ""; } catch { return ""; }
}

export function clearAdminSession() {
  try {
    sessionStorage.removeItem(KEY);
    sessionStorage.removeItem(PW_KEY);
    sessionStorage.removeItem(AT_KEY);
  } catch {}
}

function sessionFresh(): boolean {
  try {
    if (sessionStorage.getItem(KEY) !== "1") return false;
    const at = Number(sessionStorage.getItem(AT_KEY) || 0);
    return at > 0 && Date.now() - at < SESSION_MAX_MS;
  } catch { return false; }
}

/** fetch wrapper for admin API calls — a 401 kills the session and returns
 *  to the login form instead of leaving a broken admin behind. */
export async function adminApi(path: string, body: unknown): Promise<{ ok: boolean; [k: string]: unknown }> {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "x-admin-password": adminPassword() },
    body: JSON.stringify(body),
  });
  if (r.status === 401) {
    clearAdminSession();
    window.location.assign("/admin");
    return { ok: false, status: 401, error: "Session expired — log in again." };
  }
  const j = await r.json().catch(() => ({}));
  return { ok: r.ok && j.ok !== false, status: r.status, ...j };
}

function Gate({ children }: { children: ReactNode }) {
  const [ok, setOk] = useState(() => sessionFresh());
  const [pw, setPw] = useState("");
  const [bad, setBad] = useState(false);
  const [checking, setChecking] = useState(false);

  // Re-validate the stored password against the server on entry: if it's
  // stale (password changed, session revoked) drop straight to the login
  // form instead of showing an admin that 401s on every action.
  useEffect(() => {
    if (!ok) return;
    if (!sessionFresh()) { clearAdminSession(); setOk(false); return; }
    let alive = true;
    fetch("/api/tournament", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-admin-password": adminPassword() },
      body: JSON.stringify({ action: "admin", op: "ping" }),
    })
      .then((r) => {
        if (alive && r.status === 401) { clearAdminSession(); setOk(false); }
      })
      .catch(() => { /* offline — keep the session, API calls will retry */ });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!pw.trim() || checking) return;
    setChecking(true);
    setBad(false);
    try {
      // server-side check — the password never exists in this bundle
      const r = await fetch("/api/tournament", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-admin-password": pw },
        body: JSON.stringify({ action: "admin", op: "ping" }),
      });
      if (r.ok) {
        try {
          sessionStorage.setItem(KEY, "1");
          sessionStorage.setItem(PW_KEY, pw);
          sessionStorage.setItem(AT_KEY, String(Date.now()));
        } catch {}
        setOk(true);
      } else setBad(true);
    } catch {
      setBad(true);
    } finally {
      setChecking(false);
    }
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
        <button disabled={checking} className="btn-og w-full bg-acid text-ink py-3 text-sm hover:bg-bone">
          {checking ? "Checking…" : "Enter"}
        </button>
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
  { to: "/admin/tournament", label: "Tournament" },
];

export default function AdminLayout() {
  const logout = () => {
    clearAdminSession();
    window.location.assign("/admin");
  };

  return (
    <Gate>
      <div className="max-w-7xl mx-auto px-4 sm:px-6 py-10">
        <div className="flex flex-wrap items-center justify-between gap-4 mb-8">
          <h1 className="display-xl text-5xl text-bone">OG <span className="text-stroke-acid">Admin</span></h1>
          <button
            onClick={logout}
            className="btn-og border-2 border-blood/40 text-blood px-4 py-2.5 text-[10px] hover:bg-blood hover:text-bone"
          >
            Log out
          </button>
        </div>
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
