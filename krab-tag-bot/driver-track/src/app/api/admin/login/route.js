import { NextResponse } from "next/server";
import { checkPasscode, signAdminCookie } from "@/lib/adminAuth";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request) {
  let body;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  try {
    if (!checkPasscode(body?.passcode)) {
      return NextResponse.json({ error: "Wrong passcode" }, { status: 401 });
    }

    const cookie = signAdminCookie();
    const res = NextResponse.json({ ok: true });
    res.headers.set(
      "Set-Cookie",
      `track_admin=${cookie}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=43200`
    );
    return res;
  } catch (err) {
    console.error("admin login failed:", err);
    return NextResponse.json({ error: "Internal error" }, { status: 500 });
  }
}
