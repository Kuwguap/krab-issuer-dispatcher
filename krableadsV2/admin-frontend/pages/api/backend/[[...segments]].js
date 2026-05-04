/**
 * Same-origin proxy to the Flask admin on Render (or local).
 * Browser calls /api/backend/api/... → server fetches ADMIN_BACKEND_URL/api/...
 * Avoids CORS and works when NEXT_PUBLIC_* was set but the client bundle was stale.
 */
function upstreamBase() {
  const raw =
    process.env.ADMIN_BACKEND_URL ||
    process.env.RENDER_ADMIN_BACKEND_URL ||
    process.env.NEXT_PUBLIC_ADMIN_BACKEND_URL ||
    '';
  return String(raw).trim().replace(/\/+$/, '');
}

export default async function handler(req, res) {
  const base = upstreamBase();
  if (!base) {
    return res.status(503).json({
      ok: false,
      error:
        'Set ADMIN_BACKEND_URL (recommended) or NEXT_PUBLIC_ADMIN_BACKEND_URL in Vercel to your Render admin URL (e.g. https://your-service.onrender.com).',
    });
  }

  const segments = req.query.segments;
  const parts = Array.isArray(segments) ? segments : segments ? [segments] : [];
  const path = parts.length ? `/${parts.join('/')}` : '';
  const host = req.headers.host || 'localhost';
  const proto = req.headers['x-forwarded-proto'] || 'http';
  const u = new URL(req.url, `${proto}://${host}`);
  const targetUrl = `${base}${path}${u.search}`;

  const headers = new Headers();
  const incomingCt = req.headers['content-type'];
  if (incomingCt) headers.set('content-type', incomingCt);

  const init = {
    method: req.method,
    headers,
    redirect: 'manual',
  };

  if (req.method !== 'GET' && req.method !== 'HEAD' && req.method !== 'OPTIONS') {
    if (req.body !== undefined && req.body !== null) {
      if (Buffer.isBuffer(req.body)) {
        init.body = req.body;
      } else if (typeof req.body === 'string') {
        init.body = req.body;
      } else if (typeof req.body === 'object') {
        init.body = JSON.stringify(req.body);
        if (!headers.has('content-type')) {
          headers.set('content-type', 'application/json');
        }
      }
    }
  }

  try {
    const r = await fetch(targetUrl, init);
    const ct = r.headers.get('content-type') || 'application/octet-stream';
    const buf = Buffer.from(await r.arrayBuffer());
    res.status(r.status);
    res.setHeader('Content-Type', ct);
    res.send(buf);
  } catch (e) {
    console.error('[api/backend proxy]', targetUrl, e);
    res.status(502).json({
      ok: false,
      error: 'Could not reach admin backend',
      detail: String(e?.message || e),
    });
  }
}

export const config = {
  api: {
    bodyParser: true,
  },
};
