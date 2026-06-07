import { Link, useLocation } from "react-router-dom";
import { brandName } from "../api";
import ProgressTrail from "./ProgressTrail";

export default function Layout({ children }) {
  const { pathname } = useLocation();
  const name = brandName();
  const showTrail = pathname === "/" || pathname === "/apply";
  const trailStep = pathname === "/apply" ? "interview" : "work";

  return (
    <div className="site-shell">
      <header className="site-nav">
        <div className="container site-nav-inner">
          <Link to="/" className="brand">
            {name.split(" ").slice(0, -1).join(" ")}{" "}
            <span className="brand-accent">{name.split(" ").slice(-1)}</span>
          </Link>
          <nav className="nav-links">
            <Link to="/" className={`nav-link${pathname === "/" ? " active" : ""}`}>
              Home
            </Link>
            <Link to="/apply" className="btn btn-primary btn-sm">
              Interview now
            </Link>
          </nav>
        </div>
      </header>

      {showTrail && (
        <div className="progress-trail-bar">
          <div className="container">
            <ProgressTrail current={trailStep} />
          </div>
        </div>
      )}

      <main className="site-main">{children}</main>

      <footer className="site-footer">
        <div className="container">{name}</div>
      </footer>
    </div>
  );
}
