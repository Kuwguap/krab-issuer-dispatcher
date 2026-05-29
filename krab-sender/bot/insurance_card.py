"""NY FS-20 insurance ID card PDF generator with AAMVA Annex D PDF417 barcodes.

Port of the TypeScript blueprint (`buildNyInsuranceIdCardPdf` + `pdf-lib` +
`bwip-js`) into Python using ``reportlab`` (PDF) and ``pdf417gen`` (PDF417).

Public surface:
  * ``build_aamva_ny_insurance_pdf417_payload(...)`` — AAMVA byte stream encoder.
  * ``build_ny_insurance_id_card_pdf(...)`` — returns PDF bytes (US Letter portrait).
  * ``normalize_vin``, ``decode_vin_from_nhtsa`` — VIN helpers (reuses
    ``utils.vin_lookup``).
  * ``normalize_aamva_daq`` — customer/document id formatter (blank → '000000000').
  * ``generate_policy_number`` — ``ATP1234567-00`` style id.

The code is intentionally faithful to the blueprint constants so scanners that
read AAMVA Annex D streams (NY IIN 636001) accept the output without changes.
"""
from __future__ import annotations

import hashlib
import io
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# ─── AAMVA Annex D byte constants ────────────────────────────────────────────
AAMVA_DES = "\x0A"   # LF — data element separator
AAMVA_RS = "\x1E"    # RS — record separator (header byte 3)
AAMVA_ST = "\x0D"    # CR — segment terminator
AAMVA_IIN_NY = "636001"

# 1800 chars is the practical capacity for PDF417 at ec level 4 with 10 columns
_PDF417_PAYLOAD_LIMIT = 1800


# ─── Helpers (port of TS utils in lib/pdf/aamva-pdf417-insurance.ts) ─────────
_DAQ_ALNUM_RE = re.compile(r"[^A-Z0-9]")


def normalize_aamva_daq(value: Optional[str]) -> str:
    """Customer / document ID (AAMVA DAQ).

    Rules:
      * alphanumeric, uppercase
      * max 25 chars
      * empty / placeholder → ``'000000000'``

    NOTE: DAQ is **not** the policy number — policy goes under ``ZNA`` (and
    ``DCF``).  This mirrors the TS implementation exactly.
    """
    raw = (value or "").strip().upper()
    cleaned = _DAQ_ALNUM_RE.sub("", raw)
    if not cleaned:
        return "000000000"
    return cleaned[:25]


_VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def normalize_vin(vin: Optional[str]) -> str:
    """Return 17-char VIN (uppercase, no I/O/Q) or '' if invalid."""
    if not vin:
        return ""
    v = re.sub(r"\s+", "", str(vin)).upper()
    return v if _VIN_RE.match(v) else ""


# Bounded match so we don't grab 17 chars from a longer alphanumeric blob.
_VIN_BOUNDED_RE = re.compile(r"(?<![A-HJ-NPR-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-HJ-NPR-Z0-9])")


def extract_vin_from_text(blob: Optional[str]) -> str:
    """Find the first valid VIN anywhere in free-form lead text.

    ``vehicle_details`` lines can be missing or shifted vs the canonical 11-line
    Phase 1 layout; scanning the full blob avoids treating a color line as VIN.
    """
    if not blob:
        return ""
    text = str(blob).upper()
    for m in _VIN_BOUNDED_RE.finditer(text):
        v = normalize_vin(m.group(0))
        if v:
            return v
    # Fallback: remove whitespace only so spaced-out VINs still match.
    compact = re.sub(r"\s+", "", text)
    for i in range(0, max(0, len(compact) - 16)):
        chunk = compact[i : i + 17]
        v = normalize_vin(chunk)
        if v:
            return v
    return ""


_YEAR_MAKE_LINE_RE = re.compile(r"^\s*((?:19|20)\d{2})\s+\S")


