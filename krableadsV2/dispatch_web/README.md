# dispatch_web — the bot, mirrored

The krableadsV2 Telegram bot's dispatch process as web pages: a Flask blueprint
mounted at `/dispatch` on the **existing** admin dashboard service
(`admin_dashboard.py`, the thing behind tristatetags.com/backend). Same process,
same Supabase, the bot's own `utils.database.Database` wrapper — there is no
second service, no second schema, and no API between the mirror and the bot.
State lives in columns; both sides read them.

The web does **not** talk to Telegram. It writes the same rows the bot writes,
and the running bot delivers them. That split is the whole design: everything
here can be down and dispatch still works from Telegram; everything here can be
up and nothing dispatches unless the bot worker is running.

## How a web lead reaches Telegram

`/dispatch/new` creates the lead bot-shaped (`create_lead` with the same field
layout `bot.py` writes: the 11-line `vehicle_details` blob, `delivery_details`,
raw `phone_number`, 8-char `reference_id`) and sets
`ingest_dispatch_pending=True` — the same flag the HTTP ingest API
(`LEAD_INGEST.md`) sets. The bot's `process_pending_api_lead_dispatches` job
polls for that flag every ~10 seconds, claims the lead (flag flipped before
sending, so a slow send can't double-dispatch), and posts Accept buttons to
every active Dispatcher group. First team to accept wins; drivers, Monday sync,
receipts, renewals all follow the bot's normal path.

So: **no bot worker running → the lead sits saved-but-undelivered** until the
worker next boots. The web page says "the bot is dispatching this" because that
is literally who does it.

Terminology: rows in the `groups` table are **Dispatchers** in every UI string,
and drivers stay **Drivers**. The pages keep that rule.

## Routes

All under `/dispatch`. Everything except `/health` and `/login` requires the
password session; everything including `/login` answers **503** until
`DISPATCH_WEB_PASSWORD` is set (fail closed — an unset password is a locked
door, not an open one). `/health` alone is exempt, so monitoring can see the
service (and its config gap) without auth.

| Route | What / why |
|---|---|
| `GET /health` | Unauthenticated liveness probe. The one hole in the 503 gate. |
| `GET,POST /login` | Password form. `?next=` is honored only for same-origin `/dispatch...` paths. |
| `GET,POST /logout` | Drops the session flag. |
| `GET /` | **Board** — the bot's 🧾 Recent Leads browser: 25 newest, who entered each, struck or not. Struck rows stay listed; a strike that hides its row hides the gaming it exists to catch. |
| `GET /data.json` | The Board's 10-second refresh feed (rows + rendered tbody fragment, so a refresh can never drift from a full page load). |
| `GET,POST /new` | **New Lead** — paste → parse → review grid → submit. Validation mirrors the ingest parser rule-for-rule; one address fills both, `150` becomes `$150`. POST writes the bot-shaped row described above. |
| `POST /api/parse` | Pasted text in, grid fields out as JSON. Always 200; on parse failure `ok:false` plus best-effort fields so the operator finishes by hand. |
| `GET /lead/<id>` | One lead, the whole story — every stored fact, pre-shaped so a missing migrated column can't 500 the page (the bot's empty-card scar). |
| `POST /lead/<id>/strike` | `exclude_from_count = True`. Reversible on purpose. |
| `POST /lead/<id>/restore` | `exclude_from_count = False`. |
| `GET /lead/<id>/tag.pdf?car=N` | The temp-tag PDF, regenerated on demand. Reuses the stored plate/control number so the download is byte-identical to what Telegram delivered; allocates (and persists) only when a lead never got one. `car=1` is the lead's own car, 2+ its extras. `no-store`: it's a legal document with PII. |
| `GET /leaderboard` | Who entered the most clients — names and counts **only**. A board with contact details on it is a poach list. |
| `GET /rosters` | Dispatchers and Drivers with the same switches the bot's /settings has. SQL NULL `is_active` means ACTIVE (`record_is_active`), same as the dispatch loop. |
| `POST /rosters/group/<id>/toggle` | Flip a Dispatcher's active state. |
| `POST /rosters/driver/<id>/toggle` | Flip a Driver's active state. |
| `POST /rosters/driver/<id>/suspend` | The **manual** suspend flag only. Receipt-debt suspension (5+ owed) lifts by uploading receipts, so there is deliberately no button for it. |
| `GET /receipts` | Newest 30 stored receipt images. Shows what's stored; attaching stays the bot's job. |
| `GET /settings` | Instant-Tag-to-all-drivers switch, plate counters (read-only — the bot's photo flow owns them), and env **presence** booleans. Key values never reach HTML, logs, or Sentry. |
| `POST /settings/instant-all` | Flip `instant_all_drivers` — the same setting the bot's ⚡ screen writes; the bot reads it live. |

Every page tolerates a dead database: an error banner on a rendered page, never
a traceback. The board just retries on its next 10s tick.

## Env vars

| Variable | Required | Why |
|---|---|---|
| `DISPATCH_WEB_PASSWORD` | **yes** | The only auth. Unset or blank → every route (except `/health`) answers `503 set DISPATCH_WEB_PASSWORD`. Whitespace is stripped (Render copy-paste scar). |
| `DISPATCH_WEB_SECRET` | no | Signs the session cookie — only applied if the host app has no `secret_key` already. Falls back to `SUPABASE_KEY`, which is already secret, already deployed, and the host refuses to boot without it, so sessions always sign with something real. Set it explicitly if you ever rotate the Supabase key and want logins to survive. |
| `DISPATCH_WEB_USER_ID` | no | Numeric Telegram id stamped as `user_id` on web-entered leads (attribution in the bot's own views). Unset → `0`, an anonymous web entry. The "Entered by" name typed on the form fills `telegram_name`/`telegram_username` either way, which is what the leaderboard shows. |

Plus everything the host already needs (`SUPABASE_URL`, `SUPABASE_KEY`, …) —
the mirror adds no connection of its own.

## Mounting

Two lines in `admin_dashboard.py`, after `app = Flask(__name__)`:

```python
import dispatch_web
dispatch_web.register(app)
```

Importing the package attaches every view module's routes to the blueprint;
`register` is idempotent and fills `app.secret_key` only if the host has none.
One broken view module logs loudly and goes dark alone — it cannot take
tristatetags.com/backend down with it.

URLs, from inside out:

- On the service itself (krab-issuer-admin on Render): `…/dispatch/`.
- Through the existing proxy, that surfaces as **tristatetags.com/backend/dispatch**.
- The pages link with absolute `/dispatch/...` paths (literal paths, not
  `url_for` — one missing endpoint must not BuildError the whole nav). Under
  the `/backend` prefix those links resolve to `tristatetags.com/dispatch/...`,
  so for browser use add a proxy rule on the tristatetags.com front mapping
  `/dispatch/*` → the admin service's `/dispatch/*` (prefix preserved). That
  rule is also the short public URL: **tristatetags.com/dispatch**. Until it
  exists, use the Render URL directly — it matches the paths as-is.

## Deliberately NOT mirrored

The bot remains the delivery engine; the mirror only reads its state and queues
work for it. Missing on purpose:

- **The Telegram conversations** — the review card, button flows, group
  routing, driver DMs. The web has forms instead; the outcome rows are shaped
  identically.
- **Voice, photo, and PDF understanding** — dictation, screenshot parsing,
  VIN/plate reading. Paste text or type into the grid.
- **The AI chat layer** (`KRAB_CHAT_LAYER`) and fluency parsing. The web is
  deterministic: the same `utils/external_lead_parser` the ingest API uses,
  nothing softer.
- **Dispatch itself** — no Telegram token lives in this process. Accept
  buttons, driver offers, tag delivery, receipt chasing, renewals: all the
  running bot's.
- **Phone encryption** — web leads store the raw number in `phone_number`
  (`encrypted_link=None`), exactly like a bot-entered lead; the OneTimeSecret
  path belongs to the ingest API.

## Running locally

```
cd krableadsV2
# .env already carries SUPABASE_URL / SUPABASE_KEY (config.py loads it)

# cmd.exe — nothing after the value: cmd has no `#` comments, so a trailing
# one is stored INSIDE the password and login fails with "Wrong password."
set DISPATCH_WEB_PASSWORD=letmein

# PowerShell — its `set` is Set-Variable, a PS variable the child python
# never inherits (every route would 503); the env: drive is the real one:
$env:DISPATCH_WEB_PASSWORD="letmein"

python admin_dashboard.py             # PORT / ADMIN_PORT, default 5000
```

Then http://localhost:5000/dispatch/ — you should get the login page; without
the password env you get the 503 instead, which is the gate working.

To watch a web lead actually dispatch, run the worker too (`python bot.py`) —
it carries the ~10s poll. Migrations: `database/migration_lead_api_ingest.sql`
gives leads the ingest columns — and without it `create_lead` does **not**
refuse: both columns are in its optional-write set, so it retries with them
dropped, the save succeeds, the "the bot is dispatching this" flash still
shows, and the lead sits saved but never dispatched (the flag the poll looks
for was silently discarded). Verify the migration ran; there is no loud
failure to wait for. `migration_telegram_name`, `migration_driver_manual_suspend`, and
`migration_extra_vehicles` back the entered-by names, the suspend button, and
multi-car tags — the full without-it table lives in `WIRING.md`.
