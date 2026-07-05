/**
 * Parses pasted text into NJ Temporary Evidence of Insurance form fields.
 */

import { extractVinFromText } from '@/lib/insurance-paste-parser'

export type ParsedNjCardFields = Partial<{
  policyNumber: string
  effectiveMmDdYyyy: string
  expirationMmDdYyyy: string
  issuedMmDdYyyy: string
  vehicleYear: string
  vehicleMake: string
  vehicleModel: string
  vin: string
  insuredNameUpper: string
  insuredAddressLines: string
  carrierNaicCode: string
  carrierName: string
  carrierAddressLines: string
  carrierBrand: string
  servicePhone: string
  claimsPhone: string
  webDomain: string
  claimsAddress: string
  formRevision: string
}>

export const PARSE_NJ_CARD_JSON_KEYS = [
  'policyNumber',
  'effectiveMmDdYyyy',
  'expirationMmDdYyyy',
  'issuedMmDdYyyy',
  'vehicleYear',
  'vehicleMake',
  'vehicleModel',
  'vin',
  'insuredNameUpper',
  'insuredAddressLines',
  'carrierNaicCode',
  'carrierName',
  'carrierAddressLines',
  'carrierBrand',
  'servicePhone',
  'claimsPhone',
  'webDomain',
  'claimsAddress',
  'formRevision',
] as const

const MONTH_NAMES = [
  'january', 'february', 'march', 'april', 'may', 'june',
  'july', 'august', 'september', 'october', 'november', 'december',
]

function longDateToMmDdYyyy (line: string): string | undefined {
  const t = line.trim()
  const m = t.match(/^([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})$/)
  if (!m) return undefined
  const monthIdx = MONTH_NAMES.indexOf(m[1].toLowerCase())
  if (monthIdx < 0) return undefined
  const mm = String(monthIdx + 1).padStart(2, '0')
  const dd = m[2].padStart(2, '0')
  return `${mm}/${dd}/${m[3]}`
}

function isPolicyOnlyLine (line: string): boolean {
  return /^\d{6,14}$/.test(line.replace(/\s/g, ''))
}

function isCityStateZipLine (line: string): boolean {
  return /^.+\s+[A-Za-z]{2}\s+\d{5}(?:-\d{4})?\s*$/.test(line.trim())
}

function parseVehicleRow (line: string): Partial<ParsedNjCardFields> {
  const t = line.trim()
  const vin = extractVinFromText(t)
  if (!vin) return {}
  const beforeVin = t.slice(0, t.toUpperCase().indexOf(vin)).trim()
  const parts = beforeVin.split(/\s+/).filter(Boolean)
  if (parts.length >= 3) {
    return {
      vehicleYear: parts[0].replace(/\D/g, '').slice(-4),
      vehicleMake: parts[1].toUpperCase(),
      vehicleModel: parts.slice(2).join(' ').toUpperCase(),
      vin,
    }
  }
  if (parts.length === 2) {
    return {
      vehicleYear: parts[0].replace(/\D/g, '').slice(-4),
      vehicleMake: parts[1].toUpperCase(),
      vin,
    }
  }
  return { vin }
}

