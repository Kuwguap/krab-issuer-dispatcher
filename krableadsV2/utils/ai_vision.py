"""
AI vision integration: extract structured lead fields from an image.
Uses OCR + LLM (OpenAI vision) to get the same 11-field structure as text input.
Includes validation for extracted data (VIN, line count, required fields).
Driver receipt uploads: validate image is a real receipt and optionally match lead price.
"""
import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReceiptValidationResult:
    """Result of AI check on a driver-uploaded receipt image."""

    accept: bool
    message: str  # User-facing when accept is False; empty when accept is True


def extract_receipt_amounts_usd(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> list[float]:
    """
    Best-effort extraction of USD amounts from a receipt image.

    Uses the same vision JSON schema as receipt validation. Returns [] when the
    AI model is unavailable or cannot parse amounts.
    """
    from config import Config

    if not image_bytes:
        return []
    if not Config.OPENAI_API_KEY or not str(Config.OPENAI_API_KEY).strip():
        return []

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"
    try:
        from openai import OpenAI

        client = OpenAI(api_key=str(Config.OPENAI_API_KEY).strip(), max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": RECEIPT_VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=500,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("extract_receipt_amounts_usd: API error: %s", e)
        return []

    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return []

    amounts_raw = data.get("amounts_usd") or []
    out: list[float] = []
    if isinstance(amounts_raw, list):
        for x in amounts_raw:
            try:
                v = float(x)
                if v > 0:
                    out.append(v)
            except (TypeError, ValueError):
                continue
    return out

# Number of required lines for Phase 1 structured output
PHASE1_LINE_COUNT = 11


class AIVisionQuotaError(Exception):
    """Raised when the AI provider returns 429 / insufficient quota."""
    pass

# Expected output: exactly 11 lines in this order (used by parse_phase1_structured)
STRUCTURE_PROMPT = """You are extracting vehicle/registration and delivery details, plus the client's phone number, price, optional email and driver's-license ID, from an image or PDF page (screenshot, scan, or form).

STRICT RULES:
- Output ONLY a plain text block with exactly 17 lines. One line per field—nothing else on that line.
- Lines 1-11 must contain ONLY the vehicle/delivery values. No phone numbers, no URLs, no extra text.
- Line 6 (VIN): exactly 17 alphanumeric characters (no spaces, no truncation, no extra digits). Or "-" if missing. Nothing else on that line.
- Line 7 (Car): only year, make, and model—e.g. "2020 Nissan Altima". Nothing else.
- Line 8 (Color): ONLY the vehicle color. DMV/registration forms often show exactly THREE letters (e.g. GRY=gray, BLK=black, WHT=white, SIL=silver). Copy those three letters exactly in UPPERCASE—never drop a letter (wrong: GY; correct: GRY). Full words like Silver or Black are fine. If not stated, use "-". Never put city names (Brick, Jersey), addresses, or insurance names in color.
- If a value is missing or unreadable, put a single dash "-" for that line.
- Lines 12-17 must contain the phone number, price, special notes, email, and driver-license ID, each on its own line with the exact labels shown below. If a value is not visible, put a single dash "-".
- Line 16 (Email): a single email address only (e.g. john@example.com). Never invent an address. If none is visible, output "Email: -".
- Line 17 (DriverLicenseID): the customer's driver's-license / DMV ID exactly as printed (digits and/or letters). Never invent it. If none is visible, output "DriverLicenseID: -". Do NOT put the insurance policy number here.

Order and labels (one value per line, no extra text):
1) Full Name
2) Registration Address (street only)
3) Registration City, State, ZIP
4) Delivery address (street only)
5) Delivery city, State, ZIP
6) VIN (exactly 17 alphanumeric characters, never cut or add)
7) Car (year, make, model only)
8) Color
9) Insurance company
10) Insurance policy number
11) Delivery Date/Time and any extra info
12) Phone: <phone number>  (e.g. Phone: +1234567890)
13) Price: <price>  (e.g. Price: $250)
14) Issuer note: <note or ->  (e.g. Issuer note: Please double-check VIN)
15) Driver note: <note or ->  (e.g. Driver note: Call before arrival)
16) Email: <email address or ->  (e.g. Email: john.doe@example.com)
17) DriverLicenseID: <driver license / DMV id or ->  (e.g. DriverLicenseID: 123456789)

- For City, State, ZIP: use the standard two‑letter state abbreviation (e.g., "NY" not "New York"). Format as "City, ST 12345" (no extra comma before ZIP). Correct obvious misspellings only if you are certain (e.g., "Laurelton" not "Laurenton"). If you see a separate ZIP code line, merge it into the City line.
- For addresses: capitalise as appropriate, but do not invent missing parts.

Example (replace with actual values):
John Doe
123 Main St
Boston, MA 02101
456 Oak Ave
Cambridge, MA 02139
1HGBH41JXMN109186
2020 Toyota Camry
Silver
State Farm
123-456-789
Tomorrow 2pm, gate code 1234
Phone: +1234567890
Price: $150
Issuer note: -
Driver note: Ring the bell
Email: john.doe@example.com
DriverLicenseID: 123456789

Output nothing else—no explanation, no markdown, no line numbers. Only these 17 lines."""

MULTI_STRUCTURE_PROMPT = STRUCTURE_PROMPT.replace(
    "from an image or PDF page (screenshot, scan, or form).",
    "from multiple images (screenshots, scans, document photos, or rendered PDF pages). "
    "Merge information from ALL images into one result. If a field appears in more than one image, "
    "prefer the clearest and most complete value and resolve minor conflicts sensibly.",
)

# For freeform text: user can send any format; we ask the model to identify and rearrange into 11 lines
TEXT_STRUCTURE_PROMPT = """The user sent the following message. It may be in any format: paragraph, bullet list, different order, labels like "Name: John", etc. It also includes a phone number, a price (maybe with a $ sign), and possibly two notes (one for the tag issuer, one for the driver), an email, and a driver-license / DMV ID.

STRICT RULES:
- Output ONLY a plain text block with exactly 17 lines. One line per field—nothing else on that line.
- Lines 1-11 must contain ONLY the vehicle/delivery values. No phone numbers, no URLs, no extra text.
- Line 6 (VIN): exactly 17 alphanumeric characters (no spaces, no truncation, no extra digits). Or "-" if missing.
- Line 7 (Car): only year, make, and model—e.g. "2020 Nissan Altima". Nothing else.
- Line 8 (Color): ONLY the vehicle color. Three-letter DMV codes (GRY, BLK, etc.) are fine. If missing, use "-". Never put city names, addresses, or insurance names in color.
- If something is missing, put a single dash "-" for that line.
- Lines 12-17 must contain the phone number, price, notes, email, and driver-license ID, each with the labels exactly as shown below. If a value is not present, put a single dash "-".
- Line 16 (Email): a single email address only (e.g. john@example.com). Never invent one. If none, output "Email: -".
- Line 17 (DriverLicenseID): the customer's driver-license / DMV ID exactly as written. Never invent it. If none, output "DriverLicenseID: -". Do NOT put the insurance policy number here.

Order (one value per line, with labels for lines 12-17):
1) Full Name
2) Registration Address (street only)
3) Registration City, State, ZIP
4) Delivery address (street only)
5) Delivery city, State, ZIP
6) VIN (exactly 17 alphanumeric characters, never cut or add)
7) Car (year, make, model only)
8) Color
9) Insurance company
10) Insurance policy number
11) Delivery Date/Time and any extra info
12) Phone: <phone number>  (e.g. Phone: +1234567890)
13) Price: <price>  (e.g. Price: $250)
14) Issuer note: <note or ->  (e.g. Issuer note: Please double-check VIN)
15) Driver note: <note or ->  (e.g. Driver note: Call before arrival)
16) Email: <email address or ->  (e.g. Email: john.doe@example.com)
17) DriverLicenseID: <driver license / DMV id or ->  (e.g. DriverLicenseID: 123456789)

- For City, State, ZIP: use the standard two‑letter state abbreviation (e.g., "NY" not "New York"). Format as "City, ST 12345". Correct obvious misspellings only if you are certain.
- For addresses: capitalise appropriately, but do not invent missing parts.

Output nothing else—no explanation, no markdown, no line numbers. Only these 17 lines.

User message:
"""


def _call_openai_text(messages: list) -> Optional[str]:
    """Call OpenAI chat completions (text only). Returns assistant content or None."""
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed. pip install openai")
        return None
    from config import Config
    api_key = Config.OPENAI_API_KEY
    if not api_key or not api_key.strip():
        return None
    model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
    try:
        client = OpenAI(api_key=api_key.strip(), max_retries=0)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=1024,
        )
        text = (response.choices[0].message.content or "").strip()
        return text if text else None
    except AIVisionQuotaError:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        if "429" in err_msg or "insufficient_quota" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            logger.warning("OpenAI quota exceeded: %s", e)
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.exception("OpenAI text call failed: %s", e)
        return None


def extract_structured_from_text(user_message: str) -> Optional[str]:
    """
    Take freeform text from the user (any format/order) and return 11-line structured text
    suitable for parse_phase1_structured. Returns None if API not configured or request fails.
    """
    if not (user_message or user_message.strip()):
        return None
    prompt = TEXT_STRUCTURE_PROMPT + (user_message.strip()[:4000])
    return _call_openai_text([{"role": "user", "content": prompt}])


def extract_structured_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[str]:
    """
    Send image to OpenAI Vision and get back 11-line structured text suitable for parse_phase1_structured.
    Returns None if API is not configured or request fails.
    """
    from config import Config
    api_key = Config.OPENAI_API_KEY
    if not api_key or not api_key.strip():
        logger.warning("OPENAI_API_KEY not set; cannot process image.")
        return None

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": STRUCTURE_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=1024,
        )
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return None
        return text
    except AIVisionQuotaError:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        if "429" in err_msg or "insufficient_quota" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            logger.warning("AI vision quota exceeded: %s", e)
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.exception("AI vision extraction failed: %s", e)
        return None


