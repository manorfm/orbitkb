"""Ephemeral, privacy-safe state for the local terminal monitor."""
from __future__ import annotations

import os
import sqlite3

from ._util import now

_OPERATIONS = frozenset({
    "find_change_surface",
    "get_change_context",
    "plan_change",
    "assess_working_change",
})


def start(conn: sqlite3.Connection, operation: str) -> int:
    """Mark one bounded MCP operation as running without retaining its inputs."""
    if operation not in _OPERATIONS:
        raise ValueError(f"unsupported local activity operation: {operation}")
    _remove_abandoned(conn)
    cur = conn.execute(
        "INSERT INTO local_activity_runs (operation, process_id, started_at) VALUES (?, ?, ?)",
        (operation, os.getpid(), now()),
    )
    conn.commit()
    return cur.lastrowid


def finish(conn: sqlite3.Connection, activity_id: int) -> None:
    """Remove a completed activity row so the table cannot become call history."""
    conn.execute("DELETE FROM local_activity_runs WHERE id = ?", (activity_id,))
    conn.commit()


def list_active(conn: sqlite3.Connection) -> list[dict[str, str]]:
    try:
        rows = conn.execute(
            "SELECT operation, process_id, started_at FROM local_activity_runs ORDER BY started_at, id"
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: local_activity_runs" not in str(exc):
            raise
        return []
    return [
        {"operation": row["operation"], "started_at": row["started_at"]}
        for row in rows
        if _process_exists(row["process_id"])
    ]


def _remove_abandoned(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, process_id FROM local_activity_runs").fetchall()
    abandoned_ids = [(row["id"],) for row in rows if not _process_exists(row["process_id"])]
    if abandoned_ids:
        conn.executemany("DELETE FROM local_activity_runs WHERE id = ?", abandoned_ids)


def _process_exists(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
