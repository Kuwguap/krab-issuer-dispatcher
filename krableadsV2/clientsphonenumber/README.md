## What this app does
`clientsphonenumber` is a Supabase-backed, OneTimeSecret-compatible service used by the bot to:
- store sensitive values (phone numbers) keyed by a random `secret_key`
- return a OneTimeSecret-style API contract: `POST /api/v1/share`
- provide an unlock UI at `/secret/[secret_key]`

It also includes an admin UI at `/admin` to configure:
- passphrase required to unlock secrets
- whether secrets expire (and for how many days)
- whether secrets are deleted after the first successful unlock (toggle default OFF)

## Important: API compatibility
This app implements the subset your Python bot uses from `utils/onetimesecret.py`:
- `POST /api/v1/share` with Basic auth
  - form fields: `secret`, `passphrase`, `ttl`
  - response JSON: `{ secret_key, metadata_key }`

## Required environment variables
Add these to your Vercel project:
- `SUPABASE_URL`
- `SUPABASE_KEY`

- `ONETIMESECRET_USERNAME`
- `ONETIMESECRET_API_KEY`

The unlock passphrase and TTL settings are stored in Supabase (via `/admin`).

## Custom domain
Point your domain (e.g. `clientsphonenumber.com`) to this Vercel project in **Vercel → Project → Settings → Domains**. Use **HTTPS** in env vars (`ONETIMESECRET_URL`, `ONETIMESECRET_LINK_BASE`) on the bot/Render side.

## Routes
- `POST /api/v1/share` (OneTimeSecret-compatible)
- `POST /api/v1/unlock` (used by unlock UI)
- `GET  /secret/[secret_key]` (unlock page UI)
- `GET  /admin` (admin settings UI)

