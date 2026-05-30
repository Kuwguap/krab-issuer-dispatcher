import { PDFDocument, StandardFonts, rgb, type PDFFont, type PDFPage } from 'pdf-lib'

/**
 * NJ Temporary Evidence of Insurance (Form 6484T NJ) — pixel-exact port of the
 * reference Progressive policy PDF (Letter portrait, 612 × 792 pt).
 *
 * Coordinates were decoded directly from the source content stream (raw × 0.2 →
 * PDF pt; sizes Tf × 0.2 → pt). The source PDF splits several lines into
 * multiple Tj fragments at hand-tuned x positions to compensate for its own
 * embedded font widths. Standard Helvetica is wider than that source font, so
 * we merge those fragments into single strings here and draw them at the
 * leftmost x of the original fragment chain. The visual layout still matches
 * the source within sub-pt tolerance and avoids any character overlap.
 */

export interface NjInsuranceTeiInput {
  policyNumber: string
  /** MM/DD/YYYY — policy effective date (printed on each card). */
  effectiveMmDdYyyy: string
  /** MM/DD/YYYY — policy expiration (used in left-panel policy period). */
  expirationMmDdYyyy: string
  /** MM/DD/YYYY — document issued / printed date. Defaults to effective. */
  issuedMmDdYyyy?: string
  vehicleYear: string
  vehicleMake: string
  vehicleModel: string
  vin: string
  insuredNameUpper: string
  insuredAddressLines: string[]

  carrierNaicCode?: string
  carrierName?: string
  carrierAddressLines?: string[]

  carrierBrand?: string
  servicePhone?: string
  claimsPhone?: string
  webDomain?: string
  claimsAddress?: string
  formRevision?: string

  /** When false, skip the left info panel and only draw the three right-side cards. */
  includeInfoPanel?: boolean
}

const PAGE_W = 612
const PAGE_H = 792

/** Card-1 title baseline; cards 2/3 keep their absolute Y values (the source spacing isn't perfectly uniform). */
const CARD_TOP_1 = 753.4
const CARD_TOP_2 = 498.2
const CARD_TOP_3 = 239.6

interface TxItem {
  x: number
  y: number
  size: number
  bold?: boolean
  italic?: boolean
  /** Optional explicit maxWidth — pdf-lib word-wraps when set. */
  maxWidth?: number
  text: string | ((d: NjInsuranceTeiInput) => string)
}

/* ──────────────────────────────────────────────────────────────────────────
 *  Per-card text (positions are absolute for card 1; cards 2/3 reuse the
 *  same items with `dy = CARD_TOP_n - CARD_TOP_1`).
 * ────────────────────────────────────────────────────────────────────────── */

