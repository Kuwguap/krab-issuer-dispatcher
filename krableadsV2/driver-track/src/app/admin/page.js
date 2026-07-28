"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";

const AdminMap = dynamic(() => import("../../components/AdminMap"), { ssr: false });

const REFRESH_MS = 10 * 1000;
const HOURS_OPTIONS = [2, 8, 24];

const STATUS_STYLES = {
  pending: {
    color: "var(--accent-bright)",
    border: "1px solid rgba(245, 158, 11, 0.6)",
    background: "transparent",
  },
  located: {
    color: "#4ade80",
    border: "1px solid rgba(74, 222, 128, 0.4)",
    background: "rgba(74, 222, 128, 0.08)",
  },
  details_sent: {
    color: "var(--text-muted)",
    border: "1px solid rgba(122, 165, 216, 0.25)",
    background: "transparent",
  },
  overridden: {
    color: "#f87171",
    border: "1px solid rgba(248, 113, 113, 0.4)",
    background: "rgba(248, 113, 113, 0.08)",
  },
};

function timeAgo(iso) {
  const ms = Date.parse(iso || "");
  if (!Number.isFinite(ms)) return "—";
  const diff = Date.now() - ms;
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function fmtTime(iso) {
  const ms = Date.parse(iso || "");
  if (!Number.isFinite(ms)) return "—";
  return new Date(ms).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function StatusBadge({ status }) {
  const style = STATUS_STYLES[status] || STATUS_STYLES.details_sent;
  return (
    <span
      style={{
        ...style,
        display: "inline-block",
        padding: "3px 10px",
        borderRadius: 999,
        fontSize: 12,
        fontFamily: "var(--font-sora)",
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {status}
    </span>
  );
}

function LoginForm({ onSuccess }) {
  const [passcode, setPasscode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const res = await fetch("/api/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ passcode }),
      });
      if (res.ok) {
        onSuccess();
      } else {
        setError("Wrong passcode.");
      }
    } catch {
      setError("Network error.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main
      style={{
        minHeight: "100vh",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
    >
      <form
        onSubmit={submit}
        className="glass-card"
        style={{
          width: "100%",
          maxWidth: 380,
          padding: "32px 24px",
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <h1 style={{ fontSize: 22, fontWeight: 700, textAlign: "center" }}>
          Krab Tracking Admin
        </h1>
        <input
          type="password"
          value={passcode}
          onChange={(e) => setPasscode(e.target.value)}
          placeholder="Passcode"
          autoFocus
          style={{
            padding: "13px 14px",
            borderRadius: 10,
            border: "1px solid var(--glass-border)",
            background: "rgba(13, 22, 38, 0.6)",
            color: "var(--text)",
            fontSize: 16,
            outline: "none",
          }}
        />
        {error ? (
          <p style={{ color: "#f87171", margin: 0, fontSize: 14, textAlign: "center" }}>
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          disabled={busy}
          style={{
            padding: "14px 18px",
            borderRadius: 10,
            background: "var(--accent)",
            color: "var(--on-accent)",
            fontWeight: 700,
            fontSize: 16,
            opacity: busy ? 0.7 : 1,
          }}
        >
          {busy ? "Checking…" : "Enter"}
        </button>
      </form>
    </main>
  );
}

export default function AdminPage() {
  const [authed, setAuthed] = useState(null); // null = unknown, false = need login, true = ok
  const [data, setData] = useState(null);
  const [hours, setHours] = useState(8);
  const [selectedDriver, setSelectedDriver] = useState(null);
  const [drawerOpen, setDrawerOpen] = useState(true);
  const hoursRef = useRef(hours);
  hoursRef.current = hours;

  const fetchOverview = useCallback(async (h) => {
    try {
      const res = await fetch(`/api/admin/overview?hours=${h ?? hoursRef.current}`, {
        cache: "no-store",
      });
      if (res.status === 401) {
        setAuthed(false);
        return;
      }
      if (!res.ok) return;
      const json = await res.json();
      setData(json);
      setAuthed(true);
    } catch {
      // keep last data on transient failures
    }
  }, []);

  useEffect(() => {
    fetchOverview(hours);
  }, [fetchOverview, hours]);

  useEffect(() => {
    if (authed !== true) return;
    const id = setInterval(() => fetchOverview(), REFRESH_MS);
    return () => clearInterval(id);
  }, [authed, fetchOverview]);

  if (authed === false) {
    return <LoginForm onSuccess={() => fetchOverview(hours)} />;
  }

  if (authed === null || !data) {
    return (
      <main
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        <p style={{ color: "var(--text-muted)" }}>Loading…</p>
      </main>
    );
  }

  const drivers = data.drivers || [];
  const sessions = data.sessions || [];
  const trails = data.trails || {};

  return (
    <main style={{ position: "fixed", inset: 0, overflow: "hidden" }}>
      <AdminMap drivers={drivers} trails={trails} selectedDriver={selectedDriver} />

      {/* Left overlay: driver list */}
      <div
        style={{
          position: "absolute",
          top: 16,
          left: 16,
          zIndex: 1000,
          display: "flex",
          flexDirection: "column",
          gap: 8,
          maxHeight: "60vh",
          overflowY: "auto",
          width: 230,
        }}
      >
        <div className="glass-chip" style={{ padding: "8px 12px" }}>
          <span style={{ fontFamily: "var(--font-sora)", fontWeight: 700, fontSize: 13 }}>
            🦀 Drivers ({drivers.length})
          </span>
        </div>
        {drivers.map((d) => {
          const selected = selectedDriver === d.driver_id;
          return (
            <button
              key={d.driver_id}
              onClick={() => setSelectedDriver(selected ? null : d.driver_id)}
              className="glass-chip"
              style={{
                padding: "10px 12px",
                display: "flex",
                alignItems: "center",
                gap: 10,
                textAlign: "left",
                outline: selected ? "1px solid var(--accent)" : "none",
                opacity: d.live ? 1 : 0.65,
              }}
            >
              {d.live ? (
                <span className="live-dot">
                  <span className="pulse-ring" />
                </span>
              ) : (
                <span
                  style={{
                    width: 10,
                    height: 10,
                    borderRadius: "50%",
                    background: "var(--text-muted)",
                    flex: "none",
                  }}
                />
              )}
              <span style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
                <span
                  style={{
                    fontFamily: "var(--font-sora)",
                    fontWeight: 600,
                    fontSize: 14,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {d.driver_name || `Driver ${d.driver_id}`}
                </span>
                <span style={{ fontSize: 12, color: "var(--text-muted)" }}>
                  last seen {timeAgo(d.last_ping?.created_at)}
                </span>
              </span>
            </button>
          );
        })}
        {drivers.length === 0 ? (
          <div className="glass-chip" style={{ padding: "10px 12px" }}>
            <span style={{ fontSize: 13, color: "var(--text-muted)" }}>
              No pings in the last {hours}h
            </span>
          </div>
        ) : null}
      </div>

      {/* Hours selector */}
      <div
        className="glass-chip"
        style={{
          position: "absolute",
          top: 16,
          right: 16,
          zIndex: 1000,
          display: "flex",
          padding: 3,
          gap: 2,
        }}
      >
        {HOURS_OPTIONS.map((h) => (
          <button
            key={h}
            onClick={() => setHours(h)}
            style={{
              padding: "6px 12px",
              borderRadius: 7,
              fontSize: 13,
              fontWeight: 700,
              background: hours === h ? "var(--accent)" : "transparent",
              color: hours === h ? "var(--on-accent)" : "var(--text-muted)",
            }}
          >
            {h}h
          </button>
        ))}
      </div>

      {/* Bottom drawer: sessions table */}
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: 0,
          zIndex: 1000,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          pointerEvents: "none",
        }}
      >
        <button
          onClick={() => setDrawerOpen((o) => !o)}
          className="glass-chip"
          style={{
            pointerEvents: "auto",
            padding: "6px 18px",
            marginBottom: 6,
            fontSize: 13,
            fontWeight: 700,
          }}
        >
          {drawerOpen ? "▾ Hide sessions" : `▴ Sessions (${sessions.length})`}
        </button>
        {drawerOpen ? (
          <div
            className="glass-card"
            style={{
              pointerEvents: "auto",
              width: "calc(100% - 24px)",
              maxWidth: 1100,
              maxHeight: "34vh",
              overflowY: "auto",
              margin: "0 12px 12px",
              padding: 12,
              borderBottomLeftRadius: 12,
              borderBottomRightRadius: 12,
            }}
          >
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--text-muted)" }}>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Reference</th>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Driver</th>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Kind</th>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Status</th>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Created</th>
                  <th style={{ padding: "6px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>Located</th>
                </tr>
              </thead>
              <tbody>
                {sessions.map((s) => (
                  <tr
                    key={s.token}
                    style={{ borderTop: "1px solid rgba(122, 165, 216, 0.15)" }}
                  >
                    <td style={{ padding: "7px 8px", fontFamily: "var(--font-sora)", fontWeight: 600 }}>
                      {s.reference_id || "—"}
                    </td>
                    <td style={{ padding: "7px 8px" }}>{s.driver_name || "—"}</td>
                    <td style={{ padding: "7px 8px", color: "var(--text-muted)" }}>{s.kind}</td>
                    <td style={{ padding: "7px 8px" }}>
                      <StatusBadge status={s.status} />
                    </td>
                    <td style={{ padding: "7px 8px", color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                      {fmtTime(s.created_at)}
                    </td>
                    <td style={{ padding: "7px 8px", color: "var(--text-muted)", whiteSpace: "nowrap" }}>
                      {s.located_at ? fmtTime(s.located_at) : "—"}
                    </td>
                  </tr>
                ))}
                {sessions.length === 0 ? (
                  <tr>
                    <td colSpan={6} style={{ padding: 12, color: "var(--text-muted)", textAlign: "center" }}>
                      No sessions in the last 48h
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        ) : null}
      </div>
    </main>
  );
}
