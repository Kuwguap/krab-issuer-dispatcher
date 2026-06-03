/**
 * REQUIRED — set before go-live.
 *
 * Mode A (same-origin proxy — recommended when pages live on another domain):
 *   KRAB_API_BASE_URL = ""  → API calls go to YOUR site /api/... (proxy to krab-interviewer)
 *
 * Mode B (direct to krab-interviewer service):
 *   KRAB_API_BASE_URL = "https://krab-interviewer-bot.onrender.com"
 *   Also set KRAB_API_CORS_ALLOWED_ORIGINS on Render to include your site URL.
 */
window.KRAB_API_BASE_URL = "https://krab-interviewer-bot.onrender.com";

/** Optional display name in nav */
window.KRAB_BRAND_NAME = "Tri State Tags";