def infer_car_and_color_from_vehicle_lines(
    raw_lines: Iterable[str],
    *,
    vin_clean: str,
) -> tuple[str, str]:
    """Resolve car / color lines when canonical indices may be misaligned.

    Strategy:
      * If slot index 5 normalizes to ``vin_clean``, keep legacy indices 6–7.
      * Else locate the line that equals the VIN (or normalizes to it), read
        the following lines as car then color.
      * Else scan after address lines for the first ``YYYY Make …`` pattern.
    """
    lines = [str(l).strip() for l in raw_lines if str(l).strip()]
    if not vin_clean:
        return "", ""

    def ln(i: int) -> str:
        return lines[i] if i < len(lines) else ""

    if normalize_vin(ln(5)) == vin_clean:
        c_col = ln(7)
        return ln(6), c_col if c_col not in ("-", "") else ""

    vin_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if normalize_vin(line) == vin_clean:
            vin_idx = i
            break
        compact = re.sub(r"\s+", "", line.upper())
        if compact == vin_clean:
            vin_idx = i
            break

    car_raw = ""
    color = ""

    if vin_idx is not None:
        j = vin_idx + 1
        if j < len(lines):
            car_raw = lines[j]
            if car_raw in ("-", ""):
                car_raw = ""
        k = vin_idx + 2
        if k < len(lines):
            color = lines[k]
            if color in ("-", "") or (_YEAR_MAKE_LINE_RE.match(color) and not normalize_vin(color)):
                color = ""

    if not car_raw:
        for i in range(3, len(lines)):
            if _YEAR_MAKE_LINE_RE.match(lines[i]):
                car_raw = lines[i]
                if i + 1 < len(lines):
                    cand = lines[i + 1]
                    if (
                        cand not in ("-", "")
                        and not normalize_vin(cand)
                        and len(cand) <= 40
                    ):
                        color = cand
                break

    return car_raw, color


def mmddyyyy_to_aamva(mmddyyyy: str) -> str:
    """Convert ``'MM/DD/YYYY'`` to AAMVA ``'MMDDCCYY'`` (8 digits)."""
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\s*$", str(mmddyyyy or ""))
    if not m:
        return "00000000"
    mm = m.group(1).zfill(2)
    dd = m.group(2).zfill(2)
    yyyy = m.group(3)
    return f"{mm}{dd}{yyyy}"


def mmddyyyy_to_yyyymmdd(mmddyyyy: str) -> str:
    """Convert ``'MM/DD/YYYY'`` to ``'YYYYMMDD'`` used by AAMVA insurance dates (FAB/FAC/FAD)."""
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\s*$", str(mmddyyyy or ""))
    if not m:
        return "00000000"
    mm = m.group(1).zfill(2)
    dd = m.group(2).zfill(2)
    yyyy = m.group(3)
    return f"{yyyy}{mm}{dd}"


_CARRIER_CODE_RE = re.compile(r"^\s*(\d{2,5})\b")


def carrier_code_from_name(name: Optional[str]) -> str:
    """Extract the leading numeric NY DFS carrier/issuer code from a carrier label.

    Examples: ``"746 AMERICAN ROAD INSURANCE COMPANY"`` → ``"746"``;
    ``"AMERICAN ROAD INS.CO."`` → ``""``.
    """
    m = _CARRIER_CODE_RE.match(str(name or ""))
    return m.group(1) if m else ""


def date_to_mmddyyyy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


def add_months(d: date, months: int) -> date:
    """Add N months to a date; clamps day-of-month when needed."""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    # clamp day to last valid day of month
    import calendar
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last))


def generate_policy_number() -> str:
    """``ATP<7 digits>-00`` (e.g. ``ATP1234567-00``)."""
    n = random.randint(1_000_000, 9_999_999)
    return f"ATP{n}-00"


def split_insured_name(upper: str) -> dict:
    """Split a full uppercase name into AAMVA ``DCS`` (last) / ``DAC`` (first) /
    ``DAD`` (middle).
    """
    name = re.sub(r"\s+", " ", (upper or "").strip())
    if not name:
        return {"dcs": "UNKNOWN", "dac": "UNKNOWN", "dad": ""}
    # "LAST,FIRST MIDDLE" or "FIRST MIDDLE LAST"
    if "," in name:
        last, rest = name.split(",", 1)
        parts = rest.strip().split()
        first = parts[0] if parts else ""
        middle = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        parts = name.split()
        if len(parts) == 1:
            return {"dcs": parts[0], "dac": parts[0], "dad": ""}
        last = parts[-1]
        first = parts[0]
        middle = " ".join(parts[1:-1])
    return {"dcs": last or "UNKNOWN", "dac": first or "UNKNOWN", "dad": middle or ""}


_CITY_STATE_ZIP_RE = re.compile(
    r"^\s*(?P<city>.+?)\s*,\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-?\d{4})?)\s*$",
    re.IGNORECASE,
)


