"use client";

import { useEffect, useState } from "react";

function asBool(v) {
  return v === true || v === "true" || v === 1;
}

export default function AdminPage() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const [passphrase, setPassphrase] = useState("");
  const [ttlEnabled, setTtlEnabled] = useState(false);
  const [ttlDays, setTtlDays] = useState(30);
  const [oneTimeDeleteEnabled, setOneTimeDeleteEnabled] = useState(false);

  async function loadConfig() {
    setError("");
    const res = await fetch("/api/admin/config");
    if (!res.ok) throw new Error(`Failed to load config (HTTP ${res.status}).`);
    const data = await res.json();
    setPassphrase(data?.passphrase || "");
    setTtlEnabled(asBool(data?.ttl_enabled));
    setTtlDays(Number(data?.ttl_days ?? 30));
    setOneTimeDeleteEnabled(asBool(data?.one_time_delete_enabled));
  }

  useEffect(() => {
    (async () => {
      try {
        await loadConfig();
      } catch (e) {
        setError(e?.message || "Failed to load admin config.");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  async function save() {
    setError("");
    setMessage("");
    setSaving(true);
    try {
      const res = await fetch("/api/admin/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          passphrase,
          ttl_enabled: !!ttlEnabled,
          ttl_days: Number(ttlDays || 0),
          one_time_delete_enabled: !!oneTimeDeleteEnabled,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.error || `Failed to save (HTTP ${res.status}).`);
      setMessage("Saved.");
      await loadConfig();
    } catch (e) {
      setError(e?.message || "Failed to save.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <main style={{ maxWidth: 820, margin: "40px auto", padding: 16, fontFamily: "system-ui" }}>
      <h1 style={{ marginBottom: 8 }}>Clients Phone Number Admin</h1>
      <p style={{ marginBottom: 18, color: "#555" }}>
        This controls the passphrase users must enter to unlock /secret/&lt;secret_key&gt;.
      </p>

      {loading ? <p>Loading...</p> : null}
      {error ? <p style={{ color: "#dc3545" }}>{error}</p> : null}
      {message ? <p style={{ color: "#155724", fontWeight: 700 }}>{message}</p> : null}

      {!loading ? (
        <div style={{ display: "grid", gap: 14 }}>
          <label style={{ fontWeight: 700 }}>
            Note passphrase
            <input
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              style={{ display: "block", width: "100%", padding: 12, marginTop: 6 }}
              type="password"
              autoComplete="off"
            />
          </label>

          <div style={{ display: "grid", gap: 8, padding: 14, border: "1px solid #eee", borderRadius: 12 }}>
            <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <input type="checkbox" checked={ttlEnabled} onChange={(e) => setTtlEnabled(e.target.checked)} />
              Allow secrets to expire
            </label>
            <div style={{ opacity: ttlEnabled ? 1 : 0.6 }}>
              <label>
                Expire after (days)
                <input
                  value={ttlDays}
                  onChange={(e) => setTtlDays(e.target.value)}
                  style={{ display: "block", width: "100%", padding: 12, marginTop: 6 }}
                  type="number"
                  min={0}
                  disabled={!ttlEnabled}
                />
              </label>
            </div>
            <p style={{ margin: 0, color: "#888", fontSize: 12 }}>
              By default, expiry is OFF (never expires).
            </p>
          </div>

          <div style={{ display: "grid", gap: 8, padding: 14, border: "1px solid #eee", borderRadius: 12 }}>
            <label style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={oneTimeDeleteEnabled}
                onChange={(e) => setOneTimeDeleteEnabled(e.target.checked)}
              />
              Delete after first successful unlock
            </label>
            <p style={{ margin: 0, color: "#888", fontSize: 12 }}>
              By default, this is OFF (never deleted).
            </p>
          </div>

          <button
            type="button"
            onClick={save}
            disabled={saving || !passphrase.trim()}
            style={{
              padding: "12px 16px",
              border: "none",
              borderRadius: 10,
              background: "#667eea",
              color: "white",
              cursor: "pointer",
              fontWeight: 800,
              opacity: saving ? 0.7 : 1,
            }}
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      ) : null}
    </main>
  );
}

