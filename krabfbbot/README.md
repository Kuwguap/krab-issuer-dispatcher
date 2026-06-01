# Krab Facebook Sales Bot (Service A)

OpenAI-powered Facebook Messenger sales agent for Tri State Coverage. Collects full insurance intake via chat, then forwards completed leads to **krab-tg-forwarder** (Service B) which DMs your team on Telegram.

Service A and B are separate microservices — each can run and be tested alone.

## Architecture

```
Facebook user -> POST /webhook (this service) -> OpenAI chat + slot fill
              -> when complete -> POST krab-tg-forwarder/leads -> Telegram DM
```

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `META_VERIFY_TOKEN` | yes | Webhook verify token (you choose) |
| `META_PAGE_ACCESS_TOKEN` | yes | Page access token from Meta |
| `META_APP_SECRET` | recommended | Validates `X-Hub-Signature-256` |
| `OPENAI_API_KEY` | yes | For sales replies + slot extraction |
| `OPENAI_MODEL` | no | Default `gpt-4o-mini` |
| `FORWARDER_BASE_URL` | yes | e.g. `http://localhost:8090` |
| `FORWARDER_API_KEY` | yes | Must match Service B |
| `PORT` | no | Default `8080` |

## Local run

**Terminal 1 — forwarder:**

```bash
cd krab-tg-forwarder
cp .env.example .env
# FORWARDER_API_KEY=dev-key
python main.py
```

**Terminal 2 — FB bot:**

```bash
cd krabfbbot
cp .env.example .env
# Match FORWARDER_API_KEY; set OPENAI_API_KEY
python main.py
```

Simulate a Messenger message (no Facebook):

```bash
curl -X POST http://localhost:8080/admin/replay \
  -H "Content-Type: application/json" \
  -d '{"psid":"test123","text":"Hi, I need insurance for my 2020 Honda"}'
```

Or run `scripts/curl_test.sh` (bash).

### Expose webhook locally (ngrok)

```bash
ngrok http 8080
```

Use the HTTPS URL + `/webhook` in Meta Developer Console.

## Facebook Page + App setup

Messenger bots only work on **Facebook Pages**, not personal profiles.

### 1. Create a Facebook Page

- facebook.com -> Pages -> Create Page (business/brand).
- Note the Page name — you will link it to the app.

### 2. Create a Meta Developer App

1. Go to [developers.facebook.com](https://developers.facebook.com)
2. **My Apps** -> **Create App** -> type **Business**
3. Add product **Messenger**

### 3. Page access token

1. Messenger -> **Messenger API Settings**
2. Under **Access Tokens**, click **Add** / **Generate** for your Page
3. Copy token -> `META_PAGE_ACCESS_TOKEN`
4. Grant `pages_messaging` permission when prompted

### 4. Webhook

1. Messenger -> **Webhooks** -> **Add Callback URL**
   - URL: `https://<your-host>/webhook`
   - Verify token: same string as `META_VERIFY_TOKEN` in `.env`
2. Click **Verify and Save**
3. Subscribe to webhook fields: **messages**, **messaging_postbacks**
4. Under **Webhooks**, click **Add Subscriptions** on your Page row

### 5. App secret (signature verification)

1. App Settings -> **Basic** -> **App Secret** -> Show
2. Copy to `META_APP_SECRET`

### 6. Test users (Development mode)

While the app is in **Development**, only users with a role on the app can message the Page:

1. App Roles -> **Roles** -> Add testers (Facebook accounts)
2. Testers accept invite at facebook.com/developers
3. Message your Page from the tester account

### 7. Go live (optional)

Submit **App Review** for `pages_messaging` when you need to chat with the general public. Until approved, only testers can DM the Page.

## Collected fields

Full insurance intake: name, address, city/state/zip, delivery address, delivery city/state/zip, VIN, year/make/model, color, current insurance, policy number, delivery time, phone, email.

When all slots are filled, the bot confirms to the client and POSTs to the forwarder.

## Dead-letter queue

If Service B is down, leads are stored in SQLite (`pending_forwards`) and retried every 60s (`RETRY_INTERVAL_SECONDS`).

## Docker

```bash
docker build -t krabfbbot .
docker run --env-file .env -p 8080:8080 krabfbbot
```

## Render

Deployed as `krab-fb-sales-bot` from root `render.yaml`.  
`FORWARDER_BASE_URL` is wired to the `krab-tg-forwarder` service host automatically.
