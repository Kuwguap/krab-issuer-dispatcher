/** Parse Telegram Login redirect query params into widget auth payload. */
export function parseTelegramAuthFromSearch(search = window.location.search) {
  const p = new URLSearchParams(search);
  if (p.get("telegram_auth") !== "1") return null;
  const id = Number(p.get("id"));
  const auth_date = Number(p.get("auth_date"));
  const hash = p.get("hash");
  if (!id || !auth_date || !hash) return null;

  const user = { id, auth_date, hash };
  for (const [key, value] of p.entries()) {
    if (key === "telegram_auth" || key === "id" || key === "auth_date") continue;
    if (value) user[key] = value;
  }
  return user;
}

export function stripTelegramAuthFromUrl() {
  const url = new URL(window.location.href);
  if (url.searchParams.get("telegram_auth") !== "1") return;
  [
    "telegram_auth",
    "id",
    "auth_date",
    "hash",
    "username",
    "first_name",
    "last_name",
    "photo_url",
    "login_handoff",
  ].forEach((k) => url.searchParams.delete(k));
  const next = url.pathname + (url.searchParams.toString() ? `?${url.searchParams}` : "") + url.hash;
  window.history.replaceState({}, "", next);
}
