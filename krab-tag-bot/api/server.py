"""Run uvicorn in a background thread (shared process with the Telegram bot).

Cloned from krab-interviewer/api/server.py.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

_server_thread: threading.Thread | None = None
_server_port: int | None = None
_server_error: BaseException | None = None


def _port() -> int:
    for key in ("PORT", "WEB_PORT"):
        raw = (os.getenv(key) or "").strip()
        if raw.isdigit():
            return int(raw)
    return 8080


def start_in_background_thread(db_instance=None) -> None:
    global _server_thread, _server_port
    if _server_thread and _server_thread.is_alive():
        logger.info("FastAPI server already running")
        return

    import uvicorn

    from api.app import create_app
    from api.deps import init_api_deps
    from db import Database

    db = db_instance or Database()
    init_api_deps(db)

    port = _port()
    app = create_app()

    # loop="asyncio"/http="h11" (not uvloop/httptools): uvloop.install() sets a
    # global event-loop policy and creating a uvloop loop inside this non-main
    # daemon thread is fragile on Linux — the pure-Python stack binds reliably here
    # and keeps PTB's own main-thread loop untouched.
    config = uvicorn.Config(
        app, host="0.0.0.0", port=port, log_level="info",
        access_log=False, loop="asyncio", http="h11",
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        global _server_error
        logger.info("FastAPI binding on 0.0.0.0:%s ...", port)
        try:
            server.run()
        except Exception as e:  # noqa: BLE001 — surface the real reason, don't die silently
            _server_error = e
            logger.error("FastAPI server thread crashed: %s", e, exc_info=True)

    _server_thread = threading.Thread(target=_run, name="krab-tag-fastapi", daemon=True)
    _server_thread.start()
    _server_port = port
    _wait_until_listening(port)


def _wait_until_listening(port: int, timeout_sec: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if not (_server_thread and _server_thread.is_alive()):
            raise RuntimeError(f"FastAPI thread exited before binding to port {port}: {_server_error}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                logger.info("FastAPI ready on port %s", port)
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"FastAPI did not listen on port {port} within {timeout_sec}s: {_server_error}")
