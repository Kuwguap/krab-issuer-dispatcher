# OG OFFCL — ogoffcl.store

Bold streetwear storefront for **Original Gangster Official** (Accra). Vite + React + Tailwind + Framer Motion, on the same Supabase database and Paystack account as before.

## Env vars (Vercel project `ogoffcl`)

Uses the **existing** variables unchanged — no database changes needed:

- `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, `VITE_SUPABASE_STORAGE_BUCKET`, `VITE_PAYSTACK_PUBLIC_KEY`

The stored anon key has a stray `\r\n` suffix from an old paste; the app sanitizes env values at runtime (`src/lib/supabase.ts`), so leave them as they are.

Optional new ones:

| Var | Purpose | Default |
|---|---|---|
| `VITE_SITE_LOCKED` | `1` = show the lock screen to everyone | unlocked |
| `VITE_SITE_PASSWORD` | Code that unlocks the site | `OG2026` |
| `VITE_ADMIN_PASSWORD` | Password for `/admin` | `OGADMIN26` |

To lock the site for a drop: set `VITE_SITE_LOCKED=1` on Vercel → redeploy. Unlock link you can share: `https://ogoffcl.store/?unlock=<password>`.

## Pages

- `/` home · `/shop` (`?c=og`, `?c=og-femme`) · `/product/:id` · `/checkout` (Paystack GHS) · `/order-confirmation`
- `/admin` — dashboard, products (image upload to `images` bucket), orders (status/payment), categories, discount codes, gallery

## Deploy

```bash
npm install
npm run build
vercel deploy --prod   # linked to kuwguaps-projects/ogoffcl → ogoffcl.store
```
