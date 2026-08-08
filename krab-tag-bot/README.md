# krab-tag-bot

Web-only NJ 30-day temp-tag generator. Serves `POST /api/tag/generate`
(Bearer `TAG_API_KEY`) for the **tristatetags.com/tag** page.

The Telegram tag-creation flow (welcome → AI parse → review → group/driver
selection) and `/settings` (plate counters + group management) live in
**krableadsV2 / krab-issuer-bot** — the one bot. This service has no Telegram
bot; it's a FastAPI app run by uvicorn (`python bot.py`).

## One generator
The PDF generator is not duplicated. `krableadsV2/utils/tag_pdf.py` +
`krableadsV2/assets/` are the single source; the render.yaml `buildCommand`
copies them into `taggen/tag_pdf.py` + `assets/` at deploy time. For local dev /
tests, `tagcore.py` loads the generator straight from the sibling krableadsV2
checkout.

## API
`POST /api/tag/generate` → `application/pdf` (or `{url,plate,control_number,
reference}` with `?store=1`). Accepts explicit client/vehicle fields and/or a
free-text `message` (parsed by `parsing.parse_details`). Derives is_nj from
state, allocates the plate/control via the shared `allocate_temp_plate` RPC,
VIN-decodes blanks, and normalizes the body to the door-suffix format. Every
tag gets a reference # logged to tristatetags.com/backend (`X-Tag-Reference`).

## Env
`TAG_API_KEY`, `SUPABASE_URL`, `SUPABASE_KEY`, `KRAB_API_CORS_ALLOWED_ORIGINS`,
`KRAB_DISPATCH_API_URL`, `KRAB_DISPATCH_ADMIN_PASSWORD`, `PORT` (Render-provided).
