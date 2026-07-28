import { NextResponse } from "next/server";
import { getSessionByToken } from "@/lib/supabaseRest";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request, { params }) {
  try {
    const token = params?.token || "";
    const session = await getSessionByToken(token);
    if (!session) {
      return NextResponse.json({ error: "Session not found" }, { status: 404 });
    }

    const firstName = String(session.driver_name || "").trim().split(/\s+/)[0] || "";

    // Deliberately minimal: no lead data, no reference_id, no chat_id.
    return NextResponse.json({
      status: session.status,
      kind: session.kind,
      driver_first_name: firstName,
      created_at: session.created_at,
    });
  } catch (err) {
    console.error("session lookup failed:", err);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
