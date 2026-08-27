# How the pieces connect

Four things talk to each other. This is what each one needs to know about the
others, and what breaks when it does not.

```
  Telegram ──► krableadsV2 bot ──────────► Supabase ◄────── admin dashboard
                    │  (bot.py)                              (admin_dashboard.py)
                    │                                         = tristatetags.com/backend
                    ├──► tristatecoverage.com                    │
                    │    /api/integrations/clients               ├── /receipts     the board
                    │    (portal account + insurance card)       ├── /r/<token>    receipt upload
                    │                                            ├── /receipt/<id> the image
  krabinsurancebot ─┘                                            └── Stripe webhook
```

Everything shares **one Supabase project**. That is the integration: there is no
message bus, and no service calls another to ask what happened. State lives in
columns, and each side reads them.

---

## Run these migrations first

In the Supabase SQL editor, in any order. Until each is run the feature it backs
falls back to its old behaviour rather than failing loudly, so it is easy not to
notice they are missing.

| File | Without it |
|---|---|
| `database/migration_receipt_files.sql` | receipt uploads have nowhere to land — the portal 500s |
| `database/migration_lead_delivery_status.sql` | the `/receipts` board renders but no status can be set |
| `database/migration_instant_pdf.sql` | a paid instant tag is never delivered |
| `database/migration_driver_manual_suspend.sql` | manual suspend/lift does nothing |
| `database/migration_extra_vehicles.sql` | a lead's 2nd/3rd car is silently dropped and only ONE tag is issued |
| `database/migration_insurance_email_gate.sql` | the "📧 Email insurance to client" button cannot claim its send — every tap says to run this file |
| `database/migration_telegram_name.sql` | the leaderboard and Skip Dispatch posts fall back to @usernames — new leads cannot store the sender's name |

---

## Values both sides must agree on

**`RECEIPT_PORTAL_BASE`** — the bot builds upload links with it, the dashboard
serves them. Different values on the two sides means every link 404s.

**`RECEIPT_LINK_SECRET`** — signs those links. Defaults to `SUPABASE_KEY`, which
both already share, so it usually needs no setting. If you set it on one, set it on
both.

**`INTEGRATIONS_API_KEY`** — the bot authenticating to tristatecoverage.com, and
the dashboard authenticating the bot. Same key in three places: the bot, the
dashboard, and the TriStateCoverage deployment.

**`SUPERVISORY_TELEGRAM_ID`** — who may open `/settings`, `/drivers`,
`/leaderboard`, and `/entries` on the insurance bot. Extra supervisors can be added
from inside `/settings`; the ones in this variable are fixed and cannot be removed
there, so the last way in can never be locked.

---

## Stripe

One account, shared with tristatecoverage.com. Copy `STRIPE_SECRET_KEY` from there.

Then add a webhook endpoint in the Stripe dashboard:

```
https://tristatetags.com/backend/api/stripe/webhook
```

subscribed to `checkout.session.completed` and
`checkout.session.async_payment_succeeded`, and put its signing secret in
`STRIPE_WEBHOOK_SECRET`.

**Without that secret every webhook is refused**, which means a customer can pay
and never receive a tag. The signature check is not optional — without it anyone
who knows the URL could mark any lead paid.

Nothing hangs, by design. The webhook only ever writes `instant_pdf_paid_at`; the
bot sweeps every 20 seconds for paid-and-undelivered and stamps
`instant_pdf_delivered_at` **only once the document is really in the driver's
chat**. A crash between the two delays a tag; it cannot lose one, and it cannot
take money without eventually delivering.

---

## Understanding what people type

The bot reads an instruction out of ordinary speech: "I'd like to select all
drivers", "driver: Susan", "colour black please". Three switches control how far
that goes. All are read at call time, so changing one needs a restart but not a
deploy.

| Variable | Default | What it does |
|---|---|---|
| `KRAB_FLUENCY` | on | The whole layer. Set to `0` and every command falls back to the exact phrasings that worked before it existed. This is the switch to reach for if the bot ever starts mis-reading messages. |
| `KRAB_FLUENCY_SUBMIT` | **off** | Lets one sentence both comment and dispatch — "looks good send it out". Submitting is irreversible and that sentence is indistinguishable from a note ending the same way, so it is off until you want it. |
| `KRAB_FLUENCY_BARENAME` | **off** | Lets a bare name pick a driver — "give it to Susan". The only evidence is that Susan is on the roster, which is also how a client named Will Smith could stop being a client. Off until you want it. |

The layer can only ever ADD understanding: a phrase the bot already understood
keeps its existing meaning, and a test asserts that the fluent pass can never
overrule the strict one.

---

## Chat layer

In front of every parser above sits the model. **`KRAB_CHAT_LAYER`** (default
on) has it read each message first — on the review card and at idle — and turn
what it understands into card edits, selections, or a submit. Everything in the
previous section is the fallback: no key, no credit, a timeout, or a message the
model declines to claim all fall through to the deterministic ladder unchanged,
so this layer too can only add understanding. The variable is read at call time:
set `0` and restart to kill it, no deploy.

Two rules keep the model on a leash:

**Submitting asks first.** With the model reading everything, prose like "ok
looks good, send it out" can classify as submit. Only the strict submit words
send immediately; anything softer gets "Reply **yes** to confirm" and 90 seconds
to answer. A temp tag is a legal document — the confirmation is one word or one
tap.

**Extraction never overwrites.** A paste or dictation naming several fields
fills only the fields still empty on the card, the same rule the bulk-leftover
placer lives by. An explicit single-field correction may overwrite.

A circuit breaker keeps a dead OpenAI account from taxing every message with a
network timeout before its fallback: out of quota opens it for 300 seconds,
three transport failures in a row for 60. RAM-only on purpose — a restart is
already a fresh start, and a breaker that survives one is a breaker nobody
remembers to reset.

The test suite runs with the layer OFF — `tests/conftest.py` sets
`KRAB_CHAT_LAYER=0`, or a local `.env` would leak a real key into every e2e run.
The layer's own suite, `tests/test_chat_layer.py`, flips it back on with a
mocked classifier.

---

## What is deliberately not automatic

**The insurance account.** `/api/integrations/clients` lives on the deployed
tristatecoverage.com and is not in the local project (which has only `purchase`,
`stripe` and `vin` routes). Both bots now treat "this email already has an account"
as success rather than failure, so repeat customers work — but if you want the
server side changed, that repository is needed.

**OpenAI.** Photo and PDF parsing, VIN reading, plate reading, the AI field
classifier and the chat layer all need credits on the key. Everything else —
labelled edits, the pickers, dispatch, tags — is deterministic and works
without them.
