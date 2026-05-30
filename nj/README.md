# `lib/nj` — New Jersey Temporary Evidence of Insurance

Every NJ-specific piece of the PDF renderer + paste parser lives in this
folder. Everything outside this folder (API routes, page, client component) is
thin scaffolding that delegates to the exports here.

## Layout

| File                                       | Role                                                                             |
| ------------------------------------------ | -------------------------------------------------------------------------------- |
| [`insurance-card.ts`](./insurance-card.ts) | `pdf-lib` page builder + `NjInsuranceTeiInput` type + `formatLongDate()` helper. |
| [`paste-parser.ts`](./paste-parser.ts)     | Heuristic text → form-field extractor for pasted NJ TEI / Progressive policy text. |
| [`index.ts`](./index.ts)                   | Barrel — import everything from `@/lib/nj`.                                      |

## Replicating to another state

1. Copy this folder (e.g. `lib/nj` → `lib/pa`).
2. Inside the copy, rename every `Nj` / `NJ` / `nj` identifier to the new
   state's prefix (types, function names, constants, default strings).
3. Rewrite `CARD_TEXT_ITEMS` + `PANEL_TEXT_ITEMS` in `insurance-card.ts` with
   coordinates / labels decoded from the new state's reference PDF
   (raw × 0.2 → PDF pt, Tf × 0.2 → pt). Update `CARD_TOP_1/2/3` and the title.
4. Duplicate the matching scaffolding folders:
   - `app/api/insurance-card-nj/` → `app/api/insurance-card-pa/` (swap the
     `@/lib/nj` imports to `@/lib/pa`, rename types and the attachment
     filename prefix).
   - `app/insurance-card-nj/` → `app/insurance-card-pa/` (rename
     `InsuranceCardNjClient` → `InsuranceCardPaClient`, swap `/api/insurance-card-nj` → `/api/insurance-card-pa`, and update `<StateTabs activeId="…" />`).
5. Extend [`components/StateTabs.tsx`](../../components/StateTabs.tsx) with the
   new tab entry.

Once those two folders + the tab entry exist, the new state's page is
auto-routed at `/insurance-card-<code>` and the API at `/api/insurance-card-<code>`.

## Width-check tip

`pdf-lib`'s standard `Helvetica` / `Helvetica-Bold` are wider than the embedded
narrow bold fonts most carrier PDFs use, so source-style multi-fragment
positions on a single `Td` line will overlap. When you decode a new source
PDF, **merge contiguous fragments into single strings** before drawing, and
run a quick width audit with `font.widthOfTextAtSize(text, size)` to make sure
no two same-Y elements horizontally overlap.
