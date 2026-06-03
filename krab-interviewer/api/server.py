"""Run uvicorn in a background thread (shared process with Telegram bot)."""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_server_thread: threading.Thread | None = None


def _port() -> int:
    for key in ("PORT", "WEB_PORT"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 8080


def start_in_background_thread(db_instance=None) -> None:
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        logger.info("FastAPI server already running")
        return

    import uvicorn

    from api.app import create_app
    from api.deps import init_api_deps
    from utils.database import Database

    db = db_instance or Database()
    init_api_deps(db)

    port = _port()
    app = create_app()

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        logger.info("FastAPI listening on 0.0.0.0:%s", port)
        server.run()

    _server_thread = threading.Thread(target=_run, name="krab-fastapi", daemon=True)
    _server_thread.start()
