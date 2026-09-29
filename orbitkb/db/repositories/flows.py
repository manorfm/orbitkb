"""Persistence for bounded, deterministic entrypoint flow maps."""
from __future__ import annotations

import json
import sqlite3

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.models import AnalysisResult, EntryPoint
from orbitkb.db.repositories import canonical_snapshots
from orbitkb.db.repositories._util import now


def replace_analysis(conn: sqlite3.Connection, service_id: int, analysis: AnalysisResult) -> None:
    """Atomically replace one service's static analysis after a source scan."""
    snapshot = project_analysis(canonical_snapshots.service_key(conn, service_id), analysis)
    conn.execute("SAVEPOINT replace_analysis")
    try:
        _replace_analysis_rows(conn, service_id, analysis)
        canonical_snapshots.replace_snapshot(conn, service_id, snapshot)
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT replace_analysis")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT replace_analysis")


def _replace_analysis_rows(conn: sqlite3.Connection, service_id: int, analysis: AnalysisResult) -> None:
    conn.execute("DELETE FROM flow_edges WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM flow_boundaries WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_error_contracts WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_service_calls WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_resilience_policies WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM entrypoints WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_message_contracts WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_persistence_facts WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_migration_facts WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_configuration_bindings WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_feature_flags WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_cloud_facts WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_security_requirements WHERE service_id = ?", (service_id,))
    conn.execute("DELETE FROM static_api_headers WHERE service_id = ?", (service_id,))
    indexed_at = now()
    entrypoint_ids: dict[str, int] = {}
    persisted_entrypoints: list[tuple[EntryPoint, int]] = []
    for entry in analysis.entrypoints:
        cursor = conn.execute(
            """INSERT INTO entrypoints
               (service_id, kind, method, name, symbol, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, entry.kind, entry.method, entry.name, entry.symbol,
                entry.evidence.file_path, entry.evidence.start_line, entry.evidence.end_line, indexed_at,
            ),
        )
        entrypoint_ids[entry.symbol] = cursor.lastrowid
        persisted_entrypoints.append((entry, cursor.lastrowid))
    for entry, entrypoint_id in persisted_entrypoints:
        contract = entry.contract or analysis.contracts.get(entry.symbol)
        if contract is not None:
            conn.execute(
                "INSERT INTO entrypoint_contracts (entrypoint_id, contract_json) VALUES (?, ?)",
                (entrypoint_id, json.dumps(contract)),
            )
    for edge in analysis.edges:
        conn.execute(
            """INSERT INTO flow_edges
               (service_id, entrypoint_id, from_symbol, to_symbol, kind, confidence, origin, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, entrypoint_ids.get(edge.source), edge.source, edge.target,
                edge.kind, edge.confidence, edge.origin, edge.evidence.file_path,
                edge.evidence.start_line, edge.evidence.end_line, indexed_at,
            ),
        )
    for contract in analysis.message_contracts:
        conn.execute(
            """INSERT INTO static_message_contracts
               (service_id, direction, channel, routing_key, payload_type, message_version, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, contract.direction, contract.channel, contract.routing_key, contract.payload_type, contract.message_version,
             contract.evidence.file_path, contract.evidence.start_line, contract.evidence.end_line, indexed_at),
        )
    for boundary in analysis.boundaries:
        conn.execute(
            """INSERT INTO flow_boundaries (service_id, source, kind, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (service_id, boundary.source, boundary.kind, boundary.evidence.file_path,
             boundary.evidence.start_line, boundary.evidence.end_line, indexed_at),
        )
    for contract in analysis.error_contracts:
        conn.execute(
            """INSERT INTO static_error_contracts
               (service_id, source, role, error_kind, internal_type, protocol,
                transport_code, public_code, exposes_internal_detail, retryability,
                file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, contract.source, contract.role, contract.error_kind,
                contract.internal_type, contract.protocol, contract.transport_code,
                contract.public_code, int(contract.exposes_internal_detail),
                contract.retryability, contract.evidence.file_path,
                contract.evidence.start_line, contract.evidence.end_line, indexed_at,
            ),
        )
    for call in analysis.static_service_calls:
        conn.execute(
            """INSERT INTO static_service_calls
               (service_id, source, target_service, protocol, target_method, target_path,
                file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, call.source, call.target_service, call.protocol,
                call.target_method, call.target_path, call.evidence.file_path,
                call.evidence.start_line, call.evidence.end_line, indexed_at,
            ),
        )
    for policy in analysis.resilience_policies:
        conn.execute(
            """INSERT INTO static_resilience_policies
               (service_id, source, kind, mechanism, value, unit,
                file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, policy.source, policy.kind, policy.mechanism,
                policy.value, policy.unit, policy.evidence.file_path,
                policy.evidence.start_line, policy.evidence.end_line, indexed_at,
            ),
        )
    for fact in analysis.persistence_facts:
        conn.execute(
            """INSERT INTO static_persistence_facts
               (service_id, name, kind, owner, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, fact.name, fact.kind, fact.owner, fact.evidence.file_path,
             fact.evidence.start_line, fact.evidence.end_line, indexed_at),
        )
    for fact in analysis.migration_facts:
        conn.execute(
            """INSERT INTO static_migration_facts
               (service_id, operation, table_name, column_name, destructive, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, fact.operation, fact.table_name, fact.column_name, int(fact.destructive),
                fact.evidence.file_path, fact.evidence.start_line, fact.evidence.end_line, indexed_at,
            ),
        )
    for binding in analysis.configuration_bindings:
        conn.execute(
            """INSERT INTO static_configuration_bindings
               (service_id, source, key, kind, sensitive, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, binding.source, binding.key, binding.kind, int(binding.sensitive),
                binding.evidence.file_path, binding.evidence.start_line, binding.evidence.end_line, indexed_at,
            ),
        )
    for flag in analysis.feature_flags:
        conn.execute(
            """INSERT INTO static_feature_flags
               (service_id, source, key, provider, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, flag.source, flag.key, flag.provider,
                flag.evidence.file_path, flag.evidence.start_line,
                flag.evidence.end_line, indexed_at,
            ),
        )
    for fact in analysis.cloud_facts:
        conn.execute(
            """INSERT INTO static_cloud_facts
               (service_id, provider, resource_type, service_name, operation, operation_kind,
                sdk, target_name, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (service_id, fact.provider, fact.resource_type, fact.service_name, fact.operation,
             fact.operation_kind, fact.sdk, fact.target_name, fact.evidence.file_path,
             fact.evidence.start_line, fact.evidence.end_line, indexed_at),
        )
    for requirement in analysis.security_requirements:
        conn.execute(
            """INSERT INTO static_security_requirements
               (service_id, route_pattern, method, symbol, requirement, roles_json,
                file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, requirement.route_pattern, requirement.method, requirement.symbol,
                requirement.requirement, json.dumps(list(requirement.roles)),
                requirement.evidence.file_path, requirement.evidence.start_line,
                requirement.evidence.end_line, indexed_at,
            ),
        )
    for header in analysis.api_headers:
        conn.execute(
            """INSERT INTO static_api_headers
               (service_id, method, path, direction, name, file_path, start_line, end_line, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                service_id, header.method, header.path, header.direction, header.name,
                header.evidence.file_path, header.evidence.start_line, header.evidence.end_line, indexed_at,
            ),
        )


def list_entrypoints(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entrypoints WHERE service_id = ? ORDER BY kind, method, name", (service_id,)
    ).fetchall()


def get_entrypoint(conn: sqlite3.Connection, service_id: int, kind: str, method: str, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entrypoints WHERE service_id = ? AND kind = ? AND method = ? AND name = ?",
        (service_id, kind, method.upper(), name),
    ).fetchone()


def list_entrypoint_edges(conn: sqlite3.Connection, entrypoint_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM flow_edges WHERE entrypoint_id = ? ORDER BY id", (entrypoint_id,)
    ).fetchall()


def list_flow_edges(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    """All static/provider/runtime-indexed flow edges for one service."""
    return conn.execute(
        "SELECT from_symbol, to_symbol, kind FROM flow_edges WHERE service_id = ?", (service_id,)
    ).fetchall()


def get_entrypoint_contract(conn: sqlite3.Connection, entrypoint_id: int) -> dict | None:
    row = conn.execute(
        "SELECT contract_json FROM entrypoint_contracts WHERE entrypoint_id = ?", (entrypoint_id,)
    ).fetchone()
    return json.loads(row["contract_json"]) if row else None


def list_static_message_contracts(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT direction, channel, routing_key, payload_type, message_version, file_path, start_line, end_line
           FROM static_message_contracts WHERE service_id = ? ORDER BY channel, routing_key""",
        (service_id,),
    ).fetchall()


def list_static_error_contracts(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return _list_static_error_contracts(conn, service_id)


def list_static_service_calls(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return _list_static_service_calls(conn, service_id)


def list_static_resilience_policies(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return _list_static_resilience_policies(conn, service_id)


def list_static_resilience_policies_for_sources(
    conn: sqlite3.Connection, service_id: int, sources: set[str],
) -> list[sqlite3.Row]:
    if not sources:
        return []
    return _list_static_resilience_policies(conn, service_id, sources)


def _list_static_resilience_policies(
    conn: sqlite3.Connection, service_id: int, sources: set[str] | None = None,
) -> list[sqlite3.Row]:
    source_filter = ""
    params: list[object] = [service_id]
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        source_filter = f" AND source IN ({placeholders})"
        params.extend(sorted(sources))
    query = (
        "SELECT source, kind, mechanism, value, unit, file_path, start_line, end_line "  # nosec B608 - source_filter is either empty or IN (?, ?, ...) with bound placeholders; never raw input.
        "FROM static_resilience_policies WHERE service_id = ?" + source_filter +
        " ORDER BY source, kind, mechanism, value, file_path, start_line"
    )
    return conn.execute(query, params).fetchall()


def list_static_service_calls_for_sources(
    conn: sqlite3.Connection, service_id: int, sources: set[str],
) -> list[sqlite3.Row]:
    if not sources:
        return []
    return _list_static_service_calls(conn, service_id, sources)


def _list_static_service_calls(
    conn: sqlite3.Connection, service_id: int, sources: set[str] | None = None,
) -> list[sqlite3.Row]:
    source_filter = ""
    params: list[object] = [service_id]
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        source_filter = f" AND source IN ({placeholders})"
        params.extend(sorted(sources))
    query = (
        "SELECT source, target_service, protocol, target_method, target_path, "  # nosec B608 - source_filter is either empty or IN (?, ?, ...) with bound placeholders; never raw input.
        "file_path, start_line, end_line FROM static_service_calls WHERE service_id = ?" + source_filter +
        " ORDER BY source, target_service, target_method, target_path, file_path, start_line"
    )
    return conn.execute(query, params).fetchall()


def list_static_error_contracts_for_sources(
    conn: sqlite3.Connection, service_id: int, sources: set[str],
) -> list[sqlite3.Row]:
    if not sources:
        return []
    return _list_static_error_contracts(conn, service_id, sources)


def _list_static_error_contracts(
    conn: sqlite3.Connection, service_id: int, sources: set[str] | None = None,
) -> list[sqlite3.Row]:
    source_filter = ""
    params: list[object] = [service_id]
    if sources:
        placeholders = ", ".join("?" for _ in sources)
        source_filter = f" AND source IN ({placeholders})"
        params.extend(sorted(sources))
    query = (
        "SELECT source, role, error_kind, internal_type, protocol, transport_code, "  # nosec B608 - source_filter is either empty or IN (?, ?, ...) with bound placeholders; never raw input.
        "public_code, exposes_internal_detail, retryability, file_path, start_line, end_line "
        "FROM static_error_contracts WHERE service_id = ?" + source_filter +
        " ORDER BY source, role, internal_type, transport_code, file_path, start_line"
    )
    return conn.execute(query, params).fetchall()


def list_static_persistence_facts(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT name, kind, owner, file_path, start_line, end_line FROM static_persistence_facts WHERE service_id = ? ORDER BY name",
        (service_id,),
    ).fetchall()


def list_static_migration_facts(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT operation, table_name, column_name, destructive, file_path, start_line, end_line
           FROM static_migration_facts WHERE service_id = ? ORDER BY file_path, start_line, operation""",
        (service_id,),
    ).fetchall()


def list_static_configuration_bindings(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT source, key, kind, sensitive, file_path, start_line, end_line
           FROM static_configuration_bindings WHERE service_id = ? ORDER BY key, source""",
        (service_id,),
    ).fetchall()


def list_static_feature_flags(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT source, key, provider, file_path, start_line, end_line
           FROM static_feature_flags WHERE service_id = ? ORDER BY key, source""",
        (service_id,),
    ).fetchall()


def list_static_security_requirements(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT route_pattern, method, symbol, requirement, roles_json, file_path, start_line, end_line
           FROM static_security_requirements WHERE service_id = ?
           ORDER BY route_pattern, method, symbol""",
        (service_id,),
    ).fetchall()


def list_static_api_headers_for_route(
    conn: sqlite3.Connection, service_id: int, method: str, path: str,
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT direction, name, file_path, start_line, end_line FROM static_api_headers
           WHERE service_id = ? AND method = ? AND path = ? ORDER BY direction, name""",
        (service_id, method, path),
    ).fetchall()


def list_static_security_requirements_in_declaration_order(
    conn: sqlite3.Connection, service_id: int,
) -> list[sqlite3.Row]:
    """Same rows as `list_static_security_requirements`, ordered as originally
    declared (insertion order) rather than alphabetically -- needed to correlate
    a route to the requirement that actually governs it, since Spring Security
    itself evaluates `authorizeHttpRequests` rules in declaration order and stops
    at the first match; a later, broader rule (e.g. a trailing catch-all) must
    never be preferred over an earlier, more specific one just because it sorts
    first alphabetically.
    """
    return conn.execute(
        """SELECT route_pattern, method, symbol, requirement, roles_json, file_path, start_line, end_line
           FROM static_security_requirements WHERE service_id = ? ORDER BY id""",
        (service_id,),
    ).fetchall()


def list_static_cloud_facts(conn: sqlite3.Connection, service_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT provider, resource_type, service_name, operation, operation_kind, sdk, target_name,
                  file_path, start_line, end_line
           FROM static_cloud_facts WHERE service_id = ? ORDER BY service_name, operation""",
        (service_id,),
    ).fetchall()


def list_all_static_cloud_facts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every service's cloud fact in the whole system — the topology diagram's
    cloud edge set (export/mermaid.py), same posture as service_calls'
    list_external_edges."""
    return conn.execute(
        """SELECT DISTINCT s.name AS from_name, scf.provider, scf.resource_type,
                  scf.service_name, scf.target_name
           FROM static_cloud_facts scf
           JOIN services s ON s.id = scf.service_id
           ORDER BY from_name, scf.service_name"""
    ).fetchall()