/** Document-style paste from NJ TEI cards or Progressive policy pages. */
export function parseNjDocumentStylePaste (raw: string): ParsedNjCardFields {
  const out: ParsedNjCardFields = {}
  const text = raw.replace(/\r\n/g, '\n').trim()
  if (!text) return out

  let lines = text.split('\n').map(l => l.trim()).filter(Boolean)
  if (lines.length < 2 && text.includes('\t')) {
    lines = text.split('\t').map(l => l.trim()).filter(Boolean)
  }

  for (const l of lines) {
    const vinRow = parseVehicleRow(l)
    if (vinRow.vin && !out.vin) {
      Object.assign(out, vinRow)
    }
  }

  if (!out.vin) {
    const vin = extractVinFromText(text)
    if (vin) out.vin = vin
  }

  for (let i = 0; i < lines.length; i++) {
  const l = lines[i]
    if (/^policy\s*number:?$/i.test(l) && lines[i + 1] && isPolicyOnlyLine(lines[i + 1])) {
      out.policyNumber = lines[i + 1].replace(/\D/g, '').slice(0, 14) || lines[i + 1]
    }
    if (/^effective\s*date:?$/i.test(l) && lines[i + 1]) {
      const mm = longDateToMmDdYyyy(lines[i + 1]) ?? lines[i + 1].match(/^(\d{1,2}\/\d{1,2}\/\d{4})$/)?.[1]
      if (mm) out.effectiveMmDdYyyy = mm
    }
  }

  const policyCandidates = lines
    .map((l, i) => ({ l, i }))
    .filter(({ l }) => isPolicyOnlyLine(l))
  if (policyCandidates.length > 0 && !out.policyNumber) {
    out.policyNumber = policyCandidates[0].l.replace(/\D/g, '').slice(0, 14)
  }

  const periodRe = /^([A-Za-z]+\s+\d{1,2},?\s+\d{4})\s*-\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})$/
  for (const l of lines) {
    const pm = l.match(periodRe)
    if (pm) {
      const eff = longDateToMmDdYyyy(pm[1])
      const exp = longDateToMmDdYyyy(pm[2])
      if (eff) out.effectiveMmDdYyyy = eff
      if (exp) out.expirationMmDdYyyy = exp
    }
  }

  const dateRe = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/
  for (const l of lines) {
    const m = l.trim().match(dateRe)
    if (!m) {
      const ld = longDateToMmDdYyyy(l)
      if (ld) {
        if (!out.effectiveMmDdYyyy) out.effectiveMmDdYyyy = ld
        else if (!out.expirationMmDdYyyy) out.expirationMmDdYyyy = ld
        else if (!out.issuedMmDdYyyy) out.issuedMmDdYyyy = ld
      }
      continue
    }
    const mmdd = `${m[1].padStart(2, '0')}/${m[2].padStart(2, '0')}/${m[3]}`
    if (!out.effectiveMmDdYyyy) out.effectiveMmDdYyyy = mmdd
    else if (!out.expirationMmDdYyyy) out.expirationMmDdYyyy = mmdd
    else if (!out.issuedMmDdYyyy) out.issuedMmDdYyyy = mmdd
  }

  const insuredIdx = lines.findIndex(l => /^insured$/i.test(l))
  if (insuredIdx >= 0) {
    const nameLine = lines[insuredIdx + 1]
    if (nameLine && !isCityStateZipLine(nameLine) && !isPolicyOnlyLine(nameLine)) {
      out.insuredNameUpper = nameLine.toUpperCase()
    }
    const addr: string[] = []
    for (let j = insuredIdx + 2; j < lines.length; j++) {
      if (/^year$/i.test(lines[j])) break
      if (isPolicyOnlyLine(lines[j])) break
      if (/^state of new jersey/i.test(lines[j])) break
      addr.push(lines[j])
      if (isCityStateZipLine(lines[j])) break
    }
    if (addr.length) out.insuredAddressLines = addr.map(a => a.toUpperCase()).join('\n')
  }

  for (const l of lines) {
    const naicM = l.match(/^(\d{2,5})\s+(.+)$/)
    if (naicM && /ins/i.test(naicM[2])) {
      out.carrierNaicCode = naicM[1]
      out.carrierName = naicM[2].trim()
    }
  }

  const phoneRe = /1-800-\d{3}-\d{4}/g
  const phones = text.match(phoneRe) ?? []
  if (phones[0] && !out.servicePhone) out.servicePhone = phones[0]
  if (phones[1] && !out.claimsPhone) out.claimsPhone = phones[1]

  const domainM = text.match(/\b([a-z0-9-]+\.(?:com|net|org))\b/i)
  if (domainM) out.webDomain = domainM[1].toLowerCase()

  const formM = text.match(/Form\s+6484T\s+NJ\s*\([^)]+\)/i)
  if (formM) out.formRevision = formM[0]

  const oneLineClaimsM = text.match(
    /National Specialty,\s*PO Box \d+[^\n]*Providence, RI \d{5}-\d+/i,
  )
  const nationalClaimsM = text.match(
    /National Specialty Insurance Company[\s\S]*?Providence, RI \d{5}-\d+/i,
  )
  if (oneLineClaimsM) {
    out.claimsAddress = oneLineClaimsM[0].replace(/\s+/g, ' ').trim()
  } else if (nationalClaimsM) {
    out.claimsAddress = nationalClaimsM[0]
      .split(/\n+/)
      .map(s => s.trim())
      .filter(Boolean)
      .join(' ')
  } else {
    const claimsM = text.match(/Progressive Claims[^\n]+/i)
    if (claimsM) out.claimsAddress = claimsM[0].trim()
  }

  if (/progressive/i.test(text) && !out.carrierBrand) out.carrierBrand = 'Progressive'

  return out
}

export function parseNjInsurancePasteHeuristic (raw: string): ParsedNjCardFields {
  return parseNjDocumentStylePaste(raw.replace(/\u00a0/g, ' ').trim())
}
