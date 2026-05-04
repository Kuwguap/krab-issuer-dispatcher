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

export async function getClientsPhoneConfig() {
  ensureSupabaseEnv();
  // Single-row config, id=1
  const query = `select=passphrase,ttl_enabled,ttl_days,one_time_delete_enabled&id=eq.1&limit=1`;
  const rows = await supabaseFetch(restUrl("clientsphonenumber_config", query), { method: "GET" });
  return (rows || [])[0] || null;
}

export async function upsertClientsPhoneConfig(config) {
  ensureSupabaseEnv();

  // Update first; if no row exists, insert.
  const existing = await getClientsPhoneConfig();
  if (existing) {
    const query = `id=eq.1&select=passphrase,ttl_enabled,ttl_days,one_time_delete_enabled`;
    const rows = await supabaseFetch(
      restUrl("clientsphonenumber_config", query),
      {
        method: "PATCH",
        body: JSON.stringify(config),
        headers: { Prefer: "return=representation" },
      }
    );
    return (rows || [])[0] || null;
  }

  // Insert new row
  const rows = await supabaseFetch(
    restUrl("clientsphonenumber_config", "select=passphrase,ttl_enabled,ttl_days,one_time_delete_enabled"),
    {
      method: "POST",
      body: JSON.stringify({ id: 1, ...config }),
      headers: { Prefer: "return=representation" },
    }
  );
  return (rows || [])[0] || null;
}

export async function insertSecretRecord({ secret_key, metadata_key, secret_text, expires_at }) {
  ensureSupabaseEnv();

  await supabaseFetch(restUrl("clientsphonenumber_secrets"), {
    method: "POST",
    body: JSON.stringify({
      secret_key,
      metadata_key,
      secret_text,
      expires_at: expires_at || null,
    }),
    headers: { Prefer: "resolution=ignore-duplicates" },
  });
}

export async function getSecretByKey(secret_key) {
  ensureSupabaseEnv();
  const query = `select=secret_key,secret_text,expires_at,deleted_at&secret_key=eq.${encodeURIComponent(
    secret_key
  )}&limit=1`;
  const rows = await supabaseFetch(restUrl("clientsphonenumber_secrets", query), { method: "GET" });
  return (rows || [])[0] || null;
}

export async function markSecretDeleted(secret_key, deleted_at = null) {
  ensureSupabaseEnv();
  const query = `secret_key=eq.${encodeURIComponent(secret_key)}`;
  await supabaseFetch(restUrl("clientsphonenumber_secrets", query), {
    method: "PATCH",
    body: JSON.stringify({ deleted_at: deleted_at || new Date().toISOString() }),
    headers: { Prefer: "return=representation" },
  });
}

