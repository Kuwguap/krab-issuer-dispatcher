import { NextResponse } from "next/server";
import { verifyAdminCookie } from "@/lib/adminAuth";
import {
  getSessionByToken,
  getLatestPingForSession,
  getLatestPingForDriver,
  cacheSessionDest,
} from "@/lib/supabaseRest";
import {
  geocodeAddress,
  reverseGeocode,
  osrmRoute,
  formatEta,
  formatDistance,
} from "@/lib/geo";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function getCookieValue(request, name) {
  const header = request.headers.get("cookie") || "";
  for (const part of header.split(";")) {
    const [key, ...rest] = part.trim().split("=");
    if (key === name) return rest.join("=");
  }
  return null;
}

// GET /api/admin/route?token=<session token>
// Full tracking picture for one session: FROM (driver's last position,
// reverse-geocoded), TO (lead delivery address, geocoded + cached), the
// driving route geometry, distance, and ETA.
export async function GET(request) {
  try {
    const cookie = getCookieValue(request, "track_admin");
    if (!verifyAdminCookie(cookie)) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { searchParams } = new URL(request.url);
    const token = (searchParams.get("token") || "").trim();
    if (!token) {
      return NextResponse.json({ error: "token required" }, { status: 400 });
    }
    const session = await getSessionByToken(token);
    if (!session) {
      return NextResponse.json({ error: "Session not found" }, { status: 404 });
    }

    // FROM — the driver's most recent ping (prefer this session's pings; fall
    // back to any recent ping from the same driver, e.g. an earlier delivery).
    let ping = await getLatestPingForSession(token);
    if (!ping && session.driver_id) {
      ping = await getLatestPingForDriver(session.driver_id, 24);
    }
    const from = ping
      ? { lat: Number(ping.lat), lng: Number(ping.lng), at: ping.created_at }
      : null;

    // TO — cached coords, else geocode the delivery address now and cache.
    const deliveryAddress = session.delivery_address || null;
    let to = null;
    if (Number.isFinite(Number(session.dest_lat)) && Number.isFinite(Number(session.dest_lng)) && session.dest_lat != null) {
      to = { lat: Number(session.dest_lat), lng: Number(session.dest_lng), address: deliveryAddress };
    } else if (deliveryAddress) {
      const geo = await geocodeAddress(deliveryAddress);
      if (geo) {
        to = { lat: geo.lat, lng: geo.lng, address: deliveryAddress };
        try {
          await cacheSessionDest(token, geo.lat, geo.lng);
        } catch {
          // v2 migration not run yet — fine, we'll geocode again next time.
        }
      }
    }

    // Reverse-geocode the FROM point into a readable address.
    if (from) {
      from.address = await reverseGeocode(from.lat, from.lng);
    }

    // Route + ETA.
    let route = null;
    if (from && to) {
      route = await osrmRoute(from.lat, from.lng, to.lat, to.lng);
    }

    return NextResponse.json({
      ok: true,
      token,
      reference_id: session.reference_id || null,
      driver_name: session.driver_name || null,
      status: session.status,
      from,
      to: to || (deliveryAddress ? { address: deliveryAddress } : null),
      eta_seconds: route ? route.durationSec : null,
      eta_label: route ? formatEta(route.durationSec) : null,
      distance_meters: route ? route.distanceM : null,
      distance_label: route ? formatDistance(route.distanceM) : null,
      geometry: route ? route.coords : null,
    });
  } catch (err) {
    console.error("route endpoint failed:", err);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
