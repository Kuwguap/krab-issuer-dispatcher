# OG OFFCL — ogoffcl.store

Bold streetwear storefront for **Original Gangster Official** (Accra). Vite + React + Tailwind + Framer Motion + **Vercel serverless functions** (`api/`), on the same Supabase database. Payments: **Moolre mobile money**. Email: **Resend**.

## Env vars (Vercel project `ogoffcl`)

Existing (leave untouched — runtime sanitizer handles the stray `\r\n` on the anon key):
`VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_SUPABASE_STORAGE_BUCKET`

**Payments — Moolre** (server-side only, required for checkout):

| Var | What it is |
|---|---|
| `MOOLRE_API_USER` | Your Moolre username |
| `MOOLRE_API_KEY` | Private API key (initiates charges + refunds) |
| `MOOLRE_PUBKEY` | Public API key (status checks) |
| `MOOLRE_ACCOUNT_NUMBER` | Your Moolre account number |
| `MOOLRE_SANDBOX` | `1` to hit sandbox.moolre.com (optional) |

➡️ **Callback URL to enter in the Moolre dashboard:** `https://ogoffcl.store/api/pay/callback`
(The callback is verified server-side against Moolre's status API before an order is marked paid.)

**Email — Resend** (required for all emails):

| Var | What it is |
|---|---|
| `RESEND_API_KEY` | From resend.com → API Keys |
| `RESEND_FROM` | e.g. `OG OFFCL <orders@ogoffcl.store>` (verify the domain in Resend first) |
| `STORE_NOTIFY_EMAIL` | Optional — you get an email on every paid order |

**Site controls:**

| Var | Purpose | Default |
|---|---|---|
| `VITE_SITE_LOCKED` | `1` force-locks (waitlist) regardless of the admin toggle | unlocked |
| `VITE_SITE_PASSWORD` | Staff code for the lock screen | `OG2026` |
| `VITE_ADMIN_PASSWORD` | `/admin` password (also guards the admin APIs via `ADMIN_API_PASSWORD` fallback) | `OGADMIN26` |

## One-time SQL

Run in the Supabase SQL editor (safe to re-run):
1. `supabase/migration_waitlist.sql` — lock toggle + subscribers
2. `supabase/migration_moolre_email.sql` — payment/refund/tracking fields + email templates/campaigns

## Emails sent automatically

- Waitlist/newsletter signup → welcome email
- Order created → "we got your order"
- Payment confirmed → order confirmation (+ optional store notification)
- Status changes from admin → tracking updates
- Refund issued → refund confirmation

## Pages

- `/` home · `/shop` (`?c=og`, `?c=og-femme`) · `/product/:id` · `/checkout` (Paystack GHS) · `/order-confirmation`
- `/admin` — dashboard, products (image upload to `images` bucket), orders (status/payment), categories, discount codes, gallery

## Deploy

```bash
npm install
npm run build
vercel deploy --prod   # linked to kuwguaps-projects/ogoffcl → ogoffcl.store
```