def parse_city_state_zip(s: str) -> dict:
    """Parse ``'CITY, ST 12345[-1234]'`` → ``{'city','state','zip'}``.

    Falls back to best-effort if the string is partially formed.
    """
    s = (s or "").strip()
    if not s:
        return {"city": "", "state": "", "zip": ""}
    m = _CITY_STATE_ZIP_RE.match(s)
    if m:
        city = m.group("city").strip()[:20]
        st = m.group("state").upper()
        zp = re.sub(r"\D", "", m.group("zip"))[:9]
        return {"city": city, "state": st, "zip": zp}
    # Fallback: try to pull a ZIP and 2-letter state out of the tail
    zip_match = re.search(r"(\d{5}(?:-?\d{4})?)\s*$", s)
    zp = re.sub(r"\D", "", zip_match.group(1))[:9] if zip_match else ""
    head = s[: zip_match.start()].rstrip(" ,") if zip_match else s
    state_match = re.search(r",\s*([A-Z]{2})\s*$", head, re.IGNORECASE)
    st = state_match.group(1).upper() if state_match else ""
    city = (head[: state_match.start()].rstrip(", ") if state_match else head).strip()[:20]
    return {"city": city, "state": st, "zip": zp}


def build_address_parts(lines: Iterable[str]) -> dict:
    """Split an address into AAMVA ``DAG/DAH/DAI/DAJ/DAK`` fields.

    Accepts up to 3 lines: ``street1, [street2,] city-state-zip``.
    """
    raw = [str(l or "").strip() for l in lines if str(l or "").strip()]
    dag = raw[0][:35] if raw else ""
    dah = raw[1][:35] if len(raw) >= 3 else ""
    last_line = raw[-1] if raw else ""
    csz = parse_city_state_zip(last_line)
    return {
        "dag": dag.upper(),
        "dah": dah.upper(),
        "dai": csz["city"].upper(),
        "daj": csz["state"].upper(),
        "dak": csz["zip"],
    }


def _encode_subfile(subfile_type: str, pairs: list[tuple[str, str]]) -> str:
    """Encode AAMVA subfile body: ``type`` + each ``id+value`` joined by LF;
    last pair terminated by CR.
    """
    body = subfile_type
    for idx, (key, value) in enumerate(pairs):
        body += AAMVA_DES + f"{key}{value}"
        if idx == len(pairs) - 1:
            body += AAMVA_ST
    return body