def extract_structured_from_media_parts(parts: list[tuple[bytes, str]]) -> Optional[str]:
    """
    Run Phase 1 vision extraction over one or more images (PNG/JPEG bytes + MIME).

    PDFs should be converted to PNG (e.g. ``pdf_first_page_to_png_bytes``) before calling.
    Multiple parts are sent in a single multimodal request so the model can merge fields.
    """
    if not parts:
        return None
    cleaned = [(b, m) for b, m in parts if b and (m or "").strip()]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return extract_structured_from_image(cleaned[0][0], mime_type=cleaned[0][1] or "image/jpeg")

    from config import Config

    api_key = Config.OPENAI_API_KEY
    if not api_key or not api_key.strip():
        logger.warning("OPENAI_API_KEY not set; cannot process images.")
        return None

    max_parts = 12
    trimmed = cleaned[:max_parts]

    content: list[dict[str, Any]] = [{"type": "text", "text": MULTI_STRUCTURE_PROMPT}]
    for image_bytes, mime_type in trimmed:
        mt = (mime_type or "image/jpeg").strip()
        if not mt.startswith("image/"):
            mt = "image/jpeg"
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        data_url = f"data:{mt};base64,{b64}"
        content.append({"type": "image_url", "image_url": {"url": data_url}})

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key.strip(), max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": content}],
            max_tokens=1024,
        )
        text = (response.choices[0].message.content or "").strip()
        return text if text else None
    except AIVisionQuotaError:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        if "429" in err_msg or "insufficient_quota" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            logger.warning("AI vision quota exceeded (multi-image): %s", e)
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.exception("AI vision multi-image extraction failed: %s", e)
        return None


