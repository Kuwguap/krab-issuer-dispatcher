import { Link, useNavigate } from "react-router-dom";
import { runDraftFlush } from "../draftFlush";

/**
 * @param {"work" | "interview"} current - active step
 */
export default function ProgressTrail({ current }) {
  const navigate = useNavigate();
  const workClass =
    current === "work" ? "progress-pill current" : "progress-pill done progress-pill-link";
  const interviewClass =
    current === "interview" ? "progress-pill current" : "progress-pill progress-pill-link";

  const handleLeaveInterview = async (e) => {
    e.preventDefault();
    await runDraftFlush();
    navigate("/");
  };

  return (
    <div className="progress-trail">
      {current === "work" ? (
        <span className={workClass}>1 · Work</span>
      ) : (
        <Link to="/" className={workClass} onClick={handleLeaveInterview}>
          1 · Work
        </Link>
      )}
      {current === "interview" ? (
        <span className={interviewClass}>2 · Interview</span>
      ) : (
        <Link to="/apply" className={interviewClass}>
          2 · Interview
        </Link>
      )}
    </div>
  );
}
