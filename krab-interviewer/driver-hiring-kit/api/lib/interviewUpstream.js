/** Shared proxy to krab-interviewer-bot for /api/interview/* routes. */
import crypto from "node:crypto";
export const UPSTREAM = (
  process.env.KRAB_INTERVIEWER_URL || "https://krab-interviewer-bot-j5dv.onrender.com"
).replace(/\/+$/, "");

export const proxyConfig = {
  api: {
    bodyParser: false,
  },
};

function hopByHop(name) {
  const n = name.toLowerCase();
  return (
    n === "host" ||
    n === "connection" ||
    n === "content-length" ||
    n === "transfer-encoding" ||
    n === "content-encoding"
  );
}

async function readBody(req) {
  const chunks = [];
  for await (const chunk of req) {
    chunks.push(typeof chunk === "string" ? Buffer.from(chunk) : chunk);
  }
  return Buffer.concat(chunks);
}

export function clientIpFromRequest(req) {
  const pick = (name) => {
    const raw = req.headers[name] || req.headers[name.toLowerCase()];
    if (!raw) return "";
    const v = Array.isArray(raw) ? raw[0] : raw;
    return String(v).split(",")[0].trim();
  };
  return (
    pick("x-vercel-forwarded-for") ||
    pick("x-forwarded-for") ||
    pick("x-real-ip") ||
    pick("cf-connecting-ip") ||
    req.socket?.remoteAddress ||
    ""
  );
}

function queryStringFromRequest(req) {
  const rawUrl = req.url || "";
  const fromUrl = rawUrl.includes("?") ? rawUrl.slice(rawUrl.indexOf("?")) : "";
  if (fromUrl) return fromUrl;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(req.query || {})) {
    if (key === "path") continue;
    if (value == null || value === "") continue;
    if (Array.isArray(value)) {
      value.forEach((v) => params.append(key, String(v)));
    } else {
      params.append(key, String(value));
    }
  }
  const built = params.toString();
  return built ? `?${built}` : "";
}

export async function proxyToInterviewUpstream(req, res, subpath) {
  const path = String(subpath || "").replace(/^\/+|\/+$/g, "");
  const qs = queryStringFromRequest(req);
  const target = `${UPSTREAM}/api/interview${path ? `/${path}` : ""}${qs}`;

  const headers = { "accept-encoding": "identity" };
  for (const [name, value] of Object.entries(req.headers)) {
    const n = name.toLowerCase();
    if (hopByHop(n) || n === "accept-encoding") continue;
    if (value == null || value === "") continue;
    headers[name] = value;
  }

  let clientIp = clientIpFromRequest(req);
  if (!clientIp) {
    clientIp = `sess-${crypto.randomUUID()}`;
  }
  headers["x-forwarded-for"] = clientIp;
  headers["x-real-ip"] = clientIp;

  const init = { method: req.method, headers, redirect: "manual" };
  if (req.method !== "GET" && req.method !== "HEAD") {
    const body = await readBody(req);
    if (body.length) init.body = body;
  }

  try {
    const upstream = await fetch(target, init);
    const buf = Buffer.from(await upstream.arrayBuffer());

    res.status(upstream.status);
    upstream.headers.forEach((value, key) => {
      if (hopByHop(key)) return;
      res.setHeader(key, value);
    });
    if (buf.length) {
      res.setHeader("content-length", String(buf.length));
      res.send(buf);
    } else {
      res.end();
    }
  } catch (e) {
    res.status(502).json({
      detail: "Could not reach interviewer API. Check KRAB_INTERVIEWER_URL on Vercel.",
      error: e instanceof Error ? e.message : String(e),
    });
  }
}

export function subpathFromQuery(req) {
  const raw = req.query?.path;
  if (raw == null || raw === "") return "";
  if (Array.isArray(raw)) return raw.map(String).join("/");
  return String(raw).replace(/^\/+|\/+$/g, "");
}
