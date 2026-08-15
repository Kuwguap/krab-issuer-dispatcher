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


def transcribe_voice(audio_bytes: bytes, filename: str = "voice.ogg") -> Optional[str]:
    """Transcribe a voice/audio note to text via OpenAI (Whisper by default).

    Telegram voice notes are OGG/Opus, which Whisper accepts directly. Returns None
    on any error (missing key, quota, network) so callers can fall back to text.
    """
    from config import Config
    import io as _io

    if not audio_bytes:
        return None
    api_key = (getattr(Config, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, max_retries=1)
        model = (getattr(Config, "OPENAI_TRANSCRIBE_MODEL", "") or "whisper-1").strip() or "whisper-1"
        buf = _io.BytesIO(audio_bytes)
        buf.name = filename  # the SDK infers the audio format from the filename
        resp = client.audio.transcriptions.create(model=model, file=buf)
        text = (getattr(resp, "text", "") or "").strip()
        return text or None
    except Exception as e:
        logger.warning("Voice transcription failed: %s", e)
        return None


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
- Line 1 (Full Name): the registered owner. Can be either a PERSON (e.g. "John Doe", "Isabelle Reyes") OR a BUSINESS/COMPANY (e.g. "Global Transport LLC", "ABC Trucking Inc", "Smith & Sons Co"). PRESERVE company suffixes verbatim — never drop "LLC", "L.L.C.", "Inc", "Inc.", "Corp", "Corporation", "Ltd", "Ltd.", "Co.", "Company", "PLLC", "LP", "LLP", "PC", "Trust", "Group", "Holdings". If BOTH a personal owner and a business name appear (e.g. an officer/DBA combo), put the personal "First Last" FIRST, then a space, then the company name (e.g. "John Doe Global Transport LLC"). If only a business name is visible, output just the business name. Title-case personal names; preserve the registered spelling of the business name (do not lowercase or rearrange it). Never invent a person if only a company is on file.
- Line 6 (VIN): exactly 17 alphanumeric characters (no spaces, no truncation, no extra digits). Or "-" if missing. Nothing else on that line.
- Line 7 (Car): only year, make, and model—e.g. "2020 Nissan Altima". Nothing else.
- Line 8 (Color): ONLY the vehicle color. DMV/registration forms often show exactly THREE letters (e.g. GRY=gray, BLK=black, WHT=white, SIL=silver). Copy those three letters exactly in UPPERCASE—never drop a letter (wrong: GY; correct: GRY). Full words like Silver or Black are fine. If not stated, use "-". Never put city names (Brick, Jersey), addresses, or insurance names in color.
- If a value is missing or unreadable, put a single dash "-" for that line.
- Lines 12-17 must contain the phone number, price, special notes, email, and driver-license ID, each on its own line with the exact labels shown below. If a value is not visible, put a single dash "-".
- Line 12 (Phone): ONLY use a number that is EXPLICITLY labelled as a phone number in the source — e.g. "Phone", "Phone:", "Phone #", "Client phone", "Client #", "Client phone #", "Tel", "Tel.", "Cell", "Mobile", "Contact". The label may be on the same line OR on the line immediately above the number. NEVER copy the **insurance policy number**, **VIN**, **license / DMV ID**, **plate**, **account**, **reference id**, **ZIP code**, or any unlabelled digit run into the Phone field. If no clearly-labelled phone is visible, output "Phone: -".
- Line 16 (Email): a single email address only (e.g. john@example.com). Never invent an address. If none is visible, output "Email: -".
- Line 17 (DriverLicenseID): the customer's driver's-license / DMV ID exactly as printed (digits and/or letters). Never invent it. If none is visible, output "DriverLicenseID: -". Do NOT put the insurance policy number here.

Order and labels (one value per line, no extra text):
1) Full Name (person, business, or "First Last Business Name" when both are present)
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
- Line 1 (Full Name): the registered owner. Accept either a PERSON (e.g. "John Doe", "Isabelle Reyes") OR a BUSINESS/COMPANY (e.g. "Global Transport LLC", "ABC Trucking Inc", "Smith & Sons Co"). PRESERVE business suffixes exactly as written — never drop "LLC", "L.L.C.", "Inc", "Inc.", "Corp", "Corporation", "Ltd", "Ltd.", "Co.", "Company", "PLLC", "LP", "LLP", "PC", "Trust", "Group", "Holdings". Users put first name + last name (when there is one) FIRST, then the business name — keep that order. So "John Doe Global Transport LLC" stays as "John Doe Global Transport LLC"; "Global Transport LLC" stays as "Global Transport LLC"; "Isabelle Reyes" stays as "Isabelle Reyes". Never invent a person when only a company name is given, and never strip a company name when a person is also mentioned.
- Line 6 (VIN): exactly 17 alphanumeric characters (no spaces, no truncation, no extra digits). Or "-" if missing.
- Line 7 (Car): only year, make, and model—e.g. "2020 Nissan Altima". Nothing else.
- Line 8 (Color): ONLY the vehicle color. Three-letter DMV codes (GRY, BLK, etc.) are fine. If missing, use "-". Never put city names, addresses, or insurance names in color.
- If something is missing, put a single dash "-" for that line.
- Lines 12-17 must contain the phone number, price, notes, email, and driver-license ID, each with the labels exactly as shown below. If a value is not present, put a single dash "-".
- Line 12 (Phone): ONLY use a number that the user EXPLICITLY calls a phone — e.g. "Phone", "Phone #", "Client phone", "Client #", "Tel", "Cell", "Mobile", "Contact". Never copy the insurance policy number, VIN, license / DMV ID, plate, account, reference id, or ZIP into the Phone field. If no clearly-labelled phone is given, output "Phone: -".
- Line 16 (Email): a single email address only (e.g. john@example.com). Never invent one. If none, output "Email: -".
- Line 17 (DriverLicenseID): the customer's driver-license / DMV ID exactly as written. Never invent it. If none, output "DriverLicenseID: -". Do NOT put the insurance policy number here.

