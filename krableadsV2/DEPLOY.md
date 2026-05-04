# Deployment Guide

## Render Blueprint Deployment

This project includes a `render.yaml` Blueprint at the repo root for one-click deployment to Render.

### Deploy from Blueprint

1. **Push your repo to GitHub/GitLab** (must be connected to Render).

2. **Create Blueprint in Render:**
   - Go to [Render Dashboard](https://dashboard.render.com) → **Blueprints**
   - **New** → **Create Blueprint**
   - Connect your repo and select the branch
   - Render reads `render.yaml` and provisions:
     - **krab-issuer-admin** (web service) – Flask admin dashboard
     - **krab-issuer-bot** (worker) – Telegram bot

3. **Set environment variables** when prompted (or in each service's Environment tab):
   - All vars use `sync: false` – you must add values in the Render dashboard
   - See table below

4. **Supabase:** Run `database/schema.sql`, `database/schema_multi_group.sql`, `database/migration_driver_timeout.sql`, and `database/migration_clientsphonenumber.sql` in your Supabase SQL Editor before the bot receives leads.

### Environment Variables for Render

**krab-issuer-admin (web):** SUPABASE_URL, SUPABASE_KEY

**krab-issuer-bot (worker):** Add these in the Render dashboard:

| Variable | Required | Description |
|----------|----------|-------------|
| TELEGRAM_BOT_TOKEN | Yes | Telegram bot token |
| MONDAY_API_KEY | Yes | Monday.com API key |
| MONDAY_BOARD_ID | Yes | Monday.com board ID |
| ONETIMESECRET_USERNAME | Yes | Basic-auth username used by `clientsphonenumber` API |
| ONETIMESECRET_API_KEY | Yes | Basic-auth key used by `clientsphonenumber` API |
| ONETIMESECRET_URL | Yes | OneTimeSecret-compatible endpoint (e.g. `https://clientsphonenumber.com/api/v1/share`) |
| ONETIMESECRET_LINK_BASE | Yes | Link base shown to users (e.g. `https://clientsphonenumber.com/secret/`) |
| ONETIMESECRET_PASSPHRASE | Yes | Passphrase sent by bot when creating secret links (unlock checks Supabase `clientsphonenumber_config`; share API syncs passphrase from the bot on each new secret) |
| SUPABASE_URL | Yes | Supabase project URL |
| SUPABASE_KEY | Yes | Supabase anon key |
| DRIVER_TELEGRAM_ID | Optional | Legacy driver ID |
| GROUP_TELEGRAM_ID | Optional | Legacy group ID |
| SUPERVISORY_TELEGRAM_ID | Optional | Legacy supervisory ID |
| **OPENAI_API_KEY** | **Yes (for AI)** | OpenAI API key for AI vision and missing-field detection |
| OPENAI_VISION_MODEL | Optional | Model for AI (default: gpt-4o) |
| API_NINJAS_API_KEY | Optional | For premium VIN lookup (VIN_PROVIDER=api_ninjas) |
| VIN_PROVIDER | Optional | `nhtsa` (free, default) or `api_ninjas` |

**OPENAI_API_KEY** is required for:
- AI vision (screenshot/image extraction)
- AI missing-field detection (prompts users when they omit color, VIN, car, etc.)

## Git Push and Vercel

1. **Push to Git:**
   ```bash
   git add .
   git commit -m "Your message"
   git push origin main
   ```

2. **Vercel (Admin Frontend):**
   - The admin dashboard lives in `admin-frontend/` (Next.js)
   - Vercel auto-deploys from your Git repo if connected
   - Ensure `admin-frontend` is the root or set root directory in Vercel project settings
   - Add env vars in Vercel: `NEXT_PUBLIC_ADMIN_BACKEND_URL` (e.g. your Render admin web URL)

3. **Vercel (Clients Phone Number app):**
   - The OneTimeSecret-compatible app lives in `clientsphonenumber/` (Next.js)
   - Deploy as its own Vercel project; add custom domain (e.g. `clientsphonenumber.com`)
   - Add env vars:
     - `SUPABASE_URL`
     - `SUPABASE_KEY`
     - `ONETIMESECRET_USERNAME`
     - `ONETIMESECRET_API_KEY`

4. **Render:**
   - Push triggers automatic redeploy if Render is connected to your repo
   - Add `OPENAI_API_KEY` in Render → krab-issuer-bot → Environment
