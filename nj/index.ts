/**
 * NJ Temporary Evidence of Insurance — barrel.
 *
 * Everything state-specific for New Jersey lives in this folder:
 *   - `insurance-card.ts` — `pdf-lib` page builder + form-input type
 *   - `paste-parser.ts`   — heuristic + AI-friendly text → form-field extractor
 *
 * To replicate this template for another state, copy the folder (e.g.
 * `lib/nj` → `lib/pa`), rename the exported identifiers, rewrite the per-card
 * coordinates and labels, then duplicate `app/api/insurance-card-nj/` and
 * `app/insurance-card-nj/` with the same prefix. See the README in the app
 * root for the full step-by-step.
 */

export {
  buildNjInsuranceTeiPdf,
  formatLongDate,
  type NjInsuranceTeiInput,
} from './insurance-card'

export {
  PARSE_NJ_CARD_JSON_KEYS,
  parseNjDocumentStylePaste,
  parseNjInsurancePasteHeuristic,
  type ParsedNjCardFields,
} from './paste-parser'
