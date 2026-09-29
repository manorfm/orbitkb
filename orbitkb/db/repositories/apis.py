"""The `apis` and `api_validations` tables: one microservice's HTTP endpoints and
the validation/authorization rules attached to each."""
from __future__ import annotations

import json
import sqlite3

from ._util import now


def upsert_api(
    conn: sqlite3.Connection,
    service_id: int,
    method: str,
    path: str,
    summary: str,
    description: str,
    response_shape: dict,
    evidence: list[dict],
    request_shape: list[dict] | None = None,
    *,
    commit: bool = True,
) -> int:
    row = conn.execute(
        "SELECT id FROM apis WHERE service_id = ? AND method = ? AND path = ?",
        (service_id, method, path),
    ).fetchone()
    payload = (
        summary, description, json.dumps(response_shape), json.dumps(request_shape or []),
        json.dumps(evidence), now(),
    )
    if row is not None:
        conn.execute(
            """UPDATE apis SET summary = ?, description = ?, response_shape = ?, request_shape = ?,
               evidence_json = ?, updated_at = ? WHERE id = ?""",
            (*payload, row["id"]),
        )
        api_id = row["id"]
    else:
        cur = conn.execute(
            """INSERT INTO apis (service_id, method, path, summary, description, response_shape,
               request_shape, evidence_json, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, method, path, *payload),
        )
        api_id = cur.lastrowid
    if commit:
        conn.commit()
    return api_id


def replace_api_validations(
    conn: sqlite3.Connection, api_id: int, validations: list[dict], *, commit: bool = True,
) -> None:
    conn.execute("DELETE FROM api_validations WHERE api_id = ?", (api_id,))
    conn.executemany(
        "INSERT INTO api_validations (api_id, kind, description) VALUES (?, ?, ?)",
        [(api_id, v["kind"], v["description"]) for v in validations],
    )
    if commit:
        conn.commit()


def prune_apis_not_in(conn: sqlite3.Connection, service_id: int, keep_keys: set[tuple[str, str]]) -> None:
    rows = conn.execute("SELECT id, method, path FROM apis WHERE service_id = ?", (service_id,)).fetchall()
    for row in rows:
        if (row["method"], row["path"]) not in keep_keys:
            conn.execute("DELETE FROM apis WHERE id = ?", (row["id"],))
    conn.commit()


def get_api_by_key(conn: sqlite3.Connection, service_id: int, method: str, path: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM apis WHERE service_id = ? AND method = ? AND path = ?", (service_id, method, path)
    ).fetchone()


def list_api_keys(conn: sqlite3.Connection, service_id: int) -> set[tuple[str, str]]:
    return {
        (row["method"], row["path"])
        for row in conn.execute("SELECT method, path FROM apis WHERE service_id = ?", (service_id,))
    }


def list_apis(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT method, path, summary, description, evidence_json FROM apis WHERE service_id = ? ORDER BY path, method",
        (service_id,),
    ).fetchall()


def list_validations_for_api(conn: sqlite3.Connection, api_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT kind, description FROM api_validations WHERE api_id = ? ORDER BY kind", (api_id,)
    ).fetchall()