Order (one value per line, with labels for lines 12-17):
1) Full Name (person, business, or "First Last Business Name" when both are present)
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


FOLLOWUP_EXTRACT_PROMPT = (
    "Extract the client's contact details from the text below (any format/order). "
    "Reply with ONLY a JSON object exactly like "
    '{"name": "", "phone": "", "email": "", "notes": ""}. '
    "Rules: name = the client's full name; phone = their phone number digits; "
    "email = their email address; notes = one short line with any other useful "
    "details (vehicle, quote/price, missing VIN, address, timing). "
    "Use an empty string for anything not present.\n\nTEXT:\n"
)


def extract_followup_fields(user_message: str) -> Optional[dict]:
    """AI-parse a freeform /followup paste into {name, phone, email, notes}.

    Returns None when the API is unconfigured/fails so the caller can fall back
    to regex extraction. Values are stripped; missing fields come back as None.
    """
    txt = (user_message or "").strip()
    if not txt:
        return None
    raw = _call_openai_text([
        {"role": "user", "content": FOLLOWUP_EXTRACT_PROMPT + txt[:4000]}
    ])
    if not raw:
        return None
    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return None
    out: dict = {}
    for k in ("name", "phone", "email", "notes"):
        v = str(data.get(k) or "").strip()
        out[k] = v or None
    if out.get("email"):
        out["email"] = normalize_email(out["email"]) or out["email"]
    return out


SUPERVISOR_ROUTER_PROMPT = (
    "You are the intent router for a car-tag dispatch Telegram bot. A SUPERVISOR "
    "typed or dictated the message below. Classify what they want and reply with "
    "ONLY a JSON object like {\"intent\": \"\", \"args\": {}}.\n\n"
    "Valid intents:\n"
    "- \"lead\": the message is CLIENT/VEHICLE INFO or a request to add/create/start "
    "a lead/tag/client/sale (e.g. a name, address, VIN, or \"add a lead for ...\"). "
    "When unsure, choose this.\n"
    "- \"list_groups\": asking which groups/teams exist or are active.\n"
    "- \"list_drivers\": asking which drivers exist or are active.\n"
    "- \"list_suspended\": asking who is suspended / owes receipts and is blocked.\n"
    "- \"pending_receipts\": asking who owes receipts / outstanding receipt debt.\n"
    "- \"usage\": asking who has been sending leads / lead activity / recent stats.\n"
    "- \"lead_lookup\": asking about ONE lead by its reference id. args: "
    "{\"reference\": \"<the id>\"}.\n"
    "- \"driverblock\": turn driver phone-number redaction on or off. args: "
    "{\"enable\": true|false}.\n"
    "- \"group_status\": enable or disable a group/team by name. args: "
    "{\"name\": \"<group name>\", \"enable\": true|false}.\n"
    "- \"driver_status\": activate or deactivate a driver by name. args: "
    "{\"name\": \"<driver name>\", \"active\": true|false}.\n"
    "- \"broadcast\": send an announcement to everyone. args: {\"message\": \"<text>\"}.\n"
    "- \"set_plate\": change a temp-tag / plate counter to a number. args: "
    "{\"which\": \"resident_plate\"|\"nonresident_plate\"|\"resident_control\"|"
    "\"nonresident_control\", \"number\": \"<digits only>\"}. Mapping: \"resident\" / "
    "\"NJ\" / \"in-state\" / \"H\" tags are resident; \"non-resident\" / \"out of state\" / "
    "\"V\" tags are nonresident. \"tag number\" or \"plate number\" -> *_plate; \"control "
    "number\" -> *_control. Strip any leading H or trailing V letter — put ONLY the digits "
    "in number (e.g. \"H553300\" -> \"553300\").\n"
    "- \"set_plate_from_image\": the supervisor wants to read/update a temp-tag / plate "
    "number from a PICTURE or PDF they will send or attach (e.g. \"update the tag number "
    "from a picture\", \"read the temp tag off this pdf\", \"scan my tag photo\"). args: {} "
    "(the number is in the image, not the text).\n"
    "- \"help\": asking what the bot can do.\n"
    "- \"none\": small talk or unclear.\n\n"
    "Rules: return exactly one intent. Put only the requested keys in args, as an "
    "empty object {} when none apply. Do NOT invent names or ids not present in the "
    "message. Booleans must be real JSON true/false.\n\nMESSAGE:\n"
)


