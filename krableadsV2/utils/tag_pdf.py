"""NJ 30-Day Temporary Tag PDF generator.

A faithful Python port of the njtemporarytag.com generator
(``system/packages/shared/pdf/nj-temp-tag.js``). The static artwork — the
"New Jersey" banner, the state seal, the two-panel registration form with all
its labels, and the baked dealer identity ("07379U" / "ROUTE 46 MOTORS LLC")
— lives inside the ``NJ.pdf`` / ``NONNJ.pdf`` AcroForm templates shipped in
``assets/``. This module fills the small form fields and draws the four hero
elements (huge plate, EXP banner, 10-digit control number, small plate) on
top, matching the five reference sample tags pixel-for-pixel.

Resident (registration state == NJ) uses ``NJ.pdf`` and a ``H######`` plate;
everyone else uses ``NONNJ.pdf`` and a ``######V`` plate. Expiration is issue
+ 29 days (the 30th day inclusive), matching every provided sample.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from datetime import timedelta
from typing import Any, Dict, Optional

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
NJ_TEMPLATE = os.path.join(_ASSETS, "NJ.pdf")
NONNJ_TEMPLATE = os.path.join(_ASSETS, "NONNJ.pdf")

PAGE_W = 792.0
PAGE_H = 612.0

# Hero elements — coordinates in pdf-lib bottom-left origin, exactly as the JS
# generator's LAYOUT. Converted to PyMuPDF's top-left origin at draw time.
# ``baseline_top`` is the exact text baseline in PyMuPDF top-left points,
# calibrated to the reference tags (constant across every tag — the plate is
# always 7 chars, the banner a fixed width, the control 10 digits). PyMuPDF's
# TextWriter places a baseline where pdf-lib's drawText does NOT for the same
# nominal box, so we anchor the baseline directly instead of the size*0.32
# approximation, which sat the plate ~11pt low and the control ~12pt high.
# ``x_left`` anchors the hero text's left edge at the exact reference position
# (constant across all tags — plate is always 7 chars, banner a fixed 16-char
# format, control 10 digits — so the reference draws them at fixed x, not
# re-centered per string). Anchoring x removes the ±3pt jitter that per-string
# Arial width measurement would otherwise introduce. The centering box (left/
# width) is retained only for the shrink-to-fit safety guard.
_LAYOUT = {
    "plate": {"left": 38.6, "bottom": 341.9, "width": 707.5, "height": 156.3, "size": 180, "baseline_top": 238.0, "x_left": 30.0},
    "exp": {"left": 120.8, "bottom": 292.1, "width": 512.4, "height": 54.9, "size": 60, "baseline_top": 309.0, "x_left": 117.0},
    "car": {"left": 37.1, "bottom": 234.1, "width": 712.98, "height": 40, "size": 20, "baseline_top": 376.0, "x_left": 338.0},
    "plate_left": {"left": 35.8, "bottom": 138.0, "width": 72, "height": 8, "size": 7.5, "baseline_top": 472.6},
}

# Filled AcroForm fields are drawn manually at their widget rects; these three
# are the hero elements drawn separately and never filled.
_HERO_FIELDS = {"plate1", "exp3", "car"}

# Font search order: bundled Arial (drop-in) → OS Arial → Liberation Sans
# (metric-identical, installed via fonts-liberation on the Linux image) →
# PyMuPDF built-in Helvetica as a last resort.
_FONT_PATHS = {
    "bold": [
        os.path.join(_ASSETS, "fonts", "ARIALBD.TTF"),
        os.path.join(_ASSETS, "fonts", "Arial-Bold.ttf"),
        os.path.join(_ASSETS, "fonts", "LiberationSans-Bold.ttf"),
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    ],
    "regular": [
        os.path.join(_ASSETS, "fonts", "ARIAL.TTF"),
        os.path.join(_ASSETS, "fonts", "Arial.ttf"),
        os.path.join(_ASSETS, "fonts", "LiberationSans-Regular.ttf"),
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    ],
}

_font_cache: Dict[str, fitz.Font] = {}


def _load_font(kind: str) -> fitz.Font:
    if kind in _font_cache:
        return _font_cache[kind]
    for p in _FONT_PATHS[kind]:
        if os.path.exists(p):
            try:
                f = fitz.Font(fontfile=p)
                _font_cache[kind] = f
                return f
            except Exception:
                continue
    # Built-in fallback (Helvetica ~ Arial metrics).
    f = fitz.Font("helv" if kind == "regular" else "hebo")
    _font_cache[kind] = f
    return f


# ── Field-value formatting (ported from normalize-pdf-fields.js) ──────────────

_COLOR_MAP = {
    "black": "BLK", "blk": "BLK", "bk": "BLK",
    "white": "WHI", "wht": "WHI", "whi": "WHI",
    "red": "RED",
    "blue": "BLU", "blu": "BLU",
    "yellow": "YLW", "yel": "YLW",
    "green": "GRN", "grn": "GRN",
    "gray": "GRY", "grey": "GRY", "gry": "GRY",
    "silver": "SLV", "sil": "SLV", "slv": "SLV",
    "brown": "BRN", "brn": "BRN",
    "orange": "ORG", "org": "ORG",
    "gold": "GLD", "gld": "GLD",
    "beige": "BGE", "bge": "BGE",
    "tan": "TAN",
    "purple": "PUR", "pur": "PUR",
    "pink": "PNK", "pnk": "PNK",
    "maroon": "MRN", "mrn": "MRN",
    "navy": "NVY", "nvy": "NVY",
    "charcoal": "CHR", "chr": "CHR",
    "burgundy": "BRG", "brg": "BRG",
    "cream": "CRM", "crm": "CRM",
    "bronze": "BRZ", "brz": "BRZ",
    "copper": "CPR", "cpr": "CPR",
}
_VALID_COLOR_CODES = set(_COLOR_MAP.values())


def color_code(raw: str) -> str:
    """3-letter uppercase color code (BLK, WHI, TAN…) matching the samples.

    Matches by whole word first (so "navy blue" → NVY, not BLU), only accepts a
    3-letter input if it is a known valid code, and never emits an obviously
    wrong substring match ("Titanium" must not become TAN)."""
    s = str(raw or "").strip()
    if not s:
        return ""
    up = re.sub(r"[^A-Z]", "", s.upper())
    if re.fullmatch(r"[A-Z]{3}", up) and up in _VALID_COLOR_CODES:
        return up
    words = re.findall(r"[a-z]+", s.lower())
    # First color word that maps wins (word-level, not substring).
    for w in words:
        if w in _COLOR_MAP:
            return _COLOR_MAP[w]
    # Then any word that starts with / contains a known color name.
    for w in words:
        for name, code in _COLOR_MAP.items():
            if len(name) >= 4 and (w.startswith(name) or name.startswith(w)):
                return code
    # Last resort: first 3 letters of the first word, uppercased.
    return (words[0][:3].upper().ljust(3, "X")) if words else ""


_US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_STATE_ABBRS = set(_US_STATES.values())


def body_label(raw_body_class: str) -> str:
    """Map a raw NHTSA BodyClass (or free text) to the short label the samples
    print: SUV / Hatchback / Sedan / Coupe / Convertible / Wagon / Pickup /
    Van / Chassis / Truck. Empty when unknown (tag stays valid)."""
    s = str(raw_body_class or "").strip().lower()
    if not s:
        return ""
    if any(k in s for k in ("suv", "sport utility", "mpv", "crossover")):
        return "SUV"
    if any(k in s for k in ("hatchback", "liftback", "notchback")):
        return "Hatchback"
    if "wagon" in s:
        return "Wagon"
    if any(k in s for k in ("convertible", "roadster", "cabriolet")):
        return "Convertible"
    if "coupe" in s:
        return "Coupe"
    if any(k in s for k in ("sedan", "saloon")):
        return "Sedan"
    if any(k in s for k in ("minivan", "van")):
        return "Van"
    if "pickup" in s:
        return "Pickup"
    if any(k in s for k in ("chassis", "incomplete", "tractor", "cab")):
        return "Chassis"
    if "truck" in s:
        return "Truck"
    # Fall back to the first token, title-cased.
    first = re.split(r"[\s/]+", s)[0]
    return first[:1].upper() + first[1:] if first else ""


def title_case(raw: str) -> str:
    return " ".join(w[:1].upper() + w[1:].lower() for w in str(raw or "").split() if w)


def _up(v: Any) -> str:
    return "" if v is None else str(v).upper().strip()


def format_mdy(d) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def format_exp_banner(d) -> str:
    return f"EXP {_MONTHS[d.month - 1]} {d.day:02d}, {d.year}"


def split_name(full: str) -> tuple[str, str]:
    """"JOSUE PAVON" → ("JOSUE", "PAVON"); first token is the first name, the
    remainder is the last name."""
    parts = [p for p in str(full or "").split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def decode_vin_for_tag(vin: str) -> Optional[Dict[str, str]]:
    """One NHTSA vPIC call → ``{year, make, model, body}`` for the tag.

    ``make`` is title-cased and ``model`` kept verbatim (matches the samples:
    "Infiniti"/"JX35", "Subaru"/"Impreza"). ``body`` is the short label. Blocking
    HTTP — call via ``asyncio.to_thread``. Returns None if undecodable.
    """
    v = re.sub(r"[^A-Za-z0-9]", "", str(vin or "")).upper()
    if len(v) != 17:
        return None
    url = f"https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{urllib.parse.quote(v)}?format=json"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        res = (data.get("Results") or [{}])[0]
    except Exception as e:
        logger.warning("VIN decode for tag failed (%s): %s", v, e)
        return None
    if not isinstance(res, dict):
        return None
    return {
        "year": str(res.get("ModelYear") or "").strip(),
        "make": title_case(res.get("Make") or ""),
        "model": str(res.get("Model") or "").strip(),
        "body": body_label(res.get("BodyClass") or ""),
    }


# Labeled address fragments some upstreams emit, e.g.
# "BRONX STATE: NY ZIP: 10465" — the labels must be stripped, not printed.
_LABELED_STATE_RE = re.compile(r"\bSTATE\b\s*[:#]?\s*([A-Za-z]{2})\b", re.IGNORECASE)
_LABELED_ZIP_RE = re.compile(r"\bZIP\b\s*[:#]?\s*(\d{5})(?:-\d{4})?\b", re.IGNORECASE)
_LABEL_WORD_RE = re.compile(r"\b(STATE|ZIP)\b\s*[:#]?", re.IGNORECASE)


def parse_city_zip(city_state_zip: str, state: str = "") -> tuple[str, str]:
    """Split "BRONX NY 10465" / "Newark, NJ 07102" / "BRONX STATE: NY ZIP: 10465"
    → (city, zip). Labeled ``STATE:``/``ZIP:`` fragments are stripped so they
    never leak into the printed City field."""
    s = str(city_state_zip or "").strip()

    # Labeled ZIP wins; else a trailing 5-digit run.
    mz = _LABELED_ZIP_RE.search(s)
    if mz:
        zipc = mz.group(1)
    else:
        m = re.search(r"(\d{5})(?:-\d{4})?\s*$", s)
        zipc = m.group(1) if m else ""

    body = s
    # Drop "STATE: NY" and "ZIP: 10465" label groups, then the zip digits, then
    # any stray label words.
    body = _LABELED_STATE_RE.sub(" ", body)
    body = _LABELED_ZIP_RE.sub(" ", body)
    if zipc:
        body = re.sub(rf"\b{re.escape(zipc)}(?:-\d{{4}})?\b", " ", body)
    body = _LABEL_WORD_RE.sub(" ", body)

    # Strip a trailing state — passed value, 2-letter abbreviation, or spelled name.
    if state:
        body = re.sub(rf"[, ]+{re.escape(state)}\s*$", "", body, flags=re.IGNORECASE)
    for name in sorted(_US_STATES, key=len, reverse=True):
        body2 = re.sub(rf"[, ]+{re.escape(name)}\s*$", "", body, flags=re.IGNORECASE)
        if body2 != body:
            body = body2
            break
    body = re.sub(r"[, ]+[A-Za-z]{2}\s*$", lambda m: "" if m.group(0).strip(", ").upper() in _STATE_ABBRS else m.group(0), body)
    city = re.sub(r"\s{2,}", " ", body).strip().rstrip(",").strip()
    return city, zipc


def normalize_city_state_zip(city: str, state: str, zip_: str) -> tuple[str, str, str]:
    """Return clean (city, state, zip) even when ``city`` actually contains the
    whole "CITY STATE: XX ZIP: NNNNN" blob (or a trailing state/zip) and the
    separate state/zip came in blank. Mirrors the clean separated fields the
    njtemporarytag generator receives from a structured order."""
    city = str(city or "").strip()
    state = str(state or "").strip()
    zip_ = str(zip_ or "").strip()

    combined = bool(
        re.search(r"\d{5}", city)
        or _LABEL_WORD_RE.search(city)
        or (not state and re.search(r"[, ]+[A-Za-z]{2}\s*$", city))
        or (not state and any(re.search(rf"\b{re.escape(n)}\b", city, re.IGNORECASE) for n in _US_STATES))
    )
    if combined or not state or not zip_:
        st = parse_state(city)
        c2, z2 = parse_city_zip(city, st)
        if c2:
            city = c2
        if not state and st:
            state = st
        if not zip_ and z2:
            zip_ = z2

    # Normalize a spelled-out or lowercase state to its 2-letter code.
    if state and state.upper() not in _STATE_ABBRS:
        state = _US_STATES.get(state.strip().lower(), state)
    if state.upper() in _STATE_ABBRS:
        state = state.upper()
    return city, state, zip_


_TWO_WORD_MAKES = {
    "mercedes benz", "land rover", "aston martin", "alfa romeo", "rolls royce",
    "general motors",
}


def parse_car_line(car: str) -> tuple[str, str, str]:
    """"2013 Infiniti JX35" → ("2013", "Infiniti", "JX35"). Fallback when VIN
    decode is unavailable; make title-cased (two-word makes kept whole), model
    kept verbatim."""
    parts = [p for p in str(car or "").split() if p]
    year = ""
    if parts and re.fullmatch(r"(19|20)\d{2}", parts[0]):
        year = parts[0]
        parts = parts[1:]
    if not parts:
        return year, "", ""
    if len(parts) >= 3 and f"{parts[0]} {parts[1]}".lower() in _TWO_WORD_MAKES:
        make = title_case(f"{parts[0]} {parts[1]}")
        model = " ".join(parts[2:])
    else:
        make = title_case(parts[0])
        model = " ".join(parts[1:])
    return year, make, model


def parse_state(city_state_zip: str) -> str:
    """Pull the 2-letter state out of a "CITY ST ZIP" / "City, ST" / "City, New
    Jersey 07102" line (handles spelled-out names and a missing ZIP)."""
    s = str(city_state_zip or "").strip()
    if not s:
        return ""
    # Labeled "STATE: NY" (some upstreams emit "BRONX STATE: NY ZIP: 10465").
    m = _LABELED_STATE_RE.search(s)
    if m and m.group(1).upper() in _STATE_ABBRS:
        return m.group(1).upper()
    # A 2-letter abbreviation before a trailing ZIP is the most reliable signal
    # ("WASHINGTON DC 20001" → DC, not WA-the-state-name).
    m = re.search(r"\b([A-Za-z]{2})\b\s*,?\s*\d{5}(?:-\d{4})?\s*$", s)
    if m and m.group(1).upper() in _STATE_ABBRS:
        return m.group(1).upper()
    # ", ST" or a bare trailing 2-letter abbreviation.
    m = re.search(r"[,\s]([A-Za-z]{2})\s*$", s)
    if m and m.group(1).upper() in _STATE_ABBRS:
        return m.group(1).upper()
    # Spelled-out state name — the RIGHTMOST match wins (the state trails the
    # city, so "Washington, New Jersey" → NJ, not WA).
    low = s.lower()
    best_pos, best_code = -1, ""
    for name, code in _US_STATES.items():
        for mm in re.finditer(rf"\b{re.escape(name)}\b", low):
            if mm.start() > best_pos:
                best_pos, best_code = mm.start(), code
    return best_code


