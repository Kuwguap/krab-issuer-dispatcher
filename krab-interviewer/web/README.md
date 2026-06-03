# Krab Interviewer — Web Form Embedding Guide

Public driver application form and admin dashboard served by the same **krab-interviewer-bot** Render service as the Telegram bot.

## Copy to another project (no iframes)

Use the portable folder **[`../driver-hiring-kit/`](../driver-hiring-kit/)** — copy the **entire folder** into your other repo (e.g. `public/driver-hiring/`).

- **`README.md`** — quick start  
- **`AI-WIRE-UP.md`** — give this file to an AI agent to wire API proxy + links  
- **`proxy-examples/`** — nginx / Vercel / Next.js snippets  

Do **not** use iframes; serve static files and proxy `/api/*` to krab-interviewer-bot.

---

## Embedding on krab-interviewer host only

1. **Host options**
   - **CNAME:** Point `apply.yourdomain.com` → `krab-interviewer-bot.onrender.com` (or your custom Render URL).
   - **Iframe:** Embed without DNS changes (see below).
   - **Copy static files:** Copy `krab-interviewer/web/` into your site and keep API calls pointed at the interviewer service.

2. **API base URL** — Edit [`static/config.js`](static/config.js):

   ```js
   window.KRAB_API_BASE_URL = "https://krab-interviewer-bot.onrender.com";
   ```

   No other code changes are required for the stock form.

3. **Paths**

   | Path | Content |
   |------|---------|
   | `/` | Hiring funnel landing (hero, instructions, CTAs) |
   | `/requirements` | Requirements checklist (gate before form) |
   | `/interview` | **Application form** — use this for embeds |
   | `/how-to-telegram` | Telegram setup guide |
   | `/interview/embed` | Developer embed documentation (HTML) |

4. **Iframe — form only (recommended)**

   ```html
   <iframe
     src="https://apply.tristatetags.com/interview"
     width="100%"
     height="920"
     style="border:0;border-radius:12px"
     title="Driver application"
   ></iframe>
   ```

   Full funnel iframe: `src="https://apply.tristatetags.com/"`

   Live docs: `{KRAB_API_BASE_URL}/interview/embed`

5. **CORS** — The API allows `*` by default. Restrict in production:

   ```env
   KRAB_API_CORS_ALLOWED_ORIGINS=https://apply.yourdomain.com,https://www.yoursite.com
   ```

6. **Local dev**

   ```bash
   cd krab-interviewer
   pip install -r requirements.txt
   # Terminal A — HTTP only (no Telegram polling)
   uvicorn api.app:app --reload --port 8080
   # Or run full stack:
   python bot.py
   ```

   Open `http://localhost:8080/` for the funnel; form at `http://localhost:8080/interview`.

7. **Supabase migration** — Run [`database/migration_interview_drafts.sql`](../database/migration_interview_drafts.sql) in the Issuer Supabase SQL editor before accepting traffic.

8. **Admin dashboard** — `/admin` on the same host. Set `ADMIN_PASSWORD` in Render env.

---

## API reference

Base URL: `KRAB_API_BASE_URL` (no trailing slash). All JSON routes send cookies (`credentials: include` from browsers).

### `GET /api/health`

**Response:** `{ "ok": true }`

---

### `POST /api/interview/draft`

Creates or resumes the draft for this visitor (one row per IP hash).

**Response (200 / 201):**

```json
{
  "draftId": "uuid",
  "payload": { "full_name": "...", ... },
  "alreadySubmitted": false,
  "driversLicenseFileUrl": null
}
```

Sets HttpOnly cookie `krab_draft_id`.

If already submitted: `alreadySubmitted: true`, frozen payload returned (no error).

---

### `PATCH /api/interview/draft/{draftId}`

**Body:** `{ "payload": { "full_name": "Jane Doe", ... } }`

Only keys in `INTERVIEW_FIELD_KEYS` are merged.

**Response:** `{ "ok": true, "payload": { ... } }`

---

### `POST /api/interview/draft/{draftId}/license`

**Body:** `multipart/form-data`, field `file` (image).

**Response:** `{ "ok": true, "driversLicenseFileUrl": "https://..." }`

---

### `POST /api/interview/submit/{draftId}`

Validates required fields, creates row in `interviews` (`created_by_telegram_id: "web"`), marks draft `submitted`, notifies supervisors via Telegram `sendMessage`.

**Response:**

```json
{ "ok": true, "alreadySubmitted": false, "interviewId": "uuid" }
```

**409** if another pending interview exists for the same Telegram username.

---

### `POST /api/admin/login`

**Body:** `{ "password": "..." }` — must match `ADMIN_PASSWORD`.

Sets cookie `krab_admin_session`.

---

### `POST /api/admin/logout`

Clears admin session cookie.

---

### `GET /api/admin/session`

**Response:** `{ "authenticated": true | false }`

---

### `GET /api/admin/interviews?status=&q=&limit=`

Requires admin cookie.

**Response:**

```json
{
  "ok": true,
  "items": [
    {
      "draftId": "uuid",
      "createdAt": "2026-05-30T12:00:00Z",
      "name": "Jane Doe",
      "telegramUsername": "@jane",
      "phone": "5551234567",
      "email": "jane@example.com",
      "licenseUrl": "https://...",
      "status": "draft",
      "badge": { "color": "red", "label": "Started but never finished", "priority": 0 },
      "interviewStatus": null,
      "ipHashShort": "a1b2c3d4e5f6",
      "lastSeenAt": "..."
    }
  ]
}
```

Badge colors: **red** (draft, idle &gt; 1h), **yellow** (draft, active within 1h), **green** (submitted).

---

### `GET /api/admin/interviews/{draftId}`

Full draft row + `payload` + `driversLicenseFileUrl`.

---

## Field keys (mirror Telegram bot)

`full_name`, `work_commitment`, `phone_number`, `email`, `mailing_address`, `drivers_license_id`, `telegram_username`, `emergency_contact`, `referral`, `payment_method`, `profession_skill`, `telegram_id`

## Hard rules

- Raw IPs are never stored — only `sha256(ip + IP_HASH_SALT)`.
- One active draft per IP hash; resubmit from same IP returns `alreadySubmitted`.
- Business logic stays in `db.create_interview` — supervisors use `/open <id>` and **Hire** unchanged.

## Pages

| Path | File |
|------|------|
| `/` | `index.html` — funnel landing |
| `/requirements` | `requirements.html` |
| `/interview` | `interview.html` — application form |
| `/interview/embed` | `interview/embed.html` — attach to other projects |
| `/how-to-telegram` | `how-to-telegram.html` |
| `/admin` | `admin.html` |
