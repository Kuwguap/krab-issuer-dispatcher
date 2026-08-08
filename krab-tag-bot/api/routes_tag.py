"""POST /api/tag/generate — build the NJ temp-tag PDF from client/vehicle fields.

Auth: Authorization: Bearer <TAG_API_KEY>. Serves the tristatetags.com/tag page
(via its Vercel proxy) and the bot's own interface. Returns application/pdf by
default, or JSON {url,plate,control_number} when ?store=1.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import tagcore
from api.deps import get_db, require_tag_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class TagRequest(BaseModel):
    # Optional free-text labeled block (same syntax the Telegram bot accepts).
    # Parsed with the shared parser; explicit fields below override it.
    message: Optional[str] = None
    is_nj: Optional[bool] = None
    plate: Optional[str] = None
    control_number: Optional[str] = None
    vin: Optional[str] = None
    year: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    body: Optional[str] = None
    color: Optional[str] = None
    name: Optional[str] = None
    first: Optional[str] = None
    last: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None
    city_state_zip: Optional[str] = None
    insurance_company: Optional[str] = None
    insuranceCompany: Optional[str] = None
    policy: Optional[str] = None
    policyNumber: Optional[str] = None
    issued: Optional[str] = None
    expires: Optional[str] = None
    issuer: Optional[str] = None


@router.post("/api/tag/generate", dependencies=[Depends(require_tag_api_key)])
async def generate_tag(body: TagRequest, background: BackgroundTasks, store: int = Query(default=0)):
    db = get_db()
    dumped = body.model_dump()
    message = dumped.pop("message", None)
    payload: Dict[str, Any] = {k: v for k, v in dumped.items() if v is not None}
    # Free-text paste (same parser as the bot); explicit fields win over parsed.
    if message and str(message).strip():
        from parsing import parse_details
        parsed = parse_details(str(message))
        for k, v in parsed.items():
            payload.setdefault(k, v)
    issuer = str(payload.pop("issuer", "") or "").strip() or "tristatetags-web"
    try:
        pdf, fields = await asyncio.to_thread(tagcore.generate_full, payload, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Tag generation failed")
        raise HTTPException(status_code=500, detail=f"Tag generation failed: {e}")
    plate, control = fields["plate"], fields["control_number"]

    # Reference # + log to tristatetags.com/backend (same as the bot). Runs
    # AFTER the response is sent so the PDF isn't delayed by the ledger call.
    import ledger
    reference = ledger.generate_reference_id()
    client_name = f"{fields.get('first', '')} {fields.get('last', '')}".strip()
    background.add_task(
        ledger.log_tag, reference_id=reference, client_name=client_name,
        issuer_name=issuer, issuer_handle=issuer,
    )

    if store:
        url = await asyncio.to_thread(db.upload_tag_to_storage, plate, pdf)
        if not url:
            raise HTTPException(status_code=502, detail="Could not store tag PDF")
        return JSONResponse({"url": url, "plate": plate, "control_number": control, "reference": reference})

    filename = "tag_" + "".join(c for c in plate if c.isalnum()) + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Tag-Plate": plate,
            "X-Tag-Control": control,
            "X-Tag-Reference": reference,
        },
    )