const CARD_TEXT_ITEMS: TxItem[] = [
  /* Title (one line, spans most of the card width above the carrier/policy blocks). */
  { x: 271.8, y: 753.4, size: 12, bold: true, text: 'State of New Jersey Temporary Evidence of Insurance' },

  /* Right-column policy block (label + value, sits below the title baseline).
   * Source places values at x=490.6 right against the bold label colon, but
   * pdf-lib's Helvetica-Bold is wider than the source's narrow bold so we
   * push the value out to 494 to keep a clean ~3pt gap after the colon. */
  { x: 437.6, y: 741.6, size: 7.5, bold: true, text: 'Policy number:' },
  { x: 494.0, y: 741.6, size: 7.5, text: d => d.policyNumber },
  { x: 437.6, y: 732.8, size: 7.5, bold: true, text: 'Effective date:' },
  { x: 494.0, y: 732.8, size: 7.5, text: d => formatLongDate(d.effectiveMmDdYyyy) },
  { x: 437.6, y: 722.8, size: 7, text: 'This Temporary Evidence of Insurance expires 20' },
  { x: 437.6, y: 715.8, size: 7, text: 'days after the effective date shown above.' },

  /* Carrier info (left side, below title). NAIC + name merged into one string.
   *
   * Up to three address-line slots are reserved (servicer, street/PO box,
   * city-state-zip). Empty strings are skipped by `drawTextItems`, so the
   * historical two-line layout is unchanged when the third slot is unset. */
  {
    x: 271.8, y: 742.0, size: 8.5,
    text: d =>
      `${(d.carrierNaicCode ?? '169').trim()} ${(d.carrierName ?? 'National Specialty Insurance Company').trim()}`,
  },
  { x: 287.6, y: 733.2, size: 8.5, text: d => d.carrierAddressLines?.[0] ?? 'Serviced by AIPSO-SAIP' },
  { x: 287.6, y: 724.6, size: 8.5, text: d => d.carrierAddressLines?.[1] ?? 'PO Box 6400' },
  { x: 287.6, y: 716.0, size: 8.5, text: d => d.carrierAddressLines?.[2] ?? 'Providence, RI 02940-6200' },

  /* Insured block (left side, middle of card). */
  { x: 271.8, y: 699.6, size: 8.5, bold: true, text: 'Insured' },
  { x: 271.8, y: 690.4, size: 8, text: d => d.insuredNameUpper },
  { x: 271.8, y: 681.6, size: 8, text: d => d.insuredAddressLines[0] ?? '' },
  { x: 271.8, y: 673.0, size: 8, text: d => d.insuredAddressLines[1] ?? '' },

  /* Year / Make / Model / VIN column headers. */
  { x: 288.0, y: 629.2, size: 8, text: 'Year' },
  { x: 313.2, y: 629.2, size: 8, text: 'Make' },
  { x: 405.6, y: 629.2, size: 8, text: 'Model' },
  { x: 501.6, y: 629.2, size: 8, text: 'VIN' },

  /* Vehicle data row (one item per column so values align under their headers). */
  { x: 288.4, y: 619.2, size: 8.5, text: d => d.vehicleYear },
  { x: 313.2, y: 619.2, size: 8.5, text: d => d.vehicleMake },
  { x: 405.4, y: 619.2, size: 8.5, text: d => d.vehicleModel },
  { x: 502.2, y: 619.2, size: 8.5, text: d => d.vin },

  /* Medical-treatment notification block (two stacked bold lines).
   * Source emits these at 8.5pt with an embedded narrow bold font; pdf-lib's
   * Helvetica-Bold is wider and would run past the page edge at 8.5pt, so we
   * step down to 7.5pt to preserve the visual proportions of the source. */
  { x: 288.4, y: 575.6, size: 7.5, bold: true, text: 'ADDRESS FOR NOTIFICATION OF COMMENCEMENT OF MEDICAL TREATMENT:' },
  {
    x: 288.4, y: 564.8, size: 7.5, bold: true,
    text: d => d.claimsAddress ?? 'Progressive Claims, P.O. Box 512926 Los Angeles, CA 90051-0926',
  },

  /* Form revision line (bottom-left of card). */
  { x: 271.8, y: 549.6, size: 7, text: d => d.formRevision ?? 'Form 6484T NJ (10/22)' },
]

/* ──────────────────────────────────────────────────────────────────────────
 *  Left info panel (drawn once, only when `includeInfoPanel` is true).
 *
 *  Each multi-fragment source line has been merged into a single string so
 *  it flows naturally in pdf-lib's standard Helvetica without overlapping.
 * ────────────────────────────────────────────────────────────────────────── */

