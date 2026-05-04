import { NextResponse } from "next/server";
import { getClientsPhoneConfig, getSecretByKey, markSecretDeleted } from "../../../../lib/clientsphonenumber/supabaseRest";

export const runtime = "nodejs";

export async function POST(req) {
  try {
    const body = await req.json().catch(() => ({}));
    const secret_key = (body?.secret_key ?? body?.secretKey ?? "").toString().trim();
    const passphrase = (body?.passphrase ?? "").toString();

    if (!secret_key) return NextResponse.json({ error: "Missing secret_key" }, { status: 400 });
    if (!passphrase) return NextResponse.json({ error: "Missing passphrase" }, { status: 400 });

    const config = await getClientsPhoneConfig();
    if (!config) {
      return NextResponse.json({ error: "Server not configured" }, { status: 500 });
    }

    const record = await getSecretByKey(secret_key);
    if (!record || record.deleted_at) return NextResponse.json({ error: "Not found" }, { status: 404 });

    const nowIso = new Date().toISOString();
    if (record.expires_at && record.expires_at <= nowIso) {
      // If expired, treat as no longer accessible.
      await markSecretDeleted(secret_key, new Date().toISOString());
      return NextResponse.json({ error: "Expired" }, { status: 410 });
    }

    if ((config.passphrase || "").toString().trim() !== passphrase.trim()) {
      return NextResponse.json({ error: "Incorrect passphrase" }, { status: 401 });
    }

    const secret = record.secret_text;

    const oneTimeDelete = !!config.one_time_delete_enabled;
    if (oneTimeDelete) {
      await markSecretDeleted(secret_key, new Date().toISOString());
    }

    return NextResponse.json({
      secret,
      expires_at: record.expires_at,
      deleted_on_unlock: oneTimeDelete,
    });
  } catch (e) {
    return NextResponse.json({ error: e?.message || "Unlock failed" }, { status: 500 });
  }
}

