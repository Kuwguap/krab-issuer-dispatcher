import { NextResponse } from "next/server";
import {
  getSessionByToken,
  markSessionLocated,
  insertPing,
} from "@/lib/supabaseRest";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const SESSION_TTL_MS = 24 * 60 * 60 * 1000;

function finiteOrNull(value) {
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
}

export async function POST(request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const token = body?.token;
  const lat = Number(body?.lat);
  const lng = Number(body?.lng);

  if (!token || typeof token !== "string") {
    return NextResponse.json({ error: "token is required" }, { status: 400 });
  }
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    return NextResponse.json({ error: "lat must be a number between -90 and 90" }, { status: 400 });
  }
  if (!Number.isFinite(lng) || lng < -180 || lng > 180) {
    return NextResponse.json({ error: "lng must be a number between -180 and 180" }, { status: 400 });
  }

  try {
    const session = await getSessionByToken(token);
    if (!session) {
      return NextResponse.json({ error: "Unknown session" }, { status: 404 });
    }

    const createdMs = Date.parse(session.created_at || "");
    if (Number.isFinite(createdMs) && Date.now() - createdMs > SESSION_TTL_MS) {
      return NextResponse.json({ error: "Session expired" }, { status: 410 });
    }

    await insertPing({
      session_token: token,
      driver_id: session.driver_id ?? null,
      lat,
      lng,
      accuracy: finiteOrNull(body?.accuracy),
      speed: finiteOrNull(body?.speed),
      heading: finiteOrNull(body?.heading),
    });

    let first = false;
    if (session.status === "pending") {
      const claimed = await markSessionLocated(token);
      first = Array.isArray(claimed) && claimed.length > 0;
    }

    return NextResponse.json({ ok: true, first });
  } catch (err) {
    console.error("ping failed:", err);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
