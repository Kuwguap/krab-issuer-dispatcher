"""POST /api/tag/generate — build the NJ temp-tag PDF from client/vehicle fields.

Auth: Authorization: Bearer <TAG_API_KEY>. Serves the tristatetags.com/tag page
(via its Vercel proxy) and the bot's own interface. Returns application/pdf by
default, or JSON {url,plate,control_number} when ?store=1.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

import tagcore
from api.deps import get_db, require_tag_api_key

logger = logging.getLogger(__name__)
router = APIRouter()


class TagRequest(BaseModel):
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


@router.post("/api/tag/generate", dependencies=[Depends(require_tag_api_key)])
async def generate_tag(body: TagRequest, store: int = Query(default=0)):
    db = get_db()
    payload: Dict[str, Any] = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        pdf, plate, control = await asyncio.to_thread(tagcore.generate, payload, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Tag generation failed")
        raise HTTPException(status_code=500, detail=f"Tag generation failed: {e}")

    if store:
        url = await asyncio.to_thread(db.upload_tag_to_storage, plate, pdf)
        if not url:
            raise HTTPException(status_code=502, detail="Could not store tag PDF")
        return JSONResponse({"url": url, "plate": plate, "control_number": control})

    filename = "tag_" + "".join(c for c in plate if c.isalnum()) + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Tag-Plate": plate,
            "X-Tag-Control": control,
        },
    )