# ── Drawing ───────────────────────────────────────────────────────────────

def _draw_centered(tw: fitz.TextWriter, text: str, area: dict, font: fitz.Font,
                   max_width: float | None = None) -> None:
    if not text:
        return
    size = area["size"]
    text_w = font.text_length(text, size)
    # Shrink to fit so an over-long plate/banner can't run off the page.
    if max_width and text_w > max_width:
        size = size * max_width / text_w
        text_w = font.text_length(text, size)
    x = area["x_left"] if "x_left" in area else area["left"] + (area["width"] - text_w) / 2
    if "baseline_top" in area:
        y_top = area["baseline_top"]
    else:
        y_top = PAGE_H - (area["bottom"] + area["height"] / 2 - size * 0.32)
    tw.append((x, y_top), text, font=font, fontsize=size)


def _draw_left(tw: fitz.TextWriter, text: str, area: dict, font: fitz.Font) -> None:
    if not text:
        return
    size = area["size"]
    if "baseline_top" in area:
        y_top = area["baseline_top"]
    else:
        y_top = PAGE_H - (area["bottom"] + area["height"] / 2 - size * 0.35)
    tw.append((area["left"], y_top), text, font=font, fontsize=size)


def _draw_field(tw: fitz.TextWriter, rect: fitz.Rect, text: str, size: float, font: fitz.Font) -> None:
    """Left-aligned value inside a form widget rect (fitz top-origin), baseline
    vertically centered like the flattened AcroForm appearance. The font size
    auto-shrinks so long values (full carrier names, long owner names) stay
    inside their box instead of spilling across the form."""
    if not text:
        return
    avail = rect.width - 2.4
    if avail > 4:
        text_w = font.text_length(text, size)
        if text_w > avail:
            size = max(4.0, size * avail / text_w)
    baseline_y = (rect.y0 + rect.y1) / 2 + size * 0.34
    tw.append((rect.x0 + 1.2, baseline_y), text, font=font, fontsize=size)


