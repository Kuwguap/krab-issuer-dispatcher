"""SQLite conversation state for Facebook Messenger leads."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator, Optional

from config import Config


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    path = Path(Config.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                psid TEXT PRIMARY KEY,
                slots_json TEXT NOT NULL DEFAULT '{}',
                history_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'active',
                reference_id TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_forwards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                psid TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def get_conversation(psid: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE psid = ?", (psid,)).fetchone()
        if not row:
            return {
                "psid": psid,
                "slots": {},
                "history": [],
                "status": "active",
                "reference_id": None,
            }
        return {
            "psid": row["psid"],
            "slots": json.loads(row["slots_json"] or "{}"),
            "history": json.loads(row["history_json"] or "[]"),
            "status": row["status"],
            "reference_id": row["reference_id"],
        }


def save_conversation(
    psid: str,
    *,
    slots: dict[str, Any],
    history: list[dict[str, str]],
    status: str = "active",
    reference_id: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversations (psid, slots_json, history_json, status, reference_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(psid) DO UPDATE SET
                slots_json=excluded.slots_json,
                history_json=excluded.history_json,
                status=excluded.status,
                reference_id=COALESCE(excluded.reference_id, conversations.reference_id),
                updated_at=excluded.updated_at
            """,
            (
                psid,
                json.dumps(slots),
                json.dumps(history[-24:]),
                status,
                reference_id,
                _utc_now(),
            ),
        )
        conn.commit()


def enqueue_forward(psid: str, payload: dict[str, Any], error: Optional[str] = None) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO pending_forwards (psid, payload_json, attempts, last_error, created_at)
            VALUES (?, ?, 0, ?, ?)
            """,
            (psid, json.dumps(payload), error, _utc_now()),
        )
        conn.commit()


def list_pending_forwards(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, psid, payload_json, attempts, last_error
            FROM pending_forwards
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "psid": r["psid"],
                "payload": json.loads(r["payload_json"]),
                "attempts": r["attempts"],
                "last_error": r["last_error"],
            }
        )
    return out


def delete_pending(forward_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_forwards WHERE id = ?", (forward_id,))
        conn.commit()


def bump_pending_attempt(forward_id: int, error: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE pending_forwards
            SET attempts = attempts + 1, last_error = ?
            WHERE id = ?
            """,
            (error, forward_id),
        )
        conn.commit()
