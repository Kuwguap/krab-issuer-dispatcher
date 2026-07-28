import { NextResponse } from "next/server";
import { verifyAdminCookie } from "@/lib/adminAuth";
import {
  getRecentSessions,
  getLatestPingsPerDriver,
  getTrailPings,
} from "@/lib/supabaseRest";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIVE_WINDOW_MS = 2 * 60 * 1000;
const TRAIL_CAP_PER_DRIVER = 500;

function getCookieValue(request, name) {
  const header = request.headers.get("cookie") || "";
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return null;
}

export async function GET(request) {
  try {
    const cookie = getCookieValue(request, "track_admin");
    if (!verifyAdminCookie(cookie)) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { searchParams } = new URL(request.url);
    let hours = parseInt(searchParams.get("hours") || "8", 10);
    if (!Number.isFinite(hours)) hours = 8;
    hours = Math.max(1, Math.min(48, hours));

    const [sessionRows, pingRows, trailRows] = await Promise.all([
      getRecentSessions(48),
      getLatestPingsPerDriver(hours),
      getTrailPings(hours, 2000),
    ]);

    const sessions = (sessionRows || []).map((s) => ({
      token: s.token,
      kind: s.kind,
      reference_id: s.reference_id,
      driver_name: s.driver_name,
      status: s.status,
      created_at: s.created_at,
      located_at: s.located_at,
      details_sent_at: s.details_sent_at,
      delivery_address: s.delivery_address ?? null,
      dest_lat: s.dest_lat ?? null,
      dest_lng: s.dest_lng ?? null,
    }));

    // Most recent session per driver -> driver_name + session token lookup.
    // getRecentSessions is ordered created_at desc, so first hit wins.
    const nameByDriver = new Map();
    const sessionByDriver = new Map();
    for (const s of sessionRows || []) {
      if (s.driver_id != null && !nameByDriver.has(String(s.driver_id))) {
        nameByDriver.set(String(s.driver_id), s.driver_name || null);
      }
      if (s.driver_id != null && !sessionByDriver.has(String(s.driver_id))) {
        sessionByDriver.set(String(s.driver_id), s.token);
      }
    }

    // Latest ping per driver (pings are ascending, so later rows overwrite).
    const latestByDriver = new Map();
    for (const p of pingRows || []) {
      if (p.driver_id == null) continue;
      latestByDriver.set(String(p.driver_id), p);
    }

    const now = Date.now();
    const drivers = [];
    for (const [driverId, ping] of latestByDriver.entries()) {
      const pingMs = Date.parse(ping.created_at || "");
      drivers.push({
        driver_id: driverId,
        driver_name: nameByDriver.get(driverId) || null,
        session_token: sessionByDriver.get(driverId) || null,
        last_ping: {
          lat: ping.lat,
          lng: ping.lng,
          accuracy: ping.accuracy,
          created_at: ping.created_at,
        },
        live: Number.isFinite(pingMs) && now - pingMs < LIVE_WINDOW_MS,
      });
    }

    // Trails grouped by driver_id (ascending), capped per driver.
    const trails = {};
    for (const p of trailRows || []) {
      const key = p.driver_id == null ? "unknown" : String(p.driver_id);
      if (!trails[key]) trails[key] = [];
      trails[key].push([p.lat, p.lng, p.created_at]);
    }
    for (const key of Object.keys(trails)) {
      if (trails[key].length > TRAIL_CAP_PER_DRIVER) {
        trails[key] = trails[key].slice(-TRAIL_CAP_PER_DRIVER);
      }
    }

    return NextResponse.json({ sessions, drivers, trails });
  } catch (err) {
    console.error("overview failed:", err);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
