// Raw PostgREST helpers for driver tracking.
// Only ever touches driver_tracking_sessions and driver_location_pings.

const SUPABASE_URL = (process.env.SUPABASE_URL || "").trim();
const SUPABASE_KEY = (
  process.env.SUPABASE_KEY ||
  process.env.SUPABASE_SERVICE_ROLE_KEY ||
  ""
).trim();

function ensureSupabaseEnv() {
  if (!SUPABASE_URL || !SUPABASE_KEY) {
    throw new Error("Supabase env missing");
  }
}

function restUrl(table, query = "") {
  const base = `${SUPABASE_URL}/rest/v1/${table}`;
  return query ? `${base}?${query}` : base;
}

async function supabaseFetch(path, options = {}) {
  ensureSupabaseEnv();
  const res = await fetch(path, {
    ...options,
    cache: "no-store",
    headers: {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
  });

  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }

  if (!res.ok) {
    const msg = typeof data === "string" ? data : JSON.stringify(data);
    throw new Error(`Supabase REST error ${res.status}: ${msg}`);
  }
  return data;
}

export async function getSessionByToken(token) {
  // select=* keeps this tolerant of optional v2 columns (delivery_address,
  // dest_lat/dest_lng) not existing yet. Public routes must still whitelist
  // what they return to the browser.
  const query = `select=*&token=eq.${encodeURIComponent(token)}&limit=1`;
  const rows = await supabaseFetch(restUrl("driver_tracking_sessions", query), {
    method: "GET",
  });
  return (rows || [])[0] || null;
}

// Cache geocoded destination coordinates on the session (v2 columns).
// Best-effort: throws if the migration hasn't run — callers should catch.
export async function cacheSessionDest(token, lat, lng) {
  const query = `token=eq.${encodeURIComponent(token)}`;
  await supabaseFetch(restUrl("driver_tracking_sessions", query), {
    method: "PATCH",
    body: JSON.stringify({ dest_lat: lat, dest_lng: lng }),
  });
}

export async function getLatestPingForSession(sessionToken) {
  const query =
    "select=lat,lng,accuracy,speed,created_at" +
    `&session_token=eq.${encodeURIComponent(sessionToken)}` +
    "&order=created_at.desc&limit=1";
  const rows = await supabaseFetch(restUrl("driver_location_pings", query), {
    method: "GET",
  });
  return (rows || [])[0] || null;
}

export async function getLatestPingForDriver(driverId, hours = 24) {
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
  const query =
    "select=lat,lng,accuracy,speed,created_at" +
    `&driver_id=eq.${encodeURIComponent(driverId)}` +
    `&created_at=gte.${encodeURIComponent(since)}` +
    "&order=created_at.desc&limit=1";
  const rows = await supabaseFetch(restUrl("driver_location_pings", query), {
    method: "GET",
  });
  return (rows || [])[0] || null;
}

// Conditional claim: only flips pending -> located. Returns the patched rows
// (empty array if the session was no longer pending).
export async function markSessionLocated(token) {
  const query =
    `token=eq.${encodeURIComponent(token)}&status=eq.pending` +
    "&select=id,token,status,located_at";
  const rows = await supabaseFetch(restUrl("driver_tracking_sessions", query), {
    method: "PATCH",
    body: JSON.stringify({
      status: "located",
      located_at: new Date().toISOString(),
    }),
    headers: { Prefer: "return=representation" },
  });
  return rows || [];
}

export async function insertPing({
  session_token,
  driver_id,
  lat,
  lng,
  accuracy,
  speed,
  heading,
}) {
  const rows = await supabaseFetch(restUrl("driver_location_pings"), {
    method: "POST",
    body: JSON.stringify({
      session_token,
      driver_id: driver_id ?? null,
      lat,
      lng,
      accuracy: accuracy ?? null,
      speed: speed ?? null,
      heading: heading ?? null,
    }),
    headers: { Prefer: "return=representation" },
  });
  return (rows || [])[0] || null;
}

export async function getRecentSessions(hours = 48) {
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
  // select=* so optional v2 columns come through when present.
  const query =
    "select=*" +
    `&created_at=gte.${encodeURIComponent(since)}` +
    "&order=created_at.desc&limit=200";
  const rows = await supabaseFetch(restUrl("driver_tracking_sessions", query), {
    method: "GET",
  });
  return rows || [];
}

export async function getLatestPingsPerDriver(hours = 8) {
  // Fetch recent pings ascending; caller groups by driver_id and keeps the last.
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
  const query =
    "select=driver_id,lat,lng,accuracy,created_at" +
    `&created_at=gte.${encodeURIComponent(since)}` +
    "&order=created_at.asc&limit=2000";
  const rows = await supabaseFetch(restUrl("driver_location_pings", query), {
    method: "GET",
  });
  return rows || [];
}

export async function getTrailPings(hours = 8, limit = 2000) {
  const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
  const query =
    "select=driver_id,lat,lng,created_at" +
    `&created_at=gte.${encodeURIComponent(since)}` +
    `&order=created_at.asc&limit=${Math.max(1, Math.min(10000, limit))}`;
  const rows = await supabaseFetch(restUrl("driver_location_pings", query), {
    method: "GET",
  });
  return rows || [];
}
