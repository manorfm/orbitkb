"""Opaque source-method digests used for route invalidation."""

import sqlite3


def read_digests(conn: sqlite3.Connection, service_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT unit_key, content_digest FROM source_unit_digests WHERE service_id = ?", (service_id,),
    ).fetchall()
    return {row["unit_key"]: row["content_digest"] for row in rows}


def replace_digests(conn: sqlite3.Connection, service_id: int, digests: dict[str, tuple[str, str]]) -> None:
    conn.execute("DELETE FROM source_unit_digests WHERE service_id = ?", (service_id,))
    conn.executemany(
        "INSERT INTO source_unit_digests (service_id, unit_key, fact_id, content_digest) VALUES (?, ?, ?, ?)",
        [(service_id, unit_key, fact_id, digest) for unit_key, (fact_id, digest) in digests.items()],
    )
