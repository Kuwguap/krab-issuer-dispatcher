# Krab Telegram Forwarder (Service B)

Receives insurance leads over HTTP and DMs them to a Telegram user or group.  
Runs **independently** of the Facebook bot — test with `POST /test` without Telegram credentials.

## Environment

| Variable | Required | Description |
|----------|----------|-------------|
| `FORWARDER_API_KEY` | yes | Shared secret; Service A sends `X-Forwarder-Key` |
| `TELEGRAM_BOT_TOKEN` | for `/leads` | Bot token from @BotFather |
| `TELEGRAM_TARGET_CHAT_ID` | for `/leads` | Numeric chat id (user or group) |
| `PORT` | no | Default `8090` |
| `DATABASE_PATH` | no | SQLite log file |

## Local run

```bash
cd krab-tg-forwarder
cp .env.example .env
# Set FORWARDER_API_KEY=dev-key (minimum for /test)
pip install -r requirements.txt
python main.py
```

Health: `curl http://localhost:8090/health`

Dry run (no Telegram):

```bash
curl -X POST http://localhost:8090/test \
  -H "Content-Type: application/json" \
  -H "X-Forwarder-Key: dev-key" \
  -d '{"name":"Jane Doe","address":"1 Main St","city_state_zip":"NYC, NY 10001","vin":"1HGBH41JXMN109186","phone":"845-555-0100","email":"jane@example.com"}'
```

Live send (needs token + chat id):

```bash
curl -X POST http://localhost:8090/leads \
  -H "Content-Type: application/json" \
  -H "X-Forwarder-Key: dev-key" \
  -d @sample_lead.json
```

### Get `TELEGRAM_TARGET_CHAT_ID`

1. Message your bot once.
2. Open `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Copy `message.chat.id` from the JSON.

## API

- `GET /health` — liveness
- `POST /test` — validate payload, return `reference_id`, **no Telegram send**
- `POST /leads` — format 11-line block + phone/email, send to Telegram

Response: `{ "ok": true, "reference_id": "FB-A1B2C3D4" }`

## Docker

```bash
docker build -t krab-tg-forwarder .
docker run --env-file .env -p 8090:8090 krab-tg-forwarder
```

## Render

Deployed as `krab-tg-forwarder` web service from root `render.yaml`.  
Set env vars in the Render dashboard after blueprint sync.
