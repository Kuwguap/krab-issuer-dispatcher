# AI wire-up instructions — driver hiring kit

Give this file + the whole `driver-hiring-kit/` folder to an AI agent (or developer) when adding driver hiring to **another project**. **Do not use iframes.** Copy the folder, serve it as static files, proxy the API.

---

## What this folder is

| File | Role |
|------|------|
| `index.html` | Funnel landing (hero, steps, CTA) |
| `requirements.html` | Requirements checklist (checkbox gate) |
| `interview.html` | Application form (calls API) |
| `how-to-telegram.html` | Telegram setup guide |
| `static/config.js` | **Edit this** — API URL + brand name |
| `static/main.js` | Form logic (do not fork unless necessary) |
| `static/styles.css`, `funnel.css` | Styles |
| `proxy-examples/` | nginx / Vercel / Next.js proxy snippets |

**Backend (always remote):** `krab-interviewer-bot` on Render — Supabase `interviews` + Telegram supervisor notify. This folder is **frontend only**.

---

## Task checklist for the AI

Copy in order:

1. [ ] Copy entire `driver-hiring-kit/` into the target project (see placement table below).
2. [ ] Edit `static/config.js`:
   - `KRAB_API_BASE_URL = ""` if using same-origin proxy (recommended).
   - Or full URL `https://krab-interviewer-bot.onrender.com` if proxy not possible.
3. [ ] Add reverse proxy so `https://TARGET_SITE/api/*` → `https://krab-interviewer-bot.onrender.com/api/*` (see `proxy-examples/`).
4. [ ] On Render (krab-interviewer-bot), set `KRAB_API_CORS_ALLOWED_ORIGINS` to include the target site origin (only if **not** using proxy).
5. [ ] Add navigation link on the host site, e.g. `<a href="/careers/hiring/">Become a driver</a>`.
6. [ ] Confirm Supabase migration `migration_interview_drafts.sql` was run on Issuer Supabase.
7. [ ] Smoke test: open `.../index.html` → requirements → interview → submit (or save draft).

---

## Where to copy the folder

| Host project type | Copy to | Public URL example |
|-------------------|---------|-------------------|
| Plain static / nginx | `public/driver-hiring/` or `www/driver-hiring/` | `https://site.com/driver-hiring/` |
| Next.js | `public/driver-hiring/` | `https://site.com/driver-hiring/` |
| Vite / React | `public/driver-hiring/` | same |
| WordPress | theme or uploads subfolder (less ideal) | `https://site.com/wp-content/driver-hiring/` |
| Another Render static site | repo root `driver-hiring/` | `https://mysite.onrender.com/driver-hiring/` |

**Rule:** Keep **all files together** — HTML at kit root, `static/` beside them. Links are relative (`requirements.html`, `static/config.js`).

---

## API wiring (no iframe)

The form calls these endpoints (JSON + cookies):

```
POST   /api/interview/draft
PATCH  /api/interview/draft/{id}
POST   /api/interview/draft/{id}/license
POST   /api/interview/submit/{id}
```

### Recommended: same-origin proxy

Pages on `https://yourproject.com` → set in `config.js`:

```js
window.KRAB_API_BASE_URL = "";
```

Browser calls `https://yourproject.com/api/interview/draft` → your server proxies to krab-interviewer-bot.

**Why:** draft cookies (`krab_draft_id`) work reliably; no third-party cookie blocking.

Use snippets in `proxy-examples/`.

### Alternative: direct API URL

```js
window.KRAB_API_BASE_URL = "https://krab-interviewer-bot.onrender.com";
```

On Render env for krab-interviewer-bot:

```env
KRAB_API_CORS_ALLOWED_ORIGINS=https://yourproject.com,https://www.yourproject.com
```

Safari may block cross-site cookies; proxy is still preferred.

---

## Linking from the host site (no iframe)

```html
<!-- Header or careers page -->
<a href="/driver-hiring/index.html">Become a driver</a>
<a href="/driver-hiring/requirements.html">Requirements</a>
```

Adjust path to match where you copied the folder.

Optional: only link to `interview.html` if the host site has its own careers landing.

---

## Custom branding

1. `static/config.js` → `KRAB_BRAND_NAME`
2. Edit hero text in `index.html` (company name, dispatch bot mention).
3. Replace Unsplash image URLs with your own under `static/images/` and update `<img src="...">` in HTML.

Do **not** change API field keys in `main.js` unless the backend changes.

---

## Environment on krab-interviewer-bot (ops)

Not in this folder — set on Render for the **bot service**:

| Variable | Purpose |
|----------|---------|
| `ADMIN_PASSWORD` | `/admin` dashboard on bot URL |
| `IP_HASH_SALT` | Draft dedupe by visitor |
| `KRAB_API_CORS_ALLOWED_ORIGINS` | If not using proxy |
| `SUPABASE_*`, `TELEGRAM_BOT_TOKEN` | Already required |

---

## Verify after wire-up

```text
GET  {origin}/driver-hiring/index.html          → 200 HTML
GET  {origin}/driver-hiring/static/config.js    → 200 JS
POST {origin}/api/interview/draft               → 200 JSON { draftId }
```

Open interview page → browser Network tab → draft POST must succeed.

---

## Full API reference

See `../web/README.md` in krab-interviewer repo (same API as hosted `web/`).

---

## When NOT to copy this folder

- If the whole site **is** krab-interviewer-bot, use built-in `web/` routes (`/`, `/interview`) — no copy needed.
- Admin dashboard stays on bot URL: `https://krab-interviewer-bot.onrender.com/admin` (not included in kit).
