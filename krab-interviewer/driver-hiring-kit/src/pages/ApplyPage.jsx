import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { FIELDS, FORM_FIELD_KEYS } from "../fields";
import FormField from "../components/FormField";
import AiFillPanel from "../components/AiFillPanel";
import { registerDraftFlush, unregisterDraftFlush } from "../draftFlush";
import { displayTelegramUsername, normalizeTelegramUsername, telegramConnectUrl, userFacingTelegramMessage } from "../telegram";
import { stripTelegramAuthFromUrl } from "../telegramAuth";
import TelegramLoginButton, { useTelegramAuthReturn } from "../components/TelegramLoginButton";

function emptyPayload() {
  const p = { telegram_id: "" };
  FIELDS.forEach((f) => {
    p[f.key] = "";
  });
  return p;
}

function normalizePayload(data) {
  const next = emptyPayload();
  if (!data) return next;
  for (const key of FORM_FIELD_KEYS) {
    const v = data[key];
    if (v != null && v !== undefined) next[key] = String(v);
  }
  return next;
}

const COMPANION_KEYS = new Set(
  FIELDS.map((f) => f.companionKey).filter(Boolean)
);

function fieldDisplayNumber(index) {
  let n = 1;
  for (let i = 0; i < index; i += 1) {
    if (!FIELDS[i].nested && !COMPANION_KEYS.has(FIELDS[i].key)) n += 1;
  }
  return n + 1;
}

