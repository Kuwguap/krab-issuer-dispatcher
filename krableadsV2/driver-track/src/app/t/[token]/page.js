"use client";

import { useCallback, useEffect, useRef, useState } from "react";

const BOT_USERNAME = (process.env.NEXT_PUBLIC_TELEGRAM_BOT_USERNAME || "").trim();
const TELEGRAM_URL = BOT_USERNAME ? `https://t.me/${BOT_USERNAME}` : "tg://resolve";

const PING_THROTTLE_MS = 10 * 1000;

function Card({ children }) {
  return (
    <main
      className="full-h"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 20,
      }}
    >
      <div
        className="glass-card"
        style={{
          width: "100%",
          maxWidth: 420,
          padding: "32px 24px",
          textAlign: "center",
          display: "flex",
          flexDirection: "column",
          gap: 16,
          alignItems: "center",
        }}
      >
        {children}
      </div>
    </main>
  );
}

function AmberButton({ children, onClick, href }) {
  const style = {
    display: "inline-block",
    width: "100%",
    padding: "16px 20px",
    borderRadius: 12,
    background: "var(--accent)",
    color: "var(--on-accent)",
    fontFamily: "var(--font-sora)",
    fontWeight: 700,
    fontSize: 17,
    textDecoration: "none",
    textAlign: "center",
  };
  if (href) {
    return (
      <a href={href} style={style}>
        {children}
      </a>
    );
  }
  return (
    <button onClick={onClick} style={style}>
      {children}
    </button>
  );
}

function TelegramButton() {
  return <AmberButton href={TELEGRAM_URL}>Open Telegram</AmberButton>;
}

export default function TrackPage({ params }) {
  const token = params?.token || "";

  // phase: loading | invalid | done | ready | locating | success | denied | nogeo | error
  const [phase, setPhase] = useState("loading");
  const [firstName, setFirstName] = useState("");
  const [errorNote, setErrorNote] = useState("");

  const watchIdRef = useRef(null);
  const lastPingAtRef = useRef(0);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await fetch(`/api/v1/session/${encodeURIComponent(token)}`, {
          cache: "no-store",
        });
        if (cancelled) return;
        if (res.status === 404) {
          setPhase("invalid");
          return;
        }
        if (!res.ok) {
          setPhase("error");
          setErrorNote("Something went wrong. Please try again.");
          return;
        }
        const data = await res.json();
        if (cancelled) return;
        setFirstName(data.driver_first_name || "");
        if (data.status === "pending") {
          setPhase("ready");
        } else {
          // located / details_sent / overridden
          setPhase("done");
        }
      } catch {
        if (!cancelled) {
          setPhase("error");
          setErrorNote("Network error. Check your connection and try again.");
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const postPing = useCallback(
    async (position) => {
      const coords = position?.coords || {};
      const res = await fetch("/api/v1/ping", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          token,
          lat: coords.latitude,
          lng: coords.longitude,
          accuracy: coords.accuracy ?? null,
          speed: coords.speed ?? null,
          heading: coords.heading ?? null,
        }),
      });
      if (!res.ok) {
        throw new Error(`ping failed: ${res.status}`);
      }
      return res.json();
    },
    [token]
  );

  const startWatching = useCallback(() => {
    if (!navigator.geolocation || watchIdRef.current != null) return;
    watchIdRef.current = navigator.geolocation.watchPosition(
      (position) => {
        const now = Date.now();
        if (document.visibilityState !== "visible") return;
        if (now - lastPingAtRef.current < PING_THROTTLE_MS) return;
        lastPingAtRef.current = now;
        postPing(position).catch(() => {});
      },
      () => {},
      { enableHighAccuracy: true, maximumAge: 5000, timeout: 20000 }
    );
  }, [postPing]);

  useEffect(() => {
    return () => {
      if (watchIdRef.current != null && navigator.geolocation) {
        navigator.geolocation.clearWatch(watchIdRef.current);
        watchIdRef.current = null;
      }
    };
  }, []);

  const shareLocation = useCallback(() => {
    if (!navigator.geolocation) {
      setPhase("nogeo");
      return;
    }
    setPhase("locating");
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        try {
          lastPingAtRef.current = Date.now();
          await postPing(position);
          setPhase("success");
          if (BOT_USERNAME) {
            setTimeout(() => {
              window.location.href = TELEGRAM_URL;
            }, 1500);
          }
          startWatching();
        } catch {
          setPhase("error");
          setErrorNote("Could not send your location. Please try again.");
        }
      },
      (err) => {
        if (err && err.code === 1) {
          setPhase("denied");
        } else {
          setPhase("error");
          setErrorNote("Could not get your location. Please try again.");
        }
      },
      { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 }
    );
  }, [postPing, startWatching]);

  if (phase === "loading") {
    return (
      <Card>
        <p style={{ color: "var(--text-muted)", margin: 0 }}>Loading…</p>
      </Card>
    );
  }

  if (phase === "invalid") {
    return (
      <Card>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>This link is invalid or expired.</h1>
        <p style={{ color: "var(--text-muted)", margin: 0 }}>
          Ask your dispatcher to send a new one.
        </p>
      </Card>
    );
  }

  if (phase === "done") {
    return (
      <Card>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>✅ Already done — return to Telegram</h1>
        <p style={{ color: "var(--text-muted)", margin: 0 }}>
          Your location was already received. Check Telegram for the details.
        </p>
        <TelegramButton />
      </Card>
    );
  }

  if (phase === "nogeo") {
    return (
      <Card>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>📍 Location check</h1>
        <p style={{ color: "var(--text-muted)", margin: 0 }}>
          Open this link in Chrome or Safari.
        </p>
      </Card>
    );
  }

  if (phase === "denied" || phase === "error") {
    return (
      <Card>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>
          {phase === "denied" ? "Location permission needed" : "Something went wrong"}
        </h1>
        {phase === "error" && errorNote ? (
          <p style={{ color: "var(--text-muted)", margin: 0 }}>{errorNote}</p>
        ) : null}
        <AmberButton onClick={shareLocation}>Try again</AmberButton>
        <p style={{ color: "var(--text-muted)", margin: 0, fontSize: 13 }}>
          Location is required to receive the details. If you can&apos;t enable it, contact
          your supervisor.
        </p>
      </Card>
    );
  }

  if (phase === "success") {
    return (
      <main
        className="fixed-viewport"
        style={{
          background: "var(--bg)",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 24,
          padding: 24,
          textAlign: "center",
          zIndex: 50,
        }}
      >
        <div style={{ width: 60, height: 60, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div className="pulse-marker" style={{ transform: "scale(2.2)" }}>
            <span className="pulse-ring" />
            <span className="pulse-ring" />
          </div>
        </div>
        <h1 style={{ fontSize: 26, fontWeight: 800 }}>Location shared ✅</h1>
        <p style={{ color: "var(--text-muted)", margin: 0, fontSize: 16 }}>
          Your delivery details are on the way in Telegram.
        </p>
        <div style={{ width: "100%", maxWidth: 320 }}>
          <TelegramButton />
        </div>
      </main>
    );
  }

  // ready | locating
  return (
    <Card>
      <h1 style={{ fontSize: 24, fontWeight: 700 }}>📍 Location check</h1>
      <p style={{ color: "var(--text-muted)", margin: 0, fontSize: 16 }}>
        Hi {firstName || "there"} — share your location to receive the delivery details.
      </p>
      <AmberButton onClick={phase === "locating" ? undefined : shareLocation}>
        {phase === "locating" ? "Getting your location…" : "Share my location"}
      </AmberButton>
    </Card>
  );
}
