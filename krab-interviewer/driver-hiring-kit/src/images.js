/** Local images in /public/images — bundled with the site */

/** A file from public/, prefixed with wherever this build is mounted.
 *
 * Vite rewrites `/images/x.jpg` inside HTML and CSS for you, but NOT inside a
 * JavaScript string like the ones below. Served from the root that made no
 * difference; served from https://tristatetags.com/drivers/ it asked the tag
 * site for /images/x.jpg, whose SPA catch-all answered 200 with 9KB of HTML --
 * so every image and the tutorial video silently rendered broken.
 */
export function asset(path) {
  const base = import.meta.env.BASE_URL.replace(/\/+$/, "");
  return `${base}${path}`;
}

export const IMAGES = {
  hero: "https://images.unsplash.com/photo-1449965408869-eaa3f722e40d?w=1200&h=900&fit=crop&q=80",
  heroAccent: "https://images.unsplash.com/photo-1503376780353-7e6692767b70?w=800&h=600&fit=crop&q=80",
  driversLicense: asset("/images/id.jpg"),
  mailingPackage:
    "https://images.unsplash.com/photo-1566576912321-d58ddd7a6088?w=560&h=380&fit=crop&q=80",
  telegramPhone: asset("/images/telegram-username.jpg"),
  paymentApps:
    "https://images.unsplash.com/photo-1563013544-824ae1b704d3?w=560&h=380&fit=crop&q=80",
  laserPrinter: asset("/images/laserjet-printer.jpg"),
  car: "https://images.unsplash.com/photo-1492144534655-ae79c964c9d7?w=400&h=400&fit=crop&q=80",
  phone: "https://images.unsplash.com/photo-1512941937669-90a1b58e7e9c?w=400&h=400&fit=crop&q=80",
  /** Step 1 — Complete your quick driver interview */
  stepsApply: asset("/images/steps-interview.jpg"),
  /** Step 2 — Get the papers (USPS delivery) */
  stepsPackage: asset("/images/steps-get-papers.jpg"),
  /** Step 3 — Start your first delivery (earning) */
  stepsDrive: asset("/images/steps-first-delivery.jpg"),
};

export const MAILING_EXAMPLE = `Eddie Goldman
321 Main Street
Fort Lee New Jersey 07024`;

/** Alt text for requirement cards */
export const NEED_IMAGE_ALT = {
  driversLicense: "Driver's license — valid license to drive",
  laserPrinter: "Laserjet printer — print papers for clients",
  smartphone: "Smartphone — call clients and receive deliveries from dispatchers",
};

export const STEP_IMAGE_ALT = {
  interview: "Person completing a job application on a laptop",
  tracking: "USPS mail carrier with Priority Mail packages",
  delivery: "Complete your first delivery and collect $50",
};
