"""Ephemeral, aggregate-only index progress for the local terminal monitor."""
from __future__ import annotations

import os
import sqlite3

from ._util import now
from ._util import process_exists as _process_exists

_STAGES = frozenset({
    "discovery",
    "endpoint_analysis",
    "component_analysis",
    "persistence_analysis",
    "messaging_analysis",
    "overview_generation",
})


def start(conn: sqlite3.Connection, service: str, total_units: int) -> None:
    """Start one local service progress row, replacing only this process's prior row."""
    if total_units < 1:
        raise ValueError("total_units must be at least one")
    _remove_abandoned(conn)
    process_id = os.getpid()
    conn.execute(
        "DELETE FROM local_index_progress WHERE process_id = ? AND service = ?",
        (process_id, service),
    )
    conn.execute(
        """INSERT INTO local_index_progress
           (service, process_id, total_units, completed_units, stage, started_at, updated_at)
           VALUES (?, ?, ?, 0, 'discovery', ?, ?)""",
        (service, process_id, total_units, now(), now()),
    )
    conn.commit()


def stage_started(conn: sqlite3.Connection, service: str, stage: str) -> None:
    if stage not in _STAGES:
        raise ValueError(f"unsupported index progress stage: {stage}")
    conn.execute(
        """UPDATE local_index_progress SET stage = ?, updated_at = ?
           WHERE service = ? AND process_id = ?""",
        (stage, now(), service, os.getpid()),
    )
    conn.commit()


def unit_finished(conn: sqlite3.Connection, service: str, stage: str) -> None:
    if stage not in _STAGES:
        raise ValueError(f"unsupported index progress stage: {stage}")
    conn.execute(
        """UPDATE local_index_progress
           SET completed_units = MIN(completed_units + 1, total_units), stage = ?, updated_at = ?
           WHERE service = ? AND process_id = ?""",
        (stage, now(), service, os.getpid()),
    )
    conn.commit()


def finish(conn: sqlite3.Connection, service: str) -> None:
    conn.execute(
        "DELETE FROM local_index_progress WHERE service = ? AND process_id = ?",
        (service, os.getpid()),
    )
    conn.commit()


def list_active(conn: sqlite3.Connection) -> list[dict[str, int | str]]:
    try:
        rows = conn.execute(
            """SELECT service, process_id, total_units, completed_units, stage
               FROM local_index_progress ORDER BY started_at, id"""
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: local_index_progress" not in str(exc):
            raise
        return []
    return [
        {
            "service": row["service"],
            "total_units": row["total_units"],
            "completed_units": row["completed_units"],
            "stage": row["stage"],
        }
        for row in rows
        if _process_exists(row["process_id"])
    ]


def _remove_abandoned(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, process_id FROM local_index_progress").fetchall()
    abandoned_ids = [(row["id"],) for row in rows if not _process_exists(row["process_id"])]
    if abandoned_ids:
        conn.executemany("DELETE FROM local_index_progress WHERE id = ?", abandoned_ids)
