"""Schedule async Telegram hire side-effects from the FastAPI thread."""
from __future__ import annotations

import logging
from typing import Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

_application = None
_hire_job_callback: Optional[Callable] = None
_pending: List[Tuple[str, str]] = []


def set_hire_job_callback(callback: Callable) -> None:
    global _hire_job_callback
    _hire_job_callback = callback


def register_application(application) -> None:
    global _application
    _application = application
    if not _pending:
        return
    logger.info("Flushing %s queued hire side-effect job(s)", len(_pending))
    for interview_id, created_by in _pending:
        schedule_hire_side_effects(interview_id, created_by)
    _pending.clear()


def schedule_hire_side_effects(interview_id: str, created_by: str = "web") -> bool:
    """Queue channel DM, paper shipment, training videos, etc. Returns False if bot not ready."""
    if not interview_id:
        return False
    if _application is None or not _hire_job_callback:
        _pending.append((interview_id, created_by))
        logger.warning(
            "Telegram bot not ready; queued hire side effects for interview %s",
            interview_id[:8],
        )
        return False
    job_queue = getattr(_application, "job_queue", None)
    if not job_queue:
        _pending.append((interview_id, created_by))
        logger.warning("No job queue; queued hire side effects for interview %s", interview_id[:8])
        return False
    job_queue.run_once(
        _hire_job_callback,
        when=1,
        data={"interview_id": interview_id, "created_by": created_by},
        name=f"hire_fx_{interview_id[:8]}",
    )
    return True