def build_aamva_ny_insurance_pdf417_payload(
    *,
    policy: str,
    vin: str,
    insured_name_upper: str,
    insured_address_lines: Iterable[str],
    effective_mm_dd_yyyy: str,
    expiration_mm_dd_yyyy: str,
    vehicle_year: str,         # 4 digits
    vehicle_make_short: str,   # 5 alphanumeric uppercase
    daq: Optional[str] = None,
    iin: str = AAMVA_IIN_NY,
    carrier_name: Optional[str] = None,
    agent_license: Optional[str] = None,
    issue_mm_dd_yyyy: Optional[str] = None,
) -> str:
    """Build the AAMVA byte stream encoded by the NY FS-20 insurance PDF417 barcode.

    Uses the AAMVA Insurance Card format with header ``@\\x0a\\x1e\\x0dAAMVA``,
    version ``03`` and four subfiles in this order:

      * **RG** — Registered owner(s) + address (``RBD/RBE/RBF/RBC/DAQ`` + ``RBN/RBP/RBQ/RBR``)
      * **VH** — Vehicle identification (``VAD/VAL/VAK`` + ``ZZD/ZZE/ZZF``)
      * **FR** — Financial responsibility (``FAA/ZZC/FAB/FAC/FAD/ZZB``)
      * **SI** — Signature / integrity hash (``ZZZ/SAA/SAB``)

    Returns a ``str`` (the raw bytes are encoded by ``pdf417gen.encode`` itself).
    """
    name_parts = split_insured_name(insured_name_upper)
    addr_parts = build_address_parts(insured_address_lines)

    last = (name_parts["dcs"] or "").strip()
    first = (name_parts["dac"] or "").strip()
    middle = (name_parts["dad"] or "").strip()
    first_initial = first[0] if first else "N"

    rbc_combined = f"{last}@{first_initial}"[:80]

    eff_iso = mmddyyyy_to_yyyymmdd(effective_mm_dd_yyyy)
    exp_iso = mmddyyyy_to_yyyymmdd(expiration_mm_dd_yyyy)
    issue_iso = mmddyyyy_to_yyyymmdd(issue_mm_dd_yyyy) if issue_mm_dd_yyyy else eff_iso

    daq_norm = normalize_aamva_daq(daq)
    vin_clean = normalize_vin(vin) or (vin or "").upper()[:17]
    cc = carrier_code_from_name(carrier_name)
    al = (agent_license or "").strip()[:25]

    # ── RG: Registered owner + co-owner placeholders + address ────────────
    rg_pairs: list[tuple[str, str]] = [
        ("RBD", last[:40]),
        ("RBE", first[:40]),
        ("RBF", middle[:40]),
        ("RBG", ""),
        ("RBC", rbc_combined),
        ("DAQ", daq_norm),
        ("RBD", ""),
        ("RBE", ""),
        ("RBF", ""),
        ("RBG", ""),
        ("RBC", ""),
        ("DAQ", ""),
        ("ZBC", ""),
        ("ZBD", ""),
        ("ZBE", ""),
        ("RAP", ""),
        ("RBN", addr_parts["dag"][:35]),
        ("RBP", addr_parts["dai"][:20]),
        ("RBQ", addr_parts["daj"][:2]),
        ("RBR", addr_parts["dak"][:9]),
    ]
    rg_body = _encode_subfile("RG", rg_pairs)

    # ── VH: Vehicle ──────────────────────────────────────────────────────
    vh_pairs: list[tuple[str, str]] = [
        ("VAD", vin_clean),
        ("VAL", str(vehicle_year or "")[:4]),
        ("VAK", str(vehicle_make_short or "").upper()[:5]),
        ("ZZD", "N"),
        ("ZZE", ""),
        ("ZZF", ""),
    ]
    vh_body = _encode_subfile("VH", vh_pairs)

    # ── FR: Financial responsibility ──────────────────────────────────────
    fr_pairs: list[tuple[str, str]] = [
        ("FAA", al),                      # producer / agent license — optional
        ("ZZC", cc),                      # carrier code (e.g. "746")
        ("FAB", issue_iso),               # issue date YYYYMMDD
        ("FAC", eff_iso),                 # effective date YYYYMMDD
        ("FAD", exp_iso),                 # expiration date YYYYMMDD
        ("ZZB", str(policy or "")[:25]),  # policy number
    ]
    fr_body = _encode_subfile("FR", fr_pairs)

    # ── SI: Signature subfile (MD5 hash over RG + VH + FR bodies) ────────
    hash_input = (rg_body + vh_body + fr_body).encode("utf-8", errors="replace")
    sig_hash = hashlib.md5(hash_input).hexdigest().upper()
    si_pairs: list[tuple[str, str]] = [
        ("ZZZ", "IC200010"),
        ("SAA", "001"),
        ("SAB", sig_hash),
    ]
    si_body = _encode_subfile("SI", si_pairs)

    # ── Header + designators (header is 19 bytes, 4 designators × 10) ────
    # Pre-header: "@" LF RS CR "AAMVA" + IIN(6) + version "03" + entries "04"
    header_prefix = (
        "@" + AAMVA_DES + AAMVA_RS + AAMVA_ST
        + "AAMVA" + iin + "03" + "04"
    )
    designator_length = 10 * 4
    rg_offset = len(header_prefix) + designator_length
    rg_length = len(rg_body)
    vh_offset = rg_offset + rg_length
    vh_length = len(vh_body)
    fr_offset = vh_offset + vh_length
    fr_length = len(fr_body)
    si_offset = fr_offset + fr_length
    si_length = len(si_body)

    designators = (
        "RG" + str(rg_offset).zfill(4) + str(rg_length).zfill(4)
        + "VH" + str(vh_offset).zfill(4) + str(vh_length).zfill(4)
        + "FR" + str(fr_offset).zfill(4) + str(fr_length).zfill(4)
        + "SI" + str(si_offset).zfill(4) + str(si_length).zfill(4)
    )

    stream = header_prefix + designators + rg_body + vh_body + fr_body + si_body
    if len(stream) > _PDF417_PAYLOAD_LIMIT:
        logger.warning(
            "AAMVA insurance payload exceeds %d chars (%d). Some scanners may fail to read.",
            _PDF417_PAYLOAD_LIMIT,
            len(stream),
        )
        stream = stream[:_PDF417_PAYLOAD_LIMIT]
    return stream


# Convenience alias matching the TS naming (used by render layer)
def build_fs20_barcode_payload(**kwargs) -> str:
    return build_aamva_ny_insurance_pdf417_payload(**kwargs)


# ─── NHTSA VIN decode (free) ─────────────────────────────────────────────────
def decode_vin_from_nhtsa(vin: str) -> Optional[dict]:
    """Return ``{'vin','suggestedVehicleName','modelYear','vehicleMake','vehicleModel'}``
    or ``None`` if the API is unreachable.

    Wraps ``utils.vin_lookup.vin_lookup_nhtsa`` (already used by the bot for VIN
    verification) and shapes the result to the blueprint's contract.
    """
    try:
        from .vin_lookup import vin_lookup_nhtsa
    except Exception:  # pragma: no cover - circular safety
        return None
    v = normalize_vin(vin)
    if not v:
        return None
    result = vin_lookup_nhtsa(v)
    if not result:
        return None
    return {
        "vin": v,
        "modelYear": result.get("year") or "",
        "vehicleMake": result.get("make") or "",
        "vehicleModel": result.get("model") or "",
        "suggestedVehicleName": result.get("car_line") or "",
    }


