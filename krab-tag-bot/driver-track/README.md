# Krab Driver Tracking

Next.js 14 (App Router, JavaScript) site that captures driver GPS for the Krab Telegram
dispatch bot. When a driver accepts a delivery in Telegram, the bot sends them a link
`https://<site>/t/<token>`. The page asks for browser location, posts pings to the API,
and tells the driver to return to Telegram once the first ping lands (the bot then sends
the delivery details). `/admin` shows a live dark-themed Leaflet map with driver markers,
movement trails, and a sessions table.

## Routes

| Route | What it does |
| --- | --- |
| `/t/<token>` | Driver-facing location page (share location, success overlay, background pings) |
| `/admin` | Passcode-protected live map + sessions dashboard |
| `GET /api/v1/session/<token>` | Minimal session status (no lead data) |
| `POST /api/v1/ping` | `{token, lat, lng, accuracy?, speed?, heading?}` → stores a ping, claims first-ping |
| `POST /api/admin/login` | `{passcode}` → sets signed `track_admin` cookie (12h) |
| `GET /api/admin/overview?hours=8` | Sessions + latest driver positions + trails (cookie required) |

## Environment variables

See `.env.local.example`. All are server-side except the `NEXT_PUBLIC_` one:

- `SUPABASE_URL` — Supabase project URL
- `SUPABASE_KEY` — Supabase service role key
- `TRACK_ADMIN_PASSCODE` — passcode for `/admin`
- `ADMIN_COOKIE_SECRET` — random hex secret for HMAC-signing the admin cookie
- `NEXT_PUBLIC_TELEGRAM_BOT_USERNAME` — bot username without `@` (for the "Open Telegram" button)

## Database

Depends on two tables created by `database/migration_driver_tracking.sql` (in the parent
project): `driver_tracking_sessions` and `driver_location_pings`. The app never reads any
other table (no leads, no drivers).

## Local dev

```bash
npm install
cp .env.local.example .env.local   # then fill in values
npm run dev
# http://localhost:3000
```

## Deploy (Vercel CLI)

```bash
npm i -g vercel
vercel            # link + preview deploy
vercel --prod     # production
```

Add the five env vars in the Vercel project settings (or `vercel env add`) before the
production deploy, then point the Telegram bot's tracking base URL at the deployed domain.