const PANEL_TEXT_ITEMS: TxItem[] = [
  /* Policy number (label + value, both bold). */
  { x: 28.4, y: 720.2, size: 9.5, bold: true, text: 'Policy Number:' },
  { x: 102.0, y: 720.2, size: 9.5, bold: true, text: d => d.policyNumber },

  /* Issued / printed date (single date line, no label). */
  { x: 43.4, y: 709.4, size: 8.5, text: d => formatLongDate(d.issuedMmDdYyyy ?? d.effectiveMmDdYyyy) },

  /* Policy period — label + dates merged so the label can't run into the dates. */
  {
    x: 43.4, y: 698.2, size: 8.5,
    text: d =>
      `Policy Period:  ${formatLongDate(d.effectiveMmDdYyyy)} - ${formatLongDate(d.expirationMmDdYyyy)}`,
  },

  /* Big heading + sub-heading. */
  { x: 28.4, y: 645.0, size: 20, bold: true, text: 'Insurance ID Cards' },
  { x: 28.4, y: 623.2, size: 20, bold: true, italic: true, text: 'Keep these cards in' },
  { x: 28.4, y: 602.0, size: 20, bold: true, italic: true, text: 'your vehicle' },

  /* Access section. */
  { x: 31.4, y: 498.0, size: 10.5, bold: true, text: 'Access your policy' },

  /* Bulleted list (bullet glyph + text on the same line). */
  { x: 31.4, y: 485.8, size: 8.5, text: '\u2022' },
  { x: 53.0, y: 485.8, size: 8.5, text: 'Pay your bill' },
  { x: 31.4, y: 475.2, size: 8.5, text: '\u2022' },
  { x: 53.0, y: 475.2, size: 8.5, text: 'View and print your policy documents' },
  { x: 31.4, y: 464.8, size: 8.5, text: '\u2022' },
  { x: 53.0, y: 464.8, size: 8.5, text: 'Check the status of a claim' },
  { x: 31.4, y: 453.4, size: 8.5, text: '\u2022' },
  { x: 53.0, y: 453.4, size: 8.5, text: 'Get important information about your vehicle' },
  { x: 31.4, y: 442.6, size: 8.5, text: '\u2022' },
  { x: 53.0, y: 442.6, size: 8.5, text: 'For most policies find out how much it would cost to' },
  { x: 53.0, y: 431.6, size: 8.5, text: 'insure another vehicle, add a driver and more!' },
]

/* ──────────────────────────────────────────────────────────────────────────
 *  Helpers
 * ────────────────────────────────────────────────────────────────────────── */

function resolveText (text: TxItem['text'], data: NjInsuranceTeiInput): string {
  return typeof text === 'function' ? text(data) : text
}

/** `"MM/DD/YYYY"` → `"May 9, 2026"` (long-form date used throughout the source PDF). */
export function formatLongDate (mmDdYyyy: string): string {
  const m = mmDdYyyy.trim().match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/)
  if (!m) return mmDdYyyy.trim()
  const d = new Date(Number(m[3]), Number(m[1]) - 1, Number(m[2]))
  if (Number.isNaN(d.getTime())) return mmDdYyyy.trim()
  return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' })
}

function pickFont (item: TxItem, regular: PDFFont, bold: PDFFont, boldOblique: PDFFont): PDFFont {
  if (item.italic && item.bold) return boldOblique
  if (item.bold) return bold
  return regular
}

function drawTextItems (
  page: PDFPage,
  items: TxItem[],
  data: NjInsuranceTeiInput,
  dy: number,
  regular: PDFFont,
  bold: PDFFont,
  boldOblique: PDFFont,
) {
  for (const it of items) {
    const t = resolveText(it.text, data)
    if (!t) continue
    page.drawText(t, {
      x: it.x,
      y: it.y + dy,
      size: it.size,
      font: pickFont(it, regular, bold, boldOblique),
      color: rgb(0, 0, 0),
      maxWidth: it.maxWidth,
    })
  }
}

/* ──────────────────────────────────────────────────────────────────────────
 *  Builder
 * ────────────────────────────────────────────────────────────────────────── */

export async function buildNjInsuranceTeiPdf (
  input: NjInsuranceTeiInput,
): Promise<Uint8Array> {
  const doc = await PDFDocument.create()
  const page = doc.addPage([PAGE_W, PAGE_H])
  const regular = await doc.embedFont(StandardFonts.Helvetica)
  const bold = await doc.embedFont(StandardFonts.HelveticaBold)
  const boldOblique = await doc.embedFont(StandardFonts.HelveticaBoldOblique)

  const data: NjInsuranceTeiInput = {
    ...input,
    insuredAddressLines: (input.insuredAddressLines ?? []).map(l => l.trim()).filter(Boolean),
    carrierAddressLines: (input.carrierAddressLines ?? []).map(l => l.trim()).filter(Boolean),
    includeInfoPanel: input.includeInfoPanel !== false,
  }

  if (data.includeInfoPanel) {
    drawTextItems(page, PANEL_TEXT_ITEMS, data, 0, regular, bold, boldOblique)
  }

  for (const cardTop of [CARD_TOP_1, CARD_TOP_2, CARD_TOP_3]) {
    const dy = cardTop - CARD_TOP_1
    drawTextItems(page, CARD_TEXT_ITEMS, data, dy, regular, bold, boldOblique)
  }

  return doc.save()
}
