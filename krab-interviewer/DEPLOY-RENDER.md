# Fix 404 on `/api/interview/*` (tristatetags.com)

The site **proxies** interview requests to **krab-interviewer-bot** on Render. A **404** means Vercel is working, but Render is **not** serving the FastAPI routes yet.

## Quick check

Open in a browser:

```
https://krab-interviewer-bot.onrender.com/api/health
```

| Result | Meaning |
|--------|---------|
| `{"ok":true}` | API is up — set `KRAB_INTERVIEWER_URL` on Vercel if needed, hard-refresh tristatetags |
| `Not Found` / 404 | Service is still **Worker**, old code, or FastAPI failed to start — follow steps below |

---

## Step 1 — Service type must be **Web**

In [Render Dashboard](https://dashboard.render.com) → **krab-interviewer-bot** → **Settings**:

1. Confirm **Service type** is **Web** (not **Worker**).
2. If it says Worker: create a new **Web** service from the blueprint below, migrate env vars, then delete the old Worker (only one instance may poll the bot token).

Blueprint already specifies:

```yaml
type: web
healthCheckPath: /api/health
startCommand: python bot.py
```

---

## Step 2 — Deploy latest code

### Option A — Unity monorepo (recommended)

Repo: `https://github.com/Kuwguap/krab-issuer-dispatcher`  
Blueprint: root [`render.yaml`](../render.yaml)  
Service: `krab-interviewer-bot` with `rootDir: krab-interviewer`

1. Render → **Blueprints** → your unity blueprint → **Manual sync** / **Apply**.
2. Or: **krab-interviewer-bot** → **Settings** → set **Root Directory** to `krab-interviewer` and connect repo `krab-issuer-dispatcher`.
3. **Manual Deploy** → Deploy latest commit on `main`.

### Option B — Standalone `krab-interviewer` repo

Repo must include the `api/` folder and [`render.yaml`](render.yaml). Push latest, then **Manual Deploy**.

---

## Step 3 — Environment variables

On **krab-interviewer-bot** → **Environment**, set at minimum:

| Variable | Required |
|----------|----------|
| `TELEGRAM_BOT_TOKEN` | yes |
| `SUPABASE_URL` | yes |
| `SUPABASE_KEY` (or `SUPABASE_SERVICE_ROLE_KEY`) | yes |
| `SUPERVISORY_TELEGRAM_ID` | yes |
| `DRIVER_CHANNEL_ID` | yes |
| `OPENAI_API_KEY` | yes (AI auto-fill) |
| `ADMIN_PASSWORD` | yes (web `/admin`) |
| `IP_HASH_SALT` | yes (long random string) |
| `KRAB_PUBLIC_BASE_URL` | `https://krab-interviewer-bot.onrender.com` |
| `KRAB_API_CORS_ALLOWED_ORIGINS` | `https://tristatetags.com,https://www.tristatetags.com` |
| `KRAB_INTERVIEWER_BOT_USERNAME` | `krabinterviewerbot` |

Monorepo blueprint links `SUPABASE_*`, `OPENAI_API_KEY`, etc. from **krab-issuer-bot** — ensure those source services have values filled in.

After saving env, **Manual Deploy** again.

---

## Step 4 — Supabase migrations

In Supabase SQL editor (Issuer project), run in order:

1. `database/migration_krab_interviewer.sql`
2. `database/migration_interview_drafts.sql`
3. `database/migration_telegram_user_directory.sql`

Create Storage bucket **`driver_licenses`** (public read) if missing.

---

## Step 5 — Verify logs

Render → **krab-interviewer-bot** → **Logs**. On a good deploy you should see:

```
FastAPI listening on 0.0.0.0:10000
FastAPI ready on port 10000
FastAPI web API started (draft form + /api/health)
```

If the service **exits** on deploy, logs show the real error (missing env, import failure, DB migration missing).

---

## Step 6 — Vercel (tristatetags)

Vercel project → **Environment**:

| Variable | Value |
|----------|--------|
| `KRAB_INTERVIEWER_URL` | `https://krab-interviewer-bot.onrender.com` |

Redeploy: `vercel --prod`

---

## Architecture

```
Browser → tristatetags.com/api/interview/*
       → Vercel serverless (api/interview/*.js)
       → https://krab-interviewer-bot.onrender.com/api/interview/*
       → FastAPI (same process as Telegram bot)
```
