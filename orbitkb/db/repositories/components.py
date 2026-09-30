"""The `components` table: the class/controller/module layer between a single endpoint
and the whole service, synthesized from already-generated endpoint summaries. Mirrors
the upsert/prune shape of `apis.py` so each component regenerates independently — one
class's summary changing should never wipe its siblings' rows."""
from __future__ import annotations

import json
import sqlite3

from ._util import now


def upsert_component(
    conn: sqlite3.Connection, service_id: int, name: str, file_path: str, summary: str, evidence: list[dict],
    input_digest: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT id FROM components WHERE service_id = ? AND name = ? AND file_path = ?",
        (service_id, name, file_path),
    ).fetchone()
    payload = (summary, json.dumps(evidence), input_digest, now())
    if row is not None:
        conn.execute(
            "UPDATE components SET summary = ?, evidence_json = ?, input_digest = ?, updated_at = ? WHERE id = ?",
            (*payload, row["id"]),
        )
        component_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO components (service_id, name, file_path, summary, evidence_json, input_digest, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (service_id, name, file_path, *payload),
        )
        component_id = cur.lastrowid
    conn.commit()
    return component_id


def prune_components_not_in(conn: sqlite3.Connection, service_id: int, keep_keys: set[tuple[str, str]]) -> None:
    rows = conn.execute("SELECT id, name, file_path FROM components WHERE service_id = ?", (service_id,)).fetchall()
    for row in rows:
        if (row["name"], row["file_path"]) not in keep_keys:
            conn.execute("DELETE FROM components WHERE id = ?", (row["id"],))
    conn.commit()


def list_components(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, file_path, summary, evidence_json, input_digest FROM components WHERE service_id = ? ORDER BY name",
        (service_id,),
    ).fetchall()
