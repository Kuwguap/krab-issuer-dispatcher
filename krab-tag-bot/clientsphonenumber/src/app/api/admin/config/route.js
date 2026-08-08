import { NextResponse } from "next/server";
import { getClientsPhoneConfig, upsertClientsPhoneConfig } from "../../../../lib/clientsphonenumber/supabaseRest";

export const runtime = "nodejs";

export async function GET() {
  const config = await getClientsPhoneConfig();
  if (config) return NextResponse.json(config);

  await upsertClientsPhoneConfig({
    passphrase: "change-me",
    ttl_enabled: false,
    ttl_days: 30,
    one_time_delete_enabled: false,
  });
  const newConfig = await getClientsPhoneConfig();
  return NextResponse.json(newConfig || {});
}

export async function POST(req) {
  const body = await req.json().catch(() => ({}));
  const passphrase = (body?.passphrase ?? "").toString();
  const ttl_enabled = !!body?.ttl_enabled;
  const ttl_days = Number(body?.ttl_days ?? 30);
  const one_time_delete_enabled = !!body?.one_time_delete_enabled;

  if (!passphrase.trim()) {
    return NextResponse.json({ error: "passphrase is required" }, { status: 400 });
  }

  const updated = await upsertClientsPhoneConfig({
    passphrase: passphrase.trim(),
    ttl_enabled,
    ttl_days: Number.isFinite(ttl_days) ? ttl_days : 30,
    one_time_delete_enabled,
  });

  return NextResponse.json(updated || { success: true });
}

