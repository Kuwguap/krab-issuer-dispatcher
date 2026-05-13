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


def mmddyyyy_to_aamva(mmddyyyy: str) -> str:
    """Convert ``'MM/DD/YYYY'`` to AAMVA ``'MMDDCCYY'`` (8 digits)."""
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})\s*$", str(mmddyyyy or ""))
    if not m:
        return "00000000"
    mm = m.group(1).zfill(2)
    dd = m.group(2).zfill(2)
    yyyy = m.group(3)
    return f"{mm}{dd}{yyyy}"


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
) -> str:
    """Build the AAMVA Annex D byte stream encoded by the FS-20 PDF417 barcode.

    Returns a ``str`` (the raw bytes are encoded by ``pdf417gen.encode`` itself).
    """
    name_parts = split_insured_name(insured_name_upper)
    addr_parts = build_address_parts(insured_address_lines)

    eff = mmddyyyy_to_aamva(effective_mm_dd_yyyy)
    exp = mmddyyyy_to_aamva(expiration_mm_dd_yyyy)
    daq_norm = normalize_aamva_daq(daq)
    vin_clean = normalize_vin(vin) or (vin or "").upper()[:17]

    # DCF field: "${policy}|${vin}" truncated to 25 chars (per blueprint)
    dcf = f"{policy}|{vin_clean}"[:25]

    id_pairs: list[tuple[str, str]] = [
        ("DCS", name_parts["dcs"][:40]),
        ("DAC", name_parts["dac"][:40]),
        ("DAD", (name_parts["dad"] or "NONE")[:40]),
        ("DBD", eff),                  # document issue date (effective)
        ("DBB", "unavl"),              # DOB unavailable for insurance card
        ("DBA", exp),                  # document expiration date
        ("DBC", "9"),                  # sex: 9 = not specified
        ("DAY", "UNK"),                # eye color: unknown
        ("DAU", "000 in"),             # height
        ("DAG", addr_parts["dag"]),
    ]
    if addr_parts["dah"]:
        id_pairs.append(("DAH", addr_parts["dah"]))
    id_pairs.extend([
        ("DAI", addr_parts["dai"]),
        ("DAJ", addr_parts["daj"]),
        ("DAK", addr_parts["dak"]),
        ("DAQ", daq_norm),             # customer / document ID
        ("DCF", dcf),                  # document discriminator (policy|vin)
        ("DCG", "USA"),                # country
        ("DDE", "N"),                  # last name truncation
        ("DDF", "N"),                  # first name truncation
        ("DDG", "N"),                  # middle name truncation
        ("VAD", vin_clean),            # vehicle identification (custom)
    ])
    id_body = _encode_subfile("ID", id_pairs)

    zn_pairs: list[tuple[str, str]] = [
        ("ZNA", policy),
        ("ZNB", vin_clean),
        ("ZNC", str(vehicle_year)[:4]),
        ("ZND", str(vehicle_make_short)[:5]),
        ("ZNE", eff),
        ("ZNF", exp),
    ]
    zn_body = _encode_subfile("ZN", zn_pairs)

    # Subfile designators (10 bytes each: type + offset(4) + length(4))
    # The exact offset/length are computed once we know the header length.
    header_prefix = "@" + AAMVA_DES + AAMVA_RS + AAMVA_ST + "ANSI " + iin + "09" + "01" + "02"
    designator_length = 10 * 2  # two subfiles
    id_offset = len(header_prefix) + designator_length
    id_length = len(id_body)
    zn_offset = id_offset + id_length
    zn_length = len(zn_body)

    id_designator = "ID" + str(id_offset).zfill(4) + str(id_length).zfill(4)
    zn_designator = "ZN" + str(zn_offset).zfill(4) + str(zn_length).zfill(4)

    stream = header_prefix + id_designator + zn_designator + id_body + zn_body
    if len(stream) > _PDF417_PAYLOAD_LIMIT:
        logger.warning(
            "AAMVA payload exceeds %d chars (%d). Some scanners may fail to read.",
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
        from utils.vin_lookup import vin_lookup_nhtsa
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
    """Compact card name: ``LAST,F`` (last name + first initial). The full
    legal name still goes into the AAMVA payload (DCS/DAC).
    """
    parts = split_insured_name(insured_name_upper)
    last = parts["dcs"]
    first_initial = parts["dac"][0] if parts["dac"] else ""
    return f"{last},{first_initial}" if first_initial else last


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

    carrier_name: str = "TRI STATE COVERAGE INC"
    agency_phone: str = "(551) 369-5696"
    agency_name: str = "TRI STATE COVERAGE INC"
    agency_address_lines: list[str] = field(default_factory=lambda: [
        "1 N CENTRAL RD 6TH FLOOR SUITE 629",
        "FORT LEE NJ 07024",
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


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap; preserves explicit ``\n`` line breaks."""
    out: list[str] = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        line = ""
        for w in words:
            candidate = (line + " " + w).strip() if line else w
            if len(candidate) > max_chars and line:
                out.append(line)
                line = w
            else:
                line = candidate
        out.append(line)
    return out


def _draw_card(c, *, card_top: float, input_: InsuranceCardInput, card_barcode_png: bytes) -> None:
    """Draw one FS-20 ID card at the given vertical position.

    Layout matches the official New York State FS-20 Insurance Identification
    Card: heading band at top, data column on the left (value above label),
    warning column on the right, and PDF417 barcode bottom-right.

    Reportlab uses origin at bottom-left.  ``card_top`` is the Y of the card's
    top edge (PDF coordinate). The card body extends down by ``CARD_H``.
    """
    from reportlab.lib.utils import ImageReader

    card_bottom = card_top - CARD_H

    # ── Frame + corner crop marks ──────────────────────────────────────────
    c.setLineWidth(0.8)
    c.setStrokeColorRGB(0, 0, 0)
    c.rect(CARD_LEFT, card_bottom, CARD_W, CARD_H, stroke=1, fill=0)
    tick = 6
    for (cx, cy) in (
        (CARD_LEFT, card_bottom),
        (CARD_LEFT + CARD_W, card_bottom),
        (CARD_LEFT, card_top),
        (CARD_LEFT + CARD_W, card_top),
    ):
        c.line(cx - tick, cy, cx + tick, cy)
        c.line(cx, cy - tick, cx, cy + tick)

    # ── Heading band (centered title + boilerplate preamble) ───────────────
    title_y = card_top - 14
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(CARD_LEFT + CARD_W / 2, title_y, "NEW YORK STATE INSURANCE IDENTIFICATION CARD")

    preamble = (
        "An authorized NEW YORK insurer has issued an Owner's Policy of "
        "Liability Insurance complying with Article 6 (Motor Vehicle Financial "
        "Security Act) of the NEW YORK Vehicle and Traffic Law to:"
    )
    c.setFont("Helvetica", 7.6)
    py = title_y - 11
    for line in _wrap_text(preamble, 95):
        c.drawCentredString(CARD_LEFT + CARD_W / 2, py, line)
        py -= 9

    # Horizontal divider under the heading band
    divider_y = py - 4
    c.setLineWidth(0.4)
    c.line(CARD_LEFT + 6, divider_y, CARD_LEFT + CARD_W - 6, divider_y)

    # ── Body geometry: left data column + right warnings column ────────────
    col_left_x = CARD_LEFT + 12
    col_left_w = 330
    col_right_x = CARD_LEFT + CARD_W - 12 - 200
    col_right_w = 200
    body_top = divider_y - 6

    # Helper: draw a labelled field "VALUE" then "label" below it.
    def field(value: str, label: str, *, y: float, value_font: str = "Helvetica-Bold",
              value_size: float = 10.0, label_size: float = 6.6,
              underline: bool = False, x: Optional[float] = None,
              width: Optional[float] = None) -> float:
        fx = x if x is not None else col_left_x
        fw = width if width is not None else col_left_w
        c.setFont(value_font, value_size)
        c.drawString(fx, y, str(value or " "))
        if underline:
            c.setLineWidth(0.3)
            c.line(fx, y - 2, fx + fw, y - 2)
        c.setFont("Helvetica", label_size)
        c.drawString(fx, y - label_size - 2, label)
        return y - (value_size + label_size + 6)

    # Build display strings
    name_line = (input_.insured_fs20_name or input_.insured_name_upper).upper()
    addr_lines = [str(ln).upper().strip() for ln in input_.insured_address_lines if str(ln).strip()]
    if len(addr_lines) < 2:
        addr_lines = addr_lines + [""] * (2 - len(addr_lines))
    addr_lines = addr_lines[:2]

    vin_clean = normalize_vin(input_.vin) or (input_.vin or "")
    year_str = str(input_.vehicle_year_full or "").strip()
    make_str = str(input_.vehicle_make_short or "").strip().upper()
    eff = input_.effective_mm_dd_yyyy
    exp = input_.expiration_mm_dd_yyyy
    policy = input_.policy_number

    # ── Left column: insured name + address ────────────────────────────────
    y = body_top - 4
    c.setFont("Helvetica-Bold", 10.0)
    c.drawString(col_left_x, y, name_line)
    y -= 11
    c.setFont("Helvetica", 9.0)
    c.drawString(col_left_x, y, addr_lines[0])
    y -= 10
    c.drawString(col_left_x, y, addr_lines[1])
    y -= 12

    c.setFont("Helvetica-Bold", 7.4)
    c.drawString(col_left_x, y, "Applicable with respect to the following Motor Vehicle:")
    y -= 12

    # VIN row
    y = field(vin_clean, "Vehicle Identification Number", y=y, underline=True)

    # Year / Make row (side-by-side)
    year_x = col_left_x
    make_x = col_left_x + 90
    year_w = 70
    make_w = col_left_w - 90
    c.setFont("Helvetica-Bold", 10.0)
    c.drawString(year_x, y, year_str)
    c.drawString(make_x, y, make_str)
    c.setLineWidth(0.3)
    c.line(year_x, y - 2, year_x + year_w, y - 2)
    c.line(make_x, y - 2, make_x + make_w, y - 2)
    c.setFont("Helvetica", 6.6)
    c.drawString(year_x, y - 9, "Year")
    c.drawString(make_x, y - 9, "Make")
    y -= 22

    # Effective / Expiration row (side-by-side) with "12:01 a.m." sublabels
    eff_x = col_left_x
    exp_x = col_left_x + 120
    col_w = 105
    c.setFont("Helvetica-Bold", 10.0)
    c.drawString(eff_x, y, eff)
    c.drawString(exp_x, y, exp)
    c.setLineWidth(0.3)
    c.line(eff_x, y - 2, eff_x + col_w, y - 2)
    c.line(exp_x, y - 2, exp_x + col_w, y - 2)
    c.setFont("Helvetica", 6.6)
    c.drawString(eff_x, y - 9, "Effective Date")
    c.drawString(exp_x, y - 9, "Expiration Date")
    c.drawString(eff_x + 64, y - 9, "12:01 a.m.")
    c.drawString(exp_x + 64, y - 9, "12:01 a.m.")
    y -= 22

    # Policy number + parenthetical note
    c.setFont("Helvetica-Bold", 10.0)
    c.drawString(col_left_x, y, policy)
    c.setLineWidth(0.3)
    c.line(col_left_x, y - 2, col_left_x + col_left_w, y - 2)
    c.setFont("Helvetica", 6.6)
    c.drawString(col_left_x, y - 9, "Policy Number")
    c.setFont("Helvetica-Oblique", 6.2)
    c.drawString(col_left_x + 75, y - 9,
                 "(Not acceptable to obtain registration after 45 days from effective date.)")
    y -= 22

    # Agency name + address (issuer)
    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(col_left_x, y, str(input_.issuer.agency_name or "").upper())
    y -= 10
    c.setFont("Helvetica", 8.6)
    for line in (input_.issuer.agency_address_lines or [])[:2]:
        c.drawString(col_left_x, y, str(line or "").upper())
        y -= 10
    c.setFont("Helvetica", 6.6)
    c.drawString(col_left_x, y, "Name & Address of Issuer")
    y -= 12

    # Underwriting carrier + agency phone
    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(col_left_x, y, str(input_.issuer.carrier_name or "").upper())
    y -= 10
    c.setFont("Helvetica", 8.6)
    c.drawString(col_left_x, y, str(input_.issuer.agency_phone or ""))
    y -= 10

    # ── Right column: standard FS-20 warnings ──────────────────────────────
    warn_y = body_top - 4
    c.setFont("Helvetica-Bold", 7.6)
    for line in _wrap_text("THIS ID CARD MUST BE CARRIED IN THE INSURED VEHICLE FOR PRODUCTION UPON DEMAND", 36):
        c.drawString(col_right_x, warn_y, line)
        warn_y -= 9
    warn_y -= 4

    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(col_right_x, warn_y, "WARNING:")
    c.setFont("Helvetica", 6.8)
    warn_text = (
        " Any person who issues or produces an ID card knowing that an Owner's "
        "Policy of insurance is not in effect may be committing a misdemeanor. "
        "In addition, a person who presents an ID card if insurance is not in "
        "effect may be committing a misdemeanor."
    )
    # First line continues after "WARNING:"; subsequent lines start at left edge
    first_indent = c.stringWidth("WARNING:", "Helvetica-Bold", 6.8)
    lines = _wrap_text(warn_text.strip(), 40)
    if lines:
        c.drawString(col_right_x + first_indent + 2, warn_y, lines[0])
        warn_y -= 8
        for ln in lines[1:]:
            c.drawString(col_right_x, warn_y, ln)
            warn_y -= 8
    warn_y -= 4

    c.setFont("Helvetica", 6.8)
    for line in _wrap_text("The name of the registrant and the name of the insured must coincide.", 40):
        c.drawString(col_right_x, warn_y, line)
        warn_y -= 8
    warn_y -= 4

    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(col_right_x, warn_y, "REPLACEMENT VEHICLE NOTATION:")
    warn_y -= 8
    c.setFont("Helvetica", 6.8)
    for line in _wrap_text(
        "DMV WILL ONLY PROCESS A VEHICLE CHANGE (RE-REGISTRATION) USING THE "
        "REPLACED VEHICLE'S CURRENT REGISTRATION.",
        40,
    ):
        c.drawString(col_right_x, warn_y, line)
        warn_y -= 8

    # ── Card PDF417 barcode (bottom-right of card) ─────────────────────────
    if card_barcode_png:
        img = ImageReader(io.BytesIO(card_barcode_png))
        bx = CARD_LEFT + CARD_W - BARCODE_W - BARCODE_X
        by = card_bottom + BARCODE_OFFSET_FROM_CARD_BOTTOM
        c.drawImage(img, bx, by, width=BARCODE_W, height=BARCODE_H,
                    preserveAspectRatio=False, mask='auto')


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
    )

    card_barcode_png = _render_pdf417_png(payload, columns=10, scale=2)
    fax_barcode_png = _render_pdf417_png(payload, columns=12, scale=3)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    # Two duplicate cards
    _draw_card(c, card_top=CARD_TOP_1, input_=input_, card_barcode_png=card_barcode_png)
    _draw_card(c, card_top=CARD_TOP_2, input_=input_, card_barcode_png=card_barcode_png)

    # ── Bottom strip: FAX barcode + FAX INSTRUCTIONS + FS-20 badge ─────────
    from reportlab.lib.utils import ImageReader

    if fax_barcode_png:
        img = ImageReader(io.BytesIO(fax_barcode_png))
        c.drawImage(img, FAX_BARCODE_X, FAX_BARCODE_Y,
                    width=FAX_BARCODE_W, height=FAX_BARCODE_H,
                    preserveAspectRatio=False, mask='auto')

    # Label beneath the FAX barcode
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(FAX_BARCODE_X + 4, FAX_BARCODE_Y - 12, "FAX: Scanable Bar Code")

    # FAX INSTRUCTIONS column to the right of the barcode
    inst_x = FAX_BARCODE_X + FAX_BARCODE_W + 16
    inst_top = FAX_BARCODE_Y + FAX_BARCODE_H - 6
    c.setFont("Helvetica-Bold", 9.0)
    c.drawString(inst_x, inst_top, "FAX INSTRUCTIONS:")
    c.setFont("Helvetica", 7.6)
    iy = inst_top - 12
    for line in (
        "1. The entire page must be faxed.",
        "2. If submitted to DMV, either the entire page or the second",
        "   ID card and large scanable bar code will be retained.",
        "3. A faxed ID card must be replaced with a scanable",
        "   ID card within 14 days of the effective date.",
        "4. DMV will not accept a faxed ID card without a",
        "   scanable barcode.",
    ):
        c.drawString(inst_x, iy, line)
        iy -= 10

    # "FS-20" badge in the bottom-right corner
    badge_x = PAGE_W - 60
    badge_y = FAX_BARCODE_Y + 4
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.8)
    c.rect(badge_x, badge_y, 50, 22, stroke=1, fill=0)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(badge_x + 25, badge_y + 7, "FS-20")

    c.showPage()
    c.save()
    return buf.getvalue()


# ─── Plan / expiry helpers (one-month policy by default for this bot) ────────
def expiration_for_plan(effective: date, months: int = 1) -> date:
    """Compute expiration date by adding N months. Defaults to 1 month."""
    return add_months(effective, max(1, int(months or 1)))
