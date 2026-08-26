# Error reporting → Sentry → CI/CD → an agent that fixes issues

Your project is **`tristate`** in org **`tristate-70`**.

The code is done and verified against your real project — a scrubbed test event
has already arrived there. What is left needs accounts only you can log into.

**About 12 minutes**, in the order below. Steps 1–2 get errors flowing; step 3
is what makes them *fixable* by an agent; step 5 connects the agent — which is
Seer plus a coding agent, not Hermes. See step 5 for why.

---

## First, a correction to the onboarding page

Sentry showed you the **FastAPI** guide. This project is not FastAPI — it is
`python-telegram-bot` (the bot worker) plus **Flask** (the dashboard). Do not
paste that snippet: `from fastapi import FastAPI` will fail at import.

Three more things in that snippet are wrong for this codebase:

| Their snippet | Here | Why |
|---|---|---|
| `send_default_pii=True` | **`False`** | that flag alone would ship names, addresses and IPs to a third party |
| `traces_sample_rate=1.0` | `0` (env-tunable) | tracing every update is what burns the 5k/month free tier |
| `profile_lifecycle="trace"` | off | profiling a bot that idles on long-polling buys nothing |

`utils/observability.py` already does the correct init for both services. You do
not need to write any Sentry code.

---

## What can and cannot leave this bot

This is the part worth reading before you turn anything on. The bot handles legal
names, home addresses, VINs, licence numbers, insurance policies and portal
passwords. Sentry is a third party; Hermes would be a fourth.

Every event passes `_before_send` in `utils/observability.py`, which:

1. collects every value sitting under a sensitive key anywhere in the event,
2. redacts those exact values **everywhere**, including inside prose,
3. then runs pattern redaction for VIN / plate / phone / email / address / ZIP,
4. and **drops the event entirely** if the scrubber itself throws.

Order matters and was fixed during this install: a licence number came out as
`J1234[phone]` because the fuzzy phone rule ran first and ate the tail, leaving a
readable fragment. Exact values now go first.
`tests/test_observability.py` pins it (21 tests).

Here is a real scrubbed event, from your project:

```
"sentry install check — lead for [redacted] at [redacted], [redacted],
 VIN [redacted], plate [redacted], DL [redacted], policy [redacted],
 phone [redacted], email [redacted]"
```

The sentence shape survives, so the error is still debuggable. Every value is
gone.

**One known gap, documented rather than hidden:** a name typed into a log message
with no structured counterpart anywhere in the event cannot be recognised — there
is no regex for "is a person". Log `reference_id` or the lead id instead. Both
are useful in an issue and neither identifies anybody.

Nothing reports anywhere until `SENTRY_DSN` is set, so your machine and CI stay
silent by default.

---

## Step 1 — Give Render the DSN (3 min)

Your DSN:

```
https://fd22b5a677acd03d718eb65845c0d0ae@o4511974519603200.ingest.us.sentry.io/4511974611288064
```

A DSN permits **event submission only** — it cannot read your issues — but it can
be used to spam your quota, so keep it in the environment rather than the repo.

For **both** `krab-issuer-admin` and `krab-issuer-bot`:

- Render dashboard → the service → **Environment**
- `SENTRY_DSN` = the value above

That is the only one to type. `render.yaml` already declares both variables —
`SENTRY_DSN` as `sync: false` (so Render prompts for it rather than keeping a
credential in the repo) and `SENTRY_ENVIRONMENT` already set to `production`.

Then **Manual Deploy → Deploy latest commit** on both.

To turn reporting off later, clear `SENTRY_DSN`. Nothing else changes.

## Step 2 — Confirm it works (1 min)

Locally, or from a Render shell:

```bash
cd krableadsV2 && SENTRY_DSN="https://fd22b5a677acd03d718eb65845c0d0ae@o4511974519603200.ingest.us.sentry.io/4511974611288064" python verify_sentry.py
```

