"""FastAPI application factory."""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from api import routes_admin, routes_drafts
from config import Config

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"


def _cors_origins() -> list:
    raw = (Config.KRAB_API_CORS_ALLOWED_ORIGINS or "*").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def create_app() -> FastAPI:
    app = FastAPI(title="Krab Interviewer Web", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    app.include_router(routes_drafts.router)
    app.include_router(routes_admin.router)

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    async def index_page():
        p = WEB_DIR / "index.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "index.html not found"}

    @app.get("/requirements")
    async def requirements_page():
        p = WEB_DIR / "requirements.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "requirements.html not found"}

    @app.get("/interview")
    async def interview_form_page():
        p = WEB_DIR / "interview.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "interview.html not found"}

    @app.get("/interview/embed")
    async def interview_embed_docs():
        p = WEB_DIR / "interview" / "embed.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "interview/embed.html not found"}

    @app.get("/how-to-telegram")
    async def how_to_telegram():
        p = WEB_DIR / "how-to-telegram.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "how-to-telegram.html not found"}

    @app.get("/admin")
    async def admin_page():
        p = WEB_DIR / "admin.html"
        if p.is_file():
            return FileResponse(p)
        return {"error": "admin.html not found"}

    return app


app = create_app()
