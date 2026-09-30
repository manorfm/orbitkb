"""Read helpers backing the MCP tools. Thin JSON-shaping wrappers over the
db.repositories.* modules (and, for find_change_surface, over generation.change_surface)."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import sqlite3
from collections import deque
from collections.abc import Mapping
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.smells import find_entrypoint_smells
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import architecture as architecture_repo
from orbitkb.db.repositories import canonical_snapshots as canonical_snapshots_repo
from orbitkb.db.repositories import change_closure_summaries as closure_summaries_repo
from orbitkb.db.repositories import change_plans as change_plans_repo
from orbitkb.db.repositories import change_surface as change_surface_repo
from orbitkb.db.repositories import ci_commands as ci_commands_repo
from orbitkb.db.repositories import ci_validation_results as ci_validation_results_repo
from orbitkb.db.repositories import cloud_iac as cloud_iac_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import context_telemetry as context_telemetry_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import (
    kubernetes_configuration as kubernetes_configuration_repo,
)
from orbitkb.db.repositories import (
    manual_validation_results as manual_validation_results_repo,
)
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import runtime_evidence as runtime_evidence_repo
from orbitkb.db.repositories import search as search_repo
from orbitkb.db.repositories import security_findings as security_findings_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.discovery.hashing import git_working_changed_files_with_status
from orbitkb.domain.canonical import EntrypointKey, SymbolKey
from orbitkb.domain.navigation import (
    KnowledgeNavigator,
    TraversalPolicy,
    TraversalResult,
)
from orbitkb.domain.route_calls import route_declared_http_calls
from orbitkb.export.mermaid import (
    generate_entrypoint_sequence,
    generate_topology_diagram,
)
from orbitkb.generation import change_surface
from orbitkb.generation.architecture import diff_architecture_runs
from orbitkb.generation.backend_base import LLMBackend
from orbitkb.generation.change_assessment import (
    assess_change_units,
    changed_files_touch_service_roots,
    summarize_change_closure,
)
from orbitkb.generation.change_context import MAX_CONTEXT_SERVICES, build_change_context
from orbitkb.generation.change_plan import (
    derive_aggregate_ownership_review_units,
    derive_broad_timeout_handler_review_units,
    derive_change_units,
    derive_cloud_dead_letter_queue_review_units,
    derive_cloud_dependency_iac_review_units,
    derive_cloud_encryption_review_units,
    derive_cloud_versioning_review_units,
    derive_decision_points,
    derive_error_mapping_review_units,
    derive_feature_flag_review_units,
    derive_http_resilience_policy_review_units,
    derive_message_consumer_recovery_review_units,
    derive_non_atomic_service_publish_review_units,
    derive_partial_write_resilience_review_units,
    derive_persistence_migration_review_units,
    derive_public_object_storage_review_units,
    derive_read_entrypoint_side_effect_review_units,
    derive_retry_consumer_delivery_review_units,
    derive_retry_delivery_review_units,
    derive_retry_downstream_error_review_units,
    derive_retry_http_idempotency_review_units,
    derive_retry_policy_review_units,
    derive_retry_unrecovered_consumer_delivery_review_units,
    derive_retry_write_publish_review_units,
    derive_runtime_configuration_mismatch_review_units,
    derive_runtime_configuration_review_units,
    derive_runtime_configuration_source_import_unknown_review_units,
    derive_runtime_configuration_source_unknown_review_units,
    derive_timeout_fallback_review_units,
    derive_timeout_local_fallback_review_units,
    validate_decision_selections,
)
from orbitkb.generation.freshness import compute_freshness
from orbitkb.generation.provenance import infer_provenance
from orbitkb.generation.token_budget import TokenMeasurement, measure_json_tokens
from orbitkb.generation.verification import (
    verify_change_surface as _verify_change_surface,
)
from orbitkb.generation.verification import (
    verify_context_budget as _verify_context_budget,
)

# Progressive-disclosure budget for list-shaped MCP responses (describe_service's own
# lists, list_apis, describe_persistence, describe_messages): a real service can have
# far more endpoints/entities/messages than a benchmark fixture, so every such list is
# capped by default instead of returned whole — see README's context-efficiency notes.
DEFAULT_LIST_LIMIT = 50
MAX_RUNTIME_SOURCES_PER_CONFIGURATION_BINDING = 3
MAX_LIST_LIMIT = 500
MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS = 4
MAX_INLINE_WORKLOAD_EVIDENCE = 5
DEFAULT_FLOW_EDGE_LIMIT = 50
MAX_FLOW_EDGE_LIMIT = 200
# A sequence diagram becomes unreadable well before max_edges' own ceiling does --
# deliberately smaller and not caller-configurable, since it only trims how much
# of the already-fetched `edges` gets rendered as a diagram, never what's fetched.
SEQUENCE_DIAGRAM_EDGE_LIMIT = 20
DEFAULT_PLAN_TOKEN_BUDGET = 2200
MAX_PLAN_TOKEN_BUDGET = 2200
MAX_PLAN_CI_VALIDATION_COMMANDS = 3
MAX_CI_VALIDATION_DURATION_MS = 86_400_000
DEFAULT_PLAN_VALIDATION_UNIT_LIMIT = 20
MAX_PLAN_VALIDATION_UNIT_LIMIT = 50
MAX_CLOSURE_CHANGE_UNIT_IDS = 3
_FLOW_KINDS = {"invokes", "injects", "validates", "reads", "writes", "publishes", "consumes"}
_EPIC_TYPE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_INDEXING_CAPABILITIES = [
    {
        "stack": "node-ts",
        "languages": ["javascript", "typescript"],
        "entrypoint_kinds": ["http", "graphql"],
        "error_contract_protocols": ["http", "graphql"],
        "known_unknowns": ["dynamic_routes", "global_error_middleware"],
    },
    {
        "stack": "jvm-spring",
        "languages": ["java", "kotlin"],
        "entrypoint_kinds": ["http", "grpc"],
        "error_contract_protocols": ["http"],
        "known_unknowns": ["dynamic_configuration", "framework_global_error_boundaries"],
    },
    {
        "stack": "go",
        "languages": ["go"],
        "entrypoint_kinds": ["http", "grpc"],
        "error_contract_protocols": ["http"],
        "known_unknowns": ["dynamic_statuses", "custom_response_writers"],
    },
]
_RUNTIME_FILTER_DIMENSION_PRIORITY = {
    "binding_evidence_ranges": 0,
    "source_import_evidence_ranges": 0,
    "binding_evidence_files": 1,
    "source_import_evidence_files": 1,
    "binding_workloads": 2,
    "source_import_workloads": 2,
    "binding_declaration_statuses": 3,
    "source_import_declaration_statuses": 3,
    "binding_source_kinds": 4,
    "source_import_source_kinds": 4,
    "source_import_availabilities": 5,
    "source_import_container_roles": 6,
    "source_import_prefixes": 7,
}
logger = logging.getLogger(__name__)


def _validate_pagination(limit: int, offset: int) -> str | None:
    if limit < 1:
        return f"limit must be >= 1 (got {limit})"
    if offset < 0:
        return f"offset must be >= 0 (got {offset})"
    return None


def _paginate(items: list, limit: int, offset: int) -> tuple[list, dict]:
    limit = min(limit, MAX_LIST_LIMIT)
    total = len(items)
    page = items[offset : offset + limit]
    return page, {"total": total, "truncated": offset + len(page) < total}


def _fmt_call(c: sqlite3.Row) -> dict:
    return {
        "to_service_name": c["to_service_name"],
        "call_kind": c["call_kind"],
        "reason": c["reason"],
        "data_needed": json.loads(c["data_needed"] or "[]"),
        "purpose_kind": c["purpose_kind"],
        "target_kind": c["target_kind"],
        "resource_type": c["resource_type"],
    }


def _resolve_service(
    conn: sqlite3.Connection, service: str, repository: str | None,
) -> tuple[sqlite3.Row | None, dict | None]:
    """Resolve a service without silently selecting a same-named repository peer."""
    if repository is not None:
        repo = repositories_repo.get_repository_by_name(conn, repository)
        if repo is None:
            return None, {"error": f"unknown repository: {repository}"}
        row = services_repo.get_service_by_name(conn, service, repository_id=repo["id"])
        return (row, None) if row is not None else (None, {"error": f"unknown service: {service} on {repository}"})
    candidates = services_repo.list_service_candidates_by_name(conn, service)
    if not candidates:
        return None, {"error": f"unknown service: {service}"}
    if len(candidates) > 1:
        return None, {
            "error": f"ambiguous service: {service}; specify repository",
            "repositories": [candidate["repository_name"] for candidate in candidates],
        }
    return candidates[0], None


def _resolve_static_service_call_target(
    conn: sqlite3.Connection,
    caller: sqlite3.Row,
    call: sqlite3.Row,
    cache: dict[tuple[object, ...], dict],
) -> dict:
    """Resolve a literal static target without choosing among repository peers."""
    key = (
        caller["repository_id"], call["target_service"], call["protocol"],
        call["target_method"], call["target_path"],
    )
    if key in cache:
        return cache[key]
    target, candidates = services_repo.resolve_service_reference(
        conn, call["target_service"], caller["repository_id"],
    )
    if target is None:
        resolution = (
            {"status": "not_indexed"}
            if not candidates
            else {
                "status": "ambiguous",
                "repositories": sorted({candidate["repository_name"] or "standalone" for candidate in candidates}),
            }
        )
        cache[key] = resolution
        return resolution

    resolution: dict = {
        "status": "service_indexed", "service": target["name"],
        "repository": target["repository_name"],
    }
    if call["protocol"] == "http" and isinstance(call["target_method"], str) and isinstance(call["target_path"], str):
        entrypoint = flows_repo.get_entrypoint(
            conn, target["id"], "http", call["target_method"], call["target_path"],
        )
        if entrypoint is not None:
            resolution["status"] = "endpoint_indexed"
            resolution["entrypoint"] = {
                "kind": entrypoint["kind"], "method": entrypoint["method"],
                "name": entrypoint["name"], "symbol": entrypoint["symbol"],
                "evidence": {
                    "file": entrypoint["file_path"], "start_line": entrypoint["start_line"],
                    "end_line": entrypoint["end_line"],
                },
            }
    cache[key] = resolution
    return resolution


def describe_indexing_capabilities() -> dict:
    """Return the conservative, initial static-analysis capability contract."""
    return {
        "capabilities": _INDEXING_CAPABILITIES,
        "guarantee": "listed facts are deterministic; unlisted behavior remains unknown",
    }


def list_repositories(conn: sqlite3.Connection) -> dict:
    rows = repositories_repo.list_repositories(conn)
    return {
        "repositories": [
            {"name": r["name"], "root_path": r["root_path"], "service_count": r["service_count"]}
            for r in rows
        ]
    }


def list_services(conn: sqlite3.Connection, repository: str | None = None) -> dict:
    repo_id = None
    if repository is not None:
        repo = repositories_repo.get_repository_by_name(conn, repository)
        if repo is None:
            return {"error": f"unknown repository: {repository}"}
        repo_id = repo["id"]
    rows = services_repo.list_services(conn, repo_id)
    return {
        "services": [
            {
                "name": r["name"], "repository": r["repository_name"],
                "short_desc": r["short_desc"], "stack": r["stack"], "api_count": r["api_count"],
            }
            for r in rows
        ]
    }


def describe_service(
    conn: sqlite3.Connection,
    service: str,
    limit: int = DEFAULT_LIST_LIMIT,
    offset: int = 0,
    repository: str | None = None,
) -> dict:
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    calls, calls_page = _paginate(service_calls_repo.list_calls_for_service(conn, row["id"]), limit, offset)
    apis, apis_page = _paginate(apis_repo.list_apis(conn, row["id"]), limit, offset)
    components, components_page = _paginate(components_repo.list_components(conn, row["id"]), limit, offset)
    persistence, persists_page = _paginate(persistence_repo.list_persistence(conn, row["id"]), limit, offset)
    messages, messages_page = _paginate(messages_repo.list_messages(conn, row["id"]), limit, offset)
    return {
        "name": row["name"],
        "repository": row["repository_name"],
        "short_desc": row["short_desc"],
        "long_desc": row["long_desc"],
        "stack": row["stack"],
        "calls": [_fmt_call(c) for c in calls],
        "apis": [{"method": a["method"], "path": a["path"], "summary": a["summary"]} for a in apis],
        "components": [
            {"name": c["name"], "file_path": c["file_path"], "summary": c["summary"]} for c in components
        ],
        "persists": [{"name": p["name"], "kind": p["kind"], "engine": p["engine"]} for p in persistence],
        "messages": [
            {
                "direction": m["direction"], "channel": m["channel"], "provider": m["provider"],
                "description": m["description"],
            }
            for m in messages
        ],
        "freshness": compute_freshness(row["updated_at"], row["last_commit"], row["root_path"]),
        "pagination": {
            "limit": limit, "offset": offset,
            "calls": calls_page, "apis": apis_page, "components": components_page,
            "persists": persists_page, "messages": messages_page,
        },
    }


DEFAULT_TOPOLOGY_HOPS = 1


def describe_service_topology(
    conn: sqlite3.Connection, service: str, repository: str | None = None, hops: int = DEFAULT_TOPOLOGY_HOPS,
) -> dict:
    """Zero-LLM Mermaid `graph TD` of one service's own dependency neighborhood --
    who it calls, who calls it, and its queues/DBs -- scoped to `hops` steps out
    (default 1) rather than export's whole-system topology.mmd, since a single
    service's context rarely needs the entire indexed system's graph. Built purely
    from the same service_calls/messages/persistence facts describe_service and
    get_relationships already read, just rendered as a diagram instead of a list.
    """
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    mermaid = generate_topology_diagram(conn, root_services={row["name"]}, hops=hops)
    return {
        "service": row["name"],
        "repository": row["repository_name"],
        "hops": hops,
        "mermaid": mermaid,
        "legend": {
            "-->": "internal service call",
            "-.->": "call/dependency on an external vendor, cloud resource or database",
            "==>": "message link (publish on one side matched to consume on the other)",
            "((...))": "external node (vendor, cloud resource, or an unmatched message broker)",
            "[(...)]": "database node",
        },
    }


def list_apis(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    apis, page = _paginate(apis_repo.list_apis(conn, row["id"]), limit, offset)
    return {
        "service": row["name"], "repository": row["repository_name"],
        "apis": [{"method": a["method"], "path": a["path"], "summary": a["summary"]} for a in apis],
        **page,
    }


def describe_ci_commands(conn: sqlite3.Connection, repository: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0) -> dict:
    """Return bounded, source-proven validation commands indexed from GitHub Actions."""
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    repo = repositories_repo.get_repository_by_name(conn, repository)
    if repo is None:
        return {"error": f"unknown repository: {repository}"}
    commands, page = _paginate(ci_commands_repo.list_ci_commands(conn, repo["id"]), limit, offset)
    return {
        "repository": repository,
        "commands": [{
            "workflow_path": command["workflow_path"],
            "kind": command["kind"],
            "command": command["command"],
            "evidence": {
                "file": command["file_path"], "start_line": command["start_line"], "end_line": command["end_line"],
            },
        } for command in commands],
        **page,
    }


_HEALTH_CHECK_PATH = re.compile(r"/(health|healthz|actuator/health)(?:[/?]|$)", re.IGNORECASE)
_INTERNAL_PATH = re.compile(r"/internal(?:[/?]|$)", re.IGNORECASE)


def classify_endpoint_kind(path: str) -> str:
    """A literal, path-only classification -- `"health_check"`/`"internal"` when
    the path itself says so by convention, `"rest"` otherwise. Deliberately not
    `"rpc"`/`"webhook"`: gRPC entrypoints are a separate `EntryPoint` kind that
    never reaches `apis`/`describe_api` at all, and nothing in a path alone proves
    "this is a webhook receiver" the way it proves a health-check convention.
    """
    if _HEALTH_CHECK_PATH.search(path):
        return "health_check"
    if _INTERNAL_PATH.search(path):
        return "internal"
    return "rest"


def _security_shape_for_api(requirements: list[sqlite3.Row], method: str, path: str) -> dict | None:
    """The first (declaration-order) route-level `SecurityRequirement` whose
    pattern covers this API and method -- matching Spring Security's own
    first-match-wins evaluation of `authorizeHttpRequests` rules. Method-level
    `@PreAuthorize`/`@Secured` requirements aren't correlated here yet: `apis`
    doesn't store the underlying symbol a requirement's own `symbol` would need
    to match against.
    """
    rule = flows_repo.matching_route_security_requirement(requirements, method, path)
    return {"requirement": rule["requirement"], "roles": json.loads(rule["roles_json"])} if rule else None


def describe_api(conn: sqlite3.Connection, service: str, method: str, path: str, repository: str | None = None) -> dict:
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    api = apis_repo.get_api_by_key(conn, row["id"], method.upper(), path)
    if api is None:
        return {"error": f"unknown api: {method} {path} on {service}"}
    calls = service_calls_repo.list_calls_for_api(conn, api["id"])
    snapshot = canonical_snapshots_repo.read_snapshot(conn, row["id"])
    source_calls = route_declared_http_calls(
        KnowledgeNavigator(snapshot) if snapshot is not None else None,
        api["method"], api["path"],
    )
    validations = apis_repo.list_validations_for_api(conn, api["id"])
    response_shape = json.loads(api["response_shape"] or "[]")
    request_shape = json.loads(api["request_shape"] or "[]")
    security_requirements = flows_repo.list_static_security_requirements_in_declaration_order(conn, row["id"])
    headers = flows_repo.list_static_api_headers_for_route(conn, row["id"], api["method"], api["path"])
    request_headers = [h["name"] for h in headers if h["direction"] == "request"]
    response_headers = [h["name"] for h in headers if h["direction"] == "response"]
    return {
        "service": row["name"], "repository": row["repository_name"],
        "method": api["method"],
        "path": api["path"],
        "summary": api["summary"],
        "description": api["description"],
        "response_shape": response_shape,
        "request_shape": request_shape,
        "calls": [_fmt_call(c) for c in calls],
        "source_calls": [
            {"target_service": call.target_service, "target_method": call.method,
             "target_path": call.path, "destination_status": "unresolved"}
            for call in source_calls.calls
        ],
        "source_calls_status": source_calls.status.value,
        "validations": [{"kind": v["kind"], "description": v["description"]} for v in validations],
        "api_shape": {
            "method": api["method"],
            "path": api["path"],
            "endpoint_kind": classify_endpoint_kind(api["path"]),
            "request": {"body": request_shape, "headers": request_headers},
            "response": {"body": response_shape, "headers": response_headers},
            "security": _security_shape_for_api(security_requirements, api["method"], api["path"]),
        },
    }


def list_entrypoints(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    """List every transport entry into a service without loading its flow bodies."""
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    entrypoints, page = _paginate(flows_repo.list_entrypoints(conn, row["id"]), limit, offset)
    return {
        "service": row["name"], "repository": row["repository_name"], "entrypoints": [
            {
                "kind": entry["kind"], "method": entry["method"], "name": entry["name"],
                "symbol": entry["symbol"], "evidence": {
                    "file": entry["file_path"], "start_line": entry["start_line"], "end_line": entry["end_line"],
                },
            }
            for entry in entrypoints
        ],
        **page,
    }


def list_security_findings(conn: sqlite3.Connection, service: str, repository: str | None = None) -> dict:
    """Return security findings without exposing source excerpts or secret values."""
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    return {
        "service": row["name"], "repository": row["repository_name"], "findings": [
            {"kind": item["kind"], "severity": item["severity"], "file": item["file_path"], "line": item["line"], "reason": item["reason"]}
            for item in security_findings_repo.list_findings(conn, row["id"])
        ]
    }


def _canonical_source_rows(traversal: TraversalResult, kind: str) -> list[dict]:
    """Shape reached symbol facts as source-backed rows for the public response."""
    rows = []
    for fact in traversal.facts:
        if fact.kind != kind:
            continue
        if not isinstance(fact.subject, SymbolKey):
            raise ValueError(f"{kind} fact must belong to a symbol")
        for source in fact.sources:
            row = {
                "source": fact.subject.name, **fact.attributes,
                "file_path": source.file_path, "start_line": source.start_line,
                "end_line": source.end_line,
            }
            if kind == "flow_boundary":
                row["kind"] = row.pop("boundary_kind")
            elif kind == "resilience_policy":
                row["kind"] = row.pop("policy_kind")
            rows.append(row)
    return rows


def _navigation_boundary_items(traversal: TraversalResult) -> list[dict]:
    edges = {fact.id: fact for fact in traversal.facts if fact.kind == "flow_edge"}
    items = []
    for boundary in traversal.boundaries:
        if boundary.reason == "known_boundary":
            continue
        edge = edges.get(boundary.edge_id)
        source = edge.sources[0] if edge is not None and edge.sources else None
        items.append({
            "source": boundary.source, "target": boundary.target, "kind": boundary.reason,
            "evidence": (
                {"file": source.file_path, "start_line": source.start_line, "end_line": source.end_line}
                if source is not None else None
            ),
        })
    return items


def _canonical_entrypoint_traversal(
    conn: sqlite3.Connection, service_id: int, entrypoint: sqlite3.Row, max_edges: int,
) -> tuple[EntrypointKey, TraversalResult] | None:
    snapshot = canonical_snapshots_repo.read_snapshot(conn, service_id)
    if snapshot is None:
        return None
    key = EntrypointKey(snapshot.service, entrypoint["kind"], entrypoint["method"],
                        entrypoint["name"], entrypoint["symbol"])
    traversal = KnowledgeNavigator(snapshot).reachable(
        key, TraversalPolicy(max_depth=MAX_FLOW_EDGE_LIMIT, max_nodes=MAX_FLOW_EDGE_LIMIT + 1,
                             max_edges=max_edges),
    )
    return key, traversal


def describe_entrypoint(
    conn: sqlite3.Connection,
    service: str,
    kind: str,
    method: str,
    name: str,
    max_edges: int = DEFAULT_FLOW_EDGE_LIMIT,
    repository: str | None = None,
) -> dict:
    """Return a compact deterministic flow for one HTTP, GraphQL, message or CLI entrypoint."""
    if max_edges < 1:
        return {"error": f"max_edges must be >= 1 (got {max_edges})"}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    entrypoint = flows_repo.get_entrypoint(conn, row["id"], kind, method, name)
    if entrypoint is None:
        return {"error": f"unknown entrypoint: {kind} {method} {name} on {service}"}
    effective_max_edges = min(max_edges, MAX_FLOW_EDGE_LIMIT)
    navigation = _canonical_entrypoint_traversal(conn, row["id"], entrypoint, effective_max_edges)
    if navigation is None:
        return {"error": f"canonical snapshot missing for {service}; reindex the service"}
    key, traversal = navigation
    edges = []
    for fact in traversal.facts:
        if fact.kind != "flow_edge":
            continue
        source = fact.sources[0]
        edges.append({
            "from_symbol": fact.subject.name, "to_symbol": fact.attributes["target"],
            "kind": fact.attributes["relation"], "confidence": fact.attributes["confidence"],
            "origin": fact.origin, "file_path": source.file_path,
            "start_line": source.start_line, "end_line": source.end_line,
        })
    truncated = traversal.truncated
    static_service_calls = _canonical_source_rows(traversal, "service_call")
    resilience_policies = _canonical_source_rows(traversal, "resilience_policy")
    boundaries = _canonical_source_rows(traversal, "flow_boundary")
    error_contracts = _canonical_source_rows(traversal, "error_contract")
    entrypoint_fact = next(
        fact for fact in traversal.facts if fact.kind == "entrypoint" and fact.subject == key
    )
    target_cache: dict[tuple[object, ...], dict] = {}
    return {
        "service": row["name"], "repository": row["repository_name"],
        "entrypoint": {
            "kind": entrypoint["kind"], "method": entrypoint["method"], "name": entrypoint["name"],
            "symbol": entrypoint["symbol"], "evidence": {
                "file": entrypoint["file_path"], "start_line": entrypoint["start_line"], "end_line": entrypoint["end_line"],
            },
        },
        "flow": [
            {
                "from": edge["from_symbol"], "to": edge["to_symbol"], "kind": edge["kind"],
                "confidence": edge["confidence"], "origin": edge["origin"], "evidence": {
                    "file": edge["file_path"], "start_line": edge["start_line"], "end_line": edge["end_line"],
                },
            }
            for edge in edges
        ],
        "flow_pagination": {"max_edges": effective_max_edges, "truncated": truncated},
        "persistence_operations": [
            {
                "operation": edge["kind"], "target": edge["to_symbol"], "evidence": {
                    "file": edge["file_path"], "start_line": edge["start_line"], "end_line": edge["end_line"],
                },
            }
            for edge in edges
            if edge["kind"] in {"reads", "writes"}
        ],
        "boundaries": [
            {"source": item["source"], "kind": item["kind"], "evidence": {
                "file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"],
            }}
            for item in boundaries
        ] + _navigation_boundary_items(traversal),
        "error_contracts": [
            {
                "source": item["source"], "role": item["role"], "error_kind": item["error_kind"],
                "internal_type": item["internal_type"], "protocol": item["protocol"],
                "transport_code": item["transport_code"], "public_code": item["public_code"],
                "exposes_internal_detail": bool(item["exposes_internal_detail"]),
                "retryability": item["retryability"], "evidence": {
                    "file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"],
                },
            }
            for item in error_contracts
        ],
        "service_calls": [
            {
                "source": item["source"], "target_service": item["target_service"],
                "protocol": item["protocol"], "method": item["target_method"],
                "path": item["target_path"], "evidence": {
                    "file": item["file_path"], "start_line": item["start_line"],
                    "end_line": item["end_line"],
                },
                "resolved_target": _resolve_static_service_call_target(conn, row, item, target_cache),
            }
            for item in static_service_calls
        ],
        "resilience_policies": [
            {
                "source": item["source"], "kind": item["kind"],
                "mechanism": item["mechanism"], "value": item["value"],
                "unit": item["unit"], "evidence": {
                    "file": item["file_path"], "start_line": item["start_line"],
                    "end_line": item["end_line"],
                },
            }
            for item in resilience_policies
        ],
        "contract": entrypoint_fact.attributes.get("contract"),
        "smells": find_entrypoint_smells(entrypoint, edges),
        "sequence_mermaid": generate_entrypoint_sequence(
            edges[:SEQUENCE_DIAGRAM_EDGE_LIMIT], entrypoint["symbol"], f"{entrypoint['method']} {entrypoint['name']}",
        ),
    }


def describe_error_flow(
    conn: sqlite3.Connection, service: str, kind: str, method: str, name: str,
    repository: str | None = None,
) -> dict:
    """Describe only exact HTTP error mappings reached from one entrypoint.

    A flow needs a source-proven HTTP call, an indexed target endpoint with an
    explicit HTTP error contract, and a reachable caller mapping with the same
    declared error identity. Missing evidence stays in ``unknowns`` rather than
    becoming an assumed 500 or an assumed propagation.
    """
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    entrypoint = flows_repo.get_entrypoint(conn, row["id"], kind, method, name)
    if entrypoint is None:
        return {"error": f"unknown entrypoint: {kind} {method} {name} on {service}"}
    caller_navigation = _canonical_entrypoint_traversal(conn, row["id"], entrypoint, MAX_FLOW_EDGE_LIMIT)
    if caller_navigation is None:
        return {"error": f"canonical snapshot missing for {service}; reindex the service"}
    caller_traversal = caller_navigation[1]
    caller_contracts = _canonical_source_rows(caller_traversal, "error_contract")
    calls = _canonical_source_rows(caller_traversal, "service_call")
    mappings = [
        contract
        for contract in caller_contracts
        if contract["role"] in {"maps", "handles"}
        and contract["protocol"] == "http"
        and contract["transport_code"] is not None
    ]
    flows: list[dict] = []
    unknowns = _error_flow_navigation_unknowns(service, caller_traversal)
    target_cache: dict[tuple[object, ...], dict] = {}
    for call in calls:
        if call["protocol"] != "http":
            unknowns.append(f"{call['target_service']} {call['protocol']} error flow is not supported yet.")
            continue
        resolution = _resolve_static_service_call_target(conn, row, call, target_cache)
        if resolution["status"] != "endpoint_indexed":
            unknowns.append(
                f"{call['target_service']} {call['protocol']} target is not resolved to an indexed endpoint."
            )
            continue
        target, _candidates = services_repo.resolve_service_reference(
            conn, call["target_service"], row["repository_id"],
        )
        if target is None:
            continue
        target_entrypoint = flows_repo.get_entrypoint(
            conn, target["id"], "http", call["target_method"], call["target_path"],
        )
        if target_entrypoint is None:
            continue
        target_navigation = _canonical_entrypoint_traversal(
            conn, target["id"], target_entrypoint, MAX_FLOW_EDGE_LIMIT,
        )
        if target_navigation is None:
            unknowns.append(f"{target['name']} canonical snapshot is missing; reindex the service.")
            continue
        target_traversal = target_navigation[1]
        unknowns.extend(_error_flow_navigation_unknowns(target["name"], target_traversal))
        origins = [
            contract
            for contract in _canonical_source_rows(target_traversal, "error_contract")
            if contract["protocol"] == "http" and contract["transport_code"] is not None
        ]
        if not origins:
            unknowns.append(
                f"{target['name']} {call['target_method']} {call['target_path']} has no reachable indexed HTTP error contract."
            )
            continue
        for origin in origins:
            matching_mappings = [mapping for mapping in mappings if _same_error_identity(origin, mapping)]
            if not matching_mappings:
                unknowns.append(
                    f"{call['source']} has no reachable mapping for {target['name']} {origin['transport_code']} {origin['internal_type'] or origin['public_code'] or origin['error_kind']}."
                )
                continue
            for mapping in matching_mappings:
                flows.append(_error_flow(row, call, target, origin, mapping))
    return {
        "service": row["name"],
        "repository": row["repository_name"],
        "entrypoint": {
            "kind": entrypoint["kind"], "method": entrypoint["method"],
            "name": entrypoint["name"], "symbol": entrypoint["symbol"],
        },
        "error_flows": flows,
        "unknowns": sorted(set(unknowns)),
    }


def _error_flow_navigation_unknowns(service: str, traversal: TraversalResult) -> list[str]:
    unknowns = []
    if traversal.truncated:
        unknowns.append(f"{service} flow was truncated before all error evidence could be checked.")
    for boundary in traversal.boundaries:
        if boundary.reason == "unresolved":
            unknowns.append(f"{service} flow target {boundary.target or '<unknown>'} is unresolved.")
        elif boundary.reason == "known_boundary" and boundary.target != "transaction":
            unknowns.append(f"{service} flow at {boundary.source} has a {boundary.target} boundary.")
    return unknowns


def _same_error_identity(origin: Mapping, mapping: Mapping) -> bool:
    """Join errors only through a declared type or public code, never broad kind."""
    if origin["internal_type"] and mapping["internal_type"]:
        return origin["internal_type"] == mapping["internal_type"]
    return bool(origin["public_code"] and origin["public_code"] == mapping["public_code"])


def _error_flow(
    caller: sqlite3.Row, call: Mapping, target: sqlite3.Row,
    origin: Mapping, mapping: Mapping,
) -> dict:
    origin_evidence = _error_evidence(origin)
    mapping_evidence = _error_evidence(mapping)
    call_evidence = _call_evidence(call)
    status = mapping["transport_code"]
    return {
        "origin": {
            "service": target["name"], "symbol": origin["source"],
            "transport": {
                "protocol": origin["protocol"], "status": origin["transport_code"],
                "public_code": origin["public_code"],
            },
            "evidence": origin_evidence,
        },
        "handling": [{
            "service": caller["name"], "symbol": mapping["source"], "action": f"maps_to_http_{status}",
            "evidence": mapping_evidence,
        }],
        "outcome": {
            "protocol": mapping["protocol"], "status": status, "public_code": mapping["public_code"],
        },
        "confidence": 1.0,
        "evidence": _unique_evidence(call_evidence, origin_evidence, mapping_evidence),
    }


def _error_evidence(contract: Mapping) -> dict:
    return {
        "file": contract["file_path"], "start_line": contract["start_line"], "end_line": contract["end_line"],
    }


def _call_evidence(call: Mapping) -> dict:
    return {"file": call["file_path"], "start_line": call["start_line"], "end_line": call["end_line"]}


def _unique_evidence(*items: dict) -> list[dict]:
    """Keep provenance complete without repeating one source location in MCP output."""
    return list({(item["file"], item["start_line"], item["end_line"]): item for item in items}.values())


def ingest_runtime_evidence(
    conn: sqlite3.Connection, service: str, source: str, observations: list[dict], repository: str | None = None,
) -> dict:
    """Ingest normalized runtime edges, never trace IDs, attributes, payloads or source."""
    row, error = _resolve_service(conn, service, repository)
    if error:
        return error
    if source not in {"otel", "broker"}:
        return {"error": "source must be 'otel' or 'broker'"}
    accepted = rejected = 0
    for item in observations:
        if not _valid_runtime_observation(item):
            rejected += 1
            continue
        runtime_evidence_repo.upsert_observation(conn, row["id"], source, item)
        accepted += 1
    return {"accepted": accepted, "rejected": rejected}


def _valid_runtime_observation(item: object) -> bool:
    return (
        isinstance(item, dict) and set(item) == {"from", "to", "kind", "count"}
        and isinstance(item["from"], str) and isinstance(item["to"], str)
        and len(item["from"]) <= 256 and len(item["to"]) <= 256
        and item["kind"] in _FLOW_KINDS and isinstance(item["count"], int) and item["count"] > 0
    )


def describe_runtime_divergence(conn: sqlite3.Connection, service: str, repository: str | None = None) -> dict:
    """Compare runtime observations with static edges without conflating provenance."""
    row, error = _resolve_service(conn, service, repository)
    if error:
        return error
    observed = {(item["from_symbol"], item["to_symbol"], item["kind"]): item["observed_count"]
                for item in runtime_evidence_repo.list_observations(conn, row["id"])}
    static = {(item["from_symbol"], item["to_symbol"], item["kind"])
              for item in flows_repo.list_flow_edges(conn, row["id"])}
    return {
        "service": row["name"], "repository": row["repository_name"],
        "observed_only": [{"from": edge[0], "to": edge[1], "kind": edge[2], "count": observed[edge]}
                          for edge in sorted(observed.keys() - static)],
        "static_unobserved": [{"from": edge[0], "to": edge[1], "kind": edge[2]}
                              for edge in sorted(static - observed.keys())],
        "unknowns": ["A static edge not observed at runtime is not proof of dead code; coverage and sampling may be incomplete."],
    }


def describe_persistence(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    entities, page = _paginate(persistence_repo.list_persistence(conn, row["id"]), limit, offset)
    migration_facts, migration_page = _paginate(
        flows_repo.list_static_migration_facts(conn, row["id"]), limit, offset,
    )
    return {
        "service": row["name"], "repository": row["repository_name"], "entities": [
            {
                "name": e["name"], "kind": e["kind"], "engine": e["engine"],
                "schema_json": json.loads(e["schema_json"] or "[]"),
            }
            for e in entities
        ],
        "static_facts": [
            {"name": item["name"], "kind": item["kind"], "owner": item["owner"], "evidence": {
                "file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"],
            }}
            for item in flows_repo.list_static_persistence_facts(conn, row["id"])
        ],
        "migration_facts": [
            {
                "operation": item["operation"], "table_name": item["table_name"],
                "column_name": item["column_name"], "destructive": bool(item["destructive"]),
                "evidence": {
                    "file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"],
                },
            }
            for item in migration_facts
        ],
        "migration_pagination": migration_page,
        **page,
    }


def describe_configuration(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    """Return source-proven configuration-key reads without values or resolution claims."""
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    bindings, page = _paginate(
        flows_repo.list_static_configuration_bindings(conn, row["id"]), limit, offset,
    )
    environment_keys = {item["key"] for item in bindings if item["kind"] == "environment"}
    runtime_sources_by_key: dict[str, list[sqlite3.Row]] = {}
    for runtime_binding in kubernetes_configuration_repo.list_kubernetes_configuration_bindings_for_environment_keys(
        conn, row["id"], environment_keys,
    ):
        runtime_sources_by_key.setdefault(runtime_binding["environment_key"], []).append(runtime_binding)
    response_bindings = []
    for item in bindings:
        response_binding = {
            "source": item["source"], "key": item["key"], "kind": item["kind"],
            "sensitive": bool(item["sensitive"]), "evidence": {
                "file": item["file_path"], "start_line": item["start_line"],
                "end_line": item["end_line"],
            },
        }
        runtime_sources = runtime_sources_by_key.get(item["key"], [])
        if item["kind"] == "environment" and runtime_sources:
            response_binding["runtime_sources"] = {
                "count": len(runtime_sources),
                "references": [_runtime_configuration_reference(source) for source in runtime_sources[
                    :MAX_RUNTIME_SOURCES_PER_CONFIGURATION_BINDING
                ]],
                "truncated": len(runtime_sources) > MAX_RUNTIME_SOURCES_PER_CONFIGURATION_BINDING,
            }
        response_bindings.append(response_binding)
    return {
        "service": row["name"], "repository": row["repository_name"],
        "bindings": response_bindings,
        **page,
    }


def describe_runtime_configuration(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None, workloads: object = None, binding_workloads: object = None,
    source_import_workloads: object = None, source_import_declaration_statuses: object = None,
    source_import_availabilities: object = None, binding_declaration_statuses: object = None,
    binding_source_kinds: object = None, source_import_source_kinds: object = None,
    source_import_container_roles: object = None, source_import_prefixes: object = None,
    source_import_include_unprefixed: object = False, binding_evidence_files: object = None,
    source_import_evidence_files: object = None, binding_evidence_ranges: object = None,
    source_import_evidence_ranges: object = None,
) -> dict:
    """Return source-proven Kubernetes configuration references without values."""
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    if workloads is not None and (binding_workloads is not None or source_import_workloads is not None):
        return {"error": "workloads cannot be combined with binding_workloads or source_import_workloads"}
    if workloads is not None:
        selected_workload_scopes, workloads_error = _runtime_configuration_workload_scopes(workloads)
        if workloads_error is not None:
            return {"error": workloads_error}
        selected_binding_scopes = selected_workload_scopes
        selected_source_import_scopes = selected_workload_scopes
    else:
        selected_binding_scopes, bindings_error = _runtime_configuration_workload_scopes(
            binding_workloads, "binding_workloads",
        )
        if bindings_error is not None:
            return {"error": bindings_error}
        selected_source_import_scopes, source_imports_error = _runtime_configuration_workload_scopes(
            source_import_workloads, "source_import_workloads",
        )
        if source_imports_error is not None:
            return {"error": source_imports_error}
    selected_declaration_statuses, declaration_statuses_error = _runtime_configuration_choice_filter(
        source_import_declaration_statuses,
        "source_import_declaration_statuses",
        ("not_declared_locally", "not_reported"),
    )
    if declaration_statuses_error is not None:
        return {"error": declaration_statuses_error}
    selected_availabilities, availabilities_error = _runtime_configuration_choice_filter(
        source_import_availabilities,
        "source_import_availabilities",
        ("optional", "required", "unknown"),
    )
    if availabilities_error is not None:
        return {"error": availabilities_error}
    selected_binding_declaration_statuses, binding_declaration_statuses_error = _runtime_configuration_choice_filter(
        binding_declaration_statuses,
        "binding_declaration_statuses",
        ("key_not_declared", "not_declared_locally", "not_reported"),
    )
    if binding_declaration_statuses_error is not None:
        return {"error": binding_declaration_statuses_error}
    selected_binding_source_kinds, binding_source_kinds_error = _runtime_configuration_choice_filter(
        binding_source_kinds, "binding_source_kinds", ("config_map", "secret"),
    )
    if binding_source_kinds_error is not None:
        return {"error": binding_source_kinds_error}
    selected_source_import_source_kinds, source_import_source_kinds_error = _runtime_configuration_choice_filter(
        source_import_source_kinds, "source_import_source_kinds", ("config_map", "secret"),
    )
    if source_import_source_kinds_error is not None:
        return {"error": source_import_source_kinds_error}
    selected_source_import_container_roles, source_import_container_roles_error = _runtime_configuration_choice_filter(
        source_import_container_roles,
        "source_import_container_roles",
        ("application", "initialization", "unknown"),
    )
    if source_import_container_roles_error is not None:
        return {"error": source_import_container_roles_error}
    selected_prefixes, prefixes_error = _runtime_configuration_string_filter(
        source_import_prefixes, "source_import_prefixes",
    )
    if prefixes_error is not None:
        return {"error": prefixes_error}
    if not isinstance(source_import_include_unprefixed, bool):
        return {"error": "source_import_include_unprefixed must be a boolean"}
    selected_binding_evidence_files, binding_evidence_files_error = _runtime_configuration_string_filter(
        binding_evidence_files, "binding_evidence_files",
    )
    if binding_evidence_files_error is not None:
        return {"error": binding_evidence_files_error}
    selected_source_import_evidence_files, source_import_evidence_files_error = _runtime_configuration_string_filter(
        source_import_evidence_files, "source_import_evidence_files",
    )
    if source_import_evidence_files_error is not None:
        return {"error": source_import_evidence_files_error}
    selected_binding_evidence_ranges, binding_evidence_ranges_error = _runtime_configuration_evidence_ranges(
        binding_evidence_ranges, "binding_evidence_ranges",
    )
    if binding_evidence_ranges_error is not None:
        return {"error": binding_evidence_ranges_error}
    selected_source_import_evidence_ranges, source_import_evidence_ranges_error = _runtime_configuration_evidence_ranges(
        source_import_evidence_ranges, "source_import_evidence_ranges",
    )
    if source_import_evidence_ranges_error is not None:
        return {"error": source_import_evidence_ranges_error}
    binding_filter_dimensions = [
        name for name, enabled in (
            ("binding_workloads", selected_binding_scopes is not None),
            ("binding_declaration_statuses", selected_binding_declaration_statuses is not None),
            ("binding_source_kinds", selected_binding_source_kinds is not None),
            ("binding_evidence_files", selected_binding_evidence_files is not None),
            ("binding_evidence_ranges", selected_binding_evidence_ranges is not None),
        ) if enabled
    ]
    source_import_filter_dimensions = [
        name for name, enabled in (
            ("source_import_workloads", selected_source_import_scopes is not None),
            ("source_import_declaration_statuses", selected_declaration_statuses is not None),
            ("source_import_availabilities", selected_availabilities is not None),
            ("source_import_source_kinds", selected_source_import_source_kinds is not None),
            ("source_import_container_roles", selected_source_import_container_roles is not None),
            ("source_import_prefixes", selected_prefixes is not None or source_import_include_unprefixed),
            ("source_import_evidence_files", selected_source_import_evidence_files is not None),
            ("source_import_evidence_ranges", selected_source_import_evidence_ranges is not None),
        ) if enabled
    ]
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    all_bindings = kubernetes_configuration_repo.list_kubernetes_configuration_bindings_for_service(conn, row["id"])
    all_source_imports = kubernetes_configuration_repo.list_kubernetes_configuration_source_imports_for_service(
        conn, row["id"],
    )
    indexed_bindings = list(all_bindings)
    indexed_source_imports = list(all_source_imports)
    indexed_binding_total = len(all_bindings)
    indexed_source_import_total = len(all_source_imports)
    binding_filters_applied = bool(binding_filter_dimensions)
    source_import_filters_applied = bool(source_import_filter_dimensions)
    if selected_binding_scopes is not None:
        all_bindings = _filter_runtime_configuration_workloads(all_bindings, selected_binding_scopes)
    if selected_source_import_scopes is not None:
        all_source_imports = _filter_runtime_configuration_workloads(all_source_imports, selected_source_import_scopes)
    if selected_binding_evidence_files is not None:
        all_bindings = _filter_runtime_configuration_evidence_files(all_bindings, selected_binding_evidence_files)
    if selected_source_import_evidence_files is not None:
        all_source_imports = _filter_runtime_configuration_evidence_files(
            all_source_imports, selected_source_import_evidence_files,
        )
    if selected_binding_evidence_ranges is not None:
        all_bindings = _filter_runtime_configuration_evidence_ranges(
            all_bindings, selected_binding_evidence_ranges,
        )
    if selected_source_import_evidence_ranges is not None:
        all_source_imports = _filter_runtime_configuration_evidence_ranges(
            all_source_imports, selected_source_import_evidence_ranges,
        )
    if selected_binding_source_kinds is not None:
        all_bindings = _filter_runtime_configuration_source_kinds(all_bindings, selected_binding_source_kinds)
    if selected_source_import_source_kinds is not None:
        all_source_imports = _filter_runtime_configuration_source_kinds(
            all_source_imports, selected_source_import_source_kinds,
        )
    if selected_source_import_container_roles is not None:
        all_source_imports = _filter_runtime_configuration_source_import_container_roles(
            all_source_imports, selected_source_import_container_roles,
        )
    if selected_prefixes is not None or source_import_include_unprefixed:
        all_source_imports = _filter_runtime_configuration_source_import_prefixes(
            all_source_imports, selected_prefixes, source_import_include_unprefixed,
        )
    mismatches_by_reference = {
        (
            mismatch["environment_key"], mismatch["source_kind"], mismatch["source_name"], mismatch["source_key"],
            mismatch["reference_file_path"], mismatch["reference_start_line"], mismatch["reference_end_line"],
        ): mismatch
        for mismatch in kubernetes_configuration_repo.list_kubernetes_configuration_key_mismatches_for_service(
            conn, row["id"],
        )
    }
    unknowns_by_reference = {
        (
            unknown["environment_key"], unknown["source_kind"], unknown["source_name"], unknown["source_key"],
            unknown["reference_file_path"], unknown["reference_start_line"], unknown["reference_end_line"],
        ): unknown
        for unknown in kubernetes_configuration_repo.list_kubernetes_configuration_source_unknowns_for_service(
            conn, row["id"],
        )
    }
    source_import_unknowns_by_reference = {
        (
            unknown["source_kind"], unknown["source_name"], unknown["prefix"],
            unknown["reference_file_path"], unknown["reference_start_line"], unknown["reference_end_line"],
        )
        for unknown in kubernetes_configuration_repo.list_kubernetes_configuration_source_import_unknowns_for_service(
            conn, row["id"],
        )
    }
    binding_dimension_filters = {
        "binding_workloads": lambda records: _filter_runtime_configuration_workloads(records, selected_binding_scopes),
        "binding_declaration_statuses": lambda records: _filter_runtime_configuration_bindings(
            records, mismatches_by_reference, unknowns_by_reference, selected_binding_declaration_statuses,
        ),
        "binding_source_kinds": lambda records: _filter_runtime_configuration_source_kinds(
            records, selected_binding_source_kinds,
        ),
        "binding_evidence_files": lambda records: _filter_runtime_configuration_evidence_files(
            records, selected_binding_evidence_files,
        ),
        "binding_evidence_ranges": lambda records: _filter_runtime_configuration_evidence_ranges(
            records, selected_binding_evidence_ranges,
        ),
    }
    source_import_dimension_filters = {
        "source_import_workloads": lambda records: _filter_runtime_configuration_workloads(
            records, selected_source_import_scopes,
        ),
        "source_import_declaration_statuses": lambda records: _filter_runtime_configuration_source_imports(
            records, source_import_unknowns_by_reference, selected_declaration_statuses, None,
        ),
        "source_import_availabilities": lambda records: _filter_runtime_configuration_source_imports(
            records, source_import_unknowns_by_reference, None, selected_availabilities,
        ),
        "source_import_source_kinds": lambda records: _filter_runtime_configuration_source_kinds(
            records, selected_source_import_source_kinds,
        ),
        "source_import_container_roles": lambda records: _filter_runtime_configuration_source_import_container_roles(
            records, selected_source_import_container_roles,
        ),
        "source_import_prefixes": lambda records: _filter_runtime_configuration_source_import_prefixes(
            records, selected_prefixes, source_import_include_unprefixed,
        ),
        "source_import_evidence_files": lambda records: _filter_runtime_configuration_evidence_files(
            records, selected_source_import_evidence_files,
        ),
        "source_import_evidence_ranges": lambda records: _filter_runtime_configuration_evidence_ranges(
            records, selected_source_import_evidence_ranges,
        ),
    }
    if len(binding_filter_dimensions) > MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS:
        return _runtime_configuration_filter_complexity_error(
            "binding", binding_filter_dimensions, indexed_bindings, binding_dimension_filters,
        )
    if len(source_import_filter_dimensions) > MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS:
        return _runtime_configuration_filter_complexity_error(
            "source import", source_import_filter_dimensions, indexed_source_imports, source_import_dimension_filters,
        )
    if selected_binding_declaration_statuses is not None:
        all_bindings = _filter_runtime_configuration_bindings(
            all_bindings,
            mismatches_by_reference,
            unknowns_by_reference,
            selected_binding_declaration_statuses,
        )
    if selected_declaration_statuses is not None or selected_availabilities is not None:
        all_source_imports = _filter_runtime_configuration_source_imports(
            all_source_imports,
            source_import_unknowns_by_reference,
            selected_declaration_statuses,
            selected_availabilities,
        )
    bindings, page = _paginate(
        all_bindings, limit, offset,
    )
    source_imports, source_import_page = _paginate(all_source_imports, limit, offset)
    response_bindings = []
    for item in bindings:
        response_binding = {"environment_key": item["environment_key"], **_runtime_configuration_reference(item)}
        reference_key = _runtime_configuration_binding_reference_key(item)
        mismatch = mismatches_by_reference.get(reference_key)
        if mismatch is not None:
            response_binding["declaration"] = {
                "status": "key_not_declared",
                "evidence": {
                    "file": mismatch["declaration_file_path"],
                    "start_line": mismatch["declaration_start_line"],
                    "end_line": mismatch["declaration_end_line"],
                },
            }
        elif reference_key in unknowns_by_reference:
            response_binding["declaration"] = {"status": "not_declared_locally"}
        response_bindings.append(response_binding)
    response = {
        "service": row["name"], "repository": row["repository_name"],
        "bindings": response_bindings,
        **page,
    }
    filter_summary = {}
    if binding_filters_applied:
        filter_summary["bindings"] = {
            "indexed_total": indexed_binding_total,
            "selected_total": len(all_bindings),
        }
    if source_import_filters_applied:
        filter_summary["source_imports"] = {
            "indexed_total": indexed_source_import_total,
            "selected_total": len(all_source_imports),
        }
    if filter_summary:
        response["filter_summary"] = filter_summary
    filter_conflict_guidance = {}
    if binding_filters_applied and not all_bindings:
        filter_conflict_guidance["bindings"] = {
            "recommended_next_step": "inspect_filter_dimensions_individually",
            "dimension_selected_totals": _runtime_configuration_filter_dimension_selected_totals(
                binding_filter_dimensions, indexed_bindings, binding_dimension_filters,
            ),
        }
    if source_import_filters_applied and not all_source_imports:
        filter_conflict_guidance["source_imports"] = {
            "recommended_next_step": "inspect_filter_dimensions_individually",
            "dimension_selected_totals": _runtime_configuration_filter_dimension_selected_totals(
                source_import_filter_dimensions, indexed_source_imports, source_import_dimension_filters,
            ),
        }
    if filter_conflict_guidance:
        response["filter_conflict_guidance"] = filter_conflict_guidance
    if source_imports:
        response_source_imports = [
            _runtime_configuration_source_import(item, source_import_unknowns_by_reference)
            for item in source_imports
        ]
        response["source_imports"] = response_source_imports
        response["source_import_total"] = source_import_page["total"]
        response["source_import_truncated"] = source_import_page["truncated"]
        response["unknowns"] = [
            "envFrom imports source keys without explicit per-key references; exact environment keys are not indexed.",
        ]
        if any(item.get("declaration", {}).get("status") == "not_declared_locally" for item in response_source_imports):
            response["unknowns"].append(
                "An envFrom source without a local declaration may be managed by another repository, chart, controller, or deployment process.",
            )
    return response


def _runtime_configuration_reference(item: sqlite3.Row) -> dict:
    return {
        "source": {
            "kind": item["source_kind"], "name": item["source_name"], "key": item["source_key"],
        },
        "workload": {
            "kind": item["workload_kind"], "name": item["workload_name"], "container": item["container_name"],
        },
        "evidence": {
            "file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"],
        },
    }


def _runtime_configuration_workload_scopes(
    workloads: object, argument_name: str = "workloads",
) -> tuple[set[tuple[str, str, str]] | None, str | None]:
    """Validate optional direct workload scopes used to reduce runtime context."""
    if workloads is None:
        return None, None
    if not isinstance(workloads, list) or not workloads:
        return None, f"{argument_name} must be a non-empty list of workload identities"
    if len(workloads) > MAX_LIST_LIMIT:
        return None, f"{argument_name} must contain at most {MAX_LIST_LIMIT} workload identities"
    scopes: set[tuple[str, str, str]] = set()
    for workload in workloads:
        scope = _workload_scope_key(workload)
        if scope is None:
            return None, f"{argument_name} must be a non-empty list of workload identities"
        scopes.add(scope)
    return scopes, None


def _runtime_configuration_filter_complexity_error(
    surface: str, dimensions: list[str], indexed_records: list[sqlite3.Row], dimension_filters: dict,
) -> dict:
    """Provide data-backed query groups when active filters exceed the safe bound."""
    query_groups = [
        dimensions[index : index + MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS]
        for index in range(0, len(dimensions), MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS)
    ]
    selected_totals = _runtime_configuration_filter_group_selected_totals(
        query_groups, indexed_records, dimension_filters,
    )
    return {
        "error": f"{surface} filters must use at most {MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS} dimensions",
        "split_guidance": {
            "recommended_next_step": "split_filter_dimensions",
            "max_dimensions": MAX_RUNTIME_CONFIGURATION_FILTER_DIMENSIONS,
            "query_groups": query_groups,
            "query_group_selected_totals": selected_totals,
            "execution_order": _runtime_configuration_filter_group_execution_order(query_groups, selected_totals),
        },
    }


def _runtime_configuration_filter_group_selected_totals(
    query_groups: list[list[str]], indexed_records: list[sqlite3.Row], dimension_filters: dict,
) -> list[int]:
    """Count each proposed group against indexed records without returning records."""
    selected_totals = []
    for group in query_groups:
        selected_records = indexed_records
        for dimension in group:
            selected_records = dimension_filters[dimension](selected_records)
        selected_totals.append(len(selected_records))
    return selected_totals


def _runtime_configuration_filter_dimension_selected_totals(
    dimensions: list[str], indexed_records: list[sqlite3.Row], dimension_filters: dict,
) -> list[dict]:
    """Count each active filter independently to explain an empty intersection."""
    return [
        {"dimension": dimension, "selected_total": len(dimension_filters[dimension](indexed_records))}
        for dimension in dimensions
    ]


def _runtime_configuration_filter_group_execution_order(
    query_groups: list[list[str]], selected_totals: list[int],
) -> list[int]:
    """Order groups by indexed total, then evidence-locality, retaining stable ties."""
    return sorted(
        range(len(query_groups)),
        key=lambda index: (
            selected_totals[index],
            min(_RUNTIME_FILTER_DIMENSION_PRIORITY[dimension] for dimension in query_groups[index]),
            index,
        ),
    )


def _filter_runtime_configuration_workloads(
    records: list[sqlite3.Row], selected_scopes: set[tuple[str, str, str]],
) -> list[sqlite3.Row]:
    """Keep indexed runtime references only for explicitly selected workloads."""
    return [
        record for record in records
        if (record["workload_kind"], record["workload_name"], record["container_name"]) in selected_scopes
    ]


def _filter_runtime_configuration_source_kinds(
    records: list[sqlite3.Row], source_kinds: set[str],
) -> list[sqlite3.Row]:
    """Keep only records whose source kind is one of the explicit literal kinds."""
    return [record for record in records if record["source_kind"] in source_kinds]


def _filter_runtime_configuration_evidence_files(
    records: list[sqlite3.Row], evidence_files: set[str],
) -> list[sqlite3.Row]:
    """Keep only records with an exact persisted evidence file path."""
    return [record for record in records if record["file_path"] in evidence_files]


def _runtime_configuration_evidence_ranges(
    values: object, argument_name: str,
) -> tuple[list[tuple[str, int, int]] | None, str | None]:
    """Validate bounded exact evidence ranges without reading source files."""
    error = f"{argument_name} must be a non-empty list of valid evidence ranges"
    if values is None:
        return None, None
    if not isinstance(values, list) or not values or len(values) > MAX_LIST_LIMIT:
        return None, error
    ranges: list[tuple[str, int, int]] = []
    for value in values:
        if not isinstance(value, dict):
            return None, error
        file_path = value.get("file")
        start_line = value.get("start_line")
        end_line = value.get("end_line")
        if (
            not isinstance(file_path, str)
            or not file_path
            or not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
        ):
            return None, error
        ranges.append((file_path, start_line, end_line))
    return ranges, None


def _filter_runtime_configuration_evidence_ranges(
    records: list[sqlite3.Row], evidence_ranges: list[tuple[str, int, int]],
) -> list[sqlite3.Row]:
    """Keep records whose persisted evidence overlaps one requested exact range."""
    return [
        record for record in records
        if any(
            record["file_path"] == file_path
            and record["start_line"] <= end_line
            and start_line <= record["end_line"]
            for file_path, start_line, end_line in evidence_ranges
        )
    ]


def _filter_runtime_configuration_source_import_container_roles(
    records: list[sqlite3.Row], container_roles: set[str],
) -> list[sqlite3.Row]:
    """Keep imports whose indexed container role is explicitly selected."""
    return [
        record for record in records
        if _runtime_configuration_source_import_container_role(record) in container_roles
    ]


def _runtime_configuration_source_import_container_role(item: sqlite3.Row) -> str:
    """Normalize only an absent indexed role to unknown."""
    return item["container_role"] or "unknown"


def _filter_runtime_configuration_bindings(
    records: list[sqlite3.Row],
    mismatches_by_reference: dict[tuple[str, str, str, str, str, int, int], sqlite3.Row],
    unknowns_by_reference: dict[tuple[str, str, str, str, str, int, int], sqlite3.Row],
    declaration_statuses: set[str],
) -> list[sqlite3.Row]:
    """Keep bindings matching only requested persisted declaration-finding states."""
    return [
        record for record in records
        if _runtime_configuration_binding_declaration_status(
            record, mismatches_by_reference, unknowns_by_reference,
        ) in declaration_statuses
    ]


def _runtime_configuration_binding_declaration_status(
    item: sqlite3.Row,
    mismatches_by_reference: dict[tuple[str, str, str, str, str, int, int], sqlite3.Row],
    unknowns_by_reference: dict[tuple[str, str, str, str, str, int, int], sqlite3.Row],
) -> str:
    """Return the stored finding state without asserting a local declaration exists."""
    reference_key = _runtime_configuration_binding_reference_key(item)
    if reference_key in mismatches_by_reference:
        return "key_not_declared"
    if reference_key in unknowns_by_reference:
        return "not_declared_locally"
    return "not_reported"


def _runtime_configuration_binding_reference_key(
    item: sqlite3.Row,
) -> tuple[str, str, str, str, str, int, int]:
    """Build the persisted identity shared by binding findings and response shaping."""
    return (
        item["environment_key"], item["source_kind"], item["source_name"], item["source_key"],
        item["file_path"], item["start_line"], item["end_line"],
    )


def _runtime_configuration_choice_filter(
    values: object, argument_name: str, allowed_values: tuple[str, ...],
) -> tuple[set[str] | None, str | None]:
    """Validate an optional finite filter without accepting inferred states."""
    if values is None:
        return None, None
    if not isinstance(values, list) or not values:
        return None, f"{argument_name} must be a non-empty list"
    if len(values) > MAX_LIST_LIMIT or any(value not in allowed_values for value in values):
        return None, f"{argument_name} must contain only: {', '.join(allowed_values)}"
    return set(values), None


def _runtime_configuration_string_filter(
    values: object, argument_name: str,
) -> tuple[set[str] | None, str | None]:
    """Validate exact non-empty strings used to filter persisted scalar facts."""
    error = f"{argument_name} must be a non-empty list of non-empty strings"
    if values is None:
        return None, None
    if (
        not isinstance(values, list)
        or not values
        or len(values) > MAX_LIST_LIMIT
        or any(not isinstance(value, str) or not value for value in values)
    ):
        return None, error
    return set(values), None


def _filter_runtime_configuration_source_import_prefixes(
    records: list[sqlite3.Row], prefixes: set[str] | None, include_unprefixed: bool,
) -> list[sqlite3.Row]:
    """Keep imports with selected exact prefixes and optionally absent prefixes."""
    selected_prefixes = prefixes or set()
    return [
        record for record in records
        if record["prefix"] in selected_prefixes or (include_unprefixed and record["prefix"] is None)
    ]


def _filter_runtime_configuration_source_imports(
    records: list[sqlite3.Row],
    unknowns_by_reference: set[tuple[str, str, str | None, str, int, int]],
    declaration_statuses: set[str] | None,
    availabilities: set[str] | None,
) -> list[sqlite3.Row]:
    """Keep imports matching only requested static availability and finding states."""
    return [
        record for record in records
        if (
            declaration_statuses is None
            or _runtime_configuration_source_import_declaration_status(record, unknowns_by_reference)
            in declaration_statuses
        )
        and (
            availabilities is None
            or _runtime_configuration_source_import_availability(record) in availabilities
        )
    ]


def _runtime_configuration_source_import_declaration_status(
    item: sqlite3.Row, unknowns_by_reference: set[tuple[str, str, str | None, str, int, int]],
) -> str:
    """Return the stored declaration-finding state without claiming source ownership."""
    if _runtime_configuration_source_import_reference_key(item) in unknowns_by_reference:
        return "not_declared_locally"
    return "not_reported"


def _runtime_configuration_source_import_availability(item: sqlite3.Row) -> str:
    """Normalize literal optionality while retaining absent metadata as unknown."""
    if item["optional"] is None:
        return "unknown"
    return "optional" if item["optional"] else "required"


def _runtime_configuration_source_import_reference_key(
    item: sqlite3.Row,
) -> tuple[str, str, str | None, str, int, int]:
    """Build the persisted identity shared by source-import findings and output shaping."""
    return (
        item["source_kind"], item["source_name"], item["prefix"],
        item["file_path"], item["start_line"], item["end_line"],
    )


def _runtime_configuration_source_import(
    item: sqlite3.Row, source_import_unknowns_by_reference: set[tuple[str, str, str | None, str, int, int]],
) -> dict:
    """Shape an ``envFrom`` source while preserving its intentionally unknown keys."""
    workload = {
        "kind": item["workload_kind"], "name": item["workload_name"], "container": item["container_name"],
    }
    if item["container_role"] is not None:
        workload["container_role"] = item["container_role"]
    response = {
        "source": {"kind": item["source_kind"], "name": item["source_name"]},
        "workload": workload,
        "evidence": {"file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"]},
        "key_coverage": "unknown",
    }
    if item["prefix"] is not None:
        response["prefix"] = item["prefix"]
    if item["optional"] is not None:
        response["availability"] = "optional" if item["optional"] else "required"
    if _runtime_configuration_source_import_reference_key(item) in source_import_unknowns_by_reference:
        response["declaration"] = {"status": "not_declared_locally"}
    return response


def describe_feature_flags(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    """Return source-proven feature-flag reads without values or rollout claims."""
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    flags, page = _paginate(flows_repo.list_static_feature_flags(conn, row["id"]), limit, offset)
    return {
        "service": row["name"], "repository": row["repository_name"],
        "flags": [
            {
                "source": item["source"], "key": item["key"], "provider": item["provider"],
                "evidence": {
                    "file": item["file_path"], "start_line": item["start_line"],
                    "end_line": item["end_line"],
                },
            }
            for item in flags
        ],
        **page,
    }


def describe_messages(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    messages, page = _paginate(messages_repo.list_messages(conn, row["id"]), limit, offset)
    return {
        "service": row["name"], "repository": row["repository_name"], "messages": [
            {
                "direction": m["direction"],
                "channel": m["channel"],
                "provider": m["provider"],
                "shape_json": json.loads(m["shape_json"] or "[]"),
                "description": m["description"],
            }
            for m in messages
        ],
        "static_contracts": [
            {
                "direction": item["direction"], "exchange": item["channel"],
                "routing_key": item["routing_key"], "payload_type": item["payload_type"], "message_version": item["message_version"],
                "evidence": {"file": item["file_path"], "start_line": item["start_line"], "end_line": item["end_line"]},
            }
            for item in flows_repo.list_static_message_contracts(conn, row["id"])
        ],
        **page,
    }


def describe_cloud_dependencies(
    conn: sqlite3.Connection, service: str, limit: int = DEFAULT_LIST_LIMIT, offset: int = 0,
    repository: str | None = None,
) -> dict:
    error = _validate_pagination(limit, offset)
    if error:
        return {"error": error}
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error
    static_facts, facts_page = _paginate(flows_repo.list_static_cloud_facts(conn, row["id"]), limit, offset)
    iac_resources, iac_page = _paginate(
        cloud_iac_repo.list_iac_resources_for_service(conn, row["id"]), limit, offset,
    )
    return {
        "service": row["name"], "repository": row["repository_name"],
        "static_facts": [
            {
                "provider": f["provider"], "resource_type": f["resource_type"],
                "service_name": f["service_name"], "operation": f["operation"],
                "operation_kind": f["operation_kind"], "sdk": f["sdk"], "target_name": f["target_name"],
                "evidence": {"file": f["file_path"], "start_line": f["start_line"], "end_line": f["end_line"]},
            }
            for f in static_facts
        ],
        "iac_resources": [
            {
                "provider": r["provider"], "resource_type": r["resource_type"],
                "iac_resource_type": r["iac_resource_type"], "logical_name": r["logical_name"],
                "physical_name": r["physical_name"], "source_format": r["source_format"],
                "confidence": r["confidence"], "attributes": json.loads(r["attributes_json"] or "{}"),
                "evidence": {"file": r["file_path"], "start_line": r["start_line"], "end_line": r["end_line"]},
            }
            for r in iac_resources
        ],
        "pagination": {"limit": limit, "offset": offset, "static_facts": facts_page, "iac_resources": iac_page},
    }


def search(conn: sqlite3.Connection, query: str, repository: str | None = None) -> dict:
    repository_id = None
    if repository is not None:
        repo = repositories_repo.get_repository_by_name(conn, repository)
        if repo is None:
            return {"error": f"unknown repository: {repository}"}
        repository_id = repo["id"]
    return {"results": search_repo.search(conn, query, repository_id=repository_id)}


def _fmt_relationship_call(c: sqlite3.Row, *, direction: str, other_key: str, other_value: str) -> dict:
    return {
        "type": c["call_kind"].upper(),
        "direction": direction,
        other_key: other_value,
        "reason": c["reason"],
        "confidence": c["confidence"],
        "target_kind": c["target_kind"],
        "evidence": json.loads(c["evidence_json"] or "[]"),
        "provenance": infer_provenance(c["confidence"]),
    }


def _fmt_message_link(link: sqlite3.Row) -> dict:
    direction = "outbound" if link["local_direction"] == "publishes" else "inbound"
    other_key = "target_service" if direction == "outbound" else "source_service"
    return {
        "type": "MESSAGE_LINK",
        "direction": direction,
        "channel": link["channel"],
        other_key: link["other_service"],
        "reason": None,
        "confidence": None,
        "evidence": [],
        "provenance": infer_provenance(None),
    }


def get_relationships(
    conn: sqlite3.Connection, service: str, direction: str = "both", repository: str | None = None,
) -> dict:
    """Fact + semantic-interpretation edges around one service: outbound calls it
    makes, inbound calls other services make into it, and queue/topic links inferred
    from matching publish/consume channel names."""
    row, service_error = _resolve_service(conn, service, repository)
    if service_error:
        return service_error

    relationships: list[dict] = []
    if direction in ("outbound", "both"):
        for c in service_calls_repo.list_calls_for_service(conn, row["id"]):
            relationships.append(
                _fmt_relationship_call(c, direction="outbound", other_key="target_service", other_value=c["to_service_name"])
            )
    if direction in ("inbound", "both"):
        for c in service_calls_repo.list_inbound_calls(conn, row["id"]):
            relationships.append(
                _fmt_relationship_call(c, direction="inbound", other_key="source_service", other_value=c["from_service_name"])
            )

    for link in messages_repo.list_message_links(conn, row["id"]):
        local_is_outbound = link["local_direction"] == "publishes"
        if direction == "both" or (direction == "outbound" and local_is_outbound) or (direction == "inbound" and not local_is_outbound):
            relationships.append(_fmt_message_link(link))

    return {"service": row["name"], "repository": row["repository_name"], "relationships": relationships}


def _outgoing_edges(conn: sqlite3.Connection, service_row: sqlite3.Row) -> list[dict]:
    edges = []
    for c in service_calls_repo.list_calls_for_service(conn, service_row["id"]):
        edges.append(
            {
                "to_service_id": c["to_service_id"],
                "to": c["to_service_name"],
                "type": c["call_kind"].upper(),
                "reason": c["reason"],
                "confidence": c["confidence"],
                "evidence": json.loads(c["evidence_json"] or "[]"),
            }
        )
    for link in messages_repo.list_message_links(conn, service_row["id"]):
        if link["local_direction"] == "publishes":
            edges.append(
                {
                    "to": link["other_service"],
                    "type": "MESSAGE_LINK",
                    "reason": f"channel: {link['channel']}",
                    "confidence": None,
                    "evidence": [],
                }
            )
    return edges


def trace_flow(
    conn: sqlite3.Connection,
    from_service: str,
    to_service: str,
    max_hops: int = 6,
    from_repository: str | None = None,
    to_repository: str | None = None,
) -> dict:
    """Shortest directed path from one service to another, walking outbound calls and
    publish->consume message links — the multi-hop counterpart to get_relationships'
    single hop. Facts + semantic reasons per hop, same as get_relationships."""
    from_row, from_error = _resolve_service(conn, from_service, from_repository)
    if from_error:
        return from_error
    to_row, to_error = _resolve_service(conn, to_service, to_repository)
    if to_error:
        return to_error
    if from_row["id"] == to_row["id"]:
        return {"path": [], "reachable": True, "hops": 0, "note": "from and to are the same service"}

    visited = {from_row["id"]}
    queue = deque([(from_row, [])])
    while queue:
        current_row, path = queue.popleft()
        if len(path) >= max_hops:
            continue
        for edge in _outgoing_edges(conn, current_row):
            target_id = edge.pop("to_service_id")
            if target_id is None:
                continue
            target_row = services_repo.get_service_by_id(conn, target_id)
            if target_row is None:
                continue
            hop = {
                "from": current_row["name"], "from_repository": current_row["repository_name"],
                **edge, "to": target_row["name"], "to_repository": target_row["repository_name"],
            }
            new_path = path + [hop]
            if target_id == to_row["id"]:
                return {"path": new_path, "reachable": True, "hops": len(new_path)}
            if target_id not in visited:
                visited.add(target_id)
                queue.append((target_row, new_path))

    return {"path": [], "reachable": False, "note": f"no path found within {max_hops} hops"}


def find_architecture_smells(conn: sqlite3.Connection) -> dict:
    run_id = architecture_repo.latest_run_id(conn)
    if run_id is None:
        return {"findings": [], "run_id": None, "note": "no architecture run yet — index at least one service first"}
    findings = architecture_repo.list_findings(conn, run_id)
    def _format_finding(finding: sqlite3.Row) -> dict:
        detail = json.loads(finding["detail_json"] or "{}")
        return {
            "kind": finding["kind"], "severity": finding["severity"],
            "services": json.loads(finding["services_json"]), "detail": detail,
            "reason": finding["reason"], "confidence": detail.get("confidence", 1.0),
            "evidence": detail.get("evidence", []),
            "unknowns": detail.get("unknowns", ["Only indexed services and static facts were evaluated."]),
            "remediation": detail.get("remediation", []),
        }

    response = {
        "run_id": run_id,
        "findings": [_format_finding(finding) for finding in findings],
    }
    previous_run_id = architecture_repo.previous_run_id(conn, run_id)
    if previous_run_id is not None:
        # Omitted entirely (not an empty/null trend) on the very first run ever —
        # there's nothing honest to compare against yet, same "don't fabricate when
        # there's nothing to say" convention as find_change_surface's own fields.
        response["trend"] = diff_architecture_runs(conn, previous_run_id, run_id)
    return response


def find_change_surface(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    task: str,
    hint_services: list[str] | None = None,
    repository: str | None = None,
) -> dict:
    """Task/epic -> likely change surface, computed from the already-indexed System
    Knowledge Model (no source file is read here). This is a task inference, not a
    fact: every finding carries reason + confidence + evidence."""
    repository_id = None
    if repository is not None:
        repo = repositories_repo.get_repository_by_name(conn, repository)
        if repo is None:
            return {"error": f"unknown repository: {repository}"}
        repository_id = repo["id"]
    elif duplicate_names := services_repo.list_duplicate_service_names(conn):
        return {
            "error": "ambiguous service identities; specify repository",
            "duplicate_services": duplicate_names,
        }
    response = change_surface.analyze_change_surface(
        conn, task, backend, hint_services, repository_id=repository_id,
    )
    if repository is not None:
        response["scope"] = {"repository": repository}
    return response


def plan_change(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    task: str,
    hint_services: list[str] | None = None,
    repository: str | None = None,
    token_budget: int = DEFAULT_PLAN_TOKEN_BUDGET,
) -> dict:
    """Return the stable first envelope for a bounded change plan.

    This initial contract deliberately exposes only source-backed surface facts. It
    creates no target-level units or decision points until those can be derived with
    evidence rather than inferred from broad service matches.
    """
    if not 1 <= token_budget <= MAX_PLAN_TOKEN_BUDGET:
        return {
            "error": (
                f"token_budget must be between 1 and {MAX_PLAN_TOKEN_BUDGET} "
                f"(got {token_budget})"
            ),
        }
    change_surface_result = find_change_surface(conn, backend, task, hint_services, repository)
    if "error" in change_surface_result:
        return change_surface_result
    primary = change_surface_result["primary"]
    decision_points = derive_decision_points(
        change_surface_result["contracts_at_risk"], {finding["service"] for finding in primary},
    )
    status = "insufficient_evidence" if not primary else "needs_decision" if decision_points else "ready"
    repository_id = None
    if repository is not None:
        repository_id = repositories_repo.get_repository_by_name(conn, repository)["id"]
    primary_services = {finding["service"] for finding in primary}
    error_mapping_services = {
        service
        for service in primary_services
        if len(services_repo.list_service_candidates_by_name(conn, service)) == 1
    }
    if decision_points:
        change_units = []
    else:
        persistence_services = {
            item["service"]
            for item in change_surface_result["persistence_affected"]
            if item.get("service") in primary_services and item.get("kind") == "sql_table" and item.get("evidence")
        }
        migration_facts_by_service = {
            service: [dict(fact) for fact in flows_repo.list_static_migration_facts(conn, row["id"])]
            for service in persistence_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        feature_flags_by_service = {
            service: [dict(flag) for flag in flows_repo.list_static_feature_flags(conn, row["id"])]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        configuration_bindings_by_service = {
            service: [dict(binding) for binding in flows_repo.list_static_configuration_bindings(conn, row["id"])]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        runtime_configuration_bindings_by_service = {
            service: [
                dict(binding)
                for binding in kubernetes_configuration_repo.list_kubernetes_configuration_bindings_for_service(
                    conn, row["id"],
                )
            ]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        runtime_configuration_mismatches_by_service = {
            service: [
                dict(mismatch)
                for mismatch in kubernetes_configuration_repo.list_kubernetes_configuration_key_mismatches_for_service(
                    conn, row["id"],
                )
            ]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        runtime_configuration_source_unknowns_by_service = {
            service: [
                dict(unknown)
                for unknown in kubernetes_configuration_repo.list_kubernetes_configuration_source_unknowns_for_service(
                    conn, row["id"],
                )
            ]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        runtime_configuration_source_import_unknowns_by_service = {
            service: [
                dict(unknown)
                for unknown in kubernetes_configuration_repo.list_kubernetes_configuration_source_import_unknowns_for_service(
                    conn, row["id"],
                )
            ]
            for service in primary_services
            if (row := services_repo.get_service_by_name(conn, service, repository_id=repository_id)) is not None
        }
        architecture_findings = find_architecture_smells(conn)["findings"]
        change_units = [
            *_derive_http_contract_review_units(conn, sorted(primary_services), repository_id),
            *derive_aggregate_ownership_review_units(architecture_findings, error_mapping_services),
            *derive_broad_timeout_handler_review_units(architecture_findings, error_mapping_services),
            *derive_cloud_dead_letter_queue_review_units(architecture_findings, error_mapping_services),
            *derive_cloud_dependency_iac_review_units(architecture_findings, error_mapping_services),
            *derive_cloud_encryption_review_units(architecture_findings, error_mapping_services),
            *derive_cloud_versioning_review_units(architecture_findings, error_mapping_services),
            *derive_error_mapping_review_units(architecture_findings, error_mapping_services),
            *derive_http_resilience_policy_review_units(architecture_findings, error_mapping_services),
            *derive_retry_policy_review_units(architecture_findings, error_mapping_services),
            *derive_retry_http_idempotency_review_units(architecture_findings, error_mapping_services),
            *derive_retry_unrecovered_consumer_delivery_review_units(architecture_findings, error_mapping_services),
            *derive_message_consumer_recovery_review_units(architecture_findings, error_mapping_services),
            *derive_retry_delivery_review_units(architecture_findings, error_mapping_services),
            *derive_retry_consumer_delivery_review_units(architecture_findings, error_mapping_services),
            *derive_retry_downstream_error_review_units(architecture_findings, error_mapping_services),
            *derive_retry_write_publish_review_units(architecture_findings, error_mapping_services),
            *derive_non_atomic_service_publish_review_units(architecture_findings, error_mapping_services),
            *derive_partial_write_resilience_review_units(architecture_findings, error_mapping_services),
            *derive_timeout_local_fallback_review_units(architecture_findings, error_mapping_services),
            *derive_timeout_fallback_review_units(architecture_findings, error_mapping_services),
            *derive_read_entrypoint_side_effect_review_units(architecture_findings, error_mapping_services),
            *derive_public_object_storage_review_units(architecture_findings, error_mapping_services),
            *derive_persistence_migration_review_units(
                change_surface_result["persistence_affected"], migration_facts_by_service, primary_services,
            ),
            *derive_feature_flag_review_units(feature_flags_by_service, primary_services),
            *derive_runtime_configuration_review_units(
                configuration_bindings_by_service, runtime_configuration_bindings_by_service, primary_services,
            ),
            *derive_runtime_configuration_mismatch_review_units(
                runtime_configuration_mismatches_by_service, primary_services,
            ),
            *derive_runtime_configuration_source_unknown_review_units(
                runtime_configuration_source_unknowns_by_service, primary_services,
            ),
            *derive_runtime_configuration_source_import_unknown_review_units(
                runtime_configuration_source_import_unknowns_by_service, primary_services,
            ),
        ]
    plan_run_id = change_plans_repo.record_plan(
        conn, change_surface_result.get("run_id"), status, token_budget, decision_points, change_units,
    )
    response = {
        "plan_id": f"cp_{plan_run_id}",
        "status": status,
        "surface": {
            "primary": primary,
            "secondary": change_surface_result["secondary"],
            "contracts_at_risk": change_surface_result["contracts_at_risk"],
        },
        "decision_points": decision_points,
        "change_units": change_units,
        "ci_validation_commands": _compact_ci_validation_commands(conn, repository_id),
        "unknowns": change_surface_result["unknowns"],
        "budget": {
            "requested_tokens": token_budget, "estimated_tokens": 0,
            "measurement": "byte_estimate", "truncated": False,
        },
    }
    measurement = _measure_plan_response(response)
    response["budget"]["truncated"] = response["budget"]["estimated_tokens"] > token_budget
    change_plans_repo.update_measurements(
        conn, plan_run_id, response["budget"]["estimated_tokens"], response["budget"]["truncated"], measurement.method,
    )
    return response


def _compact_ci_validation_commands(conn: sqlite3.Connection, repository_id: int | None) -> list[dict]:
    """Return a tiny, safe validation hint without expanding CI details."""
    if repository_id is None:
        return []
    commands: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for command in ci_commands_repo.list_ci_commands(conn, repository_id):
        if command["kind"] not in {"test", "build"}:
            continue
        identity = (command["kind"], command["command"])
        if identity in seen:
            continue
        seen.add(identity)
        commands.append({
            "kind": command["kind"],
            "command": command["command"],
            "workflow_path": command["workflow_path"],
            "start_line": command["start_line"],
        })
        if len(commands) == MAX_PLAN_CI_VALIDATION_COMMANDS:
            break
    return commands


def _ci_validation_result_summaries(
    conn: sqlite3.Connection, plan_id: int, repository_id: int, commands: list[dict],
) -> list[dict]:
    locations_by_command: dict[tuple[str, str, str], int] = {}
    for indexed_command in ci_commands_repo.list_ci_commands(conn, repository_id):
        identity = (
            indexed_command["kind"], indexed_command["command"], indexed_command["workflow_path"],
        )
        locations_by_command.setdefault(identity, indexed_command["start_line"])
    results_by_location = {
        (result["workflow_path"], result["start_line"]): result
        for result in ci_validation_results_repo.list_results(conn, plan_id, repository_id)
    }
    summaries: list[dict] = []
    for command in commands:
        identity = (command["kind"], command["command"], command["workflow_path"])
        start_line = locations_by_command.get(identity)
        result = results_by_location.get((command["workflow_path"], start_line))
        if result is not None:
            summaries.append({
                "kind": command["kind"],
                "command": command["command"],
                "workflow_path": command["workflow_path"],
                "start_line": start_line,
                "status": result["status"],
                "duration_ms": result["duration_ms"],
            })
    return summaries


def describe_change_validation_status(conn: sqlite3.Connection, plan_id: object, repository: object) -> dict:
    """Return a bounded, agent-reported CI validation summary for one ready plan."""
    if not isinstance(plan_id, str) or (match := re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)) is None:
        return {"error": "plan_id must have the form cp_<positive integer>"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    if stored_plan["status"] != "ready":
        return {"error": f"plan must be ready before reviewing validation (status: {stored_plan['status']})"}
    if not isinstance(repository, str) or not repository:
        return {"error": "repository must be a non-empty string"}
    repo = repositories_repo.get_repository_by_name(conn, repository)
    if repo is None:
        return {"error": f"unknown repository: {repository}"}
    commands = _compact_ci_validation_commands(conn, repo["id"])
    start_lines: dict[tuple[str, str, str], int] = {}
    for indexed_command in ci_commands_repo.list_ci_commands(conn, repo["id"]):
        identity = (
            indexed_command["kind"], indexed_command["command"], indexed_command["workflow_path"],
        )
        start_lines.setdefault(identity, indexed_command["start_line"])
    results_by_command = {
        (result["kind"], result["command"], result["workflow_path"]): result
        for result in _ci_validation_result_summaries(conn, int(match.group(1)), repo["id"], commands)
    }
    command_statuses: list[dict] = []
    for command in commands:
        result = results_by_command.get((command["kind"], command["command"], command["workflow_path"]))
        command_statuses.append({
            **command,
            "start_line": start_lines[(command["kind"], command["command"], command["workflow_path"])],
            "status": result["status"] if result is not None else "pending",
            "duration_ms": result["duration_ms"] if result is not None else None,
        })
    summary = {
        "total": len(command_statuses),
        "passed": sum(command["status"] == "passed" for command in command_statuses),
        "failed": sum(command["status"] == "failed" for command in command_statuses),
        "pending": sum(command["status"] == "pending" for command in command_statuses),
    }
    status = (
        "no_indexed_commands" if not command_statuses
        else "failed" if summary["failed"]
        else "pending" if summary["pending"]
        else "reported_passed"
    )
    return {
        "plan_id": plan_id,
        "repository": repository,
        "status": status,
        "summary": summary,
        "commands": command_statuses,
    }


def _manual_validation_summary(conn: sqlite3.Connection, plan_id: int, change_units: list[dict]) -> dict:
    """Summarize persisted unit checks without exposing or accepting result text."""
    summary, _outstanding = _manual_validation_breakdown(conn, plan_id, change_units)
    return summary


def _manual_validation_breakdown(
    conn: sqlite3.Connection, plan_id: int, change_units: list[dict],
) -> tuple[dict, dict[str, list[str]]]:
    """Return aggregate state plus only IDs of units still needing manual action."""
    results = {
        (result["change_unit_id"], result["check_index"]): result["status"]
        for result in manual_validation_results_repo.list_results(conn, plan_id)
    }
    statuses_by_unit = [
        (unit["id"], [results.get((unit["id"], index), "pending") for index, _check in enumerate(unit.get("validation", []))])
        for unit in change_units
        if isinstance(unit.get("validation", []), list) and unit.get("validation")
    ]
    summary = _manual_validation_status([
        status for _unit_id, statuses in statuses_by_unit for status in statuses
    ])
    outstanding = {"pending": [], "failed": []}
    for unit_id, statuses in statuses_by_unit:
        unit_status = _manual_validation_status(statuses)["status"]
        if unit_status in outstanding:
            outstanding[unit_status].append(unit_id)
    return summary, outstanding


def _manual_validation_status(statuses: list[str]) -> dict:
    """Return one compact status for a fixed set of persisted check positions."""
    summary = {
        "total": len(statuses),
        "passed": sum(status == "passed" for status in statuses),
        "failed": sum(status == "failed" for status in statuses),
        "pending": sum(status == "pending" for status in statuses),
    }
    status = (
        "no_manual_checks" if not statuses
        else "failed" if summary["failed"]
        else "pending" if summary["pending"]
        else "reported_passed"
    )
    return {"status": status, "summary": summary}


def describe_change_plan_validation_status(
    conn: sqlite3.Connection,
    plan_id: object,
    limit: object = DEFAULT_PLAN_VALIDATION_UNIT_LIMIT,
    offset: object = 0,
) -> dict:
    """List bounded manual-validation state without repeating checklist text."""
    if not isinstance(plan_id, str) or (match := re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)) is None:
        return {"error": "plan_id must have the form cp_<positive integer>"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    if stored_plan["status"] != "ready":
        return {"error": f"plan must be ready before reviewing validation (status: {stored_plan['status']})"}
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PLAN_VALIDATION_UNIT_LIMIT:
        return {"error": f"limit must be an integer between 1 and {MAX_PLAN_VALIDATION_UNIT_LIMIT}"}
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return {"error": "offset must be a non-negative integer"}
    change_units = json.loads(stored_plan["change_units_json"])
    results = {
        (result["change_unit_id"], result["check_index"]): result["status"]
        for result in manual_validation_results_repo.list_results(conn, int(match.group(1)))
    }
    units = []
    all_statuses: list[str] = []
    for unit in change_units:
        checks = unit.get("validation", [])
        if not isinstance(checks, list) or not checks:
            continue
        statuses = [results.get((unit["id"], index), "pending") for index, _check in enumerate(checks)]
        all_statuses.extend(statuses)
        units.append({"id": unit["id"], **_manual_validation_status(statuses)})
    page = units[offset:offset + limit]
    total = len(units)
    return {
        "plan_id": plan_id,
        **_manual_validation_status(all_statuses),
        "units": page,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": total,
            "truncated": offset + len(page) < total,
            "next_offset": offset + len(page) if offset + len(page) < total else None,
        },
    }


def record_ci_validation_result(
    conn: sqlite3.Connection,
    plan_id: object,
    repository: object,
    workflow_path: object,
    start_line: object,
    status: object,
    duration_ms: object = None,
) -> dict:
    """Persist one agent-reported result for an indexed safe CI validation command."""
    if not isinstance(plan_id, str) or (match := re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)) is None:
        return {"error": "plan_id must have the form cp_<positive integer>"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    if stored_plan["status"] != "ready":
        return {"error": f"plan must be ready before recording validation (status: {stored_plan['status']})"}
    if not isinstance(repository, str) or not repository:
        return {"error": "repository must be a non-empty string"}
    repo = repositories_repo.get_repository_by_name(conn, repository)
    if repo is None:
        return {"error": f"unknown repository: {repository}"}
    if not isinstance(workflow_path, str) or not workflow_path:
        return {"error": "workflow_path must be a non-empty string"}
    if not isinstance(start_line, int) or isinstance(start_line, bool) or start_line < 1:
        return {"error": "start_line must be a positive integer"}
    if status not in {"passed", "failed"}:
        return {"error": "status must be 'passed' or 'failed'"}
    if duration_ms is not None and (
        not isinstance(duration_ms, int)
        or isinstance(duration_ms, bool)
        or not 0 <= duration_ms <= MAX_CI_VALIDATION_DURATION_MS
    ):
        return {"error": f"duration_ms must be an integer between 0 and {MAX_CI_VALIDATION_DURATION_MS}"}
    commands = ci_commands_repo.list_ci_commands_at_location(conn, repo["id"], workflow_path, start_line)
    if not commands:
        return {"error": "unknown indexed CI command"}
    if len(commands) != 1:
        return {"error": "ambiguous indexed CI command"}
    command = commands[0]
    if command["kind"] not in {"test", "build"}:
        return {"error": "indexed command is not a test or build validation"}
    ci_validation_results_repo.record_result(conn, int(match.group(1)), repo["id"], {
        "workflow_path": command["workflow_path"],
        "kind": command["kind"],
        "command": command["command"],
        "start_line": command["start_line"],
        "status": status,
        "duration_ms": duration_ms,
    })
    return {"ok": True, "status": status, "duration_ms": duration_ms}


def record_change_unit_validation_result(
    conn: sqlite3.Connection, plan_id: object, change_unit_id: object, check_index: object, status: object,
) -> dict:
    """Persist an agent-reported state for one pre-existing manual validation check."""
    if not isinstance(plan_id, str) or (match := re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)) is None:
        return {"error": "plan_id must have the form cp_<positive integer>"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    if stored_plan["status"] != "ready":
        return {"error": f"plan must be ready before recording validation (status: {stored_plan['status']})"}
    if not isinstance(change_unit_id, str) or not change_unit_id:
        return {"error": "change_unit_id must be a non-empty string"}
    if not isinstance(check_index, int) or isinstance(check_index, bool):
        return {"error": "check_index must identify a persisted validation check"}
    if not isinstance(status, str) or status not in {"passed", "failed"}:
        return {"error": "status must be 'passed' or 'failed'"}
    change_unit = next(
        (unit for unit in json.loads(stored_plan["change_units_json"]) if unit.get("id") == change_unit_id),
        None,
    )
    if change_unit is None:
        return {"error": f"unknown change_unit_id: {change_unit_id}"}
    validation = change_unit.get("validation")
    if not isinstance(validation, list) or not 0 <= check_index < len(validation):
        return {"error": "check_index must identify a persisted validation check"}
    manual_validation_results_repo.record_result(
        conn, int(match.group(1)), change_unit_id, check_index, status,
    )
    return {"ok": True, "status": status}


def _measure_plan_response(response: dict) -> TokenMeasurement:
    """Stabilize the count after the budget fields themselves enter the JSON."""
    budget = response["budget"]
    measurement = measure_json_tokens(response)
    for _ in range(3):
        if (budget["estimated_tokens"], budget["measurement"]) == (measurement.tokens, measurement.method):
            return measurement
        budget["estimated_tokens"] = measurement.tokens
        budget["measurement"] = measurement.method
        measurement = measure_json_tokens(response)
    return measurement


def _derive_http_contract_review_units(
    conn: sqlite3.Connection, primary_services: list[str], repository_id: int | None,
) -> list[dict]:
    """Return review units for fully resolved, source-proven internal HTTP calls."""
    units: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for service_name in primary_services:
        source_service = services_repo.get_service_by_name(conn, service_name, repository_id=repository_id)
        if source_service is None:
            continue
        for call in flows_repo.list_static_service_calls(conn, source_service["id"]):
            method = call["target_method"]
            path = call["target_path"]
            if call["protocol"] != "http" or not isinstance(method, str) or not isinstance(path, str):
                continue
            target_service, _candidates = services_repo.resolve_service_reference(
                conn, call["target_service"], source_service["repository_id"],
            )
            if target_service is None or flows_repo.get_entrypoint(
                conn, target_service["id"], "http", method, path,
            ) is None:
                continue
            key = service_name, target_service["name"], method, path
            if key in seen:
                continue
            seen.add(key)
            evidence = [{
                "file": call["file_path"], "start_line": call["start_line"], "end_line": call["end_line"],
            }]
            contract = f"{method} {path}"
            units.append({
                "id": f"http-contract:{service_name}:{target_service['name']}:{method}:{path}",
                "service": service_name,
                "target": {"role": "integration", "symbol": call["source"], "evidence": evidence},
                "action": "review",
                "reason": (
                    f"a source-proven HTTP call reaches {target_service['name']} {contract}; "
                    "review both sides if this boundary changes."
                ),
                "preconditions": [],
                "related_contracts": [contract],
                "dependencies": [target_service["name"]],
                "validation": [f"verify client and {target_service['name']} agree on {contract}"],
                "confidence": 1.0,
                "evidence": evidence,
            })
    return units


def refine_change_plan(conn: sqlite3.Connection, plan_id: str, decisions: list[dict]) -> dict:
    """Persist explicit decisions for an existing plan without repeating retrieval."""
    match = re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)
    if match is None:
        return {"error": "invalid plan_id"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    decision_points = json.loads(stored_plan["decision_points_json"])
    previous_selections = json.loads(stored_plan["selected_decisions_json"])
    change_units = json.loads(stored_plan["change_units_json"])
    if previous_selections:
        if decisions != previous_selections:
            return {"error": "plan decisions already finalized"}
        return _refined_plan_response(plan_id, stored_plan["status"], previous_selections, change_units)
    selections, error = validate_decision_selections(decision_points, decisions)
    if error is not None:
        return {"error": error}
    if decision_points:
        change_units = derive_change_units(decision_points, selections)
        change_plans_repo.finalize_decisions(conn, int(match.group(1)), selections, change_units)
        return _refined_plan_response(plan_id, "ready", selections, change_units)
    return _refined_plan_response(plan_id, stored_plan["status"], selections, change_units)


def _refined_plan_response(plan_id: str, status: str, selections: list[dict], change_units: list[dict]) -> dict:
    return {
        "plan_id": plan_id,
        "status": status,
        "selected_decisions": selections,
        "remaining_decision_points": [],
        "change_units": change_units,
    }


def describe_change_unit(conn: sqlite3.Connection, plan_id: str, change_unit_id: str) -> dict:
    """Return one persisted unit, its reading path and truncated-evidence follow-up."""
    match = re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)
    if match is None:
        return {"error": "invalid plan_id"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    change_unit = next(
        (unit for unit in json.loads(stored_plan["change_units_json"]) if unit.get("id") == change_unit_id),
        None,
    )
    if change_unit is None:
        return {"error": f"unknown change_unit_id: {change_unit_id}"}
    response = {
        "plan_id": plan_id,
        "change_unit": change_unit,
        "minimal_reading": _minimal_unit_reading(change_unit),
        "validation": change_unit["validation"],
        "validation_status": _change_unit_validation_status(conn, int(match.group(1)), change_unit),
    }
    if (evidence_follow_up := _evidence_follow_up(change_unit)) is not None:
        response["evidence_follow_up"] = evidence_follow_up
    return response


def _change_unit_validation_status(conn: sqlite3.Connection, plan_id: int, change_unit: dict) -> dict:
    """Project current structured statuses without duplicating persisted check text."""
    results = {
        result["check_index"]: result["status"]
        for result in manual_validation_results_repo.list_results(conn, plan_id)
        if result["change_unit_id"] == change_unit["id"]
    }
    checks = [
        {"index": index, "status": results.get(index, "pending")}
        for index, _check in enumerate(change_unit["validation"])
    ]
    return {
        "summary": {
            "total": len(checks),
            "passed": sum(check["status"] == "passed" for check in checks),
            "failed": sum(check["status"] == "failed" for check in checks),
            "pending": sum(check["status"] == "pending" for check in checks),
        },
        "checks": checks,
    }


def validate_runtime_configuration_follow_up(
    conn: sqlite3.Connection, plan_id: str, change_unit_id: str, returned_workloads: object,
    source_import_truncated: object, current_offset: object = 0, current_limit: object = DEFAULT_LIST_LIMIT,
    previous_page_fingerprint: object = None,
    include_matched_workloads: object = False,
    include_trace_details: object = False,
) -> dict:
    """Compare one runtime-configuration page with a truncated change-unit target."""
    if not isinstance(returned_workloads, list):
        return {"error": "returned_workloads must be a list"}
    if not isinstance(source_import_truncated, bool):
        return {"error": "source_import_truncated must be a boolean"}
    if not isinstance(include_matched_workloads, bool):
        return {"error": "include_matched_workloads must be a boolean"}
    if not isinstance(include_trace_details, bool):
        return {"error": "include_trace_details must be a boolean"}
    if not isinstance(current_offset, int) or isinstance(current_offset, bool) or current_offset < 0:
        return {"error": "current_offset must be a non-negative integer"}
    if (
        not isinstance(current_limit, int)
        or isinstance(current_limit, bool)
        or not 1 <= current_limit <= MAX_LIST_LIMIT
    ):
        return {"error": f"current_limit must be an integer between 1 and {MAX_LIST_LIMIT}"}
    if current_offset % current_limit != 0:
        return {"error": "current_offset must be a multiple of current_limit"}
    if previous_page_fingerprint is not None and (
        not isinstance(previous_page_fingerprint, str)
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", previous_page_fingerprint) is None
    ):
        return {"error": "previous_page_fingerprint must be a URL-safe SHA-256 digest"}
    match = re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)
    if match is None:
        return {"error": "invalid plan_id"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    change_unit = next(
        (unit for unit in json.loads(stored_plan["change_units_json"]) if unit.get("id") == change_unit_id),
        None,
    )
    if change_unit is None:
        return {"error": f"unknown change_unit_id: {change_unit_id}"}
    expected_workloads = _truncated_workload_scopes(change_unit)
    if not expected_workloads:
        return {"error": "change unit has no truncated workload evidence"}
    returned_keys, returned_workloads_error = _returned_workload_scope_keys(returned_workloads)
    if returned_workloads_error is not None:
        return {"error": returned_workloads_error}
    matched_workloads = [
        workload for workload in expected_workloads if _workload_scope_key(workload) in returned_keys
    ]
    missing_workloads = [
        workload for workload in expected_workloads if _workload_scope_key(workload) not in returned_keys
    ]
    status = (
        "complete" if not missing_workloads
        else "needs_next_page" if source_import_truncated
        else "incomplete"
    )
    page_fingerprint = None
    if status == "needs_next_page":
        page_fingerprint, page_fingerprint_error = _runtime_configuration_page_fingerprint(returned_workloads)
        if page_fingerprint_error is not None:
            return {"error": page_fingerprint_error}
        if previous_page_fingerprint == page_fingerprint:
            status = "stalled"
    response = {
        "trace_id": _runtime_configuration_follow_up_trace_id(plan_id, change_unit_id),
        "status": status,
        "matched_workload_count": len(matched_workloads),
        "missing_workloads": missing_workloads,
        "continue_pagination": status == "needs_next_page",
    }
    if include_matched_workloads:
        response["matched_workloads"] = matched_workloads
    if include_trace_details:
        response.update({"plan_id": plan_id, "change_unit_id": change_unit_id})
    if status == "needs_next_page":
        response["page_fingerprint"] = page_fingerprint
        response["next_query"] = {
            "tool": "describe_runtime_configuration",
            "arguments": {
                "service": change_unit["service"], "limit": current_limit,
                "offset": 0,
                "workloads": [
                    {field: workload[field] for field in ("kind", "name", "container")}
                    for workload in missing_workloads
                ],
            },
        }
    elif status == "stalled":
        response["reason"] = "runtime configuration page repeated"
    return response


def assess_working_change(
    conn: sqlite3.Connection, plan_id: str, repository: str, since_commit: str,
) -> dict:
    """Assess an indexed plan against one repository's Git diff without an LLM."""
    match = re.fullmatch(r"cp_([1-9][0-9]*)", plan_id)
    if match is None:
        return {"error": "plan_id must have the form cp_<positive integer>"}
    stored_plan = change_plans_repo.get_plan(conn, int(match.group(1)))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    if stored_plan["status"] != "ready":
        return {"error": f"plan must be ready before assessment (status: {stored_plan['status']})"}
    repo = repositories_repo.get_repository_by_name(conn, repository)
    if repo is None:
        return {"error": f"unknown repository: {repository}"}
    changed_files, error = git_working_changed_files_with_status(Path(repo["root_path"]), since_commit)
    if error is not None:
        return {"error": error}

    change_units = json.loads(stored_plan["change_units_json"])
    service_names = {
        service
        for unit in change_units
        for service in [unit.get("service"), *unit.get("dependencies", [])]
        if isinstance(service, str)
    }
    service_rows = {
        name: service
        for name in service_names
        if (service := services_repo.get_service_by_name(conn, name, repository_id=repo["id"])) is not None
    }
    service_roots = {name: Path(service["root_path"]) for name, service in service_rows.items()}
    public_error_contracts = [
        {**dict(contract), "service": service_name}
        for service_name, service in sorted(service_rows.items())
        for contract in flows_repo.list_static_error_contracts(conn, service["id"])
    ]
    current_public_error_contracts = _current_public_error_contracts_for_changed_services(
        Path(repo["root_path"]), changed_files, service_rows,
    )
    assessment = assess_change_units(
        Path(repo["root_path"]), changed_files, change_units, service_roots, public_error_contracts,
        current_public_error_contracts,
    )
    ci_validation_commands = (
        _compact_ci_validation_commands(conn, repo["id"])
        if changed_files_touch_service_roots(Path(repo["root_path"]), changed_files, service_roots)
        else []
    )
    ci_validation_results = _ci_validation_result_summaries(
        conn, int(match.group(1)), repo["id"], ci_validation_commands,
    )
    return {
        "plan_id": plan_id,
        "repository": repository,
        "since_commit": since_commit,
        "ci_validation_commands": ci_validation_commands,
        "ci_validation_results": ci_validation_results,
        **assessment,
    }


