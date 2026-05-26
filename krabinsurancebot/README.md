# Krab Insurance Bot

Standalone Telegram bot for **NY FS-20 insurance card** issuance only.

Flow: send image, PDF, or 11-line text → AI parses client data → review/edit → build PDF + TriStateCoverage portal + Resend email to client.

Based on the insurance-card path in `krableadsV2` (no leads dispatch, Monday, or OneTimeSecret).

## Setup

1. Copy `.env.example` to `.env` and fill in:

| Variable | Required |
|----------|----------|
| `TELEGRAM_BOT_TOKEN` | Yes |
| `OPENAI_API_KEY` | Yes (vision parsing) |
| `RESEND_API_KEY` | Yes |
| `RESEND_FROM` | Yes |
| `INTEGRATIONS_API_KEY` | Yes (TriStateCoverage) |
| `INSURANCE_*` | Optional (defaults match krableadsV2) |

2. Install dependencies:

```bash
pip install -r requirements.txt
```

On Linux/Render, install Tesseract for optional OCR helpers in `ai_vision`:

```bash
apt-get install tesseract-ocr
```

3. Run:

```bash
python bot.py
```

## Commands

- `/start` — new insurance card intake
- `/help` — format help
- `/cancel` — abort current flow

## Deploy (Render)

- Service type: **Worker**
- Build: `pip install -r requirements.txt`
- Start: `python bot.py`
- Or use the included `Dockerfile`

## 11-line text format

```
Name
Address
City, State, ZIP
Delivery address
Delivery city, State, ZIP
VIN
Car (year make model)
Color
Insurance company
Insurance policy #
Extra info
```

Include `email@example.com` anywhere in text or in the uploaded image.
