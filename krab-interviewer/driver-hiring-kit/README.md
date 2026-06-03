# Driver hiring kit (copy to any project)

**One folder — copy everything.** No iframes. Static HTML pages + API to `krab-interviewer-bot`.

## Quick start (human)

1. Copy this entire `driver-hiring-kit` folder into your project, e.g. `public/driver-hiring/`.
2. Open `static/config.js` and set `KRAB_API_BASE_URL` (see comments).
3. Proxy `/api` on your site to krab-interviewer-bot (see `proxy-examples/`).
4. Link from your site: `<a href="/driver-hiring/">Apply to drive</a>`.

**For AI / developers:** read **[AI-WIRE-UP.md](./AI-WIRE-UP.md)** — step-by-step checklist and placement table.

## Pages

| File | URL on your site (example) |
|------|----------------------------|
| `index.html` | `/driver-hiring/` |
| `requirements.html` | `/driver-hiring/requirements.html` |
| `interview.html` | `/driver-hiring/interview.html` |
| `how-to-telegram.html` | `/driver-hiring/how-to-telegram.html` |

## Contents

```
driver-hiring-kit/
  README.md              ← you are here
  AI-WIRE-UP.md          ← give this to AI when integrating
  index.html
  requirements.html
  interview.html
  how-to-telegram.html
  static/
    config.js            ← EDIT: API URL
    config.example.js
    main.js
    styles.css
    funnel.css
  proxy-examples/
    nginx.conf.snippet
    vercel.json.snippet
    next.config.snippet.js
```

## Sync from krab-interviewer

If you change `web/` in the repo, re-copy CSS/HTML or run from repo root:

```powershell
.\scripts\sync-driver-hiring-kit.ps1
```

(Script copies assets from `web/` into this kit.)
