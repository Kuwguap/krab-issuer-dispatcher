# Deploying the Whole Project

This monorepo contains two Telegram bots, their backends, and the shared admin
dashboard:

| Component                    | Location                      |
| ---------------------------- | ----------------------------- |
| Krab Dispatch API + admin UI | `krab-sender/` (FastAPI)      |
| Krab Dispatch Telegram bot   | `krab-sender/bot/`            |
| Krab Issuer admin (legacy)   | `krableadsV2/admin_dashboard.py` |
| Krab Issuer Telegram bot     | `krableadsV2/bot.py`          |
| Paper Investigator bot       | `krableadsV2/paper_investigator/` |
| Admin static frontend        | `krab-sender/admin/` (deploys to Vercel) |

---

## 1) Render (backend + bots)

Everything is described in the root-level **`render.yaml`** Blueprint. From that
one file Render will provision:

1. **krab-dispatch-api** – FastAPI, serves `/health`, the unified admin at
   `/admin/`, and all admin-protected JSON endpoints.
2. **krab-dispatch-bot** – worker (Telegram bot for Dispatch).
3. **krab-issuer-admin** – Flask admin (optional; the new unified admin
   replaces this but it's kept for legacy URLs).
4. **krab-issuer-bot** – worker (Telegram bot for Issuer).
5. **paper-investigator-bot** – worker (Paper Investigator bot).

### Deploy steps

1. Push this repo to GitHub.
2. Go to [Render dashboard](https://dashboard.render.com) → **New +** → **Blueprint**.
3. Pick the repo, keep the default branch, and confirm. Render reads
   `render.yaml` and creates all five services with suspended env vars.
4. Open each service → **Environment** and fill the `sync: false` variables.
   Critical ones:
   - Every service that talks to Supabase: `SUPABASE_URL`, and either
     `SUPABASE_SERVICE_ROLE_KEY` (Dispatch) or `SUPABASE_KEY` (Issuer). Both
     point at the same Supabase project.
   - `krab-dispatch-bot`: `API_BASE_URL` must equal the full URL of
     `krab-dispatch-api` once it's live (e.g. `https://krab-dispatch-api.onrender.com`).
   - `ADMIN_PASSWORD` must match on `krab-dispatch-api` and `krab-dispatch-bot`.
5. Save. Render auto-deploys each service.

Future git pushes to the tracked branch trigger auto-redeploy on all five
services. To redeploy only one, use the service's **Manual Deploy** button.

### Database

Dispatch uses PostgreSQL:

- Easiest: Render → **New +** → **PostgreSQL**, copy the **Internal Database
  URL**, paste into `DATABASE_URL` on both the Dispatch API and Dispatch bot.
- First boot runs `init_db()` and creates the `transactions` / `recipients`
  tables (see `krab-sender/backend/db.py`).

Issuer uses Supabase (run the SQL migrations in `krableadsV2/database/` once in
the Supabase SQL Editor).

---

## 2) Vercel (admin dashboard frontend)

The file under `krab-sender/admin/` is a pure static site: `index.html` + `app.js`
+ `vercel.json`. `app.js` auto-detects the API base:

- Opened from `localhost` / `127.0.0.1` → uses same origin (your local FastAPI).
- Opened from Vercel / any other host → uses `DEFAULT_API_BASE`
  (`https://krab-dispatch-api.onrender.com`).
- Override via `?api=https%3A%2F%2Fcustom.example.com` on the URL, or
  `localStorage.setItem("krab_api_base", "https://...")`.

### First-time link

```powershell
cd c:\Users\tatia\Downloads\unity\krab-sender\admin
npm i -g vercel         # only once per machine
vercel login
vercel link             # already linked to prj_4Rku57EyeXZtgeYuMbUfyAFiz545
```

(The `.vercel/project.json` in this folder already pins the Vercel project —
you can skip `vercel link` unless you want to relink.)

### Deploy to a preview URL

Run from the **admin** directory:

```powershell
cd c:\Users\tatia\Downloads\unity\krab-sender\admin
vercel
```

Vercel prints a preview URL like `https://krab-sender-xxx.vercel.app`.

### Deploy to production

Same folder:

```powershell
cd c:\Users\tatia\Downloads\unity\krab-sender\admin
vercel --prod
```

Production URL: the one configured on the Vercel project (e.g.
`https://krabdispatch.vercel.app`).

### CORS

Render's `krab-dispatch-api` already allows:

- Any `*.vercel.app` (covers production + preview deploys).
- `http://localhost:*` and `http://127.0.0.1:*` (local dev).

If you add a custom domain, append it to the `CORS_ORIGINS` env var on the
Dispatch API service (comma-separated).

---

## 3) Quick reference – command locations

| Action                        | Folder                                   | Command                       |
| ----------------------------- | ---------------------------------------- | ----------------------------- |
| Redeploy everything to Render | n/a (just `git push`)                    | Auto on push                  |
| Preview admin on Vercel       | `krab-sender/admin/`                     | `vercel`                      |
| Production admin on Vercel    | `krab-sender/admin/`                     | `vercel --prod`               |
| Local full stack              | `.` (monorepo root)                      | `python start_bots.py`        |
| Local admin only              | `krab-sender/`                           | `uvicorn backend.api:app`     |

---

## 4) Secrets you must never commit

Keep these in `.env` locally and in the Render dashboard remotely:

- `TELEGRAM_BOT_TOKEN` (Dispatch and Issuer — different tokens!)
- `ADMIN_PASSWORD`
- `DATABASE_URL`
- `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_SERVICE_ROLE_KEY`
- `EMAIL_SMTP_USERNAME`, `EMAIL_SMTP_PASSWORD`
- `OPENAI_API_KEY`
- `MONDAY_API_KEY`
- `ONETIMESECRET_*` values
