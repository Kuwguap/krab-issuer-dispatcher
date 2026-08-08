# krab-tag-bot

Unified NJ 30-day temp-tag service: a Telegram staff bot **and** an HTTP tag
generator, in one process (FastAPI in a daemon thread + python-telegram-bot on
the main thread — same pattern as `krab-interviewer`).

## What it does
- **Telegram:** staff DM labeled client/vehicle fields → the bot generates the
  NJ temp-tag PDF and offers to send the supervisory notice + PDF to a chosen
  dispatch group.
- **HTTP:** `POST /api/tag/generate` (Bearer `TAG_API_KEY`) → `application/pdf`
  (or `{url,plate,control_number}` with `?store=1`). Backs `tristatetags.com/tag`.

## One generator
The PDF generator is **not** duplicated. `krableadsV2/utils/tag_pdf.py` +
`krableadsV2/assets/` are the single source; the render.yaml `buildCommand`
copies them into `taggen/tag_pdf.py` + `assets/` at deploy time. For local dev /
tests, `tagcore.py` loads the generator straight from the sibling krableadsV2
checkout (identical bytes). krableadsV2 keeps generating in-process on its own
dispatch path — it does **not** call this service.

## Env
`TELEGRAM_BOT_TOKEN` (a NEW @BotFather bot — never reuse another bot's token),
`TAG_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `KRAB_API_CORS_ALLOWED_ORIGINS`,
`PORT` (Render-provided).

## Requires
The `allocate_temp_plate` Postgres RPC (krableadsV2 `migration_tag_plates.sql`)
for unified plate/control sequences; random fallback if unavailable.