def review_change_closure(conn: sqlite3.Connection, plan_id: str, repository: str, since_commit: str) -> dict:
    """Combine a bounded diff assessment and reported CI state without approving release."""
    assessment = assess_working_change(conn, plan_id, repository, since_commit)
    if "error" in assessment:
        return assessment
    ci_validation = describe_change_validation_status(conn, plan_id, repository)
    if "error" in ci_validation:
        return ci_validation
    stored_plan = change_plans_repo.get_plan(conn, int(plan_id.removeprefix("cp_")))
    if stored_plan is None:
        return {"error": f"unknown plan_id: {plan_id}"}
    manual_validation, manual_outstanding = _manual_validation_breakdown(
        conn, int(plan_id.removeprefix("cp_")), json.loads(stored_plan["change_units_json"]),
    )
    ci_outstanding = [
        {
            "workflow_path": command["workflow_path"],
            "start_line": command["start_line"],
            "kind": command["kind"],
            "status": command["status"],
        }
        for command in ci_validation["commands"]
        if command["status"] != "passed"
    ]
    closure = summarize_change_closure(
        assessment, ci_validation, manual_validation, manual_outstanding, ci_outstanding,
        MAX_CLOSURE_CHANGE_UNIT_IDS,
    )
    repo = repositories_repo.get_repository_by_name(conn, repository)
    if repo is None:
        return {"error": f"unknown repository: {repository}"}
    closure_summaries_repo.record_summary(conn, int(plan_id.removeprefix("cp_")), repo["id"], closure)
    return {
        "plan_id": plan_id,
        "repository": repository,
        "since_commit": since_commit,
        **closure,
    }