def build_tag_pdf(fields: Dict[str, Any]) -> bytes:
    """Render a filled NJ temp-tag PDF and return the bytes.

    Expected ``fields`` keys: ``is_nj`` (bool), ``plate``, ``control_number``,
    ``vin``, ``year``, ``make``, ``model``, ``color`` (raw or 3-letter),
    ``body`` (resolved short label), ``first``, ``last``, ``address``, ``city``,
    ``state``, ``zip``, ``insurance_company``, ``policy``, ``issued`` and
    ``expires`` (``datetime``/``date``). Missing values render blank.
    """
    is_nj = bool(fields.get("is_nj"))
    template = NJ_TEMPLATE if is_nj else NONNJ_TEMPLATE

    plate = _up(fields.get("plate"))
    control = str(fields.get("control_number") or "").strip()
    issued = fields.get("issued")
    if issued is None:  # a tag must always carry dates
        from datetime import date as _date
        issued = _date.today()
    expires = fields.get("expires")
    if expires is None:
        expires = issued + timedelta(days=29)

    color3 = color_code(fields.get("color"))
    make = fields.get("make") or ""
    model = fields.get("model") or ""
    year = str(fields.get("year") or "").strip()
    vehicle_parts = " ".join(p for p in (year, make, model) if p).strip()
    vehicle_name = f"{vehicle_parts},{color3}" if color3 else vehicle_parts

    issued_str = format_mdy(issued) if issued else ""
    expiry_str = format_mdy(expires) if expires else ""
    exp_banner = format_exp_banner(expires) if expires else ""

    value_map = {
        "plate2": plate,
        "vin1": _up(fields.get("vin")),
        "vin3": _up(fields.get("vin")),
        "year": year,
        "make1": make,
        "make2": make,
        "model1": model,
        "model2": model,
        "color": color3,
        "body": fields.get("body") or "",
        "vehiclename": vehicle_name,
        "first": _up(fields.get("first")),
        "last": _up(fields.get("last")),
        "address": _up(fields.get("address")),
        "city": _up(fields.get("city")),
        "state": _up(fields.get("state") or ("NJ" if is_nj else "")),
        "zip": str(fields.get("zip") or "").strip(),
        "date1": issued_str,
        "date2": issued_str,
        "exp1": expiry_str,
        "ins": _up(fields.get("insurance_company")),
        "policy": _up(fields.get("policy")),
    }

    regular = _load_font("regular")
    bold = _load_font("bold")

    doc = fitz.open(template)
    page = doc[0]

    # Snapshot each fillable widget (rect + font size) BEFORE removing the form.
    # The empty AcroForm widgets otherwise paint their blank appearance over any
    # page content we draw, hiding the values.
    small: list[tuple[fitz.Rect, str, float]] = []
    for w in page.widgets():
        if w.field_name in _HERO_FIELDS:
            continue
        val = value_map.get(w.field_name)
        if val:
            small.append((fitz.Rect(w.rect), val, w.text_fontsize or 7.0))

    # Strip the interactive form at the object level so the widget appearances
    # stop covering content (PyMuPDF's bake()/delete_widget don't persist here).
    doc.xref_set_key(page.xref, "Annots", "[]")
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", "null")
    doc = fitz.open(stream=doc.tobytes(garbage=4, deflate=True, clean=True), filetype="pdf")
    page = doc[0]

    tw = fitz.TextWriter(page.rect)
    for rect, val, size in small:
        _draw_field(tw, rect, val, size, regular)
    _draw_centered(tw, plate, _LAYOUT["plate"], bold, max_width=PAGE_W - 56)
    _draw_centered(tw, exp_banner, _LAYOUT["exp"], bold, max_width=PAGE_W - 40)
    _draw_centered(tw, control, _LAYOUT["car"], bold, max_width=_LAYOUT["car"]["width"])
    _draw_left(tw, plate, _LAYOUT["plate_left"], regular)
    tw.write_text(page)

    try:
        doc.subset_fonts()  # embed only used glyphs — keeps the file small
    except Exception:
        pass
    return doc.tobytes(garbage=4, deflate=True)
