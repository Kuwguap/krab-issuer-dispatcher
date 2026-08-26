# Error reporting → Sentry → Hermes

Everything in code is done. What follows is the part only you can do, because it
needs accounts you own.

Total: about 15 minutes.

---

## Before anything else: what leaves this bot

This bot handles legal names, home addresses, VINs, driver licence numbers,
insurance policies, one-time phone links and portal passwords. Sentry is a third
party and Hermes is a fourth, so **none of that may leave the process**.

`utils/observability.py` scrubs every event three ways:

| Layer | What it stops |
|---|---|
| SDK settings | no PII, no local variables, no request bodies |
| Key redaction | any value under `name`, `vin`, `phone`, `policy`, `password`, … |
| Pattern redaction | a VIN, plate, phone, email, address or ZIP **anywhere**, including inside an exception message |
| Cross-reference | a value found under a sensitive key is then redacted from all free text in the same event |

If the scrubber ever throws, the event is **dropped** rather than sent.

`tests/test_observability.py` proves it against a real lead and fails the build
if anything leaks. One gap is documented rather than hidden: a name typed
straight into a log message with no structured counterpart cannot be recognised.
A test guards that too — it fails when new code formats a lead field into a log
line. Log the `reference_id` or the lead id instead; both are useful in an issue
and neither identifies anybody.

**Nothing reports anywhere until `SENTRY_DSN` is set.** Your machine and CI stay
silent by default.

---

## Step 1 — Create the Sentry project (3 min)

1. <https://sentry.io> → **Projects** → **Create Project**
2. Platform **Python**, alert on **every new issue**, name it `krableads`
3. Copy the **DSN** it shows you — `https://…@o…ingest.sentry.io/…`

## Step 2 — Give Render the DSN (2 min)

`render.yaml` already declares the variables; Render will prompt for the value
because the DSN is a credential and does not belong in the repo.

For **both** `krab-issuer-admin` and `krab-issuer-bot`:

- Render dashboard → the service → **Environment**
- `SENTRY_DSN` = the DSN from step 1
- `SENTRY_ENVIRONMENT` = `production` (already set in `render.yaml`)

Then **Manual Deploy → Deploy latest commit** on both.

To turn reporting off later, clear `SENTRY_DSN`. No deploy needed beyond the
restart.

## Step 3 — Let GitHub create releases (5 min)

This is the step that makes issues **fixable** rather than merely visible. A
stack trace saying `bot.py line 9412` is useless on a file that changes several
times a day; a release ties the error to the exact commit, so Sentry — and
Hermes — can see the real source.

1. Sentry → **Settings → Auth Tokens → Create New Token**
   Scopes: `project:releases` and `org:read`. Copy it.
2. GitHub → the repo → **Settings → Secrets and variables → Actions** → three
   **New repository secrets**:

   | Name | Value |
   |---|---|
   | `SENTRY_AUTH_TOKEN` | the token from step 1 |
   | `SENTRY_ORG` | your org slug, from the Sentry URL |
   | `SENTRY_PROJECT` | `krableads` |

3. Nothing else. `.github/workflows/sentry-release.yml` runs on every green CI
   build on `main`, creates `krableads@<commit>` and attaches the commits in it.

## Step 4 — Connect the repo (2 min)

Sentry → **Settings → Integrations → GitHub** → Install → pick
`Kuwguap/krab-issuer-dispatcher`.

This is what turns a release into a **suspect commit** — Sentry names the change
that introduced a regression and who wrote it. Hermes needs this to open a PR
against the right lines.

## Step 5 — Point Hermes at it (3 min)

Hermes needs two things from Sentry, in this order:

1. **Read access to issues.** Either an auth token scoped `event:read` +
   `project:read`, or a webhook. For a webhook:
   Sentry → **Settings → Developer Settings → Custom Integration**
   → webhook URL = your Hermes endpoint, events = **issue** and **error**.
2. **Write access to the repo**, so it can open PRs — a GitHub App or a PAT with
   `contents:write` and `pull_requests:write` on this repo only.

What Hermes will receive for each issue, and why each part matters:

| Field | Why it matters |
|---|---|
| `release` = `krableads@<sha>` | checks out the exact code that failed |
| `culprit` + stack frames | the file, line and function — never redacted |
| `tags.component` | `bot` or `dashboard`, so it knows which process |
| `extra.lead_id`, `reference_id` | correlates with your own logs |
| everything else | `[redacted]` |

**Tell Hermes it cannot reproduce from the report.** The values are gone by
design, so a fix has to come from reading the code path, not from the data. If
Hermes opens PRs that assume it has the input, that is the reason.

---

## Step 6 — Prove it works

```bash
cd krableadsV2 && ./venv/Scripts/python.exe -c "
import os; os.environ['SENTRY_DSN']='PASTE_YOUR_DSN'
from utils.observability import init_sentry
init_sentry('smoke-test')
raise RuntimeError('sentry smoke test — ignore')
"
```

An issue appears within a minute, tagged `component: smoke-test`. Delete it after.

Then check the scrubber against your own data:

```bash
cd krableadsV2 && ./venv/Scripts/python.exe -m pytest tests/test_observability.py -q
```

---

## What CI does now

There was no CI at all before this. Two workflows:

**`ci.yml`** — every push and pull request:
- installs on **Python 3.11**, the version Render runs (local is 3.13, and an
  f-string backslash has bitten this repo on exactly that difference)
- `compileall` first — a syntax error fails in seconds, not minutes
- the full suite, with dummy credentials and `SENTRY_DSN` explicitly empty so
  nothing from CI can reach the live project
- the privacy tests again as their own step, so a failure there reads as what it
  is rather than as a flaky test

**`sentry-release.yml`** — only after CI passes on `main`. A release pointing at
a commit that never deployed is worse than no release at all.

**Render still deploys on push, as it always has.** CI does not gate it — that
would need Render's auto-deploy turned off and a deploy hook called from the
workflow. Say the word and I will wire that; it is the difference between
"tests told us afterwards" and "a red build cannot reach production".

---

## Costs

Sentry's free tier is 5k errors/month. This bot is chatty when something breaks —
a Telegram handler retries. Two controls, both already in place:

- `SENTRY_SAMPLE_RATE` (default `1.0`) — drop a fraction of events
- `SENTRY_TRACES_SAMPLE_RATE` (default `0`) — performance tracing is **off**;
  turning it on is what usually blows a quota

Start at the defaults and lower the sample rate only if you actually hit the cap.