def format_suggested_vehicle_name(model_year: str, make: str, model: str) -> str:
    parts = [p for p in (str(model_year or "").strip(), str(make or "").strip(), str(model or "").strip()) if p]
    return " ".join(parts)


def format_insured_fs20_name(insured_name_upper: str) -> str:
    """Compact card name: ``LAST F`` (last name + first initial, space-separated).

    The full legal name still goes into the AAMVA payload (DCS/DAC). The printed
    card never uses a comma here — see the FS-20 layout, which keeps the printed
    text comma-free per request.
    """
    parts = split_insured_name(insured_name_upper)
    last = parts["dcs"]
    first_initial = parts["dac"][0] if parts["dac"] else ""
    return f"{last} {first_initial}" if first_initial else last


# ─── Page + card geometry (port of lib/pdf/ny-insurance-id-card.ts) ──────────
PAGE_W = 612.0
PAGE_H = 792.0

CARD_TOP_1 = 777.6
CARD_PITCH = 264.24
CARD_TOP_2 = CARD_TOP_1 - CARD_PITCH
CARD_LEFT = 3.6
CARD_W = 561.6
CARD_H = 254.16

# Card PDF417 barcode rectangle (top-left = (BARCODE_X, BARCODE_Y_FROM_BOTTOM))
BARCODE_X = 18.03
BARCODE_W = 294.42
BARCODE_H = 57.54
# Vertical offset above card bottom (TS used: 532.83 - (CARD_TOP_1 - CARD_H))
BARCODE_OFFSET_FROM_CARD_BOTTOM = 532.83 - (CARD_TOP_1 - CARD_H)

# FAX scannable barcode at bottom of the page
FAX_BARCODE_X = 1.47
FAX_BARCODE_Y = 57.63
FAX_BARCODE_W = 287.94
FAX_BARCODE_H = 107.94

# Divider line
DIVIDER_Y = 518.4


@dataclass
class CardIssuer:
    """Issuer block printed on the card (same on every issued FS-20).

    Maps to the two distinct lines on a real NY FS-20:
      * ``agency_name`` + ``agency_address_lines`` — the agency that issued the
        card (e.g. "COYNE INSURANCE AGENCY", "146 COLUMBIA TURNPIKE",
        "RENSSELAER NY 12144").
      * ``carrier_name`` + ``agency_phone`` — the underwriting carrier line
        plus the agency's contact number (e.g. "484 NEW SOUTH INS.CO.",
        "5184772174").

    The older ``issuer_company_line`` / ``issuer_phone`` field names are kept
    as aliases so existing callers don't break.
    """

    carrier_name: str = "746 AMERICAN ROAD INSURANCE COMPANY"
    agency_phone: str = ""
    agency_name: str = "AMERICAN ROAD INSURANCE CO"
    agency_address_lines: list[str] = field(default_factory=lambda: [
        "P.O. BOX 6027",
        "DEARBORN, MI 48121-6027",
    ])
    iin: str = AAMVA_IIN_NY

    # Backwards-compat aliases (old field names).
    @property
    def issuer_company_line(self) -> str:  # pragma: no cover - shim
        return self.carrier_name

    @property
    def issuer_phone(self) -> str:  # pragma: no cover - shim
        return self.agency_phone


@dataclass
class InsuranceCardInput:
    policy_number: str
    effective_mm_dd_yyyy: str
    expiration_mm_dd_yyyy: str
    vehicle_year_full: str
    vehicle_make_short: str          # up to 5 chars uppercase
    vin: str
    insured_name_upper: str          # full legal name, for AAMVA payload
    insured_fs20_name: str           # compact "LAST,F" for printed card
    insured_address_lines: list[str]
    daq: Optional[str] = None
    issuer: CardIssuer = field(default_factory=CardIssuer)


def _render_pdf417_png(text: str, *, columns: int, scale: int) -> bytes:
    """Render an AAMVA payload to a PDF417 PNG (bytes).

    Wraps ``pdf417gen``. Falls back to a 1px placeholder image when the library
    is missing so PDF generation never crashes — but logs a loud warning.
    """
    try:
        from pdf417gen import encode, render_image
    except ImportError:  # pragma: no cover - dep missing in dev
        logger.error("pdf417gen not installed; insurance card barcode will be a placeholder")
        from io import BytesIO

        try:
            from PIL import Image
        except ImportError:
            return b""
        img = Image.new("RGB", (10, 10), color="white")
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    payload = text[:_PDF417_PAYLOAD_LIMIT]
    codes = encode(payload, columns=columns, security_level=4)
    image = render_image(codes, scale=scale, ratio=3)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


