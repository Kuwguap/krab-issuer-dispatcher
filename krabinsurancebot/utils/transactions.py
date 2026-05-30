"""Lightweight JSON-backed transaction log for Krab Insurance Bot."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(
    os.getenv("TRANSACTIONS_FILE")
    or (Path(__file__).resolve().parent.parent / "transactions.json")
)
_LOCK = threading.Lock()


def _load() -> list[dict[str, Any]]:
    if not _DEFAULT_PATH.exists():
        return []
    try:
        with _DEFAULT_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(data: list[dict[str, Any]]) -> None:
    _DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _DEFAULT_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(_DEFAULT_PATH)


def record(
    *,
    user_id: int | None,
    username: str | None,
    policy_number: str | None,
    email: str | None,
    vehicle: str | None,
    name: str | None,
    success: bool,
    error: str | None = None,
    plan_months: int | None = None,
    state: str | None = None,
) -> None:
    entry = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "user_id": user_id,
        "username": username,
        "policy_number": policy_number,
        "email": email,
        "vehicle": vehicle,
        "name": name,
        "success": bool(success),
        "error": error,
        "plan_months": plan_months,
        "state": state or "NY",
    }
    with _LOCK:
        data = _load()
        data.append(entry)
        _save(data)


def list_for_user(user_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
    items = [e for e in data if e.get("user_id") == user_id]
    items.sort(key=lambda e: e.get("ts") or "", reverse=True)
    if limit:
        items = items[:limit]
    return items


def list_all(limit: int | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
    data.sort(key=lambda e: e.get("ts") or "", reverse=True)
    if limit:
        data = data[:limit]
    return data
