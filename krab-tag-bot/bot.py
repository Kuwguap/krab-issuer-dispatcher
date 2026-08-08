"""krab-tag-bot — web-only NJ temp-tag generator.

Serves POST /api/tag/generate (Bearer TAG_API_KEY) for the tristatetags.com/tag
page. The Telegram tag-creation flow and /settings now live in krableadsV2 (the
one bot). Canonical generator = krableadsV2/utils/tag_pdf.py, copied into
taggen/ at build time; every generated tag is logged to tristatetags.com/backend
by reference number.
"""
from __future__ import annotations

import logging
import os

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def main() -> None:
    import uvicorn

    from api.app import app
    from api.deps import init_api_deps
    from db import Database

    init_api_deps(Database())
    port = int((os.getenv("PORT") or os.getenv("WEB_PORT") or "8080").strip() or 8080)
    logger.info("krab-tag-bot (web-only) listening on 0.0.0.0:%s", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
