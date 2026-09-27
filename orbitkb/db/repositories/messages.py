"""The `messages` table: what one microservice publishes/consumes, plus the
name-based publish<->consume linking used to connect services through a channel."""
from __future__ import annotations

import json
import sqlite3

from ._util import now


def replace_messages(conn: sqlite3.Connection, service_id: int, messages: list[dict], evidence: list[dict]) -> None:
    conn.execute("DELETE FROM messages WHERE service_id = ?", (service_id,))
    evidence_json = json.dumps(evidence)
    conn.executemany(
        """INSERT INTO messages (service_id, direction, channel, shape_json, description, provider,
           evidence_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                service_id,
                m["direction"],
                m["channel"],
                json.dumps(m.get("shape_json", {})),
                m.get("description"),
                m.get("provider") or "unknown",
                evidence_json,
                now(),
            )
            for m in messages
        ],
    )
    conn.commit()


def list_messages(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT direction, channel, shape_json, description, provider, evidence_json
           FROM messages WHERE service_id = ? ORDER BY channel""",
        (service_id,),
    ).fetchall()


def list_all_message_links(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every publisher/consumer pair connected through a shared channel name, once each
    — the topology diagram's message-link edge set (export/mermaid.py). Direction-
    constrained in SQL (m1 = publishes, m2 = consumes) so a pair never appears twice."""
    return conn.execute(
        """
        SELECT DISTINCT m1.channel AS channel, s1.name AS publisher, s2.name AS consumer
        FROM messages m1
        JOIN messages m2 ON m2.channel = m1.channel AND m2.service_id != m1.service_id
                         AND m1.direction = 'publishes' AND m2.direction = 'consumes'
        JOIN services s1 ON s1.id = m1.service_id
        JOIN services s2 ON s2.id = m2.service_id
        ORDER BY channel, publisher, consumer
        """
    ).fetchall()


def list_unmatched_message_channels(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every message row with no opposite-direction counterpart from a *different*
    service on the same channel — the complement of `list_all_message_links`. Covers
    a single-service repo publishing/consuming a real broker with no other indexed
    service on that channel, which `list_all_message_links` otherwise drops silently
    (same posture as `list_all_static_cloud_facts`: a fact about one service's own
    external target, no second service required)."""
    return conn.execute(
        """
        SELECT s.name AS service, m1.direction AS direction, m1.channel AS channel, m1.provider AS provider
        FROM messages m1
        JOIN services s ON s.id = m1.service_id
        WHERE NOT EXISTS (
            SELECT 1 FROM messages m2
            WHERE m2.channel = m1.channel AND m2.service_id != m1.service_id AND m2.direction != m1.direction
        )
        ORDER BY s.name, m1.channel
        """
    ).fetchall()


def list_message_links(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    """Other services connected to this one through a shared channel name.

    A 'publishes' row on this service links to every other service that 'consumes'
    the same channel, and vice versa. Purely a name match at query time — no extra
    storage, since channel is already the shared key both sides record.
    """
    return conn.execute(
        """
        SELECT m1.direction AS local_direction, m1.channel AS channel, s2.name AS other_service
        FROM messages m1
        JOIN messages m2 ON m2.channel = m1.channel AND m2.service_id != m1.service_id
                         AND m2.direction != m1.direction
        JOIN services s2 ON s2.id = m2.service_id
        WHERE m1.service_id = ?
        ORDER BY m1.channel, s2.name
        """,
        (service_id,),
    ).fetchall()
