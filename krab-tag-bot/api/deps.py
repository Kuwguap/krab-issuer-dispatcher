"""FastAPI dependencies: shared DB handle + Bearer API-key auth."""
from __future__ import annotations

from typing import Optional

from fastapi import Header, HTTPException

from config import Config

_db = None


def init_api_deps(db) -> None:
    global _db
    _db = db


def get_db():
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db


async def require_tag_api_key(authorization: Optional[str] = Header(default=None)) -> None:
    """Bearer <TAG_API_KEY>. 503 if unconfigured, 401 if missing/invalid."""
    expected = (Config.TAG_API_KEY or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Tag API is not configured")
    auth = (authorization or "").strip()
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization: Bearer token")
    if auth[7:].strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid API key")