`verify_sentry.py` raises an error stuffed with **invented** customer data in the
exact shapes this business handles, prints what would leave the process, refuses
to transmit if any invented value survived, and only then sends.

Expect `No invented customer value survived scrubbing.` followed by a test event
in Sentry tagged `component=verify`. Delete the issue afterwards.

Never point it at a real lead — the whole purpose is to watch redaction work, and
a real customer in a test event is the thing redaction exists to prevent.

**To confirm RENDER picked the variable up** (a different question from "does the
DSN work"), once this commit is deployed:

```bash
curl -s https://krab-issuer-admin.onrender.com/api/health
```

`"sentry": true` means the running process has a DSN. The DSN itself is never
echoed — it permits event submission, and that endpoint is public.

Before this commit is deployed, use the Render log instead: `init_sentry` writes
`sentry: reporting as bot, release <sha>` on startup. No such line means the
variable did not reach the process.

## Step 3 — Let CI create releases (5 min)

**This is the step that makes issues fixable rather than merely visible.** A
stack trace saying `bot.py line 9412` is useless on a file that changes several
times a day. A release ties the error to the exact commit — which is what lets
Sentry name a suspect commit, and what lets any agent check out the code that
actually failed.

1. Sentry → **Settings → Auth Tokens → Create New Token**
   Scopes: `project:releases` and `org:read`. Copy it.
2. GitHub → repo → **Settings → Secrets and variables → Actions** →
   **New repository secret**:

   | Name | Value |
   |---|---|
   | `SENTRY_AUTH_TOKEN` | the token from step 1 |

   One secret, not three — org `tristate-70` and project `tristate` are public
   identifiers and are set directly in the workflow.

3. Nothing else. `.github/workflows/sentry-release.yml` runs after every green CI
   build on `main`, creates `krableads@<commit>` and attaches its commits.

The release string is `krableads@<sha>` in **both** the workflow and
`utils/observability.py`. If you ever change one, change both, or events land
under a release carrying no source.

## Step 4 — Connect the repo to Sentry (2 min)

Sentry → **Settings → Integrations → GitHub** → Install → pick
`Kuwguap/krab-issuer-dispatcher`.

This turns a release into a **suspect commit**. It is also a hard prerequisite
for both agent options in step 5 — neither can open a PR without it.

## Step 5 — The agent that fixes issues

**There is no Hermes integration for Sentry.** Sentry can hand an issue to
exactly three coding agents — **Claude**, **Cursor** and **GitHub Copilot**.
"Hermes" in Sentry's own documentation is the React Native JavaScript engine,
which is unrelated. If you saw an agent described as fixing Sentry issues, it was
almost certainly **Seer**, which is Sentry's own and is what the *Ask Seer*
button on your onboarding page invokes.

The working shape is two pieces: **Seer** does root-cause analysis, then hands
off to a **coding agent** that writes the branch.

### 5a — Turn on Seer

1. Step 4 must be done first: Seer requires a connected GitHub.com or
   GitLab.com repo (cloud only — self-hosted is not supported).
2. Sentry → **Settings → Seer** → enable **Autofix**, and **Code Review** if you
   want PR review as well.

**Pricing caveat:** Seer bills on *active contributors* — anyone creating 2+ PRs
a month in a Seer-enabled repo is a billable seat. Check the current rate before
enabling it org-wide.

### 5b — Connect Claude as the coding agent

The natural fit here, since this repo is already worked on in Claude Code.

**Prerequisites:** Sentry **Owner, Manager or Admin**; a Claude workspace; GitHub
access configured in that workspace.

1. **platform.claude.com/dashboard** → create an API key for your workspace.
2. Sentry → **Settings → Integrations** → search **Claude Agent** → paste the
   key. Set workspace and environment only if you use non-default ones.
3. In your Claude workspace environment settings, either add
   `api.githubcopilot.com` to allowed network hosts, or enable **Allow MCP
   server network access**. Skipping this makes sessions fail.

**Using it on one issue:** open the issue → **Start Root Cause Analysis** → when
it finishes, the dropdown offers **Send to Claude Agent** → a card tracks the
session and links the generated branch when it is done.

**To make it automatic:** Seer settings → this project → set **Claude Agent** in
the *Coding Agent* section. It picks the first available of Claude Opus 5,
Opus 4.8, or Sonnet 5.

Start with the manual per-issue flow. Automatic hand-off on a repo that
dispatches legal documents and charges $100 payments is worth earning first.

### If you do have a separate Hermes agent

If "Hermes" is an agent you run yourself, it can still read Sentry — but the
direction is reversed, and that difference matters. The integrations above are
Sentry **pushing** an issue out to an agent. A self-hosted agent instead **pulls**
from Sentry, so nothing reaches it unless it asks.

The route is Composio's MCP tool router: `dashboard.composio.dev` for a Connect
MCP URL and API key, then in `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  composio:
    url: "https://connect.composio.dev/mcp"
    headers:
      x-consumer-api-key: "YOUR_COMPOSIO_API_KEY"
    connect_timeout: 60
    timeout: 180
```

Give it `event:read` and `project:read` and nothing more — reading issues needs
no write access to Sentry. For PRs it needs GitHub `contents:write` and
`pull_requests:write`, scoped to this repo alone.

There is also a `curl … | bash` installer for a Composio CLI. Read it before
running it; piping a remote script into a shell is not something to do on trust.

### What any of them will receive

| Field | Why it matters |
|---|---|
| `release` = `krableads@<sha>` | checks out the exact code that failed |
| `culprit` + stack frames | file, line, function — never redacted |
| `tags.component` | `bot` or `dashboard` — which process |
| `extra.lead_id`, `reference_id` | correlates with your own logs |
| everything else | `[redacted]` |

**Tell it that it cannot reproduce from the report.** The values are gone by
design, so a fix has to come from reading the code path, not from the data. If it
opens PRs that assume it has the input, this is why.

**Review every PR it opens.** This repo dispatches legal documents and charges
$100 instant-tag payments. An agent reading a scrubbed report has less context
than you do.

## What CI does now

There was no CI at all before this. Two workflows:

**`ci.yml`** — every push and pull request:
- **Python 3.11**, the version Render runs (local is 3.13, and an f-string
  backslash has bitten this repo on exactly that difference)
- `compileall` first — a syntax error fails in seconds, not minutes
- the full suite, with dummy credentials and `SENTRY_DSN` explicitly empty so
  nothing from CI reaches your live project
- the privacy tests again as their own step, so a failure there reads as a
  privacy regression rather than a flaky test

**`sentry-release.yml`** — only after CI passes on `main`. A release pointing at
a commit that never deployed is worse than no release at all.

**Known issue before you rely on a green build:**
`tests/test_bulk_send.py::test_the_bundle_that_was_reported` fails
intermittently. It is test-harness pollution, not a product bug — two test
modules each bind `bot.db` at module scope, so one reads state the other wrote.
It reproduces with either ordering of
`pytest tests/test_real_routing_e2e.py tests/test_bulk_send.py`. Until it is
fixed, CI will be red some of the time for a reason that has nothing to do with
the change being tested.

**Render still deploys on push, as it always has.** CI does not gate it — that
needs Render auto-deploy turned off and a deploy hook called from the workflow.
Say the word and I will wire it; it is the difference between "tests told us
afterwards" and "a red build cannot reach production".

---

## Costs

Sentry's free tier is 5k errors/month. This bot is chatty when something breaks —
a Telegram handler retries. Two controls, both already in place:

- `SENTRY_SAMPLE_RATE` (default `1.0`) — drop a fraction of events
- `SENTRY_TRACES_SAMPLE_RATE` (default `0`) — tracing is **off**; turning it on
  is what usually blows a quota

Start at the defaults and lower the sample rate only if you hit the cap.
