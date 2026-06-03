"""FastAPI dependencies: DB, IP hash, admin session."""
from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from config import Config
from utils.database import Database
from utils.drafts_db import DraftsDatabase, ip_hash_from_request

_db: Optional[Database] = None
_drafts: Optional[DraftsDatabase] = None
_serializer: Optional[URLSafeTimedSerializer] = None

ADMIN_COOKIE = "krab_admin_session"
DRAFT_COOKIE = "krab_draft_id"
ADMIN_SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 days


def init_api_deps(db: Database) -> None:
    global _db, _drafts, _serializer
    _db = db
    _drafts = DraftsDatabase(db.client)
    secret = (Config.IP_HASH_SALT or Config.ADMIN_PASSWORD or "krab-session").strip()
    _serializer = URLSafeTimedSerializer(secret, salt="krab-admin")


def get_db() -> Database:
    if _db is None:
        raise HTTPException(status_code=503, detail="Database not initialized")
    return _db


def get_drafts_db() -> DraftsDatabase:
    if _drafts is None:
        raise HTTPException(status_code=503, detail="Drafts DB not initialized")
    return _drafts


def _session_serializer() -> URLSafeTimedSerializer:
    if _serializer is None:
        raise HTTPException(status_code=503, detail="Session not initialized")
    return _serializer


def sign_admin_session() -> str:
    return _session_serializer().dumps({"role": "admin"})


def verify_admin_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        data = _session_serializer().loads(token, max_age=ADMIN_SESSION_MAX_AGE)
        return isinstance(data, dict) and data.get("role") == "admin"
    except (BadSignature, SignatureExpired):
        return False


async def current_ip_hash(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    remote = request.client.host if request.client else None
    return ip_hash_from_request(forwarded, remote)


async def require_admin(
    krab_admin_session: Optional[str] = Cookie(default=None),
) -> None:
    if not Config.ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Admin not configured")
    if not verify_admin_session(krab_admin_session):
        raise HTTPException(status_code=401, detail="Unauthorized")
