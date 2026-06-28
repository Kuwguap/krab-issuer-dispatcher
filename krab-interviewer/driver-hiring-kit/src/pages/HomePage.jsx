import { Link } from "react-router-dom";
import { IMAGES, NEED_IMAGE_ALT, STEP_IMAGE_ALT } from "../images";

const STEPS = [
  {
    num: 1,
    title: "Interview",
    text: "Complete your quick driver interview.",
    image: IMAGES.stepsApply,
    alt: STEP_IMAGE_ALT.interview,
  },
  {
    num: 2,
    title: "Get the papers, get the papers",
    text: "Receive your paper with tracking.",
    image: IMAGES.stepsPackage,
    alt: STEP_IMAGE_ALT.tracking,
  },
  {
    num: 3,
    title: "Start Your 1st Delivery",
    text: "Complete your first delivery and collect $50.",
    image: IMAGES.stepsDrive,
    alt: STEP_IMAGE_ALT.delivery,
  },
];

const NEEDS = [
  {
    title: "Driver’s License",
    text: "Valid license to drive",
    image: IMAGES.driversLicense,
    alt: NEED_IMAGE_ALT.driversLicense,
  },
  {
    title: "Vehicle",
    text: "Your own car to drive",
    image: IMAGES.car,
    alt: "Your own vehicle for deliveries",
  },
  {
    title: "Laserjet Printer",
    text: "To print papers for clients",
    image: IMAGES.laserPrinter,
    alt: NEED_IMAGE_ALT.laserPrinter,
  },
  {
    title: "Smartphone",
    text: "To call clients and receive deliveries from our dispatchers",
    image: IMAGES.phone,
    alt: NEED_IMAGE_ALT.smartphone,
  },
];

export default function HomePage() {
  return (
    <>
      <div className="container-narrow step-page-header">
        <header className="form-header form-header-centered">
          <p className="form-step-label">Step 1 — Work</p>
          <h1>Easy Driver Work</h1>
        </header>
      </div>

      <section className="hero-section hero-section-top">
        <div className="hero-blob hero-blob-1" aria-hidden="true" />
        <div className="hero-blob hero-blob-2" aria-hidden="true" />
        <div className="container hero-grid">
          <div className="hero-copy">
            <p className="hero-eyebrow">Start today · Same-day · Daily cash pay</p>
            <p className="hero-lead">
              No long paperwork.
              <br />
              No long waiting periods.
              <br />
              Join our delivery team and start working fast.
              <br />
              Start getting money with deliveries right away.
            </p>
            <ul className="hero-bullets">
              <li>Get hired today</li>
              <li>Get paid daily</li>
              <li>Receive $50 / per delivery</li>
              <li>Serve hundreds of clients</li>
            </ul>
            <div className="hero-cta">
              <Link to="/apply" className="btn btn-primary btn-lg">
                Interview now and start earning!
              </Link>
            </div>
          </div>
          <div className="hero-visual-wrap">
            <img
              className="hero-photo"
              src={IMAGES.hero}
              alt="Driver ready to start work today"
              width={760}
              height={570}
            />
            <div className="hero-float-card">
              Same-day start
              <span>✓ No complicated process</span>
            </div>
          </div>
        </div>
      </section>

      <section className="section section-soft">
        <div className="container">
          <div className="section-header">
            <p className="learn-step-eyebrow">3 easy steps</p>
            <h2 className="section-title">How it works</h2>
          </div>
          <div className="steps-grid">
            {STEPS.map((s) => (
              <article className="step-card" key={s.num}>
                <img className="step-card-img" src={s.image} alt={s.alt || s.title} loading="eager" decoding="async" />
                <div className="step-card-body">
                  <span className="step-num">{s.num}</span>
                  <h3>{s.title}</h3>
                  <p>{s.text}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="section">
        <div className="container">
          <div className="section-header">
            <h2 className="section-title">What You Need To Start working Today!</h2>
          </div>
          <div className="need-grid">
            {NEEDS.map((n) => (
              <article className="need-card" key={n.title}>
                <img src={n.image} alt={n.alt || n.title} loading="eager" decoding="async" />
                <div className="need-card-body">
                  <h3>{n.title}</h3>
                  <p>{n.text}</p>
                </div>
              </article>
            ))}
          </div>

          <div className="cta-band">
            <h2>Ready? Start today.</h2>
            <Link to="/apply" className="btn btn-primary btn-lg">
              Go to Interview
            </Link>
          </div>
        </div>
      </section>
    </>
  );
}
