import {
  dispatchBotUsername,
  interviewerBotUsername,
  telegramConnectUrl,
  telegramDispatchConnectUrl,
} from "../telegram";

/** Links to open interviewer + dispatch bots and tap Start. */
export default function TelegramBotStartButtons({ connectUrl, className = "" }) {
  const interviewerUrl = connectUrl || telegramConnectUrl();
  return (
    <div className={`telegram-bot-links${className ? ` ${className}` : ""}`}>
      <p className="verify-row">
        <a
          className="btn btn-secondary telegram-connect-btn"
          href={interviewerUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open @{interviewerBotUsername()} → tap Start
        </a>
      </p>
      <p className="verify-row">
        <a
          className="btn btn-secondary telegram-connect-btn"
          href={telegramDispatchConnectUrl()}
          target="_blank"
          rel="noopener noreferrer"
        >
          Open @{dispatchBotUsername()} → tap Start
        </a>
      </p>
    </div>
  );
}