def classify_supervisor_command(user_message: str) -> Optional[dict]:
    """AI-classify a supervisor's freeform message into a router intent + args.

    Returns {"intent": str, "args": dict} or None when the API is unconfigured/fails
    (caller then falls back to normal handling). ``intent`` is always a non-empty
    string from the known set; unknown labels collapse to "none".
    """
    txt = (user_message or "").strip()
    if not txt:
        return None
    raw = _call_openai_text([
        {"role": "user", "content": SUPERVISOR_ROUTER_PROMPT + txt[:2000]}
    ])
    if not raw:
        return None
    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return None
    intent = str(data.get("intent") or "").strip().lower()
    known = {
        "lead", "list_groups", "list_drivers", "list_suspended", "pending_receipts",
        "usage", "lead_lookup", "driverblock", "group_status", "driver_status",
        "broadcast", "set_plate", "set_plate_from_image", "help", "none",
    }
    if intent not in known:
        intent = "none"
    args = data.get("args")
    if not isinstance(args, dict):
        args = {}
    return {"intent": intent, "args": args}


# Appended to the vision request when the sender typed a message alongside the
# file(s) (photo caption). Typed text is authoritative for phone/price; images
# stay authoritative for the VIN (people mistype VINs far more than cameras).
TYPED_TEXT_NOTE = (
    "The sender also typed this message alongside the file(s). Use it as "
    "additional context when filling the lines. For Phone and Price, prefer "
    "the typed text over the images. For VIN, prefer the images:\n\n"
)


