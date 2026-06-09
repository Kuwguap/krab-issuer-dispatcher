import { telegramConnectUrl } from "../telegram";
import TelegramBotStartButtons from "./TelegramBotStartButtons";

export default function FormField({
  field,
  value,
  onChange,
  telegramStatus,
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
      {field.setupGuide?.length > 0 && (
        <div className="field-setup-guide">
          {field.setupGuide.map((step, i) => (
            <div key={i} className="field-setup-step">
              {step.title && <p className="field-setup-step-title">{step.title}</p>}
              {step.text && <p className="field-setup-step-text">{step.text}</p>}
              {step.lines?.length > 0 && (
                <ul className="field-setup-lines">
                  {step.lines.map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
              {step.appLinks?.length > 0 && (
                <div className="field-app-links">
                  {step.appLinks.map((link) => (
                    <p key={link.href}>
                      {link.label}{" "}
                      <a href={link.href} target="_blank" rel="noopener noreferrer">
                        {link.href}
                      </a>
                    </p>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {!field.setupGuide?.length && field.appLinks?.length > 0 && (
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
        <div className="telegram-bot-links">
          <TelegramBotStartButtons connectUrl={telegramStatus?.connectUrl || telegramConnectUrl()} />
          <p className="field-sublabel telegram-bots-note">
            We use AI 🤖 to automate our Dealership system operation — please start these 2 bots to
            receive daily client delivery alerts 🔔
          </p>
          {telegramStatus?.polling && (
            <p className="verify-msg">Waiting for link… checking every few seconds.</p>
          )}
        </div>
      )}

      {field.autoResolveTelegram && telegramStatus?.text && (
        <p className={`verify-msg ${telegramStatus.type || ""}`}>{telegramStatus.text}</p>
      )}
    </div>
  );
}