# ─── Layout constants (1:1 port of lib/pdf/ny-insurance-id-card.ts) ──────────
STROKE_THICK = 0.6

INSURED_BOX_LEFT = 7.2
INSURED_BOX_RIGHT = 219.6
INSURED_BOX_TOP = 669.6
INSURED_BOX_BOTTOM = 604.8
INSURED_CORNER_H = 18.0
INSURED_CORNER_W_L = 46.8
INSURED_CORNER_W_R = 57.6

# Absolute coordinates for VALUE underlines on card 1 (offset by dy for card 2).
VALUE_UNDERLINES: list[tuple[float, float, float]] = [
    (255.6, 742.32, 133.20),
    (255.6, 714.96, 43.20),
    (327.6, 714.96, 43.20),
    (255.6, 658.80, 36.00),
    (314.64, 658.80, 56.16),
    (255.6, 637.20, 115.20),
]

# Right-column warning lines (x=414, size=7.92 in TS).
WARN_TEXT: list[tuple[float, str]] = [
    (763.3, "THIS ID CARD MUST BE CARRIED"),
    (754.7, "IN THE INSURED VEHICLE FOR"),
    (746.0, "PRODUCTION UPON DEMAND"),
    (728.8, "WARNING: Any person who issues"),
    (720.1, "or produces an ID card knowing that"),
    (711.5, "an Owner's Policy of insurance is not in"),
    (702.8, "effect may be committing a misdemeanor."),
    (694.2, "In addition, a person who presents"),
    (685.6, "an ID card if insurance is not in"),
    (676.9, "effect may be committing a"),
    (668.3, "misdemeanor."),
    (651.0, "The name of the registrant and the"),
    (642.4, "name of the insured must coincide."),
    (625.1, "REPLACEMENT VEHICLE NOTATION:"),
    (616.4, "DMV WILL ONLY PROCESS A VEHICLE"),
    (607.8, "CHANGE (RE-REGISTRATION) USING"),
    (599.2, "THE REPLACED VEHICLE'S CURRENT"),
    (590.5, "REGISTRATION."),
]


def _first_word(s: str) -> str:
    parts = (s or "").strip().split()
    return parts[0] if parts else ""


def _rest_after_first_word(s: str) -> str:
    parts = (s or "").strip().split()
    return " ".join(parts[1:]) if len(parts) > 1 else ""


def _draw_corner_marks(c, dy: float) -> None:
    """Insured-box corner crop marks (port of TS drawCornerMarks)."""
    y_top = INSURED_BOX_TOP + dy
    y_bot = INSURED_BOX_BOTTOM + dy
    x_l = INSURED_BOX_LEFT
    x_r = INSURED_BOX_RIGHT
    c.setLineWidth(STROKE_THICK)
    c.setStrokeColorRGB(0, 0, 0)
    # Top-left corner (down-tick + right-tick)
    c.line(x_l, y_top - INSURED_CORNER_H, x_l, y_top)
    c.line(x_l, y_top, x_l + INSURED_CORNER_W_L, y_top)
    # Bottom-left corner
    c.line(x_l, y_bot + INSURED_CORNER_H, x_l, y_bot)
    c.line(x_l, y_bot, x_l + INSURED_CORNER_W_L, y_bot)
    # Top-right corner
    c.line(x_r - INSURED_CORNER_W_R, y_top, x_r, y_top)
    c.line(x_r, y_top, x_r, y_top - INSURED_CORNER_H)
    # Bottom-right corner
    c.line(x_r - INSURED_CORNER_W_R, y_bot, x_r, y_bot)
    c.line(x_r, y_bot, x_r, y_bot + INSURED_CORNER_H)


