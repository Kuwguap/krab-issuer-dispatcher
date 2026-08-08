"""Move Telegram-hosted receipt images into Supabase storage (permanent URLs).

Telegram file links die after ~1 hour. Any receipt row still pointing at
api.telegram.org gets its bytes fetched (directly while fresh, else re-signed
via the #tgfid= file_id with the token embedded in the URL) and re-hosted in
the public ``receipts`` bucket, then the lead row is repointed. Run by the bot
as a sweeper job so even receipts written by legacy deployments become
permanent before their links expire.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_TG_MARK = "https://api.telegram.org/file/bot"
_TOKEN_RE = re.compile(r"https://api\.telegram\.org/file/bot([^/]+)/")


def fetch_receipt_bytes(stored_url: str) -> Optional[bytes]:
    """Bytes for a telegram-hosted receipt URL, or None if unrecoverable."""
    base, _, frag = str(stored_url or "").partition("#")
    if _TG_MARK not in base:
        return None
    try:
        r = requests.get(base, timeout=30)
        if r.ok and r.content and "text/html" not in (r.headers.get("content-type") or ""):
            return r.content
    except Exception:
        pass
    tgfid = None
    for part in frag.split("&"):
        if part.startswith("tgfid="):
            tgfid = part[6:].strip()
    hits = _TOKEN_RE.findall(base)
    token = hits[-1] if hits else None
    if not (tgfid and token):
        return None
    try:
        gf = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": tgfid},
            timeout=20,
        )
        if not gf.ok:
            return None
        fp = ((gf.json() or {}).get("result") or {}).get("file_path")
        if not fp:
            return None
        r2 = requests.get(f"{_TG_MARK}{token}/{fp}", timeout=30)
        if r2.ok and r2.content:
            return r2.content
    except Exception:
        return None
    return None


def rescue_lead_receipt(db, lead: dict) -> bool:
    """Re-host one lead's telegram receipt into storage. True on success.

    The final write is compare-and-swap on the URL we snapshotted: receipts
    can be RE-UPLOADED while this sweep is fetching (slow Telegram HTTP), and
    blindly writing would revert the driver's replacement to the old image.
    """
    lead_id = str(lead.get("id") or "")
    stored = str(lead.get("receipt_image_url") or "")
    if not lead_id or _TG_MARK not in stored:
        return False
    data = fetch_receipt_bytes(stored)
    if not data:
        return False
    storage_url = db.upload_receipt_to_storage(
        lead_id, lead.get("reference_id") or "receipt", data, "receipt.jpg"
    )
    if not storage_url:
        return False
    swapped = db.update_lead_receipt_url_if_unchanged(lead_id, stored, storage_url)
    if not swapped:
        logger.info(
            "Receipt rescue skipped for lead %s — receipt was replaced mid-sweep", lead_id
        )
    return swapped