def extract_structured_from_image(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    typed_text: str | None = None,
) -> Optional[str]:
    """
    Send image to OpenAI Vision and get back 11-line structured text suitable for parse_phase1_structured.
    ``typed_text`` (e.g. the photo's caption) is passed to the model as extra context.
    Returns None if API is not configured or request fails.
    """
    from config import Config
    api_key = Config.OPENAI_API_KEY
    if not api_key or not api_key.strip():
        logger.warning("OPENAI_API_KEY not set; cannot process image.")
        return None

    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"

    content: list[dict[str, Any]] = [
        {"type": "text", "text": STRUCTURE_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if typed_text and typed_text.strip():
        content.append({"type": "text", "text": TYPED_TEXT_NOTE + typed_text.strip()[:4000]})

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


def extract_structured_from_media_parts(
    parts: list[tuple[bytes, str]],
    typed_text: str | None = None,
) -> Optional[str]:
    """
    Run Phase 1 vision extraction over one or more images (PNG/JPEG bytes + MIME).

    PDFs should be converted to PNG (e.g. ``pdf_first_page_to_png_bytes``) before calling.
    Multiple parts are sent in a single multimodal request so the model can merge fields.
    ``typed_text`` (joined photo captions) rides along as extra model context.
    """
    if not parts:
        return None
    cleaned = [(b, m) for b, m in parts if b and (m or "").strip()]
    if not cleaned:
        return None
    if len(cleaned) == 1:
        return extract_structured_from_image(
            cleaned[0][0], mime_type=cleaned[0][1] or "image/jpeg", typed_text=typed_text
        )

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
    if typed_text and typed_text.strip():
        content.append({"type": "text", "text": TYPED_TEXT_NOTE + typed_text.strip()[:4000]})

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


PLATE_READ_PROMPT = (
    "This image shows a temporary vehicle tag / paper license plate. Read the single "
    "main plate (tag) number printed on it. New Jersey RESIDENT temp tags look like "
    "H###### (the letter H followed by digits); NON-RESIDENT tags look like ######V "
    "(digits followed by the letter V). Reply with ONLY a JSON object exactly like "
    '{"plate": "H553300", "number": "553300", "kind": "resident"}. '
    "Rules: number = the digits only, no letters; kind = \"resident\" if the tag starts "
    "with H, \"nonresident\" if it ends with V, otherwise \"unknown\". If you cannot "
    "clearly read a plate, use empty strings and kind \"unknown\"."
)


def extract_plate_number_from_image(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[dict]:
    """Read a temp-tag plate number from an image (PNG/JPEG bytes).

    Returns {"plate": str, "number": <digits>, "kind": "resident"|"nonresident"|"unknown"}
    or None when the API is unconfigured / unreadable. Raises AIVisionQuotaError on quota.
    """
    from config import Config
    api_key = Config.OPENAI_API_KEY
    if not api_key or not api_key.strip():
        return None
    mt = (mime_type or "image/jpeg").strip()
    if not mt.startswith("image/"):
        mt = "image/jpeg"
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    data_url = f"data:{mt};base64,{b64}"
    content = [
        {"type": "text", "text": PLATE_READ_PROMPT},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key.strip(), max_retries=0)
        model = getattr(Config, "OPENAI_VISION_MODEL", None) or "gpt-4o"
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": content}], max_tokens=300,
        )
        raw = (response.choices[0].message.content or "").strip()
    except AIVisionQuotaError:
        raise
    except Exception as e:
        err_msg = str(e).lower()
        if "429" in err_msg or "insufficient_quota" in err_msg or "quota" in err_msg or "rate limit" in err_msg:
            raise AIVisionQuotaError("API quota exceeded") from e
        logger.exception("Plate-number vision read failed: %s", e)
        return None
    if not raw:
        return None
    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return None
    number = re.sub(r"\D", "", str(data.get("number") or ""))
    plate = str(data.get("plate") or "").strip()
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("resident", "nonresident"):
        up = plate.upper()
        if up.startswith("H"):
            kind = "resident"
        elif up.endswith("V"):
            kind = "nonresident"
        else:
            kind = "unknown"
    if not number:
        return None
    return {"plate": plate, "number": number, "kind": kind}


# The model sees a normalized large-format rendering so percentage boxes round-trip
# cleanly when we paint over the original image. Larger render = more accurate digit
# localisation, especially for small print in screenshots / scans.
PHONE_REDACTION_RENDER_WIDTH = 1600


def _build_phone_redaction_prompt(
    target_phones: list[str],
    img_w: int,
    img_h: int,
) -> str:
    """Compose the AI prompt; if we already extracted the lead phone, anchor on it.

    The AI is asked for **center coordinates + the exact phone text + the
    digit height in pixels** for every phone it sees. Python then computes a
    tight rectangle around the centre point using the digit count and digit
    height (rather than trusting the AI to draw a correctly-sized box, which
    is the main source of oversized / mis-located rectangles).
    """
    base = (
        "You are a privacy filter that locates phone numbers in an image.\n\n"
        f"The image is exactly {img_w} pixels wide and {img_h} pixels tall. "
        "Use absolute pixel coordinates with the image origin (0,0) at the "
        "TOP-LEFT corner. Positive X = right. Positive Y = down.\n\n"
        "For EACH phone number visible in the image, return:\n"
        "  - text:        the exact characters of the phone (digits + any "
        "separators / spaces / parentheses / + sign)\n"
        "  - cx:          horizontal center of the phone number text, in pixels\n"
        "  - cy:          vertical center of the phone number text, in pixels\n"
        "  - char_height: vertical height of a single digit glyph, in pixels\n\n"
        "Strict rules:\n"
        "- Treat as a phone number ONLY clearly-formatted phones: 10-digit "
        "US, +1 international, +44, etc. with at least 7 visible digits.\n"
        "- ZIP codes (5 digits), VIN (17 alphanumeric), policy numbers, "
        "license/plate numbers, account numbers, prices, dates, times and "
        "addresses are NOT phone numbers — do NOT return them.\n"
        "- (cx, cy) must point at the exact center of the phone-number "
        "text, NOT the center of a label or the row.\n"
        "- char_height should be the height of one digit, in pixels — not "
        "the height of the whole row.\n"
        "- If a phone wraps onto a second line, return TWO entries.\n\n"
        "Return ONLY a single JSON object of this exact shape "
        "(no prose, no code fences):\n"
        '{"phones": [{"text": <string>, "cx": <int>, "cy": <int>, '
        '"char_height": <int>}, ...]}\n'
        'If no phone numbers are visible, return {"phones": []}.\n'
    )
    if target_phones:
        targets_block = "\n".join(f"- {p}" for p in target_phones)
        base += (
            "\nThe lead's phone number is one of these spellings — locate "
            "it FIRST and include it in `phones`. Then add any other "
            "phone numbers visible in the image:\n"
            f"{targets_block}\n"
        )
    return base


_PHONE_DIGIT_RE = re.compile(r"\d")


def _phone_text_looks_real(text: str) -> bool:
    """Sanity-check the model's claim that a returned box covers a phone number.

    Filters out boxes whose ``text`` is obviously not a phone (e.g. just a
    label, an empty string, an address line) — the #1 source of false
    positives.
    """
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    digits = "".join(_PHONE_DIGIT_RE.findall(s))
    # Real phones have 7+ digits; ZIPs/policy/VIN should be filtered separately
    # but a 5-digit number alone is almost always a ZIP.
    if len(digits) < 7:
        return False
    if len(digits) > 16:
        return False
    # Reject obviously non-phone patterns like long alphanumeric VIN strings.
    if re.fullmatch(r"[A-Za-z0-9]{17}", s.replace(" ", "")):
        return False
    return True


def _phone_variants(phone: str) -> list[str]:
    """Yield common visible spellings of a single E.164-ish phone number.

    Used to anchor the AI prompt on the exact value extracted from the lead so
    the model doesn't have to guess what "the" phone number looks like.
    """
    if not phone:
        return []
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return []
    # Always preserve full international form first; many forms have +1 prefix.
    seen: list[str] = []

    def _add(v: str) -> None:
        v = v.strip()
        if v and v not in seen:
            seen.append(v)

    if len(digits) == 11 and digits.startswith("1"):
        d10 = digits[1:]
        _add(f"+1 {d10[0:3]} {d10[3:6]} {d10[6:10]}")
        _add(f"+1 ({d10[0:3]}) {d10[3:6]}-{d10[6:10]}")
        _add(f"+1{d10}")
        _add(f"1-{d10[0:3]}-{d10[3:6]}-{d10[6:10]}")
        _add(f"({d10[0:3]}) {d10[3:6]}-{d10[6:10]}")
        _add(f"{d10[0:3]}-{d10[3:6]}-{d10[6:10]}")
        _add(f"{d10[0:3]}.{d10[3:6]}.{d10[6:10]}")
        _add(f"{d10[0:3]} {d10[3:6]} {d10[6:10]}")
        _add(d10)
    elif len(digits) == 10:
        _add(f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}")
        _add(f"{digits[0:3]}-{digits[3:6]}-{digits[6:10]}")
        _add(f"{digits[0:3]}.{digits[3:6]}.{digits[6:10]}")
        _add(f"{digits[0:3]} {digits[3:6]} {digits[6:10]}")
        _add(digits)
    else:
        _add(str(phone))
        _add(digits)
    return seen


@dataclass
class _PhoneRedactionResponse:
    """Rectangles, in pixel coordinates of the **original** image.

    Produced by OCR (pytesseract) when available, otherwise by the AI fallback.
    """

    # Each entry: (x1, y1, x2, y2) in pixels of the source image.
    boxes: list[tuple[int, int, int, int]]
    api_ok: bool  # True iff at least one detection method completed successfully


# Match phone-number shapes inside an OCR text run. Require either
# parentheses around the area code, an explicit ``+`` country code, or a
# separator (dash / dot / space) between the area-code and exchange. This
# stops bare 10-digit account / policy numbers (e.g. ``2025693380``) from
# matching when they're written as one run of digits.
_OCR_PHONE_RE = re.compile(
    r"(?:"
    r"\+\d{1,3}[\s\-.()]*"               # +1 / +44 etc.
    r"(?:\(?\d{3}\)?[\s\-.]?)?"           # optional area code
    r"\d{3}[\s\-.]?\d{4}"                 # exchange + 4
    r"|"
    r"\(\d{3}\)[\s\-.]?\d{3}[\s\-.]?\d{4}"  # (732) 534-2659  / (732)534-2659
    r"|"
    r"\d{3}[\-.][\d]{3}[\-.]\d{4}"         # 732-534-2659 / 732.534.2659
    r"|"
    r"\d{3}\s\d{3}\s\d{4}"                  # 732 534 2659
    r")"
)

# Phone-context labels (lowercase, word boundaries) — if a line contains
# any of these we trust that the digit run on it is a phone number.
_PHONE_CONTEXT_TERMS = (
    "phone", "phone#", "phone #", "phn", "tel", "tel.",
    "cell", "mobile", "client phone", "client #", "client phone #",
    "contact", "call", "office",
)

# Negative-context labels — when a line contains any of these we REFUSE
# to redact digits on it, because they're almost certainly something
# else (policy / account / VIN / etc.).
_PHONE_NEGATIVE_TERMS = (
    "policy", "policy number", "policy #", "policy#",
    "vin", "license", "lic #", "lic#", "dl #", "dl#",
    "plate", "tag #", "tag#",
    "account", "acct", "acct #", "acct#",
    "zip", "postal",
    "ssn", "tax id", "ein",
    "reference", "ref #", "ref#",
    "order", "invoice", "receipt #", "receipt#",
)


def _line_has_term(line_lower: str, terms: tuple[str, ...]) -> bool:
    return any(t in line_lower for t in terms)


def _ocr_phone_boxes(image_bytes: bytes) -> Optional[list[tuple[int, int, int, int]]]:
    """Locate phone-number bounding boxes via Tesseract OCR.

    Returns ``None`` when OCR isn't available (binary or library missing) so
    the caller can fall back to AI-based detection. Returns ``[]`` when OCR
    ran but found no phone numbers.

    Why this exists: AI vision models hallucinate pixel coordinates badly.
    Tesseract gives **exact** word boxes, so we can draw a tight rectangle
    on the actual phone digits.
    """
    if not image_bytes:
        return None
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # noqa: F401
    except Exception as e:
        logger.info("OCR phone detection unavailable: %s", e)
        return None
    import io as _io

    try:
        img = Image.open(_io.BytesIO(image_bytes))
        img.load()
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except pytesseract.TesseractNotFoundError:
        logger.info("OCR phone detection unavailable: tesseract binary missing")
        return None
    except Exception as e:
        logger.warning("OCR phone detection failed: %s", e)
        return None

    n = len(data.get("text") or [])
    if not n:
        return []

    # Group consecutive words on the same line, then run the phone regex over
    # the joined text. Track each word's pixel rect so we can union them.
    rects: list[tuple[int, int, int, int]] = []
    by_line: dict[tuple, list[int]] = {}
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        if not txt:
            continue
        key = (
            int(data.get("block_num", [0]*n)[i] or 0),
            int(data.get("par_num", [0]*n)[i] or 0),
            int(data.get("line_num", [0]*n)[i] or 0),
        )
        by_line.setdefault(key, []).append(i)

    # Sort lines top-to-bottom so we can peek at the previous line's text
    # (some forms put the label "Client phone #" on its own line above the
    # number). We use median y of words on a line as the sort key.
    sorted_lines: list[tuple[int, tuple, list[int]]] = []
    for key, indices in by_line.items():
        if not indices:
            continue
        ys = [int(data["top"][i]) for i in indices]
        sorted_lines.append((sum(ys) // len(ys), key, indices))
    sorted_lines.sort(key=lambda t: t[0])
    prev_line_lower = ""
    for _y, _key, indices in sorted_lines:
        indices.sort(key=lambda i: int(data["left"][i]))
        words = [(data["text"][i] or "").strip() for i in indices]
        joined = " ".join(words)
        line_lower = joined.lower()
        # Skip lines that clearly contain non-phone identifiers — even if
        # the digit run *looks* like a phone, redacting it would be wrong.
        if _line_has_term(line_lower, _PHONE_NEGATIVE_TERMS):
            prev_line_lower = line_lower
            continue
        has_phone_context = _line_has_term(line_lower, _PHONE_CONTEXT_TERMS) or _line_has_term(
            prev_line_lower, _PHONE_CONTEXT_TERMS
        )
        for m in _OCR_PHONE_RE.finditer(joined):
            phone_text = m.group(0)
            if not _phone_text_looks_real(phone_text):
                continue
            # If we don't have an explicit phone label nearby, the digit
            # run must contain a phone-shaped separator to be trusted.
            # ``_OCR_PHONE_RE`` already enforces that, but a bare run like
            # "732 534 2659" can still appear in addresses. Require either
            # context OR strong separators (parentheses or '+' country code).
            strong_separators = ("(" in phone_text or "+" in phone_text or "-" in phone_text or "." in phone_text)
            if not has_phone_context and not strong_separators:
                continue
            # Map regex match span back to which words it covers.
            cursor = 0
            covered: list[int] = []
            for idx, w in zip(indices, words):
                word_start = cursor
                word_end = cursor + len(w)
                cursor = word_end + 1  # +1 for the joining space
                if word_end <= m.start():
                    continue
                if word_start >= m.end():
                    break
                covered.append(idx)
            if not covered:
                continue
            xs1 = min(int(data["left"][i]) for i in covered)
            ys1 = min(int(data["top"][i]) for i in covered)
            xs2 = max(int(data["left"][i]) + int(data["width"][i]) for i in covered)
            ys2 = max(int(data["top"][i]) + int(data["height"][i]) for i in covered)
            if xs2 > xs1 and ys2 > ys1:
                rects.append((xs1, ys1, xs2, ys2))
        prev_line_lower = line_lower
    return rects


def _normalize_image_for_redaction(image_bytes: bytes) -> tuple[bytes, str, int, int, float]:
    """Down-scale to ``PHONE_REDACTION_RENDER_WIDTH`` and emit as JPEG.

    Returns ``(bytes, mime, width, height, scale_from_orig)`` so the caller
    can convert pixel coordinates from the AI's coordinate space back to the
    original image. ``scale_from_orig`` is ``norm_width / original_width``.
    """
    try:
        from PIL import Image
    except Exception:
        # Without Pillow we can't measure; return as-is and let caller skip rescaling.
        return image_bytes, "image/jpeg", 0, 0, 1.0
    import io as _io

    try:
        img = Image.open(_io.BytesIO(image_bytes))
        img.load()
        if img.mode not in ("RGB",):
            img = img.convert("RGB")
        orig_w, orig_h = img.size
        scale = 1.0
        if orig_w > PHONE_REDACTION_RENDER_WIDTH:
            scale = PHONE_REDACTION_RENDER_WIDTH / float(orig_w)
            new_h = max(1, int(round(orig_h * scale)))
            img = img.resize((PHONE_REDACTION_RENDER_WIDTH, new_h), Image.LANCZOS)
        out_w, out_h = img.size
        out = _io.BytesIO()
        img.save(out, format="JPEG", quality=92)
        return out.getvalue(), "image/jpeg", out_w, out_h, scale
    except Exception as e:
        logger.warning("normalize image for redaction failed: %s", e)
        return image_bytes, "image/jpeg", 0, 0, 1.0


def _box_from_center_and_text(
    cx: float,
    cy: float,
    text: str,
    char_height: float,
    img_w: int,
    img_h: int,
) -> Optional[tuple[int, int, int, int]]:
    """Compute a tight rectangle around phone text from center + glyph height.

    This is the key fix for "oversized at wrong location":
    - Width is derived analytically from the text length and digit height
      (digit width ≈ 0.6 * digit height for proportional fonts, 0.55 for
      common DMV/form fonts), not from a fuzzy AI bounding box.
    - Height clamps to a sane phone-number row height.
    """
    if char_height <= 0 or not text:
        return None
    n_chars = max(7, len([c for c in text if not c.isspace()]))
    # Use a slightly conservative width factor so the bar always covers the
    # last digit when the AI underestimates char_height a bit.
    digit_w = char_height * 0.62
    half_w = (n_chars * digit_w) * 0.55  # *0.55 ≈ half + small padding
    half_h = char_height * 0.75
    x1 = int(round(cx - half_w))
    y1 = int(round(cy - half_h))
    x2 = int(round(cx + half_w))
    y2 = int(round(cy + half_h))
    # Clamp into the image
    x1 = max(0, min(img_w - 1, x1))
    y1 = max(0, min(img_h - 1, y1))
    x2 = max(0, min(img_w, x2))
    y2 = max(0, min(img_h, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _phone_redaction_boxes(
    image_bytes: bytes,
    mime_type: str = "image/jpeg",
    known_phone: Optional[str] = None,
) -> _PhoneRedactionResponse:
    """Ask OpenAI Vision for phone-number locations and return pixel boxes.

    The AI returns center + text + digit height; Python computes the actual
    rectangle. Coordinates returned by this function are in **pixels of the
    original input image**.
    """
    from config import Config

    empty_ok = _PhoneRedactionResponse([], True)
    empty_fail = _PhoneRedactionResponse([], False)

    if not image_bytes:
        return empty_ok

    # Prefer real OCR (tesseract) for pixel-precise phone boxes. The AI is
    # only used as a fallback when OCR isn't available, because vision LLMs
    # are unreliable at exact pixel coordinates.
    ocr_rects = _ocr_phone_boxes(image_bytes)
    if ocr_rects is not None:
        if ocr_rects:
            logger.info("OCR found %d phone box(es)", len(ocr_rects))
        return _PhoneRedactionResponse(ocr_rects, True)

    api_key = (getattr(Config, "OPENAI_API_KEY", "") or "").strip()
    if not api_key:
        return empty_fail

    norm_bytes, norm_mime, norm_w, norm_h, norm_scale = _normalize_image_for_redaction(image_bytes)
    if not norm_w or not norm_h:
        return empty_fail
    b64 = base64.standard_b64encode(norm_bytes).decode("ascii")
    data_url = f"data:{norm_mime};base64,{b64}"

    target_phones = _phone_variants(known_phone or "")
    prompt = _build_phone_redaction_prompt(target_phones, norm_w, norm_h)

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
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url, "detail": "high"},
                        },
                    ],
                }
            ],
            max_tokens=600,
            temperature=0.0,
        )
        raw = (response.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("phone redaction API call failed: %s", e)
        return empty_fail

    data = _parse_json_from_model(raw)
    if not isinstance(data, dict):
        return empty_fail
    raw_phones = data.get("phones")
    if not isinstance(raw_phones, list):
        # Backward compat: old "boxes" key still tolerated as a no-op signal.
        if isinstance(data.get("boxes"), list) and not data.get("boxes"):
            return empty_ok
        return empty_fail

    # Scale back from normalized image space (what the AI saw) to original pixels.
    if norm_scale <= 0:
        norm_scale = 1.0
    inv_scale = 1.0 / norm_scale

    try:
        from PIL import Image as _Image
        import io as _io2
        with _Image.open(_io2.BytesIO(image_bytes)) as _src:
            orig_w, orig_h = _src.size
    except Exception:
        orig_w = int(round(norm_w * inv_scale))
        orig_h = int(round(norm_h * inv_scale))

    out: list[tuple[int, int, int, int]] = []
    for entry in raw_phones:
        if not isinstance(entry, dict):
            continue
        text_val = str(entry.get("text") or "").strip()
        if not _phone_text_looks_real(text_val):
            continue
        try:
            cx_norm = float(entry.get("cx"))
            cy_norm = float(entry.get("cy"))
            ch_norm = float(entry.get("char_height"))
        except (TypeError, ValueError, AttributeError):
            continue
        if ch_norm <= 1:
            continue
        # Sanity: digit height should be a small fraction of image height —
        # a phone number isn't half the image tall.
        if ch_norm > norm_h * 0.25:
            logger.info(
                "dropping phone w/ implausible char_height %.1f (img_h=%d, text=%r)",
                ch_norm,
                norm_h,
                text_val,
            )
            continue
        # Sanity: center must sit inside the normalized image.
        if not (0 <= cx_norm <= norm_w and 0 <= cy_norm <= norm_h):
            continue
        # Map back to original-image pixels.
        cx = cx_norm * inv_scale
        cy = cy_norm * inv_scale
        ch = ch_norm * inv_scale
        rect = _box_from_center_and_text(cx, cy, text_val, ch, orig_w, orig_h)
        if not rect:
            continue
        rx1, ry1, rx2, ry2 = rect
        rw = rx2 - rx1
        rh = ry2 - ry1
        # Final guard: rectangle must be small compared to the image (phones
        # are short rows of text). >55% width or >18% height is almost
        # certainly wrong.
        if rw > orig_w * 0.55 or rh > orig_h * 0.18:
            logger.info(
                "dropping oversized computed phone rect %dx%d (img=%dx%d, text=%r)",
                rw,
                rh,
                orig_w,
                orig_h,
                text_val,
            )
            continue
        out.append((rx1, ry1, rx2, ry2))

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
    known_phone: Optional[str] = None,
) -> PhoneRedactionResult:
    """Cover phone numbers detected by OpenAI Vision with solid black bars only.

    Pass ``known_phone`` (the lead's extracted phone number, any format) to
    anchor the detector on a specific target — that lowers both false
    positives and missed redactions versus a generic "find any phone" prompt.
    """
    if not image_bytes:
        return PhoneRedactionResult(image_bytes or b"", False, False)
    try:
        from PIL import Image, ImageDraw
    except Exception as e:
        logger.warning("Pillow not available; cannot redact image: %s", e)
        return PhoneRedactionResult(image_bytes, False, False)

    resp = _phone_redaction_boxes(
        image_bytes, mime_type=mime_type, known_phone=known_phone
    )
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
        w, h = img.size
        draw = ImageDraw.Draw(img)
        # ``resp.boxes`` are already tight pixel rectangles computed from the
        # AI's center + text + char_height. Just paint solid black bars.
        for x1, y1, x2, y2 in resp.boxes:
            x1 = max(0, min(w, int(x1)))
            y1 = max(0, min(h, int(y1)))
            x2 = max(0, min(w, int(x2)))
            y2 = max(0, min(h, int(y2)))
            if x2 <= x1 or y2 <= y1:
                continue
            # Tiny safety pad: 2% of the rect on each side to catch sub-pixel
            # drift in the AI's center estimate.
            box_w = x2 - x1
            box_h = y2 - y1
            pad_x = max(1, int(round(box_w * 0.02)))
            pad_y = max(1, int(round(box_h * 0.05)))
            bx1 = max(0, x1 - pad_x)
            by1 = max(0, y1 - pad_y)
            bx2 = min(w, x2 + pad_x)
            by2 = min(h, y2 + pad_y)
            draw.rectangle([bx1, by1, bx2, by2], fill=(0, 0, 0))
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