def _draw_card(c, *, card_top: float, input_: InsuranceCardInput, card_barcode_png: bytes) -> None:
    """Render one FS-20 ID card at ``card_top`` (PDF y of the card's top edge).

    1:1 port of ``drawCard`` from ``lib/pdf/ny-insurance-id-card.ts``: every
    text item is positioned by absolute coordinate, then shifted by
    ``dy = card_top - CARD_TOP_1`` so the same template renders both cards.
    """
    from reportlab.lib.utils import ImageReader

    dy = card_top - CARD_TOP_1
    card_bottom = card_top - CARD_H

    # ── Card frame ─────────────────────────────────────────────────────────
    c.setLineWidth(STROKE_THICK)
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(CARD_LEFT, card_bottom, CARD_W, CARD_H, stroke=1, fill=0)

    # ── Insured-box corner crop marks ──────────────────────────────────────
    _draw_corner_marks(c, dy)

    # ── Right column: standard FS-20 warnings (x=414, size=7.92) ───────────
    c.setFont("Helvetica", 7.92)
    c.setFillColorRGB(0, 0, 0)
    for ref_y, line in WARN_TEXT:
        c.drawString(414.0, ref_y + dy, line)

    # ── Left/centre data + labels (TEXT_ITEMS in TS) ───────────────────────
    issuer = input_.issuer
    issuer_co = (issuer.carrier_name or "484 NEW SOUTH INS.CO.").strip()
    issuer_first = _first_word(issuer_co) or "484"
    issuer_rest = _rest_after_first_word(issuer_co) or "NEW SOUTH INS.CO."
    agency_name = (issuer.agency_name or "COYNE INSURANCE AGENCY").strip()
    # No fallback for phone — blank explicitly means "don't print a number".
    agency_phone = (issuer.agency_phone or "").strip()
    agency_lines = [str(l).strip() for l in (issuer.agency_address_lines or []) if str(l).strip()]
    agency_l1 = agency_lines[0] if len(agency_lines) >= 1 else "146 COLUMBIA TURNPIKE"
    agency_l2 = agency_lines[1] if len(agency_lines) >= 2 else "RENSSELAER NY 12144"

    def _strip_commas(s: str) -> str:
        return re.sub(r"\s*,\s*", " ", s).strip()

    insured_lines = [_strip_commas(str(l)) for l in input_.insured_address_lines if str(l).strip()]
    insured_l1 = insured_lines[0].upper() if len(insured_lines) >= 1 else ""
    insured_l2 = insured_lines[1].upper() if len(insured_lines) >= 2 else ""
    insured_l3 = insured_lines[2].upper() if len(insured_lines) >= 3 else ""

    name_print = _strip_commas(
        (input_.insured_fs20_name or input_.insured_name_upper).upper()
    )

    vehicle_make_5 = (input_.vehicle_make_short or "").upper()[:5]

    items: list[tuple[float, float, float, bool, str]] = [
        # x, y (absolute), size, bold, text
        (61.9, 766.1, 10.8, True, "NEW YORK STATE INSURANCE IDENTIFICATION CARD"),

        # Issuer block (left side)
        (10.8, 748.0, 8.64, True, issuer_first),
        (32.4, 748.0, 8.64, True, issuer_rest),
        (7.2, 730.9, 7.2, False, "Name & Address of Issuer"),
        (97.2, 729.2, 8.64, True, agency_name),
        (7.2, 719.8, 7.2, True, agency_phone),
        (97.2, 719.2, 8.64, True, agency_l1),
        (97.2, 709.1, 8.64, True, agency_l2),

        # Centre column: Policy / Effective / Expiration
        (255.6, 754.0, 8.64, False, "Policy Number"),
        (255.6, 743.6, 8.64, True, input_.policy_number),
        (255.6, 728.8, 8.64, False, "Effective Date"),
        (327.6, 728.8, 8.64, False, "Expiration Date"),
        (255.6, 716.3, 8.64, True, input_.effective_mm_dd_yyyy),
        (327.6, 716.3, 8.64, True, input_.expiration_mm_dd_yyyy),
        (255.6, 706.4, 7.2, False, "12:01 a.m."),
        (327.6, 706.4, 7.2, False, "12:01 a.m."),
        (255.6, 697.0, 7.2, False, "(Not acceptable to obtain registration"),
        (255.6, 689.8, 7.2, False, "after 45 days from effective date.)"),
        (255.6, 680.5, 7.2, False, "Applicable with respect to the following"),
        (255.6, 673.3, 7.2, False, "Motor Vehicle:"),

        # Year / Make
        (262.8, 661.6, 8.64, True, str(input_.vehicle_year_full or "")),
        (331.2, 661.6, 8.64, True, vehicle_make_5),
        (266.4, 650.9, 7.2, False, "Year"),
        (335.5, 650.9, 7.2, False, "Make"),

        # VIN
        (255.6, 639.2, 7.92, True, str(input_.vin or "")),
        (266.4, 628.6, 7.2, False, "Vehicle Identification Number"),

        # Article 6 preamble (left)
        (7.2, 690.5, 7.2, False,
         "An authorized NEW YORK insurer has issued an Owner's Policy of"),
        (7.2, 682.6, 7.2, False,
         "Liability Insurance complying with Article 6 (Motor Vehicle Financial"),
        (7.2, 674.7, 7.2, False,
         "Security Act) of the NEW YORK Vehicle and Traffic Law to:"),

        # Insured name + address (inside the corner-mark box)
        (28.8, 640.7, 8.64, True, name_print),
        (28.8, 630.6, 8.64, True, insured_l1),
        (28.8, 620.5, 8.64, True, insured_l2),
        (28.8, 610.4, 8.64, True, insured_l3),

        # FS-20 badge (per-card corner)
        (532.8, 526.8, 7.2, True, "FS-20"),
    ]

    for x, y, size, bold, text in items:
        if not text:
            continue
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(x, y + dy, str(text))

    # ── Value underlines (line beneath each variable field) ────────────────
    c.setLineWidth(STROKE_THICK)
    c.setStrokeColorRGB(0, 0, 0)
    for ux, uy, uw in VALUE_UNDERLINES:
        c.line(ux, uy + dy, ux + uw, uy + dy)

    # ── Card PDF417 barcode (bottom-right of card area) ────────────────────
    if card_barcode_png:
        img = ImageReader(io.BytesIO(card_barcode_png))
        c.drawImage(
            img,
            BARCODE_X,
            card_bottom + BARCODE_OFFSET_FROM_CARD_BOTTOM,
            width=BARCODE_W,
            height=BARCODE_H,
            preserveAspectRatio=False,
            mask="auto",
        )


