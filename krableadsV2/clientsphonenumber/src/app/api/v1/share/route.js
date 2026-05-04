import { NextResponse } from "next/server";
import { getClientsPhoneConfig, insertSecretRecord, upsertClientsPhoneConfig } from "../../../../lib/clientsphonenumber/supabaseRest";
import { generateMetadataKey, generateSecretKey } from "../../../../lib/clientsphonenumber/keys";

function unauthorized(resMsg = "Unauthorized") {
  return NextResponse.json({ error: resMsg }, { status: 401 });
}

function forbidden(resMsg = "Forbidden") {
  return NextResponse.json({ error: resMsg }, { status: 403 });
}

function parseBasicAuth(headerValue) {
  if (!headerValue || !headerValue.startsWith("Basic ")) return null;
  const b64 = headerValue.slice("Basic ".length).trim();
  const decoded = Buffer.from(b64, "base64").toString("utf8");
  const idx = decoded.indexOf(":");
  if (idx === -1) return null;
  return { username: decoded.slice(0, idx), apiKey: decoded.slice(idx + 1) };
}

async function parseShareBody(req) {
  const contentType = req.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return await req.json();
  }
  // OneTimeSecret client sends x-www-form-urlencoded via python requests `data=...`
  const form = await req.formData();
  const secret = form.get("secret");
  const passphrase = form.get("passphrase");
  const ttl = form.get("ttl");
  return { secret, passphrase, ttl };
}

export const runtime = "nodejs";

export async function POST(req) {
  try {
    const expectedUser =
      (process.env.ONETIMESECRET_USERNAME || process.env.CLIENTSPHNUMBER_USERNAME || "").trim();
    const expectedKey =
      (process.env.ONETIMESECRET_API_KEY || process.env.CLIENTSPHNUMBER_API_KEY || "").trim();
    if (!expectedUser || !expectedKey) {
      return NextResponse.json(
        { error: "Server not configured (missing ONETIMESECRET_USERNAME/ONETIMESECRET_API_KEY)" },
        { status: 500 }
      );
    }

    const auth = parseBasicAuth(req.headers.get("authorization") || "");
    if (!auth) return unauthorized("Missing Basic auth");
    if (auth.username.trim() !== expectedUser || auth.apiKey.trim() !== expectedKey) {
      return forbidden("Bad credentials");
    }

    const body = await parseShareBody(req);
    const secret = (body?.secret ?? "").toString().trim();
    const passphrase = (body?.passphrase ?? "").toString().trim();
    const ttl = body?.ttl;

    if (!secret) return NextResponse.json({ error: "Missing secret" }, { status: 400 });

    let config = await getClientsPhoneConfig();
    if (!config) {
      await upsertClientsPhoneConfig({
        passphrase: passphrase || "change-me",
        ttl_enabled: false,
        ttl_days: 30,
        one_time_delete_enabled: false,
      });
      config = await getClientsPhoneConfig();
    } else if (passphrase) {
      // Unlock compares against Supabase `clientsphonenumber_config.passphrase`, not Render/Vercel env.
      // Sync passphrase from the bot on every share so ONETIMESECRET_PASSPHRASE stays the source of truth.
      await upsertClientsPhoneConfig({ passphrase });
      config = await getClientsPhoneConfig();
    }

    const now = Date.now();
    let expiresAt = null;
    const ttlEnabled = !!config?.ttl_enabled;
    const ttlDays = Number(config?.ttl_days || 0) || 0;
    if (ttlEnabled) {
      const daysToUse = ttlDays > 0 ? ttlDays : 30;
      expiresAt = new Date(now + daysToUse * 24 * 60 * 60 * 1000).toISOString();
    } else {
      expiresAt = null;
    }

    const secret_key = generateSecretKey(16);
    const metadata_key = generateMetadataKey(16);

    await insertSecretRecord({
      secret_key,
      metadata_key,
      secret_text: secret,
      expires_at: expiresAt,
    });

    // OneTimeSecret-compatible response contract.
    return NextResponse.json({
      secret_key,
      metadata_key,
    });
  } catch (e) {
    return NextResponse.json({ error: e?.message || "Share failed" }, { status: 500 });
  }
}

