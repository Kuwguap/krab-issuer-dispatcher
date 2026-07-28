// Server-side geo helpers: geocoding (Nominatim/OSM) + driving routes & ETA
// (OSRM public server). Free, no API keys. All calls are admin-gated upstream.
// In-memory caches are per serverless instance — misses just re-fetch.

const NOMINATIM_UA = "krab-driver-track/1.0 (dispatch admin map)";

const geocodeCache = new Map(); // address -> {lat,lng,display}|null
const reverseCache = new Map(); // "lat,lng" (3dp) -> address string|null
const routeCache = new Map(); // "flat,flng>tlat,tlng" (3dp) -> {at, route}

const ROUTE_TTL_MS = 45 * 1000;

function round3(n) {
  return Math.round(Number(n) * 1000) / 1000;
}

export async function geocodeAddress(address) {
  const q = String(address || "").trim().replace(/\s+/g, " ");
  if (!q) return null;
  if (geocodeCache.has(q)) return geocodeCache.get(q);
  let result = null;
  try {
    const url =
      "https://nominatim.openstreetmap.org/search?format=json&limit=1&countrycodes=us&q=" +
      encodeURIComponent(q);
    const res = await fetch(url, {
      headers: { "User-Agent": NOMINATIM_UA },
      cache: "no-store",
    });
    if (res.ok) {
      const rows = await res.json();
      const hit = Array.isArray(rows) ? rows[0] : null;
      if (hit && Number.isFinite(Number(hit.lat)) && Number.isFinite(Number(hit.lon))) {
        result = {
          lat: Number(hit.lat),
          lng: Number(hit.lon),
          display: hit.display_name || q,
        };
      }
    }
  } catch {
    result = null;
  }
  geocodeCache.set(q, result);
  return result;
}

export async function reverseGeocode(lat, lng) {
  if (!Number.isFinite(Number(lat)) || !Number.isFinite(Number(lng))) return null;
  const key = `${round3(lat)},${round3(lng)}`;
  if (reverseCache.has(key)) return reverseCache.get(key);
  let address = null;
  try {
    const url =
      `https://nominatim.openstreetmap.org/reverse?format=json&zoom=16&lat=${Number(lat)}&lon=${Number(lng)}`;
    const res = await fetch(url, {
      headers: { "User-Agent": NOMINATIM_UA },
      cache: "no-store",
    });
    if (res.ok) {
      const data = await res.json();
      if (data && data.address) {
        const a = data.address;
        const parts = [
          [a.house_number, a.road].filter(Boolean).join(" "),
          a.city || a.town || a.village || a.hamlet || a.county,
          a.state ? `${a.state} ${a.postcode || ""}`.trim() : a.postcode,
        ].filter(Boolean);
        address = parts.join(", ") || data.display_name || null;
      } else if (data && data.display_name) {
        address = data.display_name;
      }
    }
  } catch {
    address = null;
  }
  reverseCache.set(key, address);
  return address;
}

// Driving route + duration via the OSRM demo server.
// Returns { durationSec, distanceM, coords: [[lat,lng], ...] } or null.
export async function osrmRoute(fromLat, fromLng, toLat, toLng) {
  const nums = [fromLat, fromLng, toLat, toLng].map(Number);
  if (nums.some((n) => !Number.isFinite(n))) return null;
  const key = `${round3(fromLat)},${round3(fromLng)}>${round3(toLat)},${round3(toLng)}`;
  const cached = routeCache.get(key);
  if (cached && Date.now() - cached.at < ROUTE_TTL_MS) return cached.route;
  let route = null;
  try {
    const url =
      `https://router.project-osrm.org/route/v1/driving/${Number(fromLng)},${Number(fromLat)};${Number(toLng)},${Number(toLat)}` +
      "?overview=full&geometries=geojson&alternatives=false&steps=false";
    const res = await fetch(url, { cache: "no-store" });
    if (res.ok) {
      const data = await res.json();
      const r = data && data.routes && data.routes[0];
      if (r && r.geometry && Array.isArray(r.geometry.coordinates)) {
        route = {
          durationSec: Number(r.duration) || 0,
          distanceM: Number(r.distance) || 0,
          coords: r.geometry.coordinates
            .map((c) => [Number(c[1]), Number(c[0])])
            .filter((c) => Number.isFinite(c[0]) && Number.isFinite(c[1])),
        };
      }
    }
  } catch {
    route = null;
  }
  routeCache.set(key, { at: Date.now(), route });
  return route;
}

export function formatEta(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return null;
  const mins = Math.round(s / 60);
  if (mins < 1) return "under 1 min";
  if (mins < 60) return `${mins} min`;
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  return m ? `${h}h ${m}m` : `${h}h`;
}

export function formatDistance(meters) {
  const m = Number(meters);
  if (!Number.isFinite(m) || m <= 0) return null;
  const miles = m / 1609.344;
  if (miles < 0.2) return `${Math.round(m)} m`;
  return `${miles.toFixed(miles < 10 ? 1 : 0)} mi`;
}
