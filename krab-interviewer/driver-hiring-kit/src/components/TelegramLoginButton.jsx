import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { parseTelegramAuthFromSearch, stripTelegramAuthFromUrl } from "../telegramAuth";

const BOT_USERNAME = (import.meta.env.VITE_TELEGRAM_BOT_USERNAME || "krabinterviewerbot").replace(/^@+/, "");

function buildLoginPageUrl(widgetBase, returnUrl) {
  const base = (widgetBase || "").replace(/\/$/, "");
  if (!base) return null;
  return `${base}/api/interview/telegram-login-page?return_url=${encodeURIComponent(returnUrl)}`;
}

export default function TelegramLoginButton({ onAuth, disabled }) {
  const [loading, setLoading] = useState(true);
  const [loginUrl, setLoginUrl] = useState(null);
  const [widgetBase, setWidgetBase] = useState(null);
  const onAuthRef = useRef(onAuth);
  onAuthRef.current = onAuth;

  const deliverAuth = useCallback((user) => {
    if (user) onAuthRef.current?.(user);
  }, []);

  useEffect(() => {
    const onMessage = (event) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "krab-telegram-auth" || !event.data.user) return;
      deliverAuth(event.data.user);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [deliverAuth]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const cfg = await api("/api/interview/telegram-login-config");
        if (cancelled) return;
        const base = (cfg.widgetBaseUrl || import.meta.env.VITE_TELEGRAM_LOGIN_ORIGIN || "").replace(/\/$/, "");
        setWidgetBase(base || null);
        const returnUrl = `${window.location.origin}${window.location.pathname}`;
        setLoginUrl(buildLoginPageUrl(base, returnUrl));
      } catch {
        const base = (import.meta.env.VITE_TELEGRAM_LOGIN_ORIGIN || "").replace(/\/$/, "");
        setWidgetBase(base || null);
        setLoginUrl(
          buildLoginPageUrl(base, `${window.location.origin}${window.location.pathname}`)
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const openLogin = () => {
    if (!loginUrl) return;
    const popup = window.open(loginUrl, "krab_telegram_login", "width=480,height=640");
    if (!popup) {
      window.location.href = loginUrl;
    }
  };

  if (disabled) return null;

  const sameHost =
    widgetBase && widgetBase.replace(/^https?:\/\//, "") === window.location.host;

  return (
    <div className="telegram-login-wrap">
      <p className="field-sublabel telegram-login-hint">
        Fastest: log in with Telegram — we fill in your username and ID automatically.
      </p>
      {loading ? (
        <p className="verify-msg">Loading Telegram login…</p>
      ) : loginUrl ? (
        <>
          <button type="button" className="btn btn-primary btn-sm telegram-login-btn" onClick={openLogin}>
            Log in with Telegram
          </button>
          {sameHost && (
            <p className="verify-msg">
              If the button shows “Bot domain invalid”, ask admin to run BotFather{" "}
              <code>/setdomain</code> for @{BOT_USERNAME} on this site’s domain.
            </p>
          )}
        </>
      ) : (
        <p className="verify-msg">
          Or type your @username below and open the bot link to connect.
        </p>
      )}
    </div>
  );
}

/** Call on interview page mount — handles redirect return + popup postMessage bridge. */
export function useTelegramAuthReturn(onAuth) {
  useEffect(() => {
    if (!onAuth) return;
    const user = parseTelegramAuthFromSearch();
    if (!user) return;

    if (window.opener && !window.opener.closed) {
      window.opener.postMessage({ type: "krab-telegram-auth", user }, window.location.origin);
      stripTelegramAuthFromUrl();
      window.close();
      return;
    }

    onAuth?.(user);
    stripTelegramAuthFromUrl();
  }, [onAuth]);
}
