"""Schedule async Telegram hire side-effects from the FastAPI thread."""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_application = None
_hire_job_callback: Optional[Callable] = None
_pending: List[Tuple[str, str]] = []

# Who a paper order can reach, as last checked by the bot. Counts only.
PAPER_ORDER_RECIPIENT_GROUPS = ("paper_girl_dms", "paper_girl_groups", "supervisors")
_paper_order_recipients: Optional[Dict[str, Any]] = None


def _count_or_none(value) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def set_paper_order_recipients(snapshot: Optional[Dict[str, Any]]) -> None:
    """Cache the bot's recipient counts for /api/health (set from the bot's loop).

    Only the known groups and integer counts are kept, so nothing that
    identifies a chat can reach the health output even by mistake. The whole
    dict is replaced in one assignment; the health thread only ever reads it.
    """
    global _paper_order_recipients
    src = snapshot or {}
    clean: Dict[str, Any] = {}
    for group in PAPER_ORDER_RECIPIENT_GROUPS:
        row = src.get(group) or {}
        clean[group] = {
            "configured": _count_or_none(row.get("configured")),
            "reachable": _count_or_none(row.get("reachable")),
        }
    checked = src.get("checked_at")
    clean["checked_at"] = checked if isinstance(checked, str) else None
    _paper_order_recipients = clean


def paper_order_recipients() -> Optional[Dict[str, Any]]:
    """The cached counts (a copy), or None if the bot never published any."""
    snap = _paper_order_recipients
    if snap is None:
        return None
    return {k: (dict(v) if isinstance(v, dict) else v) for k, v in snap.items()}


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