def pdf_first_page_to_png_bytes(pdf_bytes: bytes) -> Optional[bytes]:
    """
    Render the first page of a PDF to PNG bytes for vision extraction.
    Returns None if PyMuPDF is missing, the PDF is invalid, or has no pages.
    """
    if not pdf_bytes:
        return None
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.warning("PyMuPDF (pymupdf) not installed; cannot render PDF for Phase 1.")
        return None
    doc = None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if len(doc) < 1:
            return None
        page = doc[0]
        # ~150 DPI for readable text without huge payloads
        mat = fitz.Matrix(150 / 72, 150 / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    except Exception as e:
        logger.warning("pdf_first_page_to_png_bytes failed: %s", e)
        return None
    finally:
        if doc is not None:
            doc.close()


def extract_structured_from_pdf(pdf_bytes: bytes) -> Optional[str]:
    """
    Render the first PDF page to an image and run the same vision extraction as screenshots.
    Multi-page PDFs: only page 1 is used (send text or split if info is on later pages).
    """
    png = pdf_first_page_to_png_bytes(pdf_bytes)
    if not png:
        return None
    return extract_structured_from_image(png, mime_type="image/png")


# Vision prompt used to locate phone numbers for redaction.
# Returns coordinates as percentages of image dimensions so we can apply boxes
# without needing to know the image size on the model side.
PHONE_REDACTION_PROMPT = (
    "You are a privacy filter. Look at the image and find every region that "
    "contains a complete phone number — US or international, in any format. "
    "Examples include `+1 (732) 534-2659`, `732-534-2659`, `732.534.2659`, "
    "`+44 7712 345 678`, `(732) 534 2659`. Be generous: include the entire run "
    "of digits, separators, parentheses, country code, and surrounding labels "
    "such as 'Phone:' or 'Tel:'.\n\n"
    "Respond with ONLY a JSON object of the form:\n"
    "{\"boxes\": [{\"x1\": <float 0-100>, \"y1\": <float 0-100>, "
    "\"x2\": <float 0-100>, \"y2\": <float 0-100>}, ...]}\n\n"
    "Coordinates are percentages (0-100) of image width and height. "
    "Add ~1-2% padding around each phone-number region. "
    "If there are no phone numbers, return {\"boxes\": []}. "
    "Do not include any other keys, prose, code fences, or commentary."
)


@dataclass
class _PhoneRedactionResponse:
    boxes: list[tuple[float, float, float, float]]
    api_ok: bool  # True iff the model call completed and we parsed valid JSON


def _phone_redaction_boxes(
    image_bytes: bytes, mime_type: str = "image/jpeg"
) -> _PhoneRedactionResponse:
    """Ask OpenAI Vision for bounding boxes (as percentages) of visible phone numbers.

    Distinguishes "no phone numbers in image" (api_ok=True, boxes=[]) from
    "API/parse failed" (api_ok=False) so the caller can choose between
    forwarding the original and refusing to upload it.
    """
    from config import Config

    empty_ok = _PhoneRedactionResponse([], True)
    empty_fail = _PhoneRedactionResponse([], False)

    if not image_bytes:
        return empty_ok
    api_key = (getattr(Config, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return empty_fail

    mt = (mime_type or "image/jpeg").strip()
    if not mt.startswith("image/"):
        mt = "image/jpeg"
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mt};base64,{b64}"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PHONE_REDACTION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=500,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("phone redaction API call failed: %s", e)
        return empty_fail

    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return empty_fail
    raw_boxes = data.get("boxes")
    if not isinstance(raw_boxes, list):
        return empty_fail

    out: list[tuple[float, float, float, float]] = []
    for entry in raw_boxes:
        try:
            x1 = float(entry.get("x1"))
            y1 = float(entry.get("y1"))
            x2 = float(entry.get("x2"))
            y2 = float(entry.get("y2"))
        except (TypeError, ValueError, AttributeError):
            continue
        # Clamp + normalize ordering so x1<x2, y1<y2.
        x1c, x2c = max(0.0, min(100.0, min(x1, x2))), max(0.0, min(100.0, max(x1, x2)))
        y1c, y2c = max(0.0, min(100.0, min(y1, y2))), max(0.0, min(100.0, max(y1, y2)))
        # Pad a bit so the bar fully covers digits ascenders/descenders.
        pad = 1.0
        x1c = max(0.0, x1c - pad)
        y1c = max(0.0, y1c - pad)
        x2c = min(100.0, x2c + pad)
        y2c = min(100.0, y2c + pad)
        if x2c - x1c < 0.5 or y2c - y1c < 0.5:
            continue
        out.append((x1c, y1c, x2c, y2c))
    return _PhoneRedactionResponse(out, True)


@dataclass
class PhoneRedactionResult:
    """Outcome of a single image redaction attempt.

    Attributes:
        image_bytes: Bytes to send (always set; equals input if api failed).
        api_ok: True if the AI call completed; False on quota/parse/network failures.
        redacted: True if at least one black rectangle was drawn.
    """

    image_bytes: bytes
    api_ok: bool
    redacted: bool


def redact_phones_in_image_bytes(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
) -> PhoneRedactionResult:
    """Paint solid black rectangles over phone numbers detected by OpenAI Vision.

    Falls back gracefully when Pillow is unavailable — the original bytes are
    returned with ``api_ok=False`` so the caller can decide whether to upload.
    """
    if not image_bytes:
        return PhoneRedactionResult(image_bytes or b"", False, False)
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.warning("Pillow not available; cannot redact image: %s", e)
        return PhoneRedactionResult(image_bytes, False, False)

    resp = _phone_redaction_boxes(image_bytes, mime_type=mime_type)
    if not resp.api_ok:
        return PhoneRedactionResult(image_bytes, False, False)
    if not resp.boxes:
        return PhoneRedactionResult(image_bytes, True, False)

    import io as _io

    try:
        img = Image.open(_io.BytesIO(image_bytes))
        img.load()
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        w, h = img.size
        for x1p, y1p, x2p, y2p in resp.boxes:
            x1 = int(round(x1p / 100.0 * w))
            y1 = int(round(y1p / 100.0 * h))
            x2 = int(round(x2p / 100.0 * w))
            y2 = int(round(y2p / 100.0 * h))
            if x2 <= x1 or y2 <= y1:
                continue
            draw.rectangle([x1, y1, x2, y2], fill=(0, 0, 0))
        out = _io.BytesIO()
        save_format = "PNG"
        save_kwargs: dict[str, Any] = {}
        if (mime_type or "").lower() in ("image/jpeg", "image/jpg"):
            save_format = "JPEG"
            save_kwargs["quality"] = 92
            if img.mode == "RGBA":
                img = img.convert("RGB")
        img.save(out, format=save_format, **save_kwargs)
        return PhoneRedactionResult(out.getvalue(), True, True)
    except Exception as e:
        logger.warning("redact_phones_in_image_bytes failed: %s", e)
        return PhoneRedactionResult(image_bytes, False, False)


# OCR/models sometimes drop one letter from standard 3-letter DMV color codes → repair before storage.
_TWO_LETTER_DMV_TO_THREE = {
    "gy": "GRY",   # gray
    "bk": "BLK",   # black
    "wh": "WHT",   # white
    "si": "SIL",   # silver
}


def normalize_phase1_color(val: str) -> str:
    """Normalize extracted color: preserve 3-letter DMV codes (uppercase), repair common 2-letter truncations."""
    s = (val or "").strip()
    if not s or s == "-":
        return s
    compact = "".join(s.split())
    if not compact:
        return s
    if len(compact) == 2 and compact.isalpha():
        fixed = _TWO_LETTER_DMV_TO_THREE.get(compact.lower())
        if fixed:
            return fixed
        return compact.upper()
    if len(compact) == 3 and compact.isalpha():
        return compact.upper()
    if len(compact) <= 24 and " " not in s and compact.isalpha():
        return compact.title() if len(compact) > 3 else compact.upper()
    return s


def _has_value(val: str) -> bool:
    """True if field has a non-empty value (not blank or single dash)."""
    return bool(val and str(val).strip() and str(val).strip() != "-")


# RFC 5322-lite: good enough to detect "looks like an email".
EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def normalize_email(val: str) -> str:
    """Return a clean lowercase email if val looks valid, else ''."""
    s = (val or "").strip().rstrip(",.;:").strip("<>\"' ")
    if not s or s == "-" or s.lower() in ("none", "n/a", "na", "unknown"):
        return ""
    s = s.lower()
    if EMAIL_PATTERN.match(s):
        return s
    return ""


def normalize_driver_license_id(val: str) -> str:
    """Driver-license / AAMVA DAQ id: keep alphanumeric, dashes, spaces; trim and uppercase.
    Returns '' when blank/dash/placeholder.
    """
    s = (val or "").strip()
    if not s or s == "-" or s.lower() in ("none", "n/a", "na", "unknown", "tbd", "pending"):
        return ""
    # Drop anything that's clearly not part of an ID (e.g. labels left over)
    cleaned = re.sub(r"[^A-Za-z0-9\-\s]", "", s).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.upper()[:80]


def validate_phase1_extraction(normalized_text: str, state_data: dict) -> tuple[bool, list[str]]:
    """
    Run built-in checks on AI-extracted Phase 1 data.
    Returns (is_valid, list of error messages).
    We accept >= 11 lines (use first 11); VIN format is not enforced so extraction still parses.
    """
    errors: list[str] = []

    # 1) Line count: need at least 11 lines; we use first 11, extra lines are ignored
    lines = [ln.strip() for ln in normalized_text.splitlines() if ln.strip()]
    if len(lines) < PHASE1_LINE_COUNT:
        errors.append(
            f"Expected at least 11 lines from the extraction, got {len(lines)}. "
            "Please send as text or try another image or PDF."
        )

    # 2) Required fields (name and at least one delivery field)
    name = (state_data.get("name") or "").strip()
    if not _has_value(name):
        errors.append("Full name is missing or unreadable.")

    delivery_addr = (state_data.get("delivery_address") or "").strip()
    delivery_csz = (state_data.get("delivery_city_state_zip") or "").strip()
    if not _has_value(delivery_addr) and not _has_value(delivery_csz):
        errors.append("Delivery address and Delivery city/state/ZIP are both missing or unreadable.")

    # VIN format is not enforced – whatever the AI extracted is kept (no block on "Bronx New York" etc.)

    return (len(errors) == 0, errors)


# Values we treat as "no real color" – placeholders, unknowns, or common mis-extractions
# (city / address / insurance nouns that sometimes get pulled into the color field).
# NOTE: "brick" intentionally omitted — it's a valid color name.
COLOR_PLACEHOLDERS = frozenset({
    "-", "n/a", "na", "?", "??", "unknown", "none", "tbd", "pending",
    "not specified", "not provided", "blank", "x", "xx", "xxx",
    "road", "avenue", "island", "delivery", "jersey",
    "safeco", "state", "farm", "geico", "progressive", "allstate",
    "address", "street", "number", "digits", "vin",
})

# Well-known vehicle colors (full words and common 3-letter DMV codes).
# When the extracted color matches one of these, we trust it without an AI recheck.
_TRUSTED_COLOR_WORDS = frozenset({
    # full words
    "black", "white", "silver", "gray", "grey", "red", "blue",
    "green", "yellow", "orange", "brown", "beige", "tan", "gold",
    "bronze", "maroon", "burgundy", "charcoal", "champagne",
    "navy", "ivory", "pearl", "cream", "purple", "teal", "copper",
    "crimson", "brick",
    # DMV registration codes
    "blk", "wht", "sil", "gry", "red", "blu", "grn", "ylw",
    "org", "brn", "gld", "tan", "pur", "ppl",
})

# Field labels for user-friendly missing-field prompts
MISSING_FIELD_PROMPTS = {
    "color": ("You missed out the vehicle color. Please provide the exact vehicle color for accurate data.", "color"),
    "vin": ("You missed out the VIN. Can you add it?", "vin"),
    "car": ("You missed out the car (year/make/model). Can you add it?", "car"),
    "insurance_company": ("You missed out the insurance company. Can you add it?", "insurance_company"),
    "delivery_date": ("You missed out the delivery date/time. Can you add it?", "extra_info"),
}


def _ai_check_color_in_raw(extracted_color: str, raw_input: str) -> bool:
    """
    Use AI to check if vehicle color is genuinely in the raw message.
    Returns True if color is missing (we should prompt for it).
    """
    try:
        from config import Config
        if not Config.OPENAI_API_KEY or not str(Config.OPENAI_API_KEY).strip():
            return False
    except Exception:
        return False
    prompt = (
        "In the raw message below, was the VEHICLE COLOR explicitly stated?\n\n"
        "STRICT: Reply 'missing' if the user did NOT clearly provide a vehicle color. "
        "Reply 'missing' if the extracted value is a city name (e.g. Brick, Jersey), address word (road, avenue, island), "
        "insurance name (Safeco), or any placeholder. "
        "Reply 'ok' for full color names (Silver, Black, White, Red, Blue) OR standard 3-letter DMV/registration codes "
        "(e.g. GRY=gray, BLK=black, WHT=white, SIL=silver, RED, BLU)—these count as valid colors.\n\n"
        f"Extracted color: '{extracted_color}'\n\n"
        f"Raw message:\n{raw_input[:600]}\n\n"
        "Reply with exactly: missing  OR  ok"
    )
    try:
        out = _call_openai_text([{"role": "user", "content": prompt}])
        if not out:
            return False
        return "missing" in out.strip().lower()
    except Exception:
        return False


def _has_valid_color(val: str) -> bool:
    """True if color field has a real value (not blank, dash, or placeholder)."""
    v = (val or "").strip().lower()
    if not v:
        return False
    if v in COLOR_PLACEHOLDERS:
        return False
    # Reject very short/generic values
    if len(v) < 2:
        return False
    return True


def _color_is_trusted(val: str) -> bool:
    """True when the color is a clear well-known color word (no AI check needed)."""
    v = (val or "").strip().lower()
    if not v:
        return False
    if v in _TRUSTED_COLOR_WORDS:
        return True
    # Multi-word colors like "brick red", "pearl white", "sea blue" – any token matches.
    tokens = [t for t in re.split(r"[^a-z]+", v) if t]
    if tokens and any(t in _TRUSTED_COLOR_WORDS for t in tokens):
        return True
    return False


def _ai_other_missing_fields(state_data: dict, raw_input: str) -> list[str]:
    """Ask OpenAI which non-color fields are still missing. Never early-returns color."""
    try:
        from config import Config
        if not Config.OPENAI_API_KEY or not str(Config.OPENAI_API_KEY).strip():
            return []
    except Exception:
        return []

    def _has_val(key: str) -> bool:
        v = (state_data.get(key) or "").strip()
        return bool(v and v != "-")

    prompt = (
        "Vehicle/lead info extracted:\n"
        f"Name: {state_data.get('name') or '-'}\n"
        f"VIN: {state_data.get('vin') or '-'}\n"
        f"Car: {state_data.get('car') or '-'}\n"
        f"Color: {state_data.get('color') or '-'}\n"
        f"Insurance: {state_data.get('insurance_company') or '-'}\n"
        f"Extra/Delivery time: {state_data.get('extra_info') or '-'}\n\n"
        "Raw message: " + (raw_input[:600] or "") + "\n\n"
        "Which fields are MISSING or invalid? Any of -, N/A, ?, unknown, TBD, or placeholder counts as missing. "
        "Reply with ONLY a comma-separated list from: vin, car, insurance_company, delivery_date. "
        "If none missing, reply: none"
    )
    try:
        out = _call_openai_text([{"role": "user", "content": prompt}])
        if not out or not out.strip():
            return []
        out = out.strip().lower()
        if "none" in out:
            return []
        missing = []
        for w in out.replace(",", " ").split():
            w = w.strip()
            if w in ("vin", "car", "insurance_company", "delivery_date") and w not in missing:
                if w == "delivery_date" and _has_val("extra_info"):
                    continue
                if w in ("vin", "car", "insurance_company") and _has_val(w):
                    continue
                missing.append(w)
        return missing
    except Exception as e:
        logger.warning("_ai_other_missing_fields: %s", e)
        return []


def detect_missing_fields(state_data: dict, raw_input: str) -> list[str]:
    """
    Detect ALL missing fields in one pass (color, VIN, car, insurance, delivery date).
    Fields are returned in prompt order so the bot can ask them sequentially.
    """
    missing: list[str] = []

    def _has_val(key: str) -> bool:
        v = (state_data.get(key) or "").strip()
        return bool(v and v != "-")

    # Color – trust well-known color words, otherwise validate against placeholders + AI.
    color_val = (state_data.get("color") or "").strip()
    color_is_good = _has_valid_color(color_val)
    if color_is_good and not _color_is_trusted(color_val) and raw_input:
        if _ai_check_color_in_raw(color_val, raw_input):
            color_is_good = False
    if not color_is_good:
        missing.append("color")

    # Deterministic local checks for other fields so they still get asked when OPENAI isn't set.
    if not _has_val("vin"):
        missing.append("vin")
    if not _has_val("car"):
        missing.append("car")
    if not _has_val("insurance_company"):
        missing.append("insurance_company")

    # Optional AI pass — adds anything local check missed and filters false positives.
    for f in _ai_other_missing_fields(state_data, raw_input):
        if f not in missing:
            missing.append(f)

    return missing


def _parse_json_from_model(text: str) -> Optional[dict[str, Any]]:
    """Parse JSON from model output; tolerate ```json fences."""
    if not text or not str(text).strip():
        return None
    t = str(text).strip()
    if t.startswith("```"):
        t = re.sub(r"^```\w*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", t)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None


def _lead_price_to_float(raw: Optional[str]) -> Optional[float]:
    """Best-effort parse of lead price field (e.g. '$1,200', '1200', '1,200.50')."""
    if not raw:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    # Keep digits and at most one decimal point
    cleaned = ""
    dot_seen = False
    for c in s:
        if c.isdigit():
            cleaned += c
        elif c == "." and not dot_seen:
            cleaned += c
            dot_seen = True
    if not cleaned:
        return None
    try:
        v = float(cleaned)
        return v if v > 0 else None
    except ValueError:
        return None


def _usd_amounts_match(expected: float, amounts: list) -> bool:
    """True if any parsed amount matches expected within $3 or 2% (whichever is larger)."""
    if expected <= 0:
        return True
    exp_cents = int(round(expected * 100))
    tol_cents = max(300, int(abs(expected * 100) * 0.02))  # $3 or 2%
    for a in amounts:
        try:
            x = float(a)
            got = int(round(x * 100))
            if abs(got - exp_cents) <= tol_cents:
                return True
        except (TypeError, ValueError):
            continue
    return False


RECEIPT_VISION_PROMPT = """You verify images drivers upload as PAYMENT RECEIPTS for completed deliveries.

Return ONLY valid JSON (no markdown, no explanation outside JSON):
{
  "looks_like_receipt": true or false,
  "confidence": <integer 0-100>,
  "has_dollar_sign": true or false,
  "amounts_usd": [<numbers>],
  "note": "<one short English sentence>"
}

Rules:
- looks_like_receipt: true only if this clearly shows a real payment document: printed or digital receipt, invoice, cashier slip, card/terminal receipt, payment confirmation screenshot, bank app payment detail with amount, etc.
- Set looks_like_receipt to false for: random photos, memes, selfies, vehicle photos with no payment info, blank/blurry unusable images, chat screenshots with no payment line, unrelated documents.
- has_dollar_sign: true only if the ASCII dollar symbol $ is clearly visible as a currency marker on the receipt or payment screen (not guessed). False if the image uses only "USD" text, foreign currency, or no currency symbol.
- amounts_usd: list every total or payment amount in US dollars visible (e.g. 1200, 99.5). Use numbers only. If no amount is readable, use [].
- confidence: how sure you are that this is a legitimate payment/receipt image (not random upload).
"""

# Strict mode: do not prioritize matching dollar amounts — only receipt-like image + visible ASCII $ .
RECEIPT_VISION_PROMPT_STRICT = """You verify images drivers upload as PAYMENT RECEIPTS (strict mode: dollar sign check only).

Return ONLY valid JSON (no markdown, no explanation outside JSON):
{
  "looks_like_receipt": true or false,
  "confidence": <integer 0-100>,
  "has_dollar_sign": true or false,
  "amounts_usd": [],
  "note": "<one short English sentence>"
}

Rules:
- looks_like_receipt: true if this clearly shows a real payment document or payment screen (receipt, invoice, terminal slip, app payment confirmation, etc.). False for unrelated images.
- has_dollar_sign: true ONLY if the ASCII character $ appears visibly on the image as a currency marker. False if only "USD" as letters, only numbers, €, £, or no dollar sign.
- Always set amounts_usd to [] — amounts are NOT evaluated in this mode.
- confidence: how sure you are that this is a payment/receipt image.
"""


def validate_driver_receipt_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    expected_price_text: Optional[str] = None,
    detection_mode: str = "lax",
) -> ReceiptValidationResult:
    """
    Use OpenAI vision to ensure the image looks like a receipt.

    detection_mode:
    - ``lax``: if the lead has a price, require readable USD amount(s) that match within tolerance.
    - ``strict``: require a visible ``$`` on the image; do not compare amounts to the lead price.

    If OPENAI_API_KEY is not set, returns accept=True (validation skipped).
    On model/API failure to produce JSON, fails open (accept=True) with a log line.
    Raises AIVisionQuotaError on quota/rate limit (caller should ask user to retry).
    """
    from config import Config

    if not image_bytes:
        return ReceiptValidationResult(False, "❌ Empty image. Please send a photo of the receipt.")

    if not Config.OPENAI_API_KEY or not str(Config.OPENAI_API_KEY).strip():
        logger.info("validate_driver_receipt_image: OPENAI_API_KEY not set; skipping AI receipt check")
        return ReceiptValidationResult(True, "")

    mode = (detection_mode or "lax").strip().lower()
    if mode not in ("strict", "lax"):
        mode = "lax"
    vision_prompt = RECEIPT_VISION_PROMPT_STRICT if mode == "strict" else RECEIPT_VISION_PROMPT
    logger.info("validate_driver_receipt_image: using mode=%s (strict uses $-only prompt)", mode)

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"

    try:
        from openai import OpenAI

        client = OpenAI(api_key=str(Config.OPENAI_API_KEY).strip(), max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": vision_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            max_tokens=500,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        err_msg = str(e).lower()
        if "429" in err_msg or "insufficient_quota" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            logger.warning("Receipt validation quota exceeded: %s", e)
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.warning("validate_driver_receipt_image API error (allowing upload): %s", e)
        return ReceiptValidationResult(True, "")

    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        logger.warning("validate_driver_receipt_image: could not parse JSON; allowing upload")
        return ReceiptValidationResult(True, "")

    looks = data.get("looks_like_receipt")
    confidence = data.get("confidence")
    try:
        conf_int = int(confidence) if confidence is not None else 70
    except (TypeError, ValueError):
        conf_int = 70

    has_dollar = data.get("has_dollar_sign")
    if has_dollar is not True and has_dollar is not False:
        has_dollar = None

    amounts_raw = data.get("amounts_usd") or []
    amounts: list[float] = []
    if isinstance(amounts_raw, list):
        for x in amounts_raw:
            try:
                amounts.append(float(x))
            except (TypeError, ValueError):
                continue

    if looks is not True or conf_int < 38:
        msg = (
            "❌ This doesn't look like a payment receipt or confirmation.\n\n"
            "Please upload a clear photo of the actual receipt or payment screen showing the total."
        )
        return ReceiptValidationResult(False, msg)

    if mode == "strict":
        if has_dollar is not True:
            return ReceiptValidationResult(
                False,
                "❌ We need a visible **$** (dollar sign) on the receipt or payment screen.\n\n"
                "Please upload a clearer image where the dollar symbol appears on the document.",
            )
        return ReceiptValidationResult(True, "")

    # lax: optional amount match when lead has a price (never runs in strict mode above)
    expected = _lead_price_to_float(expected_price_text)
    if expected is not None and expected > 0:
        if not amounts:
            return ReceiptValidationResult(
                False,
                "❌ We couldn't read a payment amount on this image.\n\n"
                "Please upload a clearer photo where the total/paid amount is visible.",
            )
        if not _usd_amounts_match(expected, amounts):
            exp_show = (expected_price_text or "").strip() or f"{expected:.2f}"
            return ReceiptValidationResult(
                False,
                "❌ The amount on this image doesn't match the lead price.\n\n"
                f"Expected for this lead: {exp_show}\n\n"
                "Upload the receipt that shows that total, or contact dispatch if the price changed.",
            )

    return ReceiptValidationResult(True, "")
