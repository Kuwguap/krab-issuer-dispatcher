"use client";

import { useMemo, useState } from "react";

export default function SecretPage({ params }) {
  const secretKey = useMemo(() => (params?.secret_key || "").toString(), [params]);
  const [passphrase, setPassphrase] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [secret, setSecret] = useState("");
  const [unlocked, setUnlocked] = useState(false);
  const [copied, setCopied] = useState(false);

  async function unlock() {
    setError("");
    setLoading(true);
    try {
      const res = await fetch("/api/v1/unlock", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ secret_key: secretKey, passphrase }),
      });

      if (!res.ok) {
        if (res.status === 401) throw new Error("Incorrect passphrase.");
        if (res.status === 410) throw new Error("This note has expired.");
        if (res.status === 404) throw new Error("Not found (or already deleted).");
        throw new Error(`Unlock failed (HTTP ${res.status}).`);
      }

      const data = await res.json();
      setSecret(data?.secret || "");
      setUnlocked(true);
    } catch (e) {
      setSecret("");
      setUnlocked(false);
      setError(e?.message || "Unlock failed.");
    } finally {
      setLoading(false);
    }
  }

  async function copySecret() {
    if (!secret) return;
    try {
      await navigator.clipboard.writeText(secret);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <main
      style={{
        minHeight: "100vh",
        background: "#08142b",
        color: "#f3f4f6",
        padding: "60px 16px",
        fontFamily: "Georgia, 'Times New Roman', serif",
      }}
    >
      <div
        style={{
          maxWidth: 760,
          margin: "0 auto",
          background: "rgba(255,255,255,0.08)",
          border: "1px solid rgba(255,255,255,0.12)",
          borderRadius: 14,
          padding: 28,
        }}
      >
        <h1 style={{ marginBottom: 14, fontSize: 44, lineHeight: 1.1 }}>Your secure message is {unlocked ? "shown below." : "ready."}</h1>
      {!unlocked ? (
        <>
          <p style={{ marginBottom: 12, color: "#e5e7eb", fontSize: 34 }}>
            This message requires a passphrase:
          </p>
          <div style={{ display: "grid", gap: 12 }}>
            <label style={{ fontWeight: 600, color: "#dbe2ea" }}>
              <input
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
                placeholder="Enter the passphrase here"
                style={{
                  display: "block",
                  width: "100%",
                  padding: 16,
                  marginTop: 6,
                  borderRadius: 10,
                  border: "1px solid rgba(255,255,255,0.2)",
                  background: "rgba(255,255,255,0.08)",
                  color: "#fff",
                  fontSize: 30,
                }}
                type="password"
                autoComplete="off"
              />
            </label>
            <button
              type="button"
              onClick={unlock}
              disabled={loading || !secretKey}
              style={{
                padding: "16px 18px",
                border: "none",
                borderRadius: 10,
                background: "#cf421a",
                color: "white",
                cursor: "pointer",
                fontWeight: 800,
                fontSize: 42,
                opacity: loading ? 0.7 : 1,
              }}
            >
              {loading ? "Revealing..." : "Click to reveal ->"}
            </button>
          </div>
          {error ? <p style={{ marginTop: 12, color: "#ff8a8a", fontFamily: "system-ui, sans-serif" }}>{error}</p> : null}
        </>
      ) : (
        <>
          <p style={{ marginBottom: 14, color: "#f3f4f6", fontSize: 20, fontFamily: "system-ui, sans-serif" }}>
            Your secure message is shown below.
          </p>
          <div
            style={{
              background: "rgba(255,255,255,0.08)",
              borderRadius: 10,
              border: "1px solid rgba(255,255,255,0.16)",
              padding: 18,
              wordBreak: "break-word",
              fontSize: 36,
              color: "#fff",
            }}
          >
            {secret}
          </div>
          <button
            type="button"
            onClick={copySecret}
            style={{
              marginTop: 16,
              padding: "14px 20px",
              border: "none",
              borderRadius: 10,
              background: "#cf421a",
              color: "white",
              cursor: "pointer",
              fontWeight: 800,
              fontSize: 28,
            }}
          >
            {copied ? "Copied" : "Copy to clipboard"}
          </button>
          <p style={{ marginTop: 30, color: "#9ca3af", fontSize: 14, fontFamily: "system-ui, sans-serif" }}>
            You can close this window when done.
          </p>
        </>
      )}
      <p style={{ marginTop: 28, color: "#6b7280", fontSize: 12, fontFamily: "system-ui, sans-serif" }}>
        Secret key: {secretKey?.slice(0, 10)}...
      </p>
      </div>
    </main>
  );
}

