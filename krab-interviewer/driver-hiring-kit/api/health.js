import { UPSTREAM } from "./lib/interviewUpstream.js";

export default async function handler(_req, res) {
  try {
    const upstream = await fetch(`${UPSTREAM}/api/health`, {
      headers: { "accept-encoding": "identity" },
    });
    const buf = Buffer.from(await upstream.arrayBuffer());
    res.status(upstream.status);
    res.setHeader("content-type", upstream.headers.get("content-type") || "application/json");
    res.send(buf);
  } catch (e) {
    res.status(502).json({ ok: false, detail: String(e.message || e) });
  }
}
