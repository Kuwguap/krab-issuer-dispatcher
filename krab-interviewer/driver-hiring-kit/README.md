# Driver hiring site (React)

Single-folder deploy for your domain (e.g. `test.com`). **Vite + React** — light, clean UI inspired by [Whimsical](https://whimsical.com/) (soft purple accents, spacious layout, real photography).

## 2 pages

| Route | Purpose |
|-------|---------|
| `/` | Pitch — same-day cash job, 1-2-3 steps, what you need |
| `/apply` | Form with **real photo examples** beside every field → submits to Telegram bot |

## Local dev

```bash
cd driver-hiring-kit
npm install
npm run dev
```

Vite proxies `/api` → `krab-interviewer-bot.onrender.com` (see `vite.config.js`).

## Deploy (Vercel → test.com)

1. Connect repo; set **Root Directory** to `krab-interviewer/driver-hiring-kit`.
2. Add env var **`KRAB_INTERVIEWER_URL`** = `https://krab-interviewer-bot-j5dv.onrender.com` (Vercel → Settings → Environment Variables).
3. Deploy — `api/interview/[...path].js` proxies all `/api/interview/*` requests (fixes 404 on draft / verify).
4. Optional: hit `https://your-domain.com/api/health` — should return `{"ok":true}`.

**Why 404 happened:** the SPA rewrite was sending `/api/*` to `index.html`. Fixed by excluding `/api/` from that rewrite and using Vercel serverless proxy.

## Env

| Variable | Default | Notes |
|----------|---------|-------|
| `VITE_KRAB_API_BASE_URL` | *(empty)* | Same-origin `/api` proxy (recommended) |
| `VITE_BRAND_NAME` | Driver Interview Call | Nav branding |

## Custom images

Edit `src/images.js` — Unsplash URLs today. Replace with your own URLs or add files under `public/images/` and reference `/images/your-file.jpg`.

## API flow

```
/apply → POST /api/interview/draft → PATCH auto-save → POST /api/interview/submit
→ supervisor Telegram alert → /open {id} → Hire → bot adds group, channel, driver bot
```

See **AI-WIRE-UP.md** for CORS / nginx alternatives.