def _current_public_error_contracts_for_changed_services(
    repository_root: Path, changed_files: list[str], service_rows: dict[str, sqlite3.Row],
) -> list[dict]:
    """Analyze only changed planned services for an advisory public-contract diff."""
    current_contracts: list[dict] = []
    for service_name, service in service_rows.items():
        service_root = Path(service["root_path"])
        if not _service_has_changed_file(repository_root, service_root, changed_files):
            continue
        if not isinstance(service["stack"], str):
            continue
        try:
            analysis = StaticAnalysisEngine().analyze(service_root, service["stack"])
        except Exception:
            logger.warning("could not analyze changed service for public error contract assessment: %s", service_name)
            continue
        current_contracts.extend({
            "service": service_name,
            "source": contract.source,
            "role": contract.role,
            "error_kind": contract.error_kind,
            "internal_type": contract.internal_type,
            "protocol": contract.protocol,
            "transport_code": contract.transport_code,
            "public_code": contract.public_code,
            "file_path": contract.evidence.file_path,
            "start_line": contract.evidence.start_line,
            "end_line": contract.evidence.end_line,
        } for contract in analysis.error_contracts)
    return current_contracts


def _service_has_changed_file(repository_root: Path, service_root: Path, changed_files: list[str]) -> bool:
    try:
        relative_root = service_root.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return False
    if relative_root == ".":
        return bool(changed_files)
    return any(path == relative_root or path.startswith(f"{relative_root}/") for path in changed_files)


