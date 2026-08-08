"""FastAPI application factory for krab-tag-bot."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import routes_tag
from config import Config


def _cors_origins() -> list:
    raw = (Config.KRAB_API_CORS_ALLOWED_ORIGINS or "*").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    app = FastAPI(title="Krab Tag Bot", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    app.include_router(routes_tag.router)
    return app


app = create_app()