export default function ApplyPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const freshStart = searchParams.get("fresh") === "1";
  const [draftId, setDraftId] = useState(null);
  const [payload, setPayload] = useState(emptyPayload);
  const [frozen, setFrozen] = useState(false);
  const [loadingDraft, setLoadingDraft] = useState(true);
  const [licenseUrl, setLicenseUrl] = useState("");
  const [licenseParseNote, setLicenseParseNote] = useState("");
  const [telegramStatus, setTelegramStatus] = useState(null);
  const [submitting, setSubmitting] = useState(false);
  const [draftError, setDraftError] = useState(null);
  const saveTimer = useRef(null);
  const payloadRef = useRef(payload);
  const draftIdRef = useRef(null);
  const saveGenerationRef = useRef(0);
  const telegramTimer = useRef(null);
  const telegramPoll = useRef(null);
  const telegramReq = useRef(0);
  const [formSessionKey, setFormSessionKey] = useState(0);

  const fillPayload = useCallback((data) => {
    if (!data) return;
    setPayload((prev) => ({ ...prev, ...data }));
  }, []);

  const applyPayloadFromServer = useCallback((data) => {
    setPayload(normalizePayload(data));
  }, []);

  const resetLocalForm = useCallback(() => {
    clearTimeout(saveTimer.current);
    clearInterval(telegramPoll.current);
    telegramPoll.current = null;
    telegramReq.current += 1;
    saveGenerationRef.current += 1;
    const blank = emptyPayload();
    payloadRef.current = blank;
    setPayload(blank);
    setLicenseUrl("");
    setLicenseParseNote("");
    setTelegramStatus(null);
    setFormSessionKey((k) => k + 1);
    stripTelegramAuthFromUrl();
  }, []);

  useEffect(() => {
    payloadRef.current = payload;
  }, [payload]);

  useEffect(() => {
    draftIdRef.current = draftId;
  }, [draftId]);

  const flushSave = useCallback(async () => {
    clearTimeout(saveTimer.current);
    if (frozen || !draftIdRef.current) return;
    const gen = saveGenerationRef.current;
    const nextPayload = payloadRef.current;
    try {
      const res = await api(`/api/interview/draft/${draftIdRef.current}`, {
        method: "PATCH",
        body: JSON.stringify({ payload: nextPayload }),
      });
      if (gen !== saveGenerationRef.current) return;
      if (res.payload) applyPayloadFromServer(res.payload);
    } catch (e) {
      console.error("flushSave failed", e);
    }
  }, [frozen, applyPayloadFromServer]);

  const queueSave = useCallback(
    (nextPayload) => {
      if (frozen || !draftIdRef.current) return;
      const gen = saveGenerationRef.current;
      clearTimeout(saveTimer.current);
      saveTimer.current = setTimeout(async () => {
        if (gen !== saveGenerationRef.current || !draftIdRef.current || frozen) return;
        try {
          const res = await api(`/api/interview/draft/${draftIdRef.current}`, {
            method: "PATCH",
            body: JSON.stringify({ payload: nextPayload }),
          });
          if (gen !== saveGenerationRef.current) return;
          if (res.payload) applyPayloadFromServer(res.payload);
          const tr = res.telegramResolve;
          if (tr?.telegramId) {
            clearInterval(telegramPoll.current);
            telegramPoll.current = null;
            setTelegramStatus({
              type: "ok",
              text: `✓ Telegram ID ${tr.telegramId}`,
            });
          } else if (tr?.needsBotStart && normalizeTelegramUsername(nextPayload.telegram_username)) {
            setTelegramStatus({
              type: "bad",
              text: userFacingTelegramMessage(tr.message),
              connectUrl: tr.connectUrl || null,
              polling: true,
            });
          }
        } catch (e) {
          console.error("autosave failed", e);
        }
      }, 800);
    },
    [frozen, applyPayloadFromServer]
  );

  const lookupTelegram = useCallback(
    async (rawUsername, { quiet = false } = {}) => {
      const un = normalizeTelegramUsername(rawUsername);
      if (!un) {
        setTelegramStatus(null);
        clearInterval(telegramPoll.current);
        telegramPoll.current = null;
        return;
      }
      const reqId = ++telegramReq.current;
      if (!quiet) {
        setTelegramStatus({ type: "", text: "Looking up Telegram ID…" });
      }
      try {
        const res = await api("/api/interview/resolve-telegram", {
          method: "POST",
          body: JSON.stringify({ username: un }),
        });
        if (reqId !== telegramReq.current) return;

        if (res.telegramId) {
          clearInterval(telegramPoll.current);
          telegramPoll.current = null;
          const display = res.telegramUsername || displayTelegramUsername(un);
          setPayload((prev) => {
            const next = {
              ...prev,
              telegram_username: display,
              telegram_id: String(res.telegramId),
            };
            queueSave(next);
            return next;
          });
          setTelegramStatus({
            type: "ok",
            text: `✓ Telegram ID ${res.telegramId} (${display})`,
          });
        } else {
          setTelegramStatus({
            type: "bad",
            text: userFacingTelegramMessage(res.message),
            connectUrl: res.connectUrl || null,
            polling: Boolean(res.needsBotStart),
          });
        }
      } catch (err) {
        if (reqId !== telegramReq.current) return;
        const hint =
          err.status === 404
            ? "API offline — redeploy Vercel + confirm Render /api/health works."
            : err.message || "Lookup failed.";
        setTelegramStatus({ type: "bad", text: hint });
      }
    },
    [queueSave]
  );

  const onTelegramWidgetAuth = useCallback(
    async (user) => {
      if (frozen || !draftId) return;
      setTelegramStatus({ type: "", text: "Linking Telegram…" });
      try {
        const res = await api("/api/interview/verify-telegram-auth", {
          method: "POST",
          body: JSON.stringify(user),
        });
        clearInterval(telegramPoll.current);
        telegramPoll.current = null;
        const display =
          res.telegramUsername ||
          displayTelegramUsername(user.username) ||
          displayTelegramUsername(payload.telegram_username);
        setPayload((prev) => {
          const next = {
            ...prev,
            telegram_username: display || prev.telegram_username,
            telegram_id: String(res.telegramId),
          };
          queueSave(next);
          return next;
        });
        setTelegramStatus({
          type: "ok",
          text: `✓ Telegram ID ${res.telegramId}${display ? ` (${display})` : ""}`,
        });
      } catch (err) {
        setTelegramStatus({
          type: "bad",
          text: err.message || "Telegram login failed. Try the bot link below.",
          connectUrl: telegramConnectUrl(),
        });
      }
    },
    [draftId, frozen, payload.telegram_username, queueSave]
  );

  useTelegramAuthReturn(draftId && !frozen ? onTelegramWidgetAuth : null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (freshStart) {
        resetLocalForm();
      }
      setLoadingDraft(true);
      try {
        const qs = freshStart ? "?fresh=1" : "";
        const data = await api(`/api/interview/draft${qs}`, { method: "POST" });
        if (cancelled) return;
        setDraftError(null);
        if (freshStart) {
          navigate("/apply", { replace: true });
        }
        setDraftId(data.draftId);
        applyPayloadFromServer(data.payload || {});
        setLicenseUrl(data.driversLicenseFileUrl || "");
        const un = String(data.payload?.telegram_username || "").trim();
        if (un) lookupTelegram(un);
      } catch (e) {
        if (!cancelled) {
          console.error(e);
          setDraftError(
            e.message ||
              "Could not connect to the application server. Try again in a moment."
          );
        }
      } finally {
        if (!cancelled) setLoadingDraft(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [applyPayloadFromServer, freshStart, lookupTelegram, navigate, resetLocalForm]);

  useEffect(() => {
    const raw = payload.telegram_username || "";
    clearTimeout(telegramTimer.current);
    const un = normalizeTelegramUsername(raw);
    if (!un) {
      setTelegramStatus(null);
      clearInterval(telegramPoll.current);
      telegramPoll.current = null;
      return;
    }
    telegramTimer.current = setTimeout(() => lookupTelegram(raw), 600);
    return () => clearTimeout(telegramTimer.current);
  }, [payload.telegram_username, lookupTelegram]);

  useEffect(() => {
    if (!telegramStatus?.polling) return;
    const raw = payload.telegram_username || "";
    const un = normalizeTelegramUsername(raw);
    if (!un) return;

    clearInterval(telegramPoll.current);
    telegramPoll.current = setInterval(() => lookupTelegram(raw, { quiet: true }), 2500);
    const stop = setTimeout(() => {
      clearInterval(telegramPoll.current);
      telegramPoll.current = null;
    }, 300000);

    return () => {
      clearInterval(telegramPoll.current);
      telegramPoll.current = null;
      clearTimeout(stop);
    };
  }, [telegramStatus?.polling, payload.telegram_username, lookupTelegram]);

  useEffect(() => {
    registerDraftFlush(flushSave);
    return () => unregisterDraftFlush();
  }, [flushSave]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, []);

  const onFieldChange = (key, value) => {
    setPayload((prev) => {
      const next = { ...prev, [key]: value };
      if (key === "telegram_username" && !String(value || "").trim()) {
        next.telegram_id = "";
      }
      queueSave(next);
      return next;
    });
  };

  const onAiFilled = (merged) => {
    const picked = {};
    for (const key of FORM_FIELD_KEYS) {
      if (merged?.[key] != null && String(merged[key]).trim()) picked[key] = merged[key];
    }
    if (!Object.keys(picked).length) return;
    setPayload((prev) => {
      const next = { ...prev, ...picked };
      queueSave(next);
      return next;
    });
    if (picked.telegram_username) lookupTelegram(picked.telegram_username);
  };

  const onLicenseChange = async (e) => {
    const file = e.target.files?.[0];
    if (!file || !draftId) return;
    setLicenseParseNote("");
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api(`/api/interview/draft/${draftId}/license`, {
        method: "POST",
        body: fd,
      });
      if (res.driversLicenseFileUrl) setLicenseUrl(res.driversLicenseFileUrl);
      if (res.payload) {
        const picked = {};
        for (const key of FORM_FIELD_KEYS) {
          if (res.payload[key] != null && String(res.payload[key]).trim()) {
            picked[key] = res.payload[key];
          }
        }
        if (Object.keys(picked).length) {
          fillPayload(picked);
          queueSave({ ...payload, ...picked });
        }
        if (picked.telegram_username) lookupTelegram(picked.telegram_username);
      }
      if (res.parsed) {
        setLicenseParseNote("✓ License scanned — fields auto-filled below.");
      }
    } catch (err) {
      alert(err.message || "Upload failed");
    }
  };

  useEffect(() => {
    return () => {
      clearInterval(telegramPoll.current);
    };
  }, []);

  const onSubmit = async (e) => {
    e.preventDefault();
    if (frozen || !draftId || submitting) return;
    setSubmitting(true);
    try {
      let submitPayload = { ...payload };
      const un = normalizeTelegramUsername(payload.telegram_username);
      if (un && !String(payload.telegram_id || "").trim()) {
        const res = await api("/api/interview/resolve-telegram", {
          method: "POST",
          body: JSON.stringify({ username: un }),
        });
        if (res.telegramId) {
          submitPayload = {
            ...submitPayload,
            telegram_username: res.telegramUsername || displayTelegramUsername(un),
            telegram_id: String(res.telegramId),
          };
          setPayload(submitPayload);
        }
      }
      await api(`/api/interview/draft/${draftId}`, {
        method: "PATCH",
        body: JSON.stringify({ payload: submitPayload }),
      });
      await api(`/api/interview/submit/${draftId}`, { method: "POST" });
      setFrozen(true);
    } catch (err) {
      const msg =
        typeof err.data?.detail === "string"
          ? err.data.detail
          : err.message || "Submit failed";
      alert(msg);
    } finally {
      setSubmitting(false);
    }
  };

  const hasDraftData = FORM_FIELD_KEYS.some((key) => String(payload[key] || "").trim());

  const onStartOver = async () => {
    resetLocalForm();
    setDraftError(null);
    setLoadingDraft(true);
    setDraftId(null);
    draftIdRef.current = null;
    try {
      const data = await api("/api/interview/draft?fresh=1", { method: "POST" });
      const blank = emptyPayload();
      const gen = saveGenerationRef.current;
      setDraftId(data.draftId);
      draftIdRef.current = data.draftId;
      const wipe = await api(`/api/interview/draft/${data.draftId}`, {
        method: "PATCH",
        body: JSON.stringify({ payload: blank }),
      });
      if (gen !== saveGenerationRef.current) return;
      applyPayloadFromServer(wipe.payload || blank);
      setLicenseUrl("");
    } catch (e) {
      setDraftError(e.message || "Could not reset the form. Try again.");
    } finally {
      setLoadingDraft(false);
    }
  };

  if (loadingDraft && !frozen) {
    return (
      <div className="container-narrow interview-form-page">
        <div className="form-card success-card">
          <p>Loading your application…</p>
        </div>
      </div>
    );
  }

  if (frozen) {
    return (
      <div className="container-narrow interview-form-page">
        <div className="form-card success-card">
          <h2>🎉 You're hired!</h2>
          <p>
            Your interview is complete and you're officially on the team. Check Telegram for a
            welcome message from @krabinterviewerbot — then start receiving leads at @Krabdispatchbot.
          </p>
          <div className="success-actions">
            <Link to="/" className="btn btn-secondary">
              Return home
            </Link>
            <Link to="/apply?fresh=1" className="btn btn-primary">
              Submit another application
            </Link>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="container-narrow interview-form-page">
      <header className="form-header form-header-centered">
        <p className="form-step-label">Step 2 — Interview</p>
        <h1>Quick driver interview</h1>
        <p className="learn-step-eyebrow">Ai Data Parse</p>
        <p className="form-intro">
          Copy/Paste Text or upload a photo/screenshot of your interview answers — we’ll fill the
          boxes for you.
        </p>
        {hasDraftData && (
          <p className="form-intro">
            <button type="button" className="btn btn-secondary btn-sm" onClick={onStartOver}>
              Start over with a blank form
            </button>
          </p>
        )}
      </header>

      {draftError && (
        <div className="form-card verify-msg bad" role="alert">
          <p>{draftError}</p>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      )}

      {!draftId && !draftError && (
        <div className="form-card">
          <p className="verify-msg">Starting your application session…</p>
        </div>
      )}

      <form key={formSessionKey} onSubmit={onSubmit} className="interview-form">
        <AiFillPanel draftId={draftId} onFilled={onAiFilled} disabled={!draftId} />

        <div className="form-card form-field-single">
          <label className="field-label" htmlFor={`license-file-${formSessionKey}`}>
            <span className="field-num">1.</span>
            Driver’s License <span className="field-req">*</span>
          </label>
          <p className="field-sublabel">
            A valid driver’s license is required to drive. Please Upload a clear photo/picture below.
          </p>
          <input
            key={`license-file-${formSessionKey}`}
            id={`license-file-${formSessionKey}`}
            type="file"
            accept="image/*"
            capture="environment"
            className="field-input"
            onChange={onLicenseChange}
          />
          {licenseUrl && (
            <a className="license-link" href={licenseUrl} target="_blank" rel="noopener noreferrer">
              ✓ View uploaded license
            </a>
          )}
          {licenseParseNote && <p className="verify-msg ok">{licenseParseNote}</p>}
        </div>

        {FIELDS.map((field, index) => {
          if (COMPANION_KEYS.has(field.key)) return null;
          const companion = field.companionKey
            ? FIELDS.find((f) => f.key === field.companionKey)
            : null;
          return (
            <div key={field.key}>
              {field.autoResolveTelegram && (
                <div className="form-card telegram-login-card">
                  <TelegramLoginButton onAuth={onTelegramWidgetAuth} disabled={!draftId} />
                </div>
              )}
              <FormField
                field={field}
                number={fieldDisplayNumber(index)}
                value={payload[field.key] || ""}
                onChange={onFieldChange}
                telegramStatus={field.autoResolveTelegram ? telegramStatus : null}
                inputKey={`${formSessionKey}-${field.key}`}
              />
              {companion && (
                <FormField
                  field={companion}
                  nested
                  value={payload[companion.key] || ""}
                  onChange={onFieldChange}
                  inputKey={`${formSessionKey}-${companion.key}`}
                />
              )}
            </div>
          );
        })}

        <div className="form-card preflight-card">
          <h3>Before you Finish:</h3>
          <ol className="preflight-list">
            <li>Install <strong>Telegram</strong> on your phone</li>
            <li>Turn on/allow push notifications</li>
            <li>
              Set your <strong>@username</strong> in Telegram → Settings → Edit → @Username
            </li>
          </ol>
        </div>

        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={submitting || !draftId}>
          {submitting ? "Submitting…" : "Complete interview →"}
        </button>
      </form>
    </div>
  );
}
