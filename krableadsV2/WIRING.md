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

## What is deliberately not automatic

**The insurance account.** `/api/integrations/clients` lives on the deployed
tristatecoverage.com and is not in the local project (which has only `purchase`,
`stripe` and `vin` routes). Both bots now treat "this email already has an account"
as success rather than failure, so repeat customers work — but if you want the
server side changed, that repository is needed.

**OpenAI.** Photo and PDF parsing, VIN reading, plate reading and the AI field
classifier all need credits on the key. Everything else — labelled edits, the
pickers, dispatch, tags — is deterministic and works without them.
