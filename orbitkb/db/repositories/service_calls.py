"""The `service_calls` table: outbound dependency edges from one service/API to
another service, queue or third-party integration."""
from __future__ import annotations

import json
import sqlite3

from orbitkb.discovery.integration_heuristics import (
    classify_resource_type,
    classify_target_kind,
)

from ._util import now


def replace_calls_for_api(
    conn: sqlite3.Connection, from_service_id: int, api_id: int, calls: list[dict], evidence: list[dict],
    *, commit: bool = True,
) -> None:
    conn.execute("DELETE FROM service_calls WHERE from_api_id = ?", (api_id,))
    evidence_json = json.dumps(evidence)
    conn.executemany(
        """INSERT INTO service_calls
           (from_service_id, from_api_id, to_service_name, call_kind, reason, data_needed,
            purpose_kind, confidence, target_kind, resource_type, evidence_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                from_service_id,
                api_id,
                c["to_service_name"],
                c["call_kind"],
                c.get("reason"),
                json.dumps(c.get("data_needed", [])),
                c.get("purpose_kind"),
                c.get("confidence"),
                c.get("target_kind", "unknown"),
                c.get("resource_type") or "not_applicable",
                evidence_json,
                now(),
            )
            for c in calls
        ],
    )
    if commit:
        conn.commit()
    reconcile_service_call_targets(conn, service_id=from_service_id, commit=commit)


def reconcile_service_call_targets(
    conn: sqlite3.Connection, service_id: int | None = None, *, commit: bool = True,
) -> None:
    """Resolve to_service_id by exact name within the caller's repository, then refine target_kind:

    - Ground truth wins: any call that resolves to a real indexed service in the
      same repository is
      'internal', full stop, overriding whatever the LLM guessed earlier.
    - For calls that stay unresolved and whose target_kind is still 'unknown' (the
      LLM couldn't tell from the code alone), fall back to the deterministic
      vendor-name / naming-convention heuristic — never LLM-driven, purely a name
      match against the vendor list and the shape of already-known service names.

    Reconciling one row only ever depends on that row's own to_service_name plus the
    services table — never on any other service_calls row — so scoping to
    service_id (this call's own writer) is exact, not an approximation: pass it after
    writing one service's calls (replace_calls_for_api already does). Call it
    unscoped (service_id=None) once after a service is newly indexed, to resolve any
    *other* service's previously-dangling calls that named it before it existed —
    that's the one direction that genuinely needs the whole table.
    """
    # Two fully-literal query variants (scoped/unscoped) instead of interpolating a
    # WHERE-clause fragment into an f-string: functionally the same two outcomes as
    # before, but each statement is now a plain string constant end to end, with the
    # only variable part passed as a bound parameter — not a pattern a SQL-injection
    # scanner (or a future reader) has to reason about the safety of.
    scope_params = (service_id,) if service_id is not None else ()

    if service_id is not None:
        conn.execute(
            """
            UPDATE service_calls
            SET to_service_id = (
                SELECT target.id FROM services target
                JOIN services source ON source.id = service_calls.from_service_id
                WHERE target.name = service_calls.to_service_name
                  AND target.repository_id IS source.repository_id
            )
            WHERE (to_service_id IS NULL
               OR to_service_id != (
                    SELECT target.id FROM services target
                    JOIN services source ON source.id = service_calls.from_service_id
                    WHERE target.name = service_calls.to_service_name
                      AND target.repository_id IS source.repository_id
               ))
               AND from_service_id = ?
            """,
            scope_params,
        )
        conn.execute(
            "UPDATE service_calls SET target_kind = 'internal' "
            "WHERE to_service_id IS NOT NULL AND from_service_id = ?",
            scope_params,
        )
        conn.execute(
            "UPDATE service_calls SET target_kind = 'unknown' "
            "WHERE to_service_id IS NULL AND target_kind = 'internal' AND from_service_id = ?",
            scope_params,
        )
    else:
        conn.execute(
            """
            UPDATE service_calls
            SET to_service_id = (
                SELECT target.id FROM services target
                JOIN services source ON source.id = service_calls.from_service_id
                WHERE target.name = service_calls.to_service_name
                  AND target.repository_id IS source.repository_id
            )
            WHERE (to_service_id IS NULL
               OR to_service_id != (
                    SELECT target.id FROM services target
                    JOIN services source ON source.id = service_calls.from_service_id
                    WHERE target.name = service_calls.to_service_name
                      AND target.repository_id IS source.repository_id
               ))
            """
        )
        conn.execute("UPDATE service_calls SET target_kind = 'internal' WHERE to_service_id IS NOT NULL")
        conn.execute("UPDATE service_calls SET target_kind = 'unknown' WHERE to_service_id IS NULL AND target_kind = 'internal'")

    if service_id is not None:
        unresolved_unknown = conn.execute(
            "SELECT sc.id, sc.to_service_name, s.repository_id FROM service_calls sc "
            "JOIN services s ON s.id = sc.from_service_id "
            "WHERE sc.to_service_id IS NULL AND sc.target_kind = 'unknown' AND sc.from_service_id = ?",
            scope_params,
        ).fetchall()
    else:
        unresolved_unknown = conn.execute(
            "SELECT sc.id, sc.to_service_name, s.repository_id FROM service_calls sc "
            "JOIN services s ON s.id = sc.from_service_id "
            "WHERE sc.to_service_id IS NULL AND sc.target_kind = 'unknown'"
        ).fetchall()
    names_by_repository: dict[int | None, set[str]] = {}
    for row in unresolved_unknown:
        repository_id = row["repository_id"]
        if repository_id not in names_by_repository:
            names_by_repository[repository_id] = {
                service["name"]
                for service in conn.execute(
                    "SELECT name FROM services WHERE repository_id IS ?", (repository_id,)
                )
            }
        kind = classify_target_kind(row["to_service_name"], names_by_repository[repository_id])
        if kind != "unknown":
            conn.execute("UPDATE service_calls SET target_kind = ? WHERE id = ?", (kind, row["id"]))

    # Same fallback posture as target_kind: only fills a gap the LLM itself left
    # unresolved ('not_applicable'/'unknown'), and only for calls that ended up
    # 'external' — resource_type is meaningless for internal/unknown targets.
    if service_id is not None:
        unresolved_resource_type = conn.execute(
            "SELECT id, to_service_name FROM service_calls "
            "WHERE target_kind = 'external' AND resource_type IN ('not_applicable', 'unknown') AND from_service_id = ?",
            scope_params,
        ).fetchall()
    else:
        unresolved_resource_type = conn.execute(
            "SELECT id, to_service_name FROM service_calls "
            "WHERE target_kind = 'external' AND resource_type IN ('not_applicable', 'unknown')"
        ).fetchall()
    for row in unresolved_resource_type:
        resource_type = classify_resource_type(row["to_service_name"])
        if resource_type != "unknown":
            conn.execute("UPDATE service_calls SET resource_type = ? WHERE id = ?", (resource_type, row["id"]))

    if commit:
        conn.commit()


def list_calls_for_service(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT to_service_id, to_service_name, call_kind, reason, data_needed, purpose_kind, confidence,
                  target_kind, resource_type, evidence_json
           FROM service_calls WHERE from_service_id = ? ORDER BY to_service_name""",
        (service_id,),
    ).fetchall()


def list_calls_for_api(conn: sqlite3.Connection, api_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT to_service_name, call_kind, reason, data_needed, purpose_kind, confidence,
                  target_kind, resource_type, evidence_json
           FROM service_calls WHERE from_api_id = ? ORDER BY to_service_name""",
        (api_id,),
    ).fetchall()


def list_inbound_calls(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    """Calls made BY other services INTO this one — 'who depends on me'.

    Only resolves edges whose target has been reconciled to a real service_id
    (see reconcile_service_call_targets); a still-dangling to_service_name from
    an unindexed service can't be attributed to a from_service row here.
    """
    return conn.execute(
        """SELECT s.name AS from_service_name, sc.call_kind, sc.reason, sc.data_needed,
                  sc.purpose_kind, sc.confidence, sc.target_kind, sc.evidence_json
           FROM service_calls sc JOIN services s ON s.id = sc.from_service_id
           WHERE sc.to_service_id = ? ORDER BY s.name""",
        (service_id,),
    ).fetchall()


def list_external_integration_calls(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    """This service's outbound calls classified as third-party (target_kind='external')."""
    return conn.execute(
        """SELECT to_service_name, call_kind, reason, confidence, evidence_json
           FROM service_calls WHERE from_service_id = ? AND target_kind = 'external'
           ORDER BY to_service_name""",
        (service_id,),
    ).fetchall()


def list_unmapped_internal_calls(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    """This service's outbound calls that look internal but haven't been indexed yet."""
    return conn.execute(
        """SELECT to_service_name, call_kind, reason, confidence, evidence_json
           FROM service_calls
           WHERE from_service_id = ? AND target_kind = 'internal' AND to_service_id IS NULL
           ORDER BY to_service_name""",
        (service_id,),
    ).fetchall()


def list_internal_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every resolved service-to-service edge in the whole system — the topology
    diagram's internal edge set (export/mermaid.py)."""
    return conn.execute(
        """SELECT DISTINCT s1.name AS from_name, s2.name AS to_name, sc.call_kind, sc.reason
           FROM service_calls sc
           JOIN services s1 ON s1.id = sc.from_service_id
           JOIN services s2 ON s2.id = sc.to_service_id
           WHERE sc.target_kind = 'internal'
           ORDER BY from_name, to_name"""
    ).fetchall()


def list_external_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every service-to-vendor edge in the whole system — the topology diagram's
    external edge set (export/mermaid.py)."""
    return conn.execute(
        """SELECT DISTINCT s.name AS from_name, sc.to_service_name, sc.resource_type
           FROM service_calls sc
           JOIN services s ON s.id = sc.from_service_id
           WHERE sc.target_kind = 'external'
           ORDER BY from_name, to_service_name"""
    ).fetchall()


def list_unresolved_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Indexed calls whose target cannot yet be resolved or classified."""
    return conn.execute(
        """SELECT DISTINCT s.name AS from_name, sc.to_service_name, sc.call_kind
           FROM service_calls sc
           JOIN services s ON s.id = sc.from_service_id
           WHERE sc.to_service_id IS NULL AND sc.target_kind = 'unknown'
           ORDER BY from_name, to_service_name, call_kind"""
    ).fetchall()