def _minimal_unit_reading(change_unit: dict) -> list[dict]:
    producer = change_unit["service"]
    if (
        change_unit["target"]["role"] == "integration"
        or change_unit["id"].startswith("retry-downstream-error:")
    ):
        target_service = change_unit["dependencies"][0]
        return [
            {
                "service": producer,
                "purpose": "confirm the literal outbound HTTP client",
                "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
            },
            {
                "service": target_service,
                "purpose": "confirm the resolved target endpoint contract",
                "recommended_query": {"tool": "list_entrypoints", "arguments": {"service": target_service}},
            },
        ]
    if change_unit["id"].startswith("partial-write-resilience:"):
        return [{
            "service": producer,
            "purpose": "confirm local writes and the indexed outbound HTTP boundary",
            "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
        }]
    if change_unit["id"].startswith("retry-policy:"):
        return [{
            "service": producer,
            "purpose": "confirm the indexed local retry policy and error flow",
            "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
        }]
    if change_unit["id"].startswith("retry-http-idempotency:"):
        return [{
            "service": producer,
            "purpose": "confirm retry idempotency at the indexed outbound HTTP boundary",
            "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
        }]
    if change_unit["id"].startswith("timeout-local-fallback:"):
        return [{
            "service": producer,
            "purpose": "confirm the indexed timeout boundary and local fallback coverage",
            "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
        }]
    if change_unit["id"].startswith("http-resilience-policy:"):
        return [{
            "service": producer,
            "purpose": "confirm the indexed outbound HTTP boundary and resilience policy",
            "recommended_query": {"tool": "describe_service", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "error_mapping":
        return [{
            "service": producer,
            "purpose": "identify the entrypoint that owns the public error contract",
            "recommended_query": {"tool": "list_entrypoints", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "persistence":
        return [{
            "service": producer,
            "purpose": "confirm the affected schema and indexed migration operations",
            "recommended_query": {"tool": "describe_persistence", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "feature_flag":
        return [{
            "service": producer,
            "purpose": "confirm the indexed feature flag and its guarded behavior",
            "recommended_query": {"tool": "describe_feature_flags", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "deployment":
        return [{
            "service": producer,
            "purpose": "confirm the indexed cloud dependency and IaC declaration",
            "recommended_query": {"tool": "describe_cloud_dependencies", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "entrypoint":
        return [{
            "service": producer,
            "purpose": "confirm the indexed entrypoint contract and reachable flow",
            "recommended_query": {"tool": "list_entrypoints", "arguments": {"service": producer}},
        }]
    if change_unit["target"]["role"] == "configuration":
        if change_unit["target"]["symbol"].startswith("kubernetes:"):
            is_unresolved_import = change_unit["id"].startswith("runtime-configuration-source-import-unknown:")
            purpose = (
                "confirm the owner of the unresolved Kubernetes configuration source"
                if change_unit["id"].startswith((
                    "runtime-configuration-source-unknown:",
                    "runtime-configuration-source-import-unknown:",
                ))
                else "confirm the indexed Kubernetes configuration mismatch"
            )
            if is_unresolved_import and (scope := _kubernetes_workload_reading_scope(change_unit["target"])):
                purpose = f"{purpose} and inspect {scope}"
            return [{
                "service": producer,
                "purpose": purpose,
                "recommended_query": {
                    "tool": "describe_runtime_configuration", "arguments": {"service": producer},
                },
            }]
        return [{
            "service": producer,
            "purpose": "confirm the indexed code and Kubernetes configuration binding",
            "recommended_query": {"tool": "describe_configuration", "arguments": {"service": producer}},
        }]
    reading = [{
        "service": producer,
        "purpose": "confirm the producer contract",
        "recommended_query": {"tool": "describe_messages", "arguments": {"service": producer}},
    }]
    for consumer in change_unit["dependencies"]:
        if consumer == producer:
            continue
        reading.append({
            "service": consumer,
            "purpose": "confirm consumer compatibility",
            "recommended_query": {"tool": "describe_messages", "arguments": {"service": consumer}},
        })
    return reading


def _truncated_workload_scopes(change_unit: dict) -> list[dict]:
    """Return unique, valid workload scopes whose compact evidence was bounded."""
    target = change_unit.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("workloads"), list):
        return []
    scopes: list[dict] = []
    for workload in target["workloads"]:
        if not isinstance(workload, dict) or workload.get("evidence_truncated") is not True:
            continue
        if (scope_key := _workload_scope_key(workload)) is None:
            continue
        kind, name, container = scope_key
        scope = {"kind": kind, "name": name, "container": container}
        evidence_total = workload.get("evidence_total")
        if isinstance(evidence_total, int) and not isinstance(evidence_total, bool) and evidence_total > 0:
            scope["evidence_total"] = evidence_total
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def _workload_scope_key(workload: object) -> tuple[str, str, str] | None:
    """Normalize a workload identity without trusting optional evidence fields."""
    if not isinstance(workload, dict):
        return None
    kind, name, container = (workload.get(field) for field in ("kind", "name", "container"))
    if not all(isinstance(value, str) and value for value in (kind, name, container)):
        return None
    return kind, name, container


def _returned_workload_scope_keys(workloads: list[object]) -> tuple[set[tuple[str, str, str]], str | None]:
    """Normalize direct workload identities or native runtime-configuration entries."""
    scopes: set[tuple[str, str, str]] = set()
    for workload in workloads:
        direct_scope = _workload_scope_key(workload)
        nested_scope = None
        if isinstance(workload, dict) and "workload" in workload:
            nested_scope = _workload_scope_key(workload["workload"])
            if nested_scope is None:
                return set(), "each returned workload must have non-empty kind, name, and container"
        if direct_scope is not None and nested_scope is not None and direct_scope != nested_scope:
            return set(), "returned workload identity conflicts with nested workload"
        scope = nested_scope or direct_scope
        if scope is None:
            return set(), "each returned workload must have non-empty kind, name, and container"
        scopes.add(scope)
    return scopes, None


def _runtime_configuration_page_fingerprint(workloads: list[object]) -> tuple[str | None, str | None]:
    """Return a stable marker for one agent-provided runtime-configuration page."""
    try:
        canonical_page = json.dumps(workloads, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return None, "returned_workloads must contain JSON values"
    digest = hashlib.sha256(canonical_page.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("="), None


def _runtime_configuration_follow_up_trace_id(plan_id: str, change_unit_id: str) -> str:
    """Return a compact deterministic correlator for one persisted plan unit."""
    digest = hashlib.sha256(f"{plan_id}\0{change_unit_id}".encode()).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _truncated_workload_evidence_next_step(workloads: list[dict]) -> str:
    """Choose the smallest safe next action from persisted evidence counts."""
    totals = [workload.get("evidence_total") for workload in workloads]
    if any(not isinstance(total, int) or isinstance(total, bool) or total < 1 for total in totals):
        return "query_runtime_configuration"
    if max(totals, default=0) <= MAX_INLINE_WORKLOAD_EVIDENCE:
        return "inspect_change_unit_evidence"
    return "query_runtime_configuration"


def _evidence_follow_up(change_unit: dict) -> dict | None:
    """Build a bounded next action only when workload evidence was truncated."""
    workloads = _truncated_workload_scopes(change_unit)
    if not workloads:
        return None
    next_step = _truncated_workload_evidence_next_step(workloads)
    follow_up = {
        "reason": "workload evidence is truncated",
        "recommended_next_step": next_step,
        "workloads": workloads,
        "recommended_query": {
            "tool": "describe_runtime_configuration", "arguments": {"service": change_unit["service"]},
        },
    }
    if next_step == "query_runtime_configuration":
        follow_up["pagination"] = {
            "limit": DEFAULT_LIST_LIMIT, "offset": 0, "next_offset": DEFAULT_LIST_LIMIT,
            "continue_when": "source_import_truncated", "stop_when": "all_selected_workloads_found",
        }
    return follow_up


def _kubernetes_workload_reading_scope(target: dict) -> str | None:
    """Format only persisted, source-proven workload scopes for a reading prompt."""
    workloads = target.get("workloads")
    if not isinstance(workloads, list):
        return None
    scopes: list[str] = []
    for workload in workloads:
        if not isinstance(workload, dict):
            continue
        kind, name, container = (workload.get(field) for field in ("kind", "name", "container"))
        if not all(isinstance(value, str) and value for value in (kind, name, container)):
            continue
        scope = f"{kind} {name} container {container}"
        locations = _workload_evidence_locations(workload.get("evidence"))
        if locations:
            scope = f"{scope} ({', '.join(locations)})"
        if scope not in scopes:
            scopes.append(scope)
    return " and ".join(scopes) or None


def _workload_evidence_locations(evidence: object) -> list[str]:
    """Return a bounded set of manifest locations for a workload-reading prompt."""
    if not isinstance(evidence, list):
        return []
    locations: list[str] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        file_path, start_line, end_line = (item.get(field) for field in ("file", "start_line", "end_line"))
        if not isinstance(file_path, str) or not isinstance(start_line, int) or not isinstance(end_line, int):
            continue
        location = f"{file_path}:{start_line}-{end_line}"
        if location not in locations:
            locations.append(location)
        if len(locations) == 2:
            break
    return locations


def get_change_context(
    conn: sqlite3.Connection,
    backend: LLMBackend,
    task: str,
    hint_services: list[str] | None = None,
    repository: str | None = None,
    max_services: int = 3,
    epic_type: str = "unspecified",
) -> dict:
    """Return a bounded epic briefing from one change-surface inference plus facts.

    No source file is read here. The lower-level tools remain the detailed follow-up
    path; this response only selects their highest-value context for the first plan.
    """
    if not 1 <= max_services <= MAX_CONTEXT_SERVICES:
        return {"error": f"max_services must be between 1 and {MAX_CONTEXT_SERVICES} (got {max_services})"}
    if not _EPIC_TYPE.fullmatch(epic_type):
        return {"error": "epic_type must be a lowercase identifier (letters, numbers, _ or -, max 64 chars)"}
    repository_id = None
    if repository is not None:
        repo = repositories_repo.get_repository_by_name(conn, repository)
        if repo is None:
            return {"error": f"unknown repository: {repository}"}
        repository_id = repo["id"]
    surface = find_change_surface(conn, backend, task, hint_services, repository)
    if "error" in surface:
        return surface
    architecture = find_architecture_smells(conn)
    context = build_change_context(
        conn, task, surface, architecture["findings"], max_services, repository_id,
    )
    if repository is not None:
        context["scope"] = {"repository": repository}
    _record_context_telemetry(conn, context, surface, repository_id, epic_type)
    return context


def _record_context_telemetry(
    conn: sqlite3.Connection, context: dict, surface: dict, repository_id: int | None, epic_type: str,
) -> None:
    """Record calibration metadata after delivery data is ready, never blocking it.

    Task text, cards, code and recommendation reasons deliberately stay out of the
    telemetry tables. Only service IDs and bounded tool identifiers are retained.
    """
    candidates: list[dict] = []
    seen_names: set[str] = set()
    for role in ("primary", "secondary"):
        for finding in surface.get(role, []):
            name = finding["service"]
            if name in seen_names:
                continue
            seen_names.add(name)
            row = services_repo.get_service_by_name(conn, name, repository_id)
            if row is not None:
                candidates.append({"service_id": row["id"], "role": role, "rank": len(candidates) + 1})
    included = [
        row["id"]
        for card in context["services"]
        if (row := services_repo.get_service_by_name(conn, card["service"], repository_id)) is not None
    ]
    omitted = [candidate["service_id"] for candidate in candidates if candidate["service_id"] not in included]
    recommendations = []
    for rank, item in enumerate(context["recommended_next_queries"], start=1):
        arguments = item.get("arguments", {})
        service_id = None
        if service := arguments.get("service"):
            row = services_repo.get_service_by_name(conn, service, repository_id)
            service_id = row["id"] if row is not None else None
        recommendations.append({"tool": item["tool"], "service_id": service_id, "rank": rank})
    base_measurement = measure_json_tokens(context)
    base_bytes = len(json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    metadata = {
        "change_surface_run_id": surface.get("run_id"), "repository_id": repository_id, "epic_type": epic_type,
        "requested_budget": context["budget"]["max_services"], "returned_cards": context["budget"]["returned_services"],
        "candidate_count": len(candidates), "truncated": context["budget"]["truncated"],
        "response_bytes": base_bytes, "estimated_tokens": base_measurement.tokens,
        "token_measurement": base_measurement.method,
        "included_service_ids": included, "omitted_service_ids": omitted,
        "candidate_ranking": candidates, "recommended_queries": recommendations,
    }
    try:
        run_id = context_telemetry_repo.record_run(conn, metadata)
        context["telemetry"] = {"recorded": True, "run_id": run_id}
        response_bytes = len(json.dumps(context, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
        response_measurement = measure_json_tokens(context)
        context_telemetry_repo.update_response_measurements(
            conn, run_id, response_bytes, response_measurement.tokens, response_measurement.method,
        )
    except Exception:  # telemetry must never turn an otherwise valid context into an error
        logger.warning("context telemetry recording failed")
        context["telemetry"] = {"recorded": False}


def record_change_context_feedback(
    conn: sqlite3.Connection,
    run_id: int,
    outcome: str,
    note: str | None = None,
    missing_services: list[str] | None = None,
) -> dict:
    """Record whether a compact briefing was sufficient without retaining its note."""
    if outcome not in ("sufficient", "insufficient", "excessive"):
        return {"error": "outcome must be 'sufficient', 'insufficient' or 'excessive'"}
    run = context_telemetry_repo.get_run(conn, run_id)
    if run is None:
        return {"error": f"unknown context run_id: {run_id}"}
    missing_ids: list[int] = []
    for service in missing_services or []:
        row = services_repo.get_service_by_name(conn, service, run["repository_id"])
        if row is None:
            return {"error": f"unknown missing service: {service}"}
        missing_ids.append(row["id"])
    context_telemetry_repo.record_feedback(conn, run_id, outcome, note, missing_ids)
    return {"ok": True, "note_recorded": note is not None}


def record_context_query_execution(
    conn: sqlite3.Connection, run_id: int, tool: str, service: str | None = None,
) -> dict:
    """Associate an executed follow-up tool call with a context briefing."""
    run = context_telemetry_repo.get_run(conn, run_id)
    if run is None:
        return {"error": f"unknown context run_id: {run_id}"}
    service_id = None
    if service is not None:
        row = services_repo.get_service_by_name(conn, service, run["repository_id"])
        if row is None:
            return {"error": f"unknown service: {service}"}
        service_id = row["id"]
    recommendations = json.loads(run["recommended_queries_json"])
    if not any(item["tool"] == tool and item.get("service_id") == service_id for item in recommendations):
        return {"error": "tool/service was not recommended for this context run"}
    context_telemetry_repo.record_query_execution(conn, run_id, tool, service_id)
    return {"ok": True}


def get_context_budget_metrics(conn: sqlite3.Connection, epic_type: str | None = None) -> dict:
    """Return aggregate calibration data; no tasks, code, prompt or card text is exposed."""
    if epic_type is not None and not _EPIC_TYPE.fullmatch(epic_type):
        return {"error": "epic_type must be a lowercase identifier (letters, numbers, _ or -, max 64 chars)"}
    metrics = context_telemetry_repo.aggregate(conn, epic_type)
    feedback_total = sum(metrics["sufficiency"].values())
    metrics["recommendation"] = (
        {"status": "insufficient_history", "minimum_feedback": 3, "feedback_count": feedback_total}
        if feedback_total < 3 else
        {"status": "keep_fixed_cap", "max_services": MAX_CONTEXT_SERVICES,
         "reason": "adaptive selection is deferred until budget-specific history is evaluated"}
    )
    if epic_type is not None:
        metrics["epic_type"] = epic_type
    return metrics


def verify_context_budget(conn: sqlite3.Connection, run_id: int, repository: str, since_commit: str) -> dict:
    """Use Git ground truth to measure context-card precision, recall and omission."""
    return _verify_context_budget(conn, run_id, repository, since_commit)


def record_change_surface_feedback(conn: sqlite3.Connection, run_id: int, service: str, outcome: str) -> dict:
    """Closes the loop on a past find_change_surface call: report whether a finding
    was actually confirmed (you changed that service) or rejected (it wasn't needed).
    Future find_change_surface confidence for this service is nudged by this history
    (see generation.change_surface._recalibrate_confidence)."""
    if outcome not in ("confirmed", "rejected"):
        return {"error": f"invalid outcome: {outcome!r} (expected 'confirmed' or 'rejected')"}
    run = change_surface_repo.get_change_surface_run(conn, run_id)
    if run is None:
        return {"error": f"unknown change surface run_id: {run_id}"}
    findings = change_surface_repo.list_change_surface_findings(conn, run_id)
    if not any(f["service"] == service for f in findings):
        return {"error": f"service {service!r} was not part of run {run_id}"}
    change_surface_repo.record_change_surface_feedback(conn, run_id, service, outcome)
    return {"ok": True}


def verify_change_surface(conn: sqlite3.Connection, run_id: int, repository: str, since_commit: str) -> dict:
    """Read-only comparison of a past find_change_surface run against what a
    repository's commits actually changed since a given commit (git ground truth).
    Never records feedback itself — call record_change_surface_feedback separately
    if you want this comparison to influence future confidence."""
    return _verify_change_surface(conn, run_id, repository, since_commit, record_feedback=False)
