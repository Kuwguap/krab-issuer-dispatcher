/** Strip @ and lowercase for API; display can add @ back. */
export function normalizeTelegramUsername(raw) {
  const un = String(raw || "")
    .trim()
    .replace(/^@+/, "")
    .toLowerCase();
  if (!un || un.length < 5) return "";
  if (!/^[a-z0-9_]{5,32}$/.test(un)) return "";
  return un;
}

export function displayTelegramUsername(raw) {
  const un = normalizeTelegramUsername(raw);
  return un ? `@${un}` : "";
}

export function telegramConnectUrl(start = "web") {
  const bot = (import.meta.env.VITE_TELEGRAM_BOT_USERNAME || "krabinterviewerbot").replace(/^@+/, "");
  return `https://t.me/${encodeURIComponent(bot)}?start=${encodeURIComponent(start)}`;
}

/** Hide legacy /start copy from older API builds. */
export function userFacingTelegramMessage(msg) {
  if (!msg || /tap Start|verify your username|Open @|Log in with Telegram below/i.test(msg)) {
    return "Username saved. Open the bot link below, tap Start once, then come back — we check automatically every few seconds.";
  }
  return msg;
}