def build_ny_insurance_id_card_pdf(input_: InsuranceCardInput) -> bytes:
    """Build the FS-20 PDF (two ID cards + bottom FAX barcode). Returns PDF bytes."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    # Build the AAMVA payload ONCE; re-rasterized in two different sizes.
    payload = build_aamva_ny_insurance_pdf417_payload(
        policy=input_.policy_number,
        vin=input_.vin,
        insured_name_upper=input_.insured_name_upper,
        insured_address_lines=input_.insured_address_lines,
        effective_mm_dd_yyyy=input_.effective_mm_dd_yyyy,
        expiration_mm_dd_yyyy=input_.expiration_mm_dd_yyyy,
        vehicle_year=input_.vehicle_year_full,
        vehicle_make_short=input_.vehicle_make_short,
        daq=input_.daq,
        iin=input_.issuer.iin,
        carrier_name=input_.issuer.carrier_name,
    )

    card_barcode_png = _render_pdf417_png(payload, columns=10, scale=2)
    fax_barcode_png = _render_pdf417_png(payload, columns=12, scale=3)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Two duplicate cards
    _draw_card(c, card_top=CARD_TOP_1, input_=input_, card_barcode_png=card_barcode_png)
    _draw_card(c, card_top=CARD_TOP_2, input_=input_, card_barcode_png=card_barcode_png)
    # ── Page-level: divider, FAX barcode, FAX instructions ─────────────────
    # 1:1 port of build code at the bottom of buildNyInsuranceIdCardPdf in
    # lib/pdf/ny-insurance-id-card.ts.
    from reportlab.lib.utils import ImageReader

    # Horizontal divider between top half (cards) and bottom (FAX) at y=518.4
    c.setLineWidth(STROKE_THICK)
    c.setStrokeColorRGB(0, 0, 0)
    c.line(0, DIVIDER_Y, PAGE_W, DIVIDER_Y)

    # "FAX: Scanable Bar Code" caption
    c.setFont("Helvetica", 12.96)
    c.setFillColorRGB(0, 0, 0)
    c.drawString(14.4, 170.5, "FAX: Scanable Bar Code")

    # FAX scannable barcode
    if fax_barcode_png:
        img = ImageReader(io.BytesIO(fax_barcode_png))
        c.drawImage(
            img,
            FAX_BARCODE_X,
            FAX_BARCODE_Y,
            width=FAX_BARCODE_W,
            height=FAX_BARCODE_H,
            preserveAspectRatio=False,
            mask="auto",
        )

    # FAX INSTRUCTIONS heading
    c.setFont("Helvetica", 10.08)
    c.drawString(331.2, 160.1, "FAX INSTRUCTIONS:")

    # Numbered instruction list
    c.setFont("Helvetica", 7.92)
    fax_instructions: list[tuple[float, float, str]] = [
        (316.8, 144.1, "1. The entire page must be faxed."),
        (316.8, 126.8, "2. If submitted to DMV, either the entire page or the second"),
        (323.3, 118.2, "ID card and large scanable bar code will be retained"),
        (316.8, 100.9, "3. A faxed ID card must be replaced with a scanable"),
        (323.3, 92.3, "ID card within 14 days of the effective date."),
        (316.8, 75.0, "4. DMV will not accept a faxed ID card without a"),
        (323.3, 66.4, "scanable barcode"),
    ]
    for x, y, line in fax_instructions:
        c.drawString(x, y, line)

    c.showPage()
    c.save()
    return buf.getvalue()


# ─── Plan / expiry helpers (one-month policy by default for this bot) ────────
def expiration_for_plan(effective: date, months: int = 1) -> date:
    """Compute expiration date by adding N months. Defaults to 1 month."""
    return add_months(effective, max(1, int(months or 1)))
