import { telegramConnectUrl } from "../telegram";

export default function FormField({
  field,
  value,
  onChange,
  telegramStatus,
  telegramBotUsername = "krabinterviewerbot",
  number,
  nested = false,
  inputKey,
}) {
  const InputTag = field.textarea ? "textarea" : "input";
  const controlId = inputKey || field.key;
  const autoComplete =
    field.key === "telegram_username" ? "off" : field.type === "email" ? "email" : undefined;

  return (
    <div
      className={`form-card form-field-single${field.highlight ? " form-card-highlight" : ""}${nested ? " form-field-nested" : ""}`}
      id={field.key}
    >
      <label className="field-label" htmlFor={field.choices ? undefined : controlId}>
        {number != null && <span className="field-num">{number}.</span>}
        {field.label}
        {field.required && <span className="field-req"> *</span>}
      </label>
      {field.sublabel && <p className="field-sublabel">{field.sublabel}</p>}
      {field.appLinks?.length > 0 && (
        <div className="field-app-links">
          {field.appLinks.map((link) => (
            <p key={link.href}>
              {link.label}{" "}
              <a href={link.href} target="_blank" rel="noopener noreferrer">
                {link.href}
              </a>
            </p>
          ))}
        </div>
      )}

      {field.choices ? (
        <>
          <div className="field-choices" role="group" aria-label={field.label}>
            {field.choices.map((choice) => (
              <button
                key={choice}
                type="button"
                className={`field-choice-btn${value === choice ? " selected" : ""}`}
                aria-pressed={value === choice}
                onClick={() => onChange(field.key, choice)}
              >
                {choice}
              </button>
            ))}
          </div>
          <input type="hidden" name={field.key} value={value} required={field.required} />
        </>
      ) : (
        <InputTag
          key={controlId}
          id={controlId}
          name={controlId}
          className={field.textarea ? "field-textarea" : "field-input"}
          type={field.textarea ? undefined : field.type || "text"}
          placeholder={field.placeholder}
          value={value}
          onChange={(e) => onChange(field.key, e.target.value)}
          required={field.required}
          autoComplete={autoComplete}
        />
      )}

      {field.autoResolveTelegram && (
        <p className="verify-row">
          <a
            className="btn btn-secondary telegram-connect-btn"
            href={telegramStatus?.connectUrl || telegramConnectUrl()}
            target="_blank"
            rel="noopener noreferrer"
          >
            Open @{telegramBotUsername.replace(/^@+/, "")} → tap Start
          </a>
          {telegramStatus?.polling && (
            <span className="verify-msg">Waiting for link… checking every few seconds.</span>
          )}
        </p>
      )}

      {field.autoResolveTelegram && telegramStatus?.text && (
        <p className={`verify-msg ${telegramStatus.type || ""}`}>{telegramStatus.text}</p>
      )}
    </div>
  );
}
