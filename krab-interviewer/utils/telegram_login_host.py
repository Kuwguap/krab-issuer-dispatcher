"""Telegram Login Widget hosted on the API domain (fixes Bot domain invalid on Vercel)."""
from __future__ import annotations

from typing import List
from urllib.parse import quote, urlparse

from config import Config


def telegram_bot_username() -> str:
    return (Config.KRAB_INTERVIEWER_BOT_USERNAME or "krabinterviewerbot").strip().lstrip("@")


def widget_base_url() -> str:
    """Public URL where the Login Widget is served (must match BotFather /setdomain)."""
    raw = (getattr(Config, "TELEGRAM_LOGIN_WIDGET_BASE_URL", None) or Config.KRAB_PUBLIC_BASE_URL or "").strip()
    return raw.rstrip("/")


def allowed_return_origins() -> List[str]:
    raw = (getattr(Config, "TELEGRAM_LOGIN_RETURN_ORIGINS", None) or "").strip()
    origins: List[str] = []
    seen: set[str] = set()
    if raw:
        for part in raw.split(","):
            o = part.strip().rstrip("/")
            if o and o not in seen:
                seen.add(o)
                origins.append(o)
    cors = (Config.KRAB_API_CORS_ALLOWED_ORIGINS or "").strip()
    if cors and cors != "*":
        for part in cors.split(","):
            o = part.strip().rstrip("/")
            if o and o not in seen:
                seen.add(o)
                origins.append(o)
    for fallback in ("http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:4173"):
        if fallback not in seen:
            seen.add(fallback)
            origins.append(fallback)
    return origins


def is_allowed_return_url(url: str) -> bool:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    origin = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    allowed = allowed_return_origins()
    if not allowed:
        return True
    return origin in allowed


def auth_return_bridge_path() -> str:
    return "/api/interview/telegram-auth-return"


def auth_return_bridge_url(final_return_url: str) -> str:
    """Intermediate page on the API host that forwards full auth data to the frontend."""
    base = widget_base_url()
    path = auth_return_bridge_path()
    return f"{base}{path}?target={quote(final_return_url, safe='')}"


def is_api_auth_bridge_url(url: str) -> bool:
    """True when url is our hosted auth-return bridge (same host as Login Widget)."""
    base = widget_base_url()
    if not base:
        return False
    parsed = urlparse((url or "").strip())
    expected = urlparse(f"{base}{auth_return_bridge_path()}")
    return (
        parsed.scheme == expected.scheme
        and parsed.netloc == expected.netloc
        and parsed.path.rstrip("/") == expected.path.rstrip("/")
        and bool(parsed.query)
    )


def is_allowed_callback_return_url(url: str) -> bool:
    """Callback may redirect to the API bridge or an allowed frontend origin."""
    if is_api_auth_bridge_url(url):
        return True
    return is_allowed_return_url(url)


def auth_return_bridge_html() -> str:
    """Parse full Telegram auth query on the API domain and postMessage to the form."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Telegram login</title>
  <style>
    body {
      font-family: system-ui, sans-serif;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f8f7fc;
      color: #1c1633;
      padding: 1.5rem;
      text-align: center;
    }
  </style>
</head>
<body>
  <p>Finishing Telegram login…</p>
  <script>
    (function () {
      var params = new URLSearchParams(window.location.search);
      var target = params.get("target");
      if (!target) {
        document.body.textContent = "Missing return URL.";
        return;
      }
      params.delete("target");
      if (params.get("telegram_auth") !== "1") {
        window.location.replace(target);
        return;
      }
      var id = Number(params.get("id"));
      var authDate = Number(params.get("auth_date"));
      var hash = params.get("hash");
      if (!id || !authDate || !hash) {
        window.location.replace(target);
        return;
      }
      var user = { id: id, auth_date: authDate, hash: hash };
      params.forEach(function (value, key) {
        if (key === "telegram_auth" || key === "id" || key === "auth_date") return;
        if (value) user[key] = value;
      });
      var origin;
      try {
        origin = new URL(target).origin;
      } catch (e) {
        document.body.textContent = "Invalid return URL.";
        return;
      }
      if (window.opener && !window.opener.closed) {
        window.opener.postMessage({ type: "krab-telegram-auth", user: user }, origin);
        window.close();
        return;
      }
      var sep = target.indexOf("?") >= 0 ? "&" : "?";
      window.location.replace(target + sep + params.toString());
    })();
  </script>
</body>
</html>"""


def login_page_html(return_url: str) -> str:
    base = widget_base_url()
    bot = telegram_bot_username()
    bridge = auth_return_bridge_url(return_url)
    callback = f"{base}/api/interview/telegram-widget-callback?return_url={quote(bridge, safe='')}"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Log in with Telegram</title>
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #f8f7fc;
      color: #1c1633;
      padding: 1.5rem;
    }}
    .box {{
      text-align: center;
      max-width: 22rem;
    }}
    h1 {{ font-size: 1.15rem; margin: 0 0 0.5rem; }}
    p {{ font-size: 0.9rem; color: #5c5470; margin: 0 0 1.25rem; line-height: 1.45; }}
  </style>
</head>
<body>
  <div class="box">
    <h1>Log in with Telegram</h1>
    <p>Tap the button below. We’ll send your username and ID back to the interview form.</p>
    <script async src="https://telegram.org/js/telegram-widget.js?22"
      data-telegram-login="{bot}"
      data-size="large"
      data-radius="8"
      data-auth-url="{callback}"
      data-request-access="write"></script>
  </div>
</body>
</html>"""
