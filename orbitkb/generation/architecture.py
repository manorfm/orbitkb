"""Deterministic, whole-graph architecture findings — no LLM, pure SQL + a graph
traversal over facts index/update already wrote (service_calls, persistence_entities).
Recomputed after every index/update (see orchestrator.py), so a finding is only ever as
fresh as the last indexing run; a system with unindexed services still gets findings
over the subgraph that IS indexed, the same posture find_change_surface already takes
toward partial knowledge — never silently "no smells" when the truth is "not enough
indexed to tell".
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from orbitkb.db.repositories import architecture as architecture_repo
from orbitkb.db.repositories import canonical_snapshots as canonical_snapshots_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.domain.canonical import CanonicalFact, EntrypointKey, SymbolKey
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.generation.runtime_configuration import ordered_kubernetes_workloads

# Starting heuristic, not a trained threshold: flag a service once its fan-in or
# fan-out crosses this count. Low enough to catch small systems, high enough that a
# handful of legitimate dependencies doesn't trigger noise.
FAN_THRESHOLD = 4
# Same posture as FAN_THRESHOLD, at the component (intra-service) scale instead of the
# service (system) scale. A single service's internal call graph tends to be denser
# than the graph between services, so this starts higher — still a starting heuristic,
# not a value calibrated against a real large indexed project.
COMPONENT_FAN_THRESHOLD = 5
READ_ENTRYPOINT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
BROAD_EXCEPTION_TYPES = frozenset({"exception", "throwable", "error", "runtimeexception"})
POTENTIALLY_NON_IDEMPOTENT_HTTP_METHODS = frozenset({"POST", "PATCH"})
CLIENT_ERROR_KINDS = frozenset({"authorization", "conflict", "not_found", "rate_limit", "validation"})
DOWNSTREAM_FLOW_TRUNCATED_UNKNOWN = "The downstream endpoint flow was truncated before all errors could be checked."


def _internal_edges(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT DISTINCT from_service_id, to_service_id FROM service_calls "
        "WHERE target_kind = 'internal' AND to_service_id IS NOT NULL"
    ).fetchall()
    return [(row["from_service_id"], row["to_service_id"]) for row in rows]


def _service_names(conn: sqlite3.Connection) -> dict[int, str]:
    """Return an unambiguous display identity for every indexed service.

    A short service name remains pleasant when unique. In a cumulative knowledge
    base, however, a repeated name must carry its repository so a whole-system
    finding cannot silently point an agent at the wrong checkout.
    """
    rows = conn.execute(
        "SELECT s.id, s.name, r.name AS repository_name FROM services s "
        "LEFT JOIN repositories r ON r.id = s.repository_id"
    ).fetchall()
    occurrences: dict[str, int] = defaultdict(int)
    for row in rows:
        occurrences[row["name"]] += 1
    return {
        row["id"]: (
            row["name"] if occurrences[row["name"]] == 1
            else f"{row['repository_name'] or 'standalone'}/{row['name']}"
        )
        for row in rows
    }


def _tarjan_scc(edges: list[tuple[int, int]]) -> list[list[int]]:
    """Strongly connected components with more than one node — a genuine cycle, not
    just a node reachable from itself trivially."""
    graph: dict[int, list[int]] = defaultdict(list)
    nodes: set[int] = set()
    for a, b in edges:
        graph[a].append(b)
        nodes.add(a)
        nodes.add(b)

    index_counter = [0]
    stack: list[int] = []
    lowlink: dict[int, int] = {}
    index: dict[int, int] = {}
    on_stack: dict[int, bool] = {}
    result: list[list[int]] = []

    def strongconnect(node: int) -> None:
        index[node] = index_counter[0]
        lowlink[node] = index_counter[0]
        index_counter[0] += 1
        stack.append(node)
        on_stack[node] = True

        for neighbor in graph.get(node, []):
            if neighbor not in index:
                strongconnect(neighbor)
                lowlink[node] = min(lowlink[node], lowlink[neighbor])
            elif on_stack.get(neighbor):
                lowlink[node] = min(lowlink[node], index[neighbor])

        if lowlink[node] == index[node]:
            component: list[int] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                component.append(w)
                if w == node:
                    break
            result.append(component)

    for node in nodes:
        if node not in index:
            strongconnect(node)

    return [component for component in result if len(component) > 1]


def find_cycles(conn: sqlite3.Connection) -> list[dict]:
    edges = _internal_edges(conn)
    names = _service_names(conn)
    findings = []
    for component in _tarjan_scc(edges):
        cycle_names = sorted(names[node] for node in component if node in names)
        findings.append({
            "kind": "cycle",
            "severity": "warning",
            "services": cycle_names,
            "reason": (
                f"{' -> '.join(cycle_names)} -> {cycle_names[0]} form a circular dependency: "
                "each one reaches the next, and the chain closes back on itself."
            ),
            "detail": {"service_ids": sorted(component)},
        })
    return findings


def find_fan_imbalance(conn: sqlite3.Connection) -> list[dict]:
    names = _service_names(conn)
    findings = []
    fan_out_rows = conn.execute(
        """SELECT from_service_id, COUNT(DISTINCT to_service_id) AS n FROM service_calls
           WHERE target_kind = 'internal' AND to_service_id IS NOT NULL
           GROUP BY from_service_id HAVING n >= ?""",
        (FAN_THRESHOLD,),
    ).fetchall()
    for row in fan_out_rows:
        name = names.get(row["from_service_id"])
        if name is None:
            continue
        findings.append({
            "kind": "fan_out", "severity": "info", "services": [name],
            "reason": f"{name} calls {row['n']} other internal services directly — a broad orchestrator, or a candidate to split.",
            "detail": {"count": row["n"]},
        })
    fan_in_rows = conn.execute(
        """SELECT to_service_id, COUNT(DISTINCT from_service_id) AS n FROM service_calls
           WHERE target_kind = 'internal' AND to_service_id IS NOT NULL
           GROUP BY to_service_id HAVING n >= ?""",
        (FAN_THRESHOLD,),
    ).fetchall()
    for row in fan_in_rows:
        name = names.get(row["to_service_id"])
        if name is None:
            continue
        findings.append({
            "kind": "fan_in", "severity": "info", "services": [name],
            "reason": f"{row['n']} other internal services call {name} directly — a potential bottleneck or single point of coupling.",
            "detail": {"count": row["n"]},
        })
    return findings


def _owner(symbol: str) -> str:
    """The class/receiver identity a flow_edges endpoint belongs to — everything
    before the first '.', with a leading 'this.' stripped first (same normalization
    resolution.py already applies). A symbol with no '.' (a free function call) is
    its own owner."""
    return symbol.removeprefix("this.").split(".", 1)[0]


def _internal_component_edges(conn: sqlite3.Connection) -> dict[int, list[tuple[str, str]]]:
    """(owner_from, owner_to) pairs per service_id, restricted to invokes/injects
    edges between two "local" owners — an owner that never appears as the source of
    an edge in this service is a boundary (a DB client, a cloud SDK receiver, an
    unresolved receiver), not another component, and is dropped without needing an
    exclusion list. A self-loop (an owner calling itself) is not coupling and is
    dropped too."""
    rows = conn.execute(
        "SELECT service_id, from_symbol, to_symbol FROM flow_edges WHERE kind IN ('invokes', 'injects')"
    ).fetchall()
    pairs_by_service: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for row in rows:
        pairs_by_service[row["service_id"]].append((_owner(row["from_symbol"]), _owner(row["to_symbol"])))
    result: dict[int, list[tuple[str, str]]] = {}
    for service_id, pairs in pairs_by_service.items():
        local_owners = {owner_from for owner_from, _ in pairs}
        result[service_id] = [
            (owner_from, owner_to) for owner_from, owner_to in pairs
            if owner_from != owner_to and owner_from in local_owners and owner_to in local_owners
        ]
    return result


def find_component_cycles(conn: sqlite3.Connection) -> list[dict]:
    """The intra-service analog of find_cycles: a circular dependency between
    components of the SAME service, found only within traced entrypoint-to-boundary
    flows (flow_edges), never a claim about the service's whole code graph."""
    names = _service_names(conn)
    findings = []
    for service_id, pairs in _internal_component_edges(conn).items():
        service_name = names.get(service_id)
        if service_name is None:
            continue
        owners = sorted({owner for pair in pairs for owner in pair})
        owner_ids = {owner: index for index, owner in enumerate(owners)}
        edges = [(owner_ids[owner_from], owner_ids[owner_to]) for owner_from, owner_to in pairs]
        for component in _tarjan_scc(edges):
            cycle_names = sorted(owners[index] for index in component)
            findings.append({
                "kind": "component_cycle", "severity": "warning", "services": [service_name],
                "reason": (
                    f"Within {service_name}, {' -> '.join(cycle_names)} -> {cycle_names[0]} form a circular "
                    "dependency inside traced entrypoint-to-boundary flows — not a full code graph, but a "
                    "real cycle among the paths this index has proof for."
                ),
                "detail": {"components": cycle_names},
            })
    return findings


def _component_fan_finding(kind: str, service_name: str, component: str, count: int, ce: int, ca: int) -> dict:
    reason = (
        f"Within {service_name}, {component} calls {count} other components directly inside traced flows — "
        "a broad orchestrator, or a candidate to split."
        if kind == "component_fan_out" else
        f"Within {service_name}, {count} other components call {component} directly inside traced flows — "
        "a potential bottleneck or single point of coupling."
    )
    detail: dict = {"component": component, "count": count}
    if ca + ce > 0:
        detail["instability"] = round(ce / (ca + ce), 2)
    return {"kind": kind, "severity": "info", "services": [service_name], "reason": reason, "detail": detail}


def find_component_fan_imbalance(conn: sqlite3.Connection) -> list[dict]:
    """The intra-service analog of find_fan_imbalance: a component whose fan-in or
    fan-out, within traced entrypoint-to-boundary flows, crosses COMPONENT_FAN_THRESHOLD.
    Carries Robert Martin's instability index (Ce/(Ca+Ce)) as context in `detail`, not
    as a threshold of its own."""
    names = _service_names(conn)
    findings = []
    for service_id, pairs in _internal_component_edges(conn).items():
        service_name = names.get(service_id)
        if service_name is None:
            continue
        efferent: dict[str, set[str]] = defaultdict(set)
        afferent: dict[str, set[str]] = defaultdict(set)
        for owner_from, owner_to in pairs:
            efferent[owner_from].add(owner_to)
            afferent[owner_to].add(owner_from)
        for component in sorted(set(efferent) | set(afferent)):
            ce, ca = len(efferent.get(component, ())), len(afferent.get(component, ()))
            if ce >= COMPONENT_FAN_THRESHOLD:
                findings.append(_component_fan_finding("component_fan_out", service_name, component, ce, ce, ca))
            if ca >= COMPONENT_FAN_THRESHOLD:
                findings.append(_component_fan_finding("component_fan_in", service_name, component, ca, ce, ca))
    return findings


def find_shared_database(conn: sqlite3.Connection) -> list[dict]:
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT name, engine, GROUP_CONCAT(DISTINCT service_id) AS service_ids
           FROM persistence_entities WHERE engine != 'unknown'
           GROUP BY LOWER(name), engine HAVING COUNT(DISTINCT service_id) > 1"""
    ).fetchall()
    findings = []
    for row in rows:
        service_ids = [int(x) for x in row["service_ids"].split(",")]
        service_names = sorted(names[i] for i in service_ids if i in names)
        findings.append({
            "kind": "shared_database", "severity": "warning", "services": service_names,
            "reason": (
                f"{', '.join(service_names)} all persist an entity named '{row['name']}' on {row['engine']} — "
                "likely sharing a database, which couples their schemas."
            ),
            "detail": {"entity": row["name"], "engine": row["engine"]},
        })
    return findings


def find_aggregate_ownership_overlap(conn: sqlite3.Connection) -> list[dict]:
    """Surface competing static ownership declarations across services.

    The detector intentionally relies on deterministic entity/model declarations,
    not generated descriptions or table-name guesses. Matching declarations prove
    an overlap worth reviewing; they do not prove a shared physical database or
    rule out a deliberate read model.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT f.name, f.kind, f.owner, f.file_path, f.start_line, f.end_line,
               f.service_id
        FROM static_persistence_facts f
        ORDER BY LOWER(f.name), f.kind, f.service_id, f.owner, f.file_path, f.start_line
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(row["name"].casefold(), row["kind"])].append(row)

    findings: list[dict] = []
    for (_normalized_name, persistence_kind), facts in grouped.items():
        service_ids = {fact["service_id"] for fact in facts}
        if len(service_ids) < 2:
            continue
        aggregate = facts[0]["name"]
        ordered_facts = sorted(
            facts,
            key=lambda fact: (names[fact["service_id"]], fact["owner"], fact["file_path"], fact["start_line"]),
        )
        owners = sorted({(names[fact["service_id"]], fact["owner"]) for fact in facts})
        service_names = sorted(names[service_id] for service_id in service_ids)
        findings.append(
            {
                "kind": "possible_aggregate_ownership_overlap", "severity": "warning",
                "services": service_names,
                "reason": (
                    f"{', '.join(service_names)} each declare ownership of {persistence_kind} "
                    f"'{aggregate}'; validate one source of truth or an explicit read-model boundary."
                ),
                "detail": {
                    "aggregate": aggregate, "persistence_kind": persistence_kind,
                    "owners": [{"service": service, "owner": owner} for service, owner in owners],
                    "confidence": 0.65,
                    "evidence": [_edge_evidence(fact) for fact in ordered_facts],
                    "unknowns": [
                        "Static declarations cannot establish whether these services share a physical database or intentionally maintain read models.",
                    ],
                    "remediation": [
                        "Assign one write owner for the aggregate, or document the replication/read-model contract between services.",
                    ],
                },
            }
        )
    return findings


def find_duplicate_external_integrations(conn: sqlite3.Connection) -> list[dict]:
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT to_service_name, GROUP_CONCAT(DISTINCT from_service_id) AS service_ids
           FROM service_calls WHERE target_kind = 'external'
           GROUP BY LOWER(to_service_name) HAVING COUNT(DISTINCT from_service_id) > 1"""
    ).fetchall()
    findings = []
    for row in rows:
        service_ids = [int(x) for x in row["service_ids"].split(",")]
        service_names = sorted(names[i] for i in service_ids if i in names)
        findings.append({
            "kind": "duplicate_external_integration", "severity": "info", "services": service_names,
            "reason": (
                f"{', '.join(service_names)} each integrate with '{row['to_service_name']}' independently — "
                "worth checking whether that's intentional or should be consolidated behind one service."
            ),
            "detail": {"vendor": row["to_service_name"]},
        })
    return findings


def _edge_evidence(row: sqlite3.Row) -> dict:
    return {"file": row["file_path"], "start_line": row["start_line"], "end_line": row["end_line"]}


def _direct_entrypoint_operations(conn: sqlite3.Connection) -> list[dict]:
    """Return source-proven persistence/message operations owned by each entrypoint."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT e.id AS entrypoint_id, e.kind AS entrypoint_kind, e.method, e.name,
               e.symbol, s.id AS service_id,
               fe.kind AS edge_kind, fe.to_symbol, fe.file_path, fe.start_line, fe.end_line
        FROM entrypoints e
        JOIN services s ON s.id = e.service_id
        JOIN flow_edges fe ON fe.entrypoint_id = e.id
        WHERE fe.origin = 'static' AND fe.kind IN ('writes', 'publishes')
        ORDER BY s.name, e.id, fe.id
        """
    ).fetchall()
    by_entrypoint: dict[int, dict] = {}
    for row in rows:
        entrypoint = by_entrypoint.setdefault(
            row["entrypoint_id"],
            {
                "service_id": row["service_id"], "service_name": names[row["service_id"]],
                "kind": row["entrypoint_kind"], "method": row["method"], "name": row["name"],
                "symbol": row["symbol"], "operations": [],
            },
        )
        entrypoint["operations"].append({
            "kind": row["edge_kind"], "target": row["to_symbol"], "evidence": _edge_evidence(row),
        })
    return list(by_entrypoint.values())


def _entrypoint_detail(entrypoint: dict) -> dict:
    return {
        "kind": entrypoint["kind"], "method": entrypoint["method"],
        "name": entrypoint["name"], "symbol": entrypoint["symbol"],
    }


def find_flow_hypotheses(conn: sqlite3.Connection) -> list[dict]:
    """Surface bounded flow risks as hypotheses, never as architecture verdicts.

    The evidence comes only from direct static edges owned by an entrypoint. Whether
    a GraphQL service is actually a BFF, or whether a transaction really encloses a
    publication at runtime, remains deliberately explicit in `unknowns`.
    """
    findings: list[dict] = []
    for entrypoint in _direct_entrypoint_operations(conn):
        writes = [operation for operation in entrypoint["operations"] if operation["kind"] == "writes"]
        publishes = [operation for operation in entrypoint["operations"] if operation["kind"] == "publishes"]
        entrypoint_detail = _entrypoint_detail(entrypoint)
        if entrypoint["kind"] == "graphql" and entrypoint["method"] == "MUTATION" and (writes or publishes):
            evidence = [item["evidence"] for item in [*writes, *publishes]]
            findings.append(
                {
                    "kind": "possible_bff_domain_leakage", "severity": "warning",
                    "services": [entrypoint["service_name"]],
                    "reason": (
                        "A GraphQL mutation directly writes state or publishes an event; validate whether this service "
                        "is a BFF and whether reusable domain policy belongs behind a domain service."
                    ),
                    "detail": {
                        "entrypoint": entrypoint_detail, "confidence": 0.6, "evidence": evidence,
                        "unknowns": ["The static flow cannot establish whether this GraphQL service is a BFF."],
                        "remediation": [
                            "Keep reusable domain policy behind a domain service when this service is a BFF.",
                        ],
                    },
                }
            )
        if not (writes and publishes):
            continue
        has_transaction = conn.execute(
            """SELECT 1 FROM flow_boundaries
               WHERE service_id = ? AND source = ? AND kind = 'transaction' LIMIT 1""",
            (entrypoint["service_id"], entrypoint["symbol"]),
        ).fetchone()
        if has_transaction is not None:
            continue
        findings.append(
            {
                "kind": "possible_non_atomic_publish", "severity": "warning",
                "services": [entrypoint["service_name"]],
                "reason": (
                    "One entrypoint writes state and publishes an event without a source-proven transaction boundary; "
                    "validate transactional outbox or equivalent delivery guarantees."
                ),
                "detail": {
                    "entrypoint": entrypoint_detail, "confidence": 0.5,
                    "evidence": [writes[0]["evidence"], publishes[0]["evidence"]],
                    "unknowns": ["The static flow cannot prove the runtime transaction scope or broker delivery semantics."],
                    "remediation": [
                        "Validate a transactional outbox or equivalent delivery guarantee for this write and publication.",
                    ],
                },
            }
        )
    return findings


def find_read_entrypoint_side_effects(conn: sqlite3.Connection) -> list[dict]:
    """Flag source-proven side effects behind read-only transport contracts.

    This is intentionally narrower than a generic controller-to-repository rule:
    it only observes direct static writes or publications from HTTP safe methods and
    GraphQL queries. A source fact proves the side effect; whether it is an accepted
    cache, metric or legacy exception remains explicit for human validation.
    """
    findings: list[dict] = []
    for entrypoint in _direct_entrypoint_operations(conn):
        is_safe_http = entrypoint["kind"] == "http" and entrypoint["method"] in READ_ENTRYPOINT_METHODS
        is_graphql_query = entrypoint["kind"] == "graphql" and entrypoint["method"] == "QUERY"
        if not (is_safe_http or is_graphql_query):
            continue
        operations = entrypoint["operations"]
        findings.append(
            {
                "kind": "possible_read_entrypoint_side_effect", "severity": "warning",
                "services": [entrypoint["service_name"]],
                "reason": (
                    "A read-only transport entrypoint directly writes state or publishes an event; "
                    "validate whether this observable side effect is intentional."
                ),
                "detail": {
                    "entrypoint": _entrypoint_detail(entrypoint), "confidence": 0.8,
                    "operations": [{"kind": item["kind"], "target": item["target"]} for item in operations],
                    "evidence": [item["evidence"] for item in operations],
                    "unknowns": [
                        "The static flow cannot determine whether the side effect is an approved cache, metric or legacy exception.",
                    ],
                    "remediation": [
                        "Move externally observable writes or publications behind a command entrypoint, or document the exception.",
                    ],
                },
            }
        )
    return findings


def find_overbroad_exception_handlers(conn: sqlite3.Connection) -> list[dict]:
    """Flag source-proven generic exception mappings without judging runtime behavior.

    A generic mapping can be a legitimate final fallback. It is still worth making
    visible because it may collapse domain/client errors into a single transport
    response. The detector intentionally requires an indexed ``maps`` contract;
    a bare ``catch (Exception)`` is not enough evidence on its own.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, source, internal_type, protocol, transport_code,
                  file_path, start_line, end_line
           FROM static_error_contracts
           WHERE role = 'maps' AND internal_type IS NOT NULL
           ORDER BY service_id, source, file_path, start_line""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        exception_type = row["internal_type"].rsplit(".", 1)[-1].casefold()
        if exception_type not in BROAD_EXCEPTION_TYPES:
            continue
        findings.append({
            "kind": "possible_overbroad_exception_handler", "severity": "info",
            "services": [names[row["service_id"]]],
            "reason": (
                f"{row['source']} maps broad exception type '{row['internal_type']}' to "
                f"{row['protocol']} {row['transport_code'] or 'unknown'}; validate that "
                "domain and client errors retain their intended semantics."
            ),
            "detail": {
                "handler": row["source"], "internal_type": row["internal_type"],
                "transport": {"protocol": row["protocol"], "code": row["transport_code"]},
                "confidence": 0.75,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "Static analysis cannot establish whether this handler delegates to more specific mappings or is an intentional final fallback.",
                ],
                "remediation": [
                    "Keep a safe generic fallback, but add explicit mappings for expected validation, authorization, not-found and conflict errors.",
                ],
            },
        })
    return findings


def find_broad_handlers_that_can_swallow_timeouts(conn: sqlite3.Connection) -> list[dict]:
    """Correlate broad HTTP 500 mappings with local timeout-protected HTTP calls.

    A generic handler may be an intentional final fallback, and a timeout can be
    translated before reaching it. The detector therefore presents their service-level
    coexistence as a review risk, never a proven runtime catch path.
    """
    names = _service_names(conn)
    handlers = conn.execute(
        """SELECT service_id, source, internal_type, transport_code,
                  file_path, start_line, end_line
           FROM static_error_contracts
           WHERE role = 'maps'
             AND protocol = 'http'
             AND transport_code = '500'
             AND internal_type IS NOT NULL
           ORDER BY service_id, source, internal_type, file_path, start_line""",
    ).fetchall()
    timeout_policies = _resilience_policies_by_source(conn, "timeout")
    calls = conn.execute(
        """SELECT service_id, source, target_service, target_method, target_path,
                  file_path, start_line, end_line
           FROM static_service_calls
           WHERE protocol = 'http'
           ORDER BY service_id, source, target_service, target_method, target_path,
                    file_path, start_line""",
    ).fetchall()
    timeout_flows = [
        (call, timeout_policies[(call["service_id"], call["source"])])
        for call in calls
        if (call["service_id"], call["source"]) in timeout_policies
    ]
    findings: list[dict] = []
    for handler in handlers:
        if handler["internal_type"].rsplit(".", 1)[-1].casefold() not in BROAD_EXCEPTION_TYPES:
            continue
        for call, policies in timeout_flows:
            if call["service_id"] != handler["service_id"]:
                continue
            service = names[handler["service_id"]]
            findings.append({
                "kind": "possible_broad_handler_swallows_timeout", "severity": "warning",
                "services": [service],
                "reason": (
                    f"{handler['source']} maps broad {handler['internal_type']} to HTTP 500 while "
                    f"{call['source']} has a timeout-protected internal HTTP call; review timeout semantics."
                ),
                "detail": {
                    "handler": {
                        "symbol": handler["source"], "error_type": handler["internal_type"],
                        "status": handler["transport_code"],
                    },
                    "timeout_flow": {
                        "symbol": call["source"], "target_service": call["target_service"],
                        "method": call["target_method"], "path": call["target_path"],
                    },
                    "timeout_policies": [
                        {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                        for policy in policies
                    ],
                    "confidence": 0.65,
                    "evidence": [
                        _edge_evidence(handler), _edge_evidence(call),
                        *[_edge_evidence(policy) for policy in policies],
                    ],
                    "unknowns": [
                        "The generic handler may be an intentional final fallback or may not apply to this execution path.",
                        "The timeout may be handled or translated before it reaches the broad mapping.",
                    ],
                    "remediation": [
                        "Keep a safe generic fallback, but add and document a specific timeout mapping with unavailable or gateway-timeout semantics.",
                        "Verify the timeout client boundary preserves its intended error contract before the broad handler is reached.",
                    ],
                },
            })
    return findings


def find_resilience_policies_on_write_flows(conn: sqlite3.Connection) -> list[dict]:
    """Flag local write flows that also have an internal call under resilience policy.

    Coexisting write, call and policy facts do not establish their order or transaction
    boundary. The finding makes that uncertainty visible for flows where a timeout or
    retry could otherwise leave a partial effect to be reasoned about manually.
    """
    names = _service_names(conn)
    policies_by_source = _resilience_policies_by_source(conn, None)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    calls = conn.execute(
        """SELECT service_id, source, target_service, target_method, target_path,
                  file_path, start_line, end_line
           FROM static_service_calls
           WHERE protocol = 'http'
           ORDER BY service_id, source, target_service, target_method, target_path,
                    file_path, start_line""",
    ).fetchall()
    findings: list[dict] = []
    for call in calls:
        source_key = (call["service_id"], call["source"])
        policies = policies_by_source.get(source_key)
        writes = writes_by_source.get(source_key)
        if not policies or not writes:
            continue
        service = names[call["service_id"]]
        visible_writes = writes[:3]
        findings.append({
            "kind": "possible_resilience_policy_on_partial_write_flow", "severity": "warning",
            "services": [service],
            "reason": (
                f"{call['source']} has local writes and calls {call['target_service']} under "
                "a literal timeout or retry policy; review partial-effect semantics."
            ),
            "detail": {
                "flow": {"symbol": call["source"]},
                "target": {
                    "service": call["target_service"], "method": call["target_method"],
                    "path": call["target_path"],
                },
                "resilience_policies": [
                    {
                        "kind": policy["kind"], "mechanism": policy["mechanism"],
                        "value": policy["value"], "unit": policy["unit"],
                    }
                    for policy in policies
                ],
                "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                "write_count": len(writes),
                "confidence": 0.7,
                "evidence": [
                    _edge_evidence(call),
                    *[_edge_evidence(policy) for policy in policies],
                    *[_edge_evidence(write) for write in visible_writes],
                ],
                "unknowns": [
                    "Static analysis does not establish whether the write occurs before or after the remote call.",
                    "A transaction, outbox, idempotency key or compensating action may protect this flow outside the indexed facts.",
                ],
                "remediation": [
                    "Review ordering, idempotency and recovery for the write and remote call as one failure boundary.",
                    "Use a transaction, outbox, compensation or an explicit retry-safe contract where the operation can be partially applied.",
                ],
            },
        })
    return findings


def _flow_edges_by_source(conn: sqlite3.Connection, kind: str) -> dict[tuple[int, str], list[sqlite3.Row]]:
    """Group one deterministic flow-edge kind by its local source symbol."""
    edges_by_source: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    for edge in conn.execute(
        """SELECT service_id, from_symbol, to_symbol, file_path, start_line, end_line
           FROM flow_edges
           WHERE kind = ?
           ORDER BY service_id, from_symbol, to_symbol, file_path, start_line""",
        (kind,),
    ).fetchall():
        edges_by_source[(edge["service_id"], edge["from_symbol"])].append(edge)
    return edges_by_source


def find_retries_on_write_publish_flows(conn: sqlite3.Connection) -> list[dict]:
    """Flag a retrying source that both writes local state and publishes an event.

    The three static facts do not establish sequencing or atomicity. This is a bounded
    prompt to verify outbox, transaction, de-duplication and retry behavior together.
    """
    names = _service_names(conn)
    retries_by_source = _retry_policies_by_source(conn)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    publishes_by_source = _flow_edges_by_source(conn, "publishes")
    findings: list[dict] = []
    for source_key, policies in retries_by_source.items():
        writes = writes_by_source.get(source_key)
        publishes = publishes_by_source.get(source_key)
        if not writes or not publishes:
            continue
        service_id, symbol = source_key
        visible_writes = writes[:3]
        visible_publishes = publishes[:3]
        findings.append({
            "kind": "possible_retry_on_write_publish_flow", "severity": "warning",
            "services": [names[service_id]],
            "reason": (
                f"{symbol} writes local state and publishes an event under a literal retry policy; "
                "review duplicate-event and partial-effect handling."
            ),
            "detail": {
                "flow": {"symbol": symbol},
                "retry_policies": [
                    {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                    for policy in policies
                ],
                "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                "write_count": len(writes),
                "publishes": [{"target": publish["to_symbol"]} for publish in visible_publishes],
                "publish_count": len(publishes),
                "confidence": 0.7,
                "evidence": [
                    *[_edge_evidence(policy) for policy in policies],
                    *[_edge_evidence(write) for write in visible_writes],
                    *[_edge_evidence(publish) for publish in visible_publishes],
                ],
                "unknowns": [
                    "Static analysis does not establish whether the write and publication share one transaction or their execution order.",
                    "An outbox, idempotent producer or consumer de-duplication may protect against duplicate delivery outside the indexed facts.",
                ],
                "remediation": [
                    "Review the write and publication as one retry boundary and confirm duplicate-event behavior.",
                    "Use an outbox, idempotency key or documented de-duplication when retries can repeat event publication.",
                ],
            },
        })
    return findings


def find_retry_write_publish_flows_with_consumers(conn: sqlite3.Connection) -> list[dict]:
    """Prioritize retrying write/publish flows whose literal channel has consumers.

    Channel matching is exact and intentionally does not infer broker topology,
    bindings or delivery. Consumers in the publishing service are excluded because
    this signal is specifically about potential cross-service duplicate impact.
    """
    names = _service_names(conn)
    retries_by_source = _retry_policies_by_source(conn)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    publishes_by_source = _flow_edges_by_source(conn, "publishes")
    consumers_by_channel: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for consumer in conn.execute(
        """SELECT service_id, channel, file_path, start_line, end_line
           FROM static_message_contracts
           WHERE direction = 'consumes'
           ORDER BY channel, service_id, file_path, start_line""",
    ).fetchall():
        consumers_by_channel[consumer["channel"]].append(consumer)

    findings: list[dict] = []
    for source_key, policies in retries_by_source.items():
        writes = writes_by_source.get(source_key)
        publishes = publishes_by_source.get(source_key)
        if not writes or not publishes:
            continue
        service_id, symbol = source_key
        visible_writes = writes[:3]
        for publish in publishes:
            consumers = [
                consumer for consumer in consumers_by_channel.get(publish["to_symbol"], [])
                if consumer["service_id"] != service_id
            ]
            if not consumers:
                continue
            visible_consumers = consumers[:3]
            findings.append({
                "kind": "possible_retry_write_publish_reaches_consumer", "severity": "warning",
                "services": [names[service_id], *[names[item["service_id"]] for item in visible_consumers]],
                "reason": (
                    f"{symbol} writes state and retries publication on channel {publish['to_symbol']}, "
                    "which has source-proven consumers in other services."
                ),
                "detail": {
                    "flow": {"symbol": symbol},
                    "channel": publish["to_symbol"],
                    "retry_policies": [
                        {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                        for policy in policies
                    ],
                    "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                    "write_count": len(writes),
                    "consumers": [
                        {"service": names[consumer["service_id"]], "channel": consumer["channel"]}
                        for consumer in visible_consumers
                    ],
                    "consumer_count": len(consumers),
                    "confidence": 0.75,
                    "evidence": [
                        *[_edge_evidence(policy) for policy in policies],
                        *[_edge_evidence(write) for write in visible_writes],
                        _edge_evidence(publish),
                        *[_edge_evidence(consumer) for consumer in visible_consumers],
                    ],
                    "unknowns": [
                        "Exact channel equality does not prove broker routing, delivery, ordering or that the consumer processes this producer's message at runtime.",
                        "Outbox, idempotent producer or consumer de-duplication may already protect against duplicate delivery outside the indexed facts.",
                    ],
                    "remediation": [
                        "Review producer retries and the listed consumers as one duplicate-delivery boundary.",
                        "Confirm an outbox or idempotency strategy and consumer de-duplication for this event channel.",
                    ],
                },
            })
    return findings


def _persistent_message_consumers_by_channel(
    conn: sqlite3.Connection,
) -> dict[str, list[tuple[sqlite3.Row, list[sqlite3.Row]]]]:
    """Return literal-channel consumers whose entrypoint has local writes."""
    writes_by_source = _flow_edges_by_source(conn, "writes")
    persistent_consumers_by_channel: dict[str, list[tuple[sqlite3.Row, list[sqlite3.Row]]]] = defaultdict(list)
    consumer_rows = conn.execute(
        """SELECT contract.service_id, contract.channel, contract.file_path,
                  contract.start_line, contract.end_line, entrypoint.symbol
           FROM static_message_contracts contract
           JOIN entrypoints entrypoint
             ON entrypoint.service_id = contract.service_id
            AND entrypoint.kind = 'message'
            AND entrypoint.method = 'CONSUME'
            AND entrypoint.name = contract.channel
           WHERE contract.direction = 'consumes'
           ORDER BY contract.channel, contract.service_id, entrypoint.symbol,
                    contract.file_path, contract.start_line""",
    ).fetchall()
    for consumer in consumer_rows:
        writes = writes_by_source.get((consumer["service_id"], consumer["symbol"]))
        if writes:
            persistent_consumers_by_channel[consumer["channel"]].append((consumer, writes))
    return persistent_consumers_by_channel


def find_retry_write_publish_flows_with_persistent_consumers(conn: sqlite3.Connection) -> list[dict]:
    """Prioritize duplicate-delivery review when a known consumer also writes state.

    A consumer must have a matching literal channel, a message entrypoint for that
    channel and source-proven writes from that entrypoint. This avoids inferring
    persistence impact from a consumer declaration alone.
    """
    names = _service_names(conn)
    retries_by_source = _retry_policies_by_source(conn)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    publishes_by_source = _flow_edges_by_source(conn, "publishes")
    persistent_consumers_by_channel = _persistent_message_consumers_by_channel(conn)

    findings: list[dict] = []
    for source_key, policies in retries_by_source.items():
        producer_writes = writes_by_source.get(source_key)
        publishes = publishes_by_source.get(source_key)
        if not producer_writes or not publishes:
            continue
        service_id, symbol = source_key
        visible_producer_writes = producer_writes[:3]
        for publish in publishes:
            consumers = [
                item for item in persistent_consumers_by_channel.get(publish["to_symbol"], [])
                if item[0]["service_id"] != service_id
            ]
            if not consumers:
                continue
            visible_consumers = consumers[:3]
            consumer_details = []
            consumer_evidence = []
            for consumer, writes in visible_consumers:
                visible_writes = writes[:3]
                consumer_details.append({
                    "service": names[consumer["service_id"]], "symbol": consumer["symbol"],
                    "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                    "write_count": len(writes),
                })
                consumer_evidence.extend([_edge_evidence(consumer), *[_edge_evidence(write) for write in visible_writes]])
            findings.append({
                "kind": "possible_retry_write_publish_reaches_persistent_consumer", "severity": "warning",
                "services": [names[service_id], *[item["service"] for item in consumer_details]],
                "reason": (
                    f"{symbol} writes state and retries publication on channel {publish['to_symbol']}, "
                    "which reaches a source-proven state-writing consumer in another service."
                ),
                "detail": {
                    "flow": {"symbol": symbol}, "channel": publish["to_symbol"],
                    "retry_policies": [
                        {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                        for policy in policies
                    ],
                    "writes": [{"target": write["to_symbol"]} for write in visible_producer_writes],
                    "write_count": len(producer_writes),
                    "consumers": consumer_details,
                    "consumer_count": len(consumers),
                    "confidence": 0.8,
                    "evidence": [
                        *[_edge_evidence(policy) for policy in policies],
                        *[_edge_evidence(write) for write in visible_producer_writes],
                        _edge_evidence(publish), *consumer_evidence,
                    ],
                    "unknowns": [
                        "Literal channel and consumer writes do not prove broker routing, message delivery, ordering or runtime processing of this producer's event.",
                        "Outbox, producer idempotency or consumer de-duplication may already prevent duplicate persistent effects outside the indexed facts.",
                    ],
                    "remediation": [
                        "Review producer retries and the listed persistent consumers as one duplicate-state boundary.",
                        "Confirm an outbox/idempotency strategy and consumer de-duplication before relying on retries for this channel.",
                    ],
                },
            })
    return findings


def find_retry_write_publish_flows_with_unrecovered_persistent_consumers(
    conn: sqlite3.Connection,
) -> list[dict]:
    """Prioritize retry flows that reach a persistent RabbitMQ consumer without recovery proof.

    This joins two deliberately narrow facts: a producer that retries after a
    write/publication flow, and a cross-service persistent RabbitMQ consumer with
    no indexed retry boundary, retry delay or dead-letter route. It does not make a
    claim about recovery configured outside the indexed source.
    """
    names = _service_names(conn)
    retries_by_source = _retry_policies_by_source(conn)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    publishes_by_source = _flow_edges_by_source(conn, "publishes")
    persistent_consumers_by_channel = _persistent_message_consumers_by_channel(conn)
    unrecovered_consumers = {
        (consumer["service_id"], consumer["symbol"]): consumer
        for consumer in _rabbitmq_consumers_without_recovery(conn)
    }

    findings: list[dict] = []
    for source_key, policies in retries_by_source.items():
        producer_writes = writes_by_source.get(source_key)
        publishes = publishes_by_source.get(source_key)
        if not producer_writes or not publishes:
            continue
        service_id, symbol = source_key
        visible_producer_writes = producer_writes[:3]
        for publish in publishes:
            consumers = [
                (consumer, writes, unrecovered_consumers[(consumer["service_id"], consumer["symbol"])])
                for consumer, writes in persistent_consumers_by_channel.get(publish["to_symbol"], [])
                if consumer["service_id"] != service_id
                and (consumer["service_id"], consumer["symbol"]) in unrecovered_consumers
            ]
            if not consumers:
                continue
            visible_consumers = consumers[:3]
            consumer_details = []
            consumer_evidence = []
            for consumer, writes, recovery in visible_consumers:
                visible_writes = writes[:3]
                consumer_detail = {
                    "service": names[consumer["service_id"]], "symbol": consumer["symbol"],
                    "queue": recovery["queue"],
                    "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                    "write_count": len(writes),
                }
                if json.loads(recovery["contract_json"]).get("idempotency") == "detected":
                    consumer_detail["idempotency"] = "declared"
                consumer_details.append(consumer_detail)
                consumer_evidence.extend([
                    _edge_evidence(consumer),
                    _edge_evidence(recovery),
                    *[_edge_evidence(write) for write in visible_writes],
                ])
            findings.append({
                "kind": "possible_retry_write_publish_reaches_unrecovered_persistent_consumer",
                "severity": "warning",
                "services": [names[service_id], *[item["service"] for item in consumer_details]],
                "reason": (
                    f"{symbol} writes state and retries publication on channel {publish['to_symbol']}, "
                    "which reaches a persistent RabbitMQ consumer without source-proven recovery."
                ),
                "detail": {
                    "flow": {"symbol": symbol}, "channel": publish["to_symbol"],
                    "retry_policies": [
                        {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                        for policy in policies
                    ],
                    "writes": [{"target": write["to_symbol"]} for write in visible_producer_writes],
                    "write_count": len(producer_writes),
                    "consumers": consumer_details,
                    "consumer_count": len(consumers),
                    "confidence": 0.65,
                    "evidence": [
                        *[_edge_evidence(policy) for policy in policies],
                        *[_edge_evidence(write) for write in visible_producer_writes],
                        _edge_evidence(publish), *consumer_evidence,
                    ],
                    "unknowns": [
                        "Literal channel equality does not prove broker routing, delivery, ordering or runtime processing of this producer's event.",
                        "Broker recovery, an outbox, producer idempotency or consumer de-duplication may exist outside the indexed facts.",
                    ],
                    "remediation": [
                        "Review producer retries and the listed consumers as one duplicate-state and recovery boundary.",
                        "Confirm a RabbitMQ retry/dead-letter policy plus idempotent consumer handling for this event channel.",
                    ],
                },
            })
    return findings


def find_non_atomic_service_publish_flows(conn: sqlite3.Connection) -> list[dict]:
    """Extend the write/publication review to non-entrypoint service symbols.

    Entrypoint-owned operations are already covered by ``find_flow_hypotheses``.
    This detector covers delegated application/service methods while avoiding a
    duplicate finding for the entrypoint itself.
    """
    names = _service_names(conn)
    writes_by_source = _flow_edges_by_source(conn, "writes")
    publishes_by_source = _flow_edges_by_source(conn, "publishes")
    entrypoint_sources = {
        (row["service_id"], row["symbol"])
        for row in conn.execute("SELECT service_id, symbol FROM entrypoints").fetchall()
    }
    transactional_sources = {
        (row["service_id"], row["source"])
        for row in conn.execute(
            "SELECT service_id, source FROM flow_boundaries WHERE kind = 'transaction'"
        ).fetchall()
    }
    findings: list[dict] = []
    for source_key, writes in writes_by_source.items():
        publishes = publishes_by_source.get(source_key)
        if not publishes or source_key in entrypoint_sources or source_key in transactional_sources:
            continue
        service_id, symbol = source_key
        visible_writes = writes[:3]
        visible_publishes = publishes[:3]
        findings.append({
            "kind": "possible_non_atomic_service_publish", "severity": "warning",
            "services": [names[service_id]],
            "reason": (
                f"Service symbol {symbol} writes local state and publishes an event without "
                "a source-proven transaction boundary; review outbox or equivalent recovery."
            ),
            "detail": {
                "flow": {"symbol": symbol},
                "writes": [{"target": write["to_symbol"]} for write in visible_writes],
                "write_count": len(writes),
                "publishes": [{"target": publish["to_symbol"]} for publish in visible_publishes],
                "publish_count": len(publishes),
                "confidence": 0.5,
                "evidence": [
                    *[_edge_evidence(write) for write in visible_writes],
                    *[_edge_evidence(publish) for publish in visible_publishes],
                ],
                "unknowns": [
                    "Static analysis cannot establish the runtime transaction scope, operation order or broker delivery semantics.",
                    "An outbox, compensation or other delivery guarantee may exist outside the indexed source facts.",
                ],
                "remediation": [
                    "Validate a transactional outbox or equivalent recovery guarantee for this write and publication.",
                    "Document compensation and duplicate-delivery behavior when the operations cannot share one transaction.",
                ],
            },
        })
    return findings


def find_error_semantics_lost(conn: sqlite3.Connection) -> list[dict]:
    """Find a source-proven client/domain error degraded to an HTTP 5xx mapping.

    Both sides of the finding must refer to the same explicit local exception type.
    This is intentionally intra-service: connecting an error across services also
    requires a resolved client-to-endpoint relation and is a later capability.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT raised.service_id, raised.source AS raised_source,
                  raised.error_kind AS raised_kind, raised.internal_type,
                  raised.file_path AS raised_file_path, raised.start_line AS raised_start_line,
                  raised.end_line AS raised_end_line, mapped.source AS mapped_source,
                  mapped.protocol AS mapped_protocol, mapped.transport_code AS mapped_code,
                  mapped.file_path AS mapped_file_path, mapped.start_line AS mapped_start_line,
                  mapped.end_line AS mapped_end_line
           FROM static_error_contracts raised
           JOIN static_error_contracts mapped
             ON mapped.service_id = raised.service_id
            AND mapped.internal_type = raised.internal_type
           WHERE raised.role = 'raises'
             AND mapped.role = 'maps'
             AND raised.internal_type IS NOT NULL
             AND raised.error_kind IN ('authorization', 'conflict', 'not_found', 'rate_limit', 'validation')
             AND mapped.protocol = 'http'
             AND CAST(mapped.transport_code AS INTEGER) BETWEEN 500 AND 599
           ORDER BY raised.service_id, raised.internal_type, raised.source, mapped.source""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        findings.append({
            "kind": "possible_error_semantics_lost", "severity": "warning",
            "services": [names[row["service_id"]]],
            "reason": (
                f"{row['raised_source']} raises {row['raised_kind']} error '{row['internal_type']}', "
                f"but {row['mapped_source']} maps the same type to HTTP {row['mapped_code']}."
            ),
            "detail": {
                "error_type": row["internal_type"], "origin": {
                    "symbol": row["raised_source"], "kind": row["raised_kind"],
                },
                "mapping": {
                    "symbol": row["mapped_source"], "protocol": row["mapped_protocol"],
                    "code": row["mapped_code"],
                },
                "confidence": 0.85,
                "evidence": [
                    {"file": row["raised_file_path"], "start_line": row["raised_start_line"], "end_line": row["raised_end_line"]},
                    {"file": row["mapped_file_path"], "start_line": row["mapped_start_line"], "end_line": row["mapped_end_line"]},
                ],
                "unknowns": [
                    "Static analysis cannot establish whether a gateway or an external contract intentionally requires this 5xx translation.",
                ],
                "remediation": [
                    "Preserve the documented client/domain error status, or document why this error must become a server failure.",
                ],
            },
        })
    return findings


def find_unhandled_endpoint_errors(conn: sqlite3.Connection) -> list[dict]:
    """Flag known client errors reachable from an HTTP entrypoint without a local mapping.

    The static graph proves that an error-producing symbol is reachable from the
    entrypoint. It cannot prove framework-global handlers, gateways or proxies, so
    the finding remains a review signal rather than a claim about a runtime 5xx.
    """
    names = _service_names(conn)
    entrypoints = conn.execute(
        """SELECT service_id, method, name, symbol, file_path, start_line, end_line
           FROM entrypoints WHERE kind = 'http'
           ORDER BY service_id, method, name, symbol""",
    ).fetchall()
    navigators: dict[int, KnowledgeNavigator | None] = {}
    mapped_by_service: dict[int, set[str]] = {}
    findings: list[dict] = []
    for entrypoint in entrypoints:
        service_id = entrypoint["service_id"]
        if service_id not in navigators:
            if not canonical_snapshots_repo.has_snapshot(conn, service_id):
                navigators[service_id] = None
                continue
            snapshot = canonical_snapshots_repo.read_snapshot(conn, service_id)
            if snapshot is None:
                navigators[service_id] = None
                continue
            navigators[service_id] = KnowledgeNavigator(snapshot)
            mapped_by_service[service_id] = {
                fact.attributes["internal_type"] for fact in snapshot.facts
                if fact.kind == "error_contract" and fact.attributes.get("role") == "maps"
                and fact.attributes.get("protocol") == "http"
                and isinstance(fact.attributes.get("internal_type"), str)
            }
        navigator = navigators[service_id]
        if navigator is None:
            continue
        key = EntrypointKey(navigator.snapshot.service, "http", entrypoint["method"],
                            entrypoint["name"], entrypoint["symbol"])
        traversal = navigator.reachable(key, TraversalPolicy(max_depth=200, max_nodes=201, max_edges=200))
        raised_by_type: dict[str, CanonicalFact] = {}
        for contract in traversal.facts:
            if contract.kind != "error_contract" or not isinstance(contract.subject, SymbolKey):
                continue
            error_type = contract.attributes.get("internal_type")
            if (
                contract.attributes.get("role") == "raises"
                and isinstance(error_type, str)
                and contract.attributes.get("error_kind") in CLIENT_ERROR_KINDS
                and error_type not in mapped_by_service[service_id]
                and contract.sources
            ):
                raised_by_type.setdefault(error_type, contract)
        for error_type, contract in raised_by_type.items():
            source = contract.sources[0]
            findings.append({
                "kind": "possible_unhandled_endpoint_error", "severity": "warning",
                "services": [names[service_id]],
                "reason": (
                    f"{names[service_id]} {entrypoint['method']} {entrypoint['name']} can reach "
                    f"{contract.subject.name}, which raises {contract.attributes['error_kind']} error '{error_type}'; "
                    "no local HTTP mapping for that type is indexed."
                ),
                "detail": {
                    "entrypoint": {
                        "method": entrypoint["method"], "path": entrypoint["name"],
                        "symbol": entrypoint["symbol"],
                    },
                    "origin": {
                        "symbol": contract.subject.name, "error_type": error_type,
                        "kind": contract.attributes["error_kind"],
                    },
                    "confidence": 0.75,
                    "evidence": [
                        {
                            "file": entrypoint["file_path"], "start_line": entrypoint["start_line"],
                            "end_line": entrypoint["end_line"],
                        },
                        {"file": source.file_path, "start_line": source.start_line, "end_line": source.end_line},
                    ],
                    "unknowns": [
                        "A framework-global handler, gateway or proxy may map this error outside indexed source.",
                        *(["The endpoint flow was truncated before all error evidence could be checked."]
                          if traversal.truncated else []),
                    ],
                    "remediation": [
                        "Add or verify a safe HTTP mapping for the expected error type, including its public status and code.",
                    ],
                },
            })
    return findings


def find_internal_error_exposures(conn: sqlite3.Connection) -> list[dict]:
    """Flag source-proven public mappings that include an internal error detail.

    Extractors retain only the boolean fact and source evidence. They never persist
    the message, stack trace or cause that may have been placed in the response.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, source, protocol, transport_code, public_code,
                  file_path, start_line, end_line
           FROM static_error_contracts
           WHERE exposes_internal_detail = 1
             AND (
                 (role = 'maps' AND protocol = 'http')
                 OR (role = 'raises' AND protocol = 'graphql')
             )
           ORDER BY service_id, source, protocol, transport_code""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        public_result = row["transport_code"] if row["protocol"] == "http" else row["public_code"]
        mapping = {
            "symbol": row["source"], "protocol": row["protocol"],
            "code": public_result,
        }
        response_description = (
            f"HTTP {public_result}"
            if row["protocol"] == "http"
            else f"GraphQL error code {public_result}"
        )
        findings.append({
            "kind": "possible_internal_error_exposure", "severity": "critical",
            "services": [names[row["service_id"]]],
            "reason": (
                f"{names[row['service_id']]} exposes a direct internal error detail from "
                f"{row['source']} through {response_description}."
            ),
            "detail": {
                "mapping": mapping,
                "public_code": row["public_code"],
                "confidence": 0.9,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "Static analysis cannot establish whether a runtime redaction hook transforms this response before it leaves the service.",
                ],
                "remediation": [
                    "Return a stable public error code and message; log diagnostic detail only on the internal, correlated error path.",
                ],
            },
        })
    return findings


def find_static_http_calls_without_resilience_policy(conn: sqlite3.Connection) -> list[dict]:
    """Flag a proven internal HTTP call with no literal policy on its source symbol.

    Absence from the static model is deliberately not treated as absence at runtime:
    Spring or client defaults may be configured elsewhere. This is a narrow review
    signal that points to the exact boundary that needs verification.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT call.service_id, call.source, call.target_service,
                  call.target_method, call.target_path,
                  call.file_path, call.start_line, call.end_line
           FROM static_service_calls call
           WHERE call.protocol = 'http'
             AND NOT EXISTS (
                 SELECT 1 FROM static_resilience_policies policy
                 WHERE policy.service_id = call.service_id
                   AND policy.source = call.source
             )
           ORDER BY call.service_id, call.source, call.target_service,
                    call.target_method, call.target_path, call.file_path, call.start_line""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        caller = names[row["service_id"]]
        target = f"{row['target_method'] or 'HTTP'} {row['target_path'] or '/'}"
        findings.append({
            "kind": "possible_missing_http_resilience_policy", "severity": "info",
            "services": [caller],
            "reason": (
                f"{row['source']} calls internal service {row['target_service']} ({target}) "
                "without a source-proven literal timeout or retry policy."
            ),
            "detail": {
                "caller": {"service": caller, "symbol": row["source"]},
                "target": {
                    "service": row["target_service"], "method": row["target_method"],
                    "path": row["target_path"],
                },
                "confidence": 0.5,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "Timeouts or retries may be configured globally, by a client factory, or outside the indexed source.",
                    "Static analysis cannot establish whether retrying this operation is safe or idempotent.",
                ],
                "remediation": [
                    "Review the client boundary and document or declare an appropriate timeout.",
                    "Add retries only when the downstream operation is safe to repeat or protected by an idempotency key.",
                ],
            },
        })
    return findings


def find_retries_on_potentially_non_idempotent_http_calls(conn: sqlite3.Connection) -> list[dict]:
    """Surface literal retries on POST/PATCH calls for an idempotency review.

    POST and PATCH are not proof of unsafe repetition: an API may enforce an
    idempotency key or server-side de-duplication. The detector therefore reports
    the precise static combination and preserves that uncertainty for review.
    """
    names = _service_names(conn)
    methods = tuple(sorted(POTENTIALLY_NON_IDEMPOTENT_HTTP_METHODS))
    placeholders = ", ".join("?" for _ in methods)
    query = (
        "SELECT service_id, source, target_service, target_method, target_path, "  # nosec B608 - placeholders are generated from a fixed constant tuple; values are bound.
        "file_path, start_line, end_line FROM static_service_calls "
        "WHERE protocol = 'http' AND target_method IN (" + placeholders + ") "
        "ORDER BY service_id, source, target_service, target_method, target_path, file_path, start_line"
    )
    calls = conn.execute(query, methods).fetchall()
    policies_by_source = _retry_policies_by_source(conn)

    findings: list[dict] = []
    for call in calls:
        policies = policies_by_source.get((call["service_id"], call["source"]))
        if not policies:
            continue
        caller = names[call["service_id"]]
        findings.append({
            "kind": "possible_retry_on_non_idempotent_http_call", "severity": "warning",
            "services": [caller],
            "reason": (
                f"{call['source']} declares retry and calls {call['target_method']} "
                f"{call['target_service']}{call['target_path'] or '/'}; validate repeat safety."
            ),
            "detail": {
                "caller": {"service": caller, "symbol": call["source"]},
                "target": {
                    "service": call["target_service"], "method": call["target_method"],
                    "path": call["target_path"],
                },
                "retry_policies": [
                    {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                    for policy in policies
                ],
                "confidence": 0.65,
                "evidence": [
                    _edge_evidence(call),
                    *[_edge_evidence(policy) for policy in policies],
                ],
                "unknowns": [
                    "The target may use an idempotency key, request de-duplication or another repeat-safe contract outside the indexed source.",
                    "A source-level retry declaration does not prove which branch or response category it retries at runtime.",
                ],
                "remediation": [
                    "Confirm that repeating this POST or PATCH is safe before retaining retries.",
                    "Use an idempotency key or documented server-side de-duplication when retrying can repeat a state change.",
                ],
            },
        })
    return findings


def _retry_policies_by_source(conn: sqlite3.Connection) -> dict[tuple[int, str], list[sqlite3.Row]]:
    """Group literal retry declarations by their local source symbol."""
    return _resilience_policies_by_source(conn, "retry")


def find_retries_on_non_retryable_errors(conn: sqlite3.Connection) -> list[dict]:
    """Flag a local retry declaration next to a source-proven permanent client error."""
    names = _service_names(conn)
    contracts = conn.execute(
        """SELECT service_id, source, error_kind, internal_type, transport_code,
                  file_path, start_line, end_line
           FROM static_error_contracts
           WHERE role = 'raises'
             AND retryability = 'not_retryable'
             AND error_kind IN ('authorization', 'conflict', 'not_found', 'rate_limit', 'validation')
           ORDER BY service_id, source, error_kind, internal_type, transport_code""",
    ).fetchall()
    policies_by_source = _retry_policies_by_source(conn)
    findings: list[dict] = []
    for contract in contracts:
        policies = policies_by_source.get((contract["service_id"], contract["source"]))
        if not policies:
            continue
        service = names[contract["service_id"]]
        findings.append({
            "kind": "possible_retry_on_non_retryable_error", "severity": "warning",
            "services": [service],
            "reason": (
                f"{contract['source']} declares retry and raises the non-retryable "
                f"{contract['error_kind']} error '{contract['internal_type']}'."
            ),
            "detail": {
                "error": {
                    "symbol": contract["source"], "type": contract["internal_type"],
                    "kind": contract["error_kind"], "status": contract["transport_code"],
                },
                "retry_policies": [
                    {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                    for policy in policies
                ],
                "confidence": 0.8,
                "evidence": [
                    _edge_evidence(contract),
                    *[_edge_evidence(policy) for policy in policies],
                ],
                "unknowns": [
                    "The retry predicate may exclude this error, or the error may be raised outside the retried branch.",
                ],
                "remediation": [
                    "Exclude permanent client/domain errors from retry predicates and preserve retries for documented transient failures only.",
                ],
            },
        })
    return findings


def _resilience_policies_by_source(
    conn: sqlite3.Connection, kind: str | None,
) -> dict[tuple[int, str], list[sqlite3.Row]]:
    """Group one kind, or all kinds, of literal resilience declarations by source."""
    policies_by_source: dict[tuple[int, str], list[sqlite3.Row]] = defaultdict(list)
    query = (
        """SELECT service_id, source, kind, mechanism, value, unit,
                  file_path, start_line, end_line
           FROM static_resilience_policies
           WHERE kind = ?
           ORDER BY service_id, source, mechanism, value, unit, file_path, start_line"""
        if kind is not None
        else """SELECT service_id, source, kind, mechanism, value, unit,
                       file_path, start_line, end_line
                FROM static_resilience_policies
                ORDER BY service_id, source, kind, mechanism, value, unit, file_path, start_line"""
    )
    for policy in conn.execute(
        query,
        (kind,) if kind is not None else (),
    ).fetchall():
        policies_by_source[(policy["service_id"], policy["source"])].append(policy)
    return policies_by_source


def find_retries_on_downstream_client_errors(conn: sqlite3.Connection) -> list[dict]:
    """Flag retries whose resolved HTTP target exposes a source-proven 4xx.

    The facts establish that retry and an HTTP call share a local source, not that a
    retry predicate handles every downstream response. Ambiguous target service names
    are excluded rather than joined to an arbitrary repository.
    """
    names = _service_names(conn)
    calls = conn.execute(
        """SELECT call.service_id AS from_service_id, target.id AS to_service_id,
                  call.source AS caller_source, call.target_method AS api_method,
                  call.target_path AS api_path, call.file_path AS caller_file_path,
                  call.start_line AS caller_start_line, call.end_line AS caller_end_line
           FROM static_service_calls call
           JOIN services caller ON caller.id = call.service_id
           JOIN services target ON LOWER(target.name) = LOWER(call.target_service)
           WHERE call.protocol = 'http'
             AND (
                 (SELECT COUNT(*) FROM services candidate
                  WHERE LOWER(candidate.name) = LOWER(call.target_service)) = 1
                 OR (
                     caller.repository_id IS NOT NULL
                     AND target.repository_id = caller.repository_id
                     AND (SELECT COUNT(*) FROM services candidate
                          WHERE LOWER(candidate.name) = LOWER(call.target_service)
                            AND candidate.repository_id = caller.repository_id) = 1
                 )
             )
           ORDER BY call.service_id, target.id, call.source, call.target_method,
                    call.target_path, call.file_path, call.start_line""",
    ).fetchall()
    policies_by_source = _retry_policies_by_source(conn)
    endpoint_contracts: dict[tuple[int, str | None, str | None], tuple[str, list[dict], bool]] = {}
    findings: list[dict] = []
    for call in calls:
        policies = policies_by_source.get((call["from_service_id"], call["caller_source"]))
        if not policies:
            continue
        scope, contracts, truncated = _downstream_contract_scope(conn, call, endpoint_contracts)
        for contract in contracts:
            if not _is_client_error_contract(contract):
                continue
            confidence = 0.8 if scope == "endpoint_flow" else 0.65
            findings.append({
                "kind": "possible_retry_on_downstream_client_error", "severity": "warning",
                "services": [names[call["from_service_id"]], names[call["to_service_id"]]],
                "reason": (
                    f"{call['caller_source']} declares retry and calls {names[call['to_service_id']]} "
                    f"{call['api_method']} {call['api_path']}, whose indexed contract exposes "
                    f"HTTP {contract['transport_code']} ({contract['internal_type']})."
                ),
                "detail": {
                    "caller": {
                        "service": names[call["from_service_id"]], "symbol": call["caller_source"],
                        "method": call["api_method"], "path": call["api_path"],
                    },
                    "downstream": {
                        "service": names[call["to_service_id"]], "symbol": contract["source"],
                        "error_type": contract["internal_type"], "kind": contract["error_kind"],
                        "status": contract["transport_code"],
                    },
                    "retry_policies": [
                        {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                        for policy in policies
                    ],
                    "scope": scope,
                    "confidence": confidence,
                    "evidence": [
                        {
                            "file": call["caller_file_path"], "start_line": call["caller_start_line"],
                            "end_line": call["caller_end_line"],
                        },
                        *[_edge_evidence(policy) for policy in policies],
                        _edge_evidence(contract),
                    ],
                    "unknowns": [
                        "The retry predicate may exclude this response, or the policy may apply to another branch in the same source symbol.",
                        (
                            "The target endpoint flow proves this error source is reachable, but not which runtime response branch it receives."
                            if scope == "endpoint_flow"
                            else "The downstream mapping may be global or may not apply to this endpoint."
                        ),
                        *([DOWNSTREAM_FLOW_TRUNCATED_UNKNOWN] if truncated else []),
                    ],
                    "remediation": [
                        "Review retry predicates and exclude permanent client errors unless the downstream contract explicitly marks them transient.",
                        "Document exceptions such as rate limits before retrying a 4xx response.",
                    ],
                },
            })
    return findings


def find_timeouts_without_local_fallback(conn: sqlite3.Connection) -> list[dict]:
    """Surface timeout-protected HTTP calls lacking an explicit local timeout handler.

    This does not assert that the timeout is unhandled globally. It only records the
    absence of a source-proven typed timeout fallback at the client boundary itself.
    """
    names = _service_names(conn)
    calls = conn.execute(
        """SELECT service_id, source, target_service, target_method, target_path,
                  file_path, start_line, end_line
           FROM static_service_calls
           WHERE protocol = 'http'
           ORDER BY service_id, source, target_service, target_method, target_path,
                    file_path, start_line""",
    ).fetchall()
    timeout_policies = _resilience_policies_by_source(conn, "timeout")
    handled_sources = {
        (row["service_id"], row["source"])
        for row in conn.execute(
            """SELECT service_id, source
               FROM static_error_contracts
               WHERE role = 'handles' AND error_kind = 'timeout'""",
        ).fetchall()
    }
    findings: list[dict] = []
    for call in calls:
        source_key = (call["service_id"], call["source"])
        policies = timeout_policies.get(source_key)
        if not policies or source_key in handled_sources:
            continue
        caller = names[call["service_id"]]
        findings.append({
            "kind": "possible_timeout_without_local_fallback", "severity": "info",
            "services": [caller],
            "reason": (
                f"{call['source']} declares a timeout for internal call "
                f"{call['target_service']} {call['target_method'] or 'HTTP'} "
                f"{call['target_path'] or '/'} without a source-proven local timeout fallback."
            ),
            "detail": {
                "caller": {"service": caller, "symbol": call["source"]},
                "target": {
                    "service": call["target_service"], "method": call["target_method"],
                    "path": call["target_path"],
                },
                "timeout_policies": [
                    {"mechanism": policy["mechanism"], "value": policy["value"], "unit": policy["unit"]}
                    for policy in policies
                ],
                "confidence": 0.55,
                "evidence": [
                    _edge_evidence(call),
                    *[_edge_evidence(policy) for policy in policies],
                ],
                "unknowns": [
                    "A controller, gateway, client factory or global handler may handle this timeout outside the indexed source symbol.",
                    "Only typed catch and typed Reactor fallback declarations are recognized as local timeout handling.",
                ],
                "remediation": [
                    "Review the client boundary and add or document an intentional timeout fallback or controlled error translation.",
                    "Keep any fallback safe for partial downstream execution and preserve a clear timeout contract for callers.",
                ],
            },
        })
    return findings


def find_timeout_fallbacks_masking_failures(conn: sqlite3.Connection) -> list[dict]:
    """Flag an HTTP endpoint that explicitly turns a local timeout into 2xx.

    Only an explicit ``ResponseEntity`` success fallback is classified this way by
    the static analyzer. A legitimate cached or partial response is still possible,
    so this remains a review signal rather than an error classification.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT fallback.service_id, fallback.source, fallback.internal_type,
                  fallback.transport_code, fallback.file_path, fallback.start_line,
                  fallback.end_line, entrypoint.method AS entrypoint_method,
                  entrypoint.name AS entrypoint_path, call.target_service,
                  call.target_method, call.target_path, call.file_path AS call_file_path,
                  call.start_line AS call_start_line, call.end_line AS call_end_line
           FROM static_error_contracts fallback
           JOIN entrypoints entrypoint
             ON entrypoint.service_id = fallback.service_id
            AND entrypoint.symbol = fallback.source
           JOIN static_service_calls call
             ON call.service_id = fallback.service_id
            AND call.source = fallback.source
           WHERE fallback.role = 'handles'
             AND fallback.error_kind = 'timeout'
             AND fallback.protocol = 'http'
             AND CAST(fallback.transport_code AS INTEGER) BETWEEN 200 AND 299
             AND call.protocol = 'http'
           ORDER BY fallback.service_id, fallback.source, call.target_service,
                    call.target_method, call.target_path, fallback.file_path, fallback.start_line""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        findings.append({
            "kind": "possible_timeout_fallback_masks_failure", "severity": "warning",
            "services": [service],
            "reason": (
                f"HTTP endpoint {row['entrypoint_method']} {row['entrypoint_path']} handles "
                f"{row['internal_type']} from an internal call by returning HTTP {row['transport_code']}."
            ),
            "detail": {
                "entrypoint": {
                    "method": row["entrypoint_method"], "path": row["entrypoint_path"],
                    "symbol": row["source"],
                },
                "target": {
                    "service": row["target_service"], "method": row["target_method"],
                    "path": row["target_path"],
                },
                "fallback": {
                    "error_type": row["internal_type"], "status": row["transport_code"],
                },
                "confidence": 0.8,
                "evidence": [
                    {
                        "file": row["call_file_path"], "start_line": row["call_start_line"],
                        "end_line": row["call_end_line"],
                    },
                    _edge_evidence(row),
                ],
                "unknowns": [
                    "The successful response may be an intentional cached, partial or otherwise documented degraded result.",
                    "The static facts do not establish whether clients receive an explicit degradation signal in the response body or headers.",
                ],
                "remediation": [
                    "Expose an explicit degraded-result signal or return a controlled timeout/service-unavailable contract.",
                    "Document any intentional success fallback, including cache freshness and partial-result semantics.",
                ],
            },
        })
    return findings


def find_timeouts_mapped_as_internal_server_errors(conn: sqlite3.Connection) -> list[dict]:
    """Flag an explicit timeout mapping that exposes HTTP 500 instead of availability semantics."""
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, source, internal_type, transport_code,
                  file_path, start_line, end_line
           FROM static_error_contracts
           WHERE role = 'maps'
             AND error_kind = 'timeout'
             AND protocol = 'http'
             AND transport_code = '500'
           ORDER BY service_id, source, internal_type, file_path, start_line""",
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        findings.append({
            "kind": "possible_timeout_mapped_as_internal_server_error", "severity": "warning",
            "services": [service],
            "reason": (
                f"{row['source']} maps timeout type {row['internal_type']} to HTTP 500; "
                "review whether callers need an explicit unavailable or gateway-timeout contract."
            ),
            "detail": {
                "mapping": {
                    "symbol": row["source"], "error_type": row["internal_type"],
                    "status": row["transport_code"],
                },
                "confidence": 0.8,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "A gateway, compatibility contract or operational policy may intentionally require HTTP 500 for this timeout.",
                    "Static analysis cannot determine whether the timeout was caused by an internal defect rather than downstream unavailability.",
                ],
                "remediation": [
                    "Confirm the public error contract and prefer an explicit 503 or 504 when the timeout represents temporary unavailability.",
                    "Document any intentional 500 translation so clients and retry policies can preserve the expected semantics.",
                ],
            },
        })
    return findings


def find_unmapped_downstream_errors(conn: sqlite3.Connection) -> list[dict]:
    """Surface internal HTTP calls whose downstream 4xx has no known caller mapping.

    A source-proven call is preferred over a service-level relation inferred while
    indexing. Neither proves runtime translation behavior, so both remain review
    signals instead of claims that an error will become HTTP 500.
    """
    names = _service_names(conn)
    static_calls = conn.execute(
        """SELECT caller.id AS from_service_id, target.id AS to_service_id,
                  call.source AS caller_source, call.target_method AS api_method,
                  call.target_path AS api_path, call.file_path AS caller_file_path,
                  call.start_line AS caller_start_line, call.end_line AS caller_end_line
           FROM static_service_calls call
           JOIN services caller ON caller.id = call.service_id
           JOIN services target ON LOWER(target.name) = LOWER(call.target_service)
           WHERE call.protocol = 'http'
           ORDER BY caller.id, target.id, call.source, call.target_method, call.target_path,
                    call.file_path, call.start_line""",
    ).fetchall()
    rows = conn.execute(
        """SELECT sc.from_service_id, sc.to_service_id, a.method AS api_method,
                  a.path AS api_path, downstream.source AS downstream_source,
                  downstream.error_kind, downstream.internal_type,
                  downstream.transport_code, downstream.public_code,
                  downstream.file_path, downstream.start_line, downstream.end_line
           FROM service_calls sc
           JOIN apis a ON a.id = sc.from_api_id
           JOIN static_error_contracts downstream ON downstream.service_id = sc.to_service_id
           WHERE sc.call_kind = 'http'
             AND sc.target_kind = 'internal'
             AND sc.to_service_id IS NOT NULL
             AND downstream.role IN ('raises', 'maps')
             AND downstream.protocol = 'http'
             AND CAST(downstream.transport_code AS INTEGER) BETWEEN 400 AND 499
             AND downstream.internal_type IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM static_error_contracts caller
                 WHERE caller.service_id = sc.from_service_id
                   AND caller.role = 'maps'
                   AND caller.internal_type = downstream.internal_type
                   AND caller.protocol = 'http'
                   AND CAST(caller.transport_code AS INTEGER) BETWEEN 400 AND 499
             )
             AND NOT EXISTS (
                 SELECT 1 FROM static_service_calls static_call
                 JOIN services static_target
                   ON LOWER(static_target.name) = LOWER(static_call.target_service)
                 WHERE static_call.service_id = sc.from_service_id
                   AND static_call.protocol = 'http'
                   AND static_target.id = sc.to_service_id
             )
           ORDER BY sc.from_service_id, sc.to_service_id, a.method, a.path,
                    downstream.internal_type, downstream.source""",
    ).fetchall()
    findings = _static_unmapped_downstream_error_findings(conn, static_calls, names)
    for row in rows:
        findings.append({
            "kind": "possible_unmapped_downstream_error", "severity": "info",
            "services": [names[row["from_service_id"]], names[row["to_service_id"]]],
            "reason": (
                f"{names[row['from_service_id']]} {row['api_method']} {row['api_path']} calls "
                f"{names[row['to_service_id']]}, which exposes {row['internal_type']} as HTTP "
                f"{row['transport_code']}; no same-type client-error mapping is indexed in the caller."
            ),
            "detail": {
                "caller": {
                    "service": names[row["from_service_id"]], "method": row["api_method"], "path": row["api_path"],
                },
                "downstream": {
                    "service": names[row["to_service_id"]], "symbol": row["downstream_source"],
                    "error_type": row["internal_type"], "kind": row["error_kind"],
                    "status": row["transport_code"], "public_code": row["public_code"],
                },
                "confidence": 0.35,
                "evidence": [{
                    "file": row["file_path"], "start_line": row["start_line"], "end_line": row["end_line"],
                }],
                "unknowns": [
                    "The resolved service call does not prove which client branch receives this status.",
                    "The caller may translate the downstream error to another local type or rely on a handler outside the indexed source.",
                ],
                "remediation": [
                    "Review the client boundary and preserve, explicitly translate, or document this downstream client-error contract.",
                ],
            },
        })
    return findings


def _static_unmapped_downstream_error_findings(
    conn: sqlite3.Connection, calls: list[sqlite3.Row], names: dict[int, str],
) -> list[dict]:
    """Build client-error review findings from source-proven service calls.

    When the remote endpoint is indexed, its bounded static flow scopes the error
    contracts. Falling back to service-wide contracts preserves useful coverage for
    partially indexed systems, but deliberately carries lower confidence.
    """
    caller_mappings: dict[int, set[str]] = {}
    endpoint_contracts: dict[tuple[int, str | None, str | None], tuple[str, list[dict], bool]] = {}
    findings: list[dict] = []
    for call in calls:
        caller_id = call["from_service_id"]
        target_id = call["to_service_id"]
        mapped_types = caller_mappings.get(caller_id)
        if mapped_types is None:
            mapped_types = _mapped_client_error_types(conn, caller_id)
            caller_mappings[caller_id] = mapped_types
        scope, contracts, truncated = _downstream_contract_scope(conn, call, endpoint_contracts)
        for contract in contracts:
            if not _is_client_error_contract(contract) or contract["internal_type"] in mapped_types:
                continue
            confidence = 0.75 if scope == "endpoint_flow" else 0.6
            target_unknown = (
                "The target endpoint flow proves this error source is reachable, but not which runtime response branch it receives."
                if scope == "endpoint_flow"
                else "The downstream mapping may be global or may not apply to this endpoint."
            )
            findings.append({
                "kind": "possible_unmapped_downstream_error", "severity": "info",
                "services": [names[caller_id], names[target_id]],
                "reason": (
                    f"{names[caller_id]} {call['api_method']} {call['api_path']} calls "
                    f"{names[target_id]}, which exposes {contract['internal_type']} as HTTP "
                    f"{contract['transport_code']}; no same-type client-error mapping is indexed in the caller."
                ),
                "detail": {
                    "caller": {
                        "service": names[caller_id], "symbol": call["caller_source"],
                        "method": call["api_method"], "path": call["api_path"],
                    },
                    "downstream": {
                        "service": names[target_id], "symbol": contract["source"],
                        "error_type": contract["internal_type"], "kind": contract["error_kind"],
                        "status": contract["transport_code"], "public_code": contract["public_code"],
                    },
                    "scope": scope,
                    "confidence": confidence,
                    "evidence": [
                        {
                            "file": call["caller_file_path"], "start_line": call["caller_start_line"],
                            "end_line": call["caller_end_line"],
                        },
                        _edge_evidence(contract),
                    ],
                    "unknowns": [
                        target_unknown,
                        "The caller may translate the downstream error to another local type or rely on a handler outside the indexed source.",
                        *([DOWNSTREAM_FLOW_TRUNCATED_UNKNOWN] if truncated else []),
                    ],
                    "remediation": [
                        "Review the client boundary and preserve, explicitly translate, or document this downstream client-error contract.",
                    ],
                },
            })
    return findings


def _downstream_contract_scope(
    conn: sqlite3.Connection,
    call: sqlite3.Row,
    endpoint_contracts: dict[tuple[int, str | None, str | None], tuple[str, list[dict], bool]],
) -> tuple[str, list[dict], bool]:
    """Return endpoint-reachable contracts when the literal target is indexed."""
    method = call["api_method"]
    path = call["api_path"]
    target_id = call["to_service_id"]
    key = (target_id, method if isinstance(method, str) else None,
           path if isinstance(path, str) else None)
    if key not in endpoint_contracts:
        if not canonical_snapshots_repo.has_snapshot(conn, target_id):
            endpoint_contracts[key] = ("service_contracts", [], False)
            return endpoint_contracts[key]
        snapshot = canonical_snapshots_repo.read_snapshot(conn, target_id)
        if snapshot is None:
            endpoint_contracts[key] = ("service_contracts", [], False)
            return endpoint_contracts[key]
        entrypoint = (
            flows_repo.get_entrypoint(conn, target_id, "http", method, path)
            if isinstance(method, str) and isinstance(path, str) else None
        )
        if entrypoint is None:
            endpoint_contracts[key] = ("service_contracts", _canonical_error_contract_rows(snapshot.facts), False)
        else:
            entry_key = EntrypointKey(snapshot.service, "http", entrypoint["method"],
                                      entrypoint["name"], entrypoint["symbol"])
            traversal = KnowledgeNavigator(snapshot).reachable(
                entry_key, TraversalPolicy(max_depth=200, max_nodes=201, max_edges=200),
            )
            endpoint_contracts[key] = ("endpoint_flow", _canonical_error_contract_rows(traversal.facts),
                                       traversal.truncated)
    return endpoint_contracts[key]


def _canonical_error_contract_rows(facts: tuple[CanonicalFact, ...]) -> list[dict]:
    rows = []
    for fact in facts:
        if fact.kind != "error_contract":
            continue
        if not isinstance(fact.subject, SymbolKey):
            raise ValueError("error contract must belong to a symbol")
        for source in fact.sources:
            rows.append({
                "source": fact.subject.name, **fact.attributes,
                "file_path": source.file_path, "start_line": source.start_line,
                "end_line": source.end_line,
            })
    return rows


def _mapped_client_error_types(conn: sqlite3.Connection, service_id: int) -> set[str]:
    rows = conn.execute(
        """SELECT internal_type FROM static_error_contracts
           WHERE service_id = ? AND role = 'maps' AND protocol = 'http'
             AND internal_type IS NOT NULL
             AND CAST(transport_code AS INTEGER) BETWEEN 400 AND 499""",
        (service_id,),
    ).fetchall()
    return {row["internal_type"] for row in rows}


def _is_client_error_contract(contract: sqlite3.Row) -> bool:
    if (
        contract["role"] not in {"raises", "maps"}
        or contract["protocol"] != "http"
        or contract["internal_type"] is None
    ):
        return False
    try:
        return 400 <= int(contract["transport_code"]) <= 499
    except (TypeError, ValueError):
        return False


def _rabbitmq_consumers_without_recovery(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return RabbitMQ consumers lacking source-proven local recovery facts."""
    rows = conn.execute(
        """
        SELECT e.service_id, e.name AS queue, e.symbol, e.file_path, e.start_line, e.end_line,
               c.contract_json,
               EXISTS(
                   SELECT 1 FROM flow_boundaries b
                   WHERE b.service_id = e.service_id AND b.source = e.symbol AND b.kind = 'retry'
               ) AS has_retry_boundary
        FROM entrypoints e
        JOIN entrypoint_contracts c ON c.entrypoint_id = e.id
        WHERE e.kind = 'message' AND e.method = 'CONSUME'
        ORDER BY e.service_id, e.name, e.symbol
        """
    ).fetchall()
    unrecovered: list[sqlite3.Row] = []
    for row in rows:
        contract = json.loads(row["contract_json"])
        if contract.get("transport") != "rabbitmq" or contract.get("direction") != "consumes":
            continue
        dead_letter = contract.get("dead_letter_routing_key")
        retry_delay = contract.get("retry_delay_ms")
        retry_boundary = bool(row["has_retry_boundary"])
        if dead_letter is not None or retry_delay is not None or retry_boundary:
            continue
        unrecovered.append(row)
    return unrecovered


def find_message_consumers_without_recovery_policy(conn: sqlite3.Connection) -> list[dict]:
    """Flag RabbitMQ consumers without a source-proven recovery mechanism.

    A missing local declaration is not proof that the broker lacks a policy. The
    detector therefore only considers consumers whose static contract established
    RabbitMQ, and reports the missing *source proof* with a deliberately low
    confidence rather than asserting a production configuration defect.
    """
    names = _service_names(conn)
    findings: list[dict] = []
    for row in _rabbitmq_consumers_without_recovery(conn):
        contract = json.loads(row["contract_json"])
        dead_letter = contract.get("dead_letter_routing_key")
        retry_delay = contract.get("retry_delay_ms")
        retry_boundary = bool(row["has_retry_boundary"])
        findings.append(
            {
                "kind": "possible_message_consumer_without_recovery_policy", "severity": "warning",
                "services": [names[row["service_id"]]],
                "reason": (
                    "A RabbitMQ consumer has no source-proven retry boundary, retry delay or dead-letter route; "
                    "validate its recovery policy."
                ),
                "detail": {
                    "consumer": {"queue": row["queue"], "symbol": row["symbol"]},
                    "source_proven": {
                        "dead_letter_routing_key": dead_letter,
                        "retry_delay_ms": retry_delay,
                        "retry_boundary": retry_boundary,
                    },
                    "confidence": 0.45,
                    "evidence": [{
                        "file": row["file_path"], "start_line": row["start_line"], "end_line": row["end_line"],
                    }],
                    "unknowns": [
                        "Broker topology or retry policy may be declared outside the indexed source/configuration.",
                    ],
                    "remediation": [
                        "Confirm a retry and dead-letter policy in broker configuration, then declare it near the consumer when practical.",
                    ],
                },
            }
        )
    return findings


def find_cloud_code_without_iac(conn: sqlite3.Connection) -> list[dict]:
    """A service's own code proves it publishes/consumes/reads/writes a named
    cloud resource, but no Terraform/CloudFormation declaration in its
    repository resolves to that same literal name. Only facts with a
    source-proven `target_name` are considered — an unresolved call site can't
    be compared to anything, so it is silently excluded rather than guessed
    into either bucket."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT scf.service_id, scf.provider, scf.resource_type, scf.service_name,
               scf.operation, scf.target_name, scf.file_path, scf.start_line, scf.end_line
        FROM static_cloud_facts scf
        JOIN services svc ON svc.id = scf.service_id
        WHERE scf.target_name IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM cloud_iac_resources cir
              WHERE cir.repository_id IS svc.repository_id
                AND cir.provider = scf.provider AND cir.resource_type = scf.resource_type
                AND cir.physical_name = scf.target_name
          )
        ORDER BY scf.service_id, scf.target_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        findings.append({
            "kind": "cloud_dependency_without_iac", "severity": "warning",
            "services": [service],
            "reason": (
                f"{service} code {row['operation']}s '{row['target_name']}' ({row['provider']}:"
                f"{row['service_name']}), but no matching Terraform/CloudFormation declaration was "
                "found in this repository."
            ),
            "detail": {
                "target_name": row["target_name"], "provider": row["provider"],
                "resource_type": row["resource_type"], "confidence": 0.7,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "The resource may be declared in IaC outside this repository, or provisioned manually.",
                ],
                "remediation": ["Confirm this resource is declared somewhere, or add its IaC declaration."],
            },
        })
    return findings


def find_cloud_iac_unused_in_code(conn: sqlite3.Connection) -> list[dict]:
    """The inverse of find_cloud_code_without_iac: a Terraform/CloudFormation
    declaration whose literal name no indexed code in the same repository
    references. Only resources structurally attributed to one indexed service
    are considered — a repository-scoped resource (no single service root
    contains its declaring file) has no service to report this against, and is
    excluded rather than attached to an arbitrary one."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT cir.service_id, cir.provider, cir.resource_type, cir.iac_resource_type,
               cir.logical_name, cir.physical_name, cir.file_path, cir.start_line, cir.end_line
        FROM cloud_iac_resources cir
        JOIN services svc ON svc.id = cir.service_id
        WHERE cir.physical_name IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM static_cloud_facts scf
              JOIN services fact_svc ON fact_svc.id = scf.service_id
              WHERE fact_svc.repository_id IS svc.repository_id
                AND scf.provider = cir.provider AND scf.resource_type = cir.resource_type
                AND scf.target_name = cir.physical_name
          )
        ORDER BY cir.service_id, cir.physical_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        findings.append({
            # A provisioned-but-unreferenced resource is far less alarming than
            # code referencing something undeclared, hence "info" not "warning".
            "kind": "cloud_iac_resource_unused", "severity": "info",
            "services": [service],
            "reason": (
                f"{service}'s Terraform/CloudFormation declares {row['iac_resource_type']} "
                f"'{row['physical_name']}', but no indexed code in this repository references it."
            ),
            "detail": {
                "iac_resource_type": row["iac_resource_type"], "logical_name": row["logical_name"],
                "physical_name": row["physical_name"], "confidence": 0.6,
                "evidence": [_edge_evidence(row)],
                "unknowns": [
                    "Code that references this resource may live in a service not yet indexed.",
                ],
                "remediation": ["Confirm the resource is still needed, or index the code that uses it."],
            },
        })
    return findings


def find_kubernetes_configuration_key_mismatches(conn: sqlite3.Connection) -> list[dict]:
    """Surface only already-proven local manifest key conflicts.

    The scanner excludes absent and ambiguous declarations, but rendered or runtime
    mutation can still change the deployed source. Keep the finding advisory.
    """
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, environment_key, source_kind, source_name, source_key,
                  reference_file_path, reference_start_line, reference_end_line,
                  declaration_file_path, declaration_start_line, declaration_end_line
           FROM kubernetes_configuration_key_mismatches
           WHERE service_id IS NOT NULL
           ORDER BY service_id, environment_key, source_name, source_key, reference_file_path, reference_start_line"""
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        source_label = "ConfigMap" if row["source_kind"] == "config_map" else "Secret"
        findings.append({
            "kind": "possible_kubernetes_configuration_key_not_declared", "severity": "warning",
            "services": [service],
            "reason": (
                f"{service} references {source_label} {row['source_name']} key {row['source_key']} for "
                f"{row['environment_key']}, but its single indexed declaration does not list that key."
            ),
            "detail": {
                "environment_key": row["environment_key"], "source_kind": row["source_kind"],
                "source_name": row["source_name"], "source_key": row["source_key"], "confidence": 0.9,
                "evidence": [
                    {
                        "file": row["reference_file_path"], "start_line": row["reference_start_line"],
                        "end_line": row["reference_end_line"],
                    },
                    {
                        "file": row["declaration_file_path"], "start_line": row["declaration_start_line"],
                        "end_line": row["declaration_end_line"],
                    },
                ],
                "unknowns": [
                    "Kustomize, admission controllers, or runtime mutation may add the key outside indexed YAML.",
                ],
                "remediation": ["Confirm the source declaration and workload reference agree before rollout."],
            },
        })
    return findings


def find_kubernetes_configuration_source_unknowns(conn: sqlite3.Connection) -> list[dict]:
    """Return low-confidence possible external configuration dependencies."""
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, environment_key, source_kind, source_name, source_key,
                  reference_file_path, reference_start_line, reference_end_line
           FROM kubernetes_configuration_source_unknowns
           WHERE service_id IS NOT NULL
           ORDER BY service_id, environment_key, source_name, source_key, reference_file_path, reference_start_line"""
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service = names[row["service_id"]]
        source_label = "ConfigMap" if row["source_kind"] == "config_map" else "Secret"
        findings.append({
            "kind": "possible_kubernetes_configuration_source_not_declared_locally", "severity": "info",
            "services": [service],
            "reason": (
                f"{service} references {source_label} {row['source_name']} key {row['source_key']} for "
                f"{row['environment_key']}, but no matching source declaration was indexed locally."
            ),
            "detail": {
                "environment_key": row["environment_key"], "source_kind": row["source_kind"],
                "source_name": row["source_name"], "source_key": row["source_key"], "confidence": 0.4,
                "evidence": [{
                    "file": row["reference_file_path"], "start_line": row["reference_start_line"],
                    "end_line": row["reference_end_line"],
                }],
                "unknowns": [
                    "The source may be managed by another repository, Helm chart, controller, or deployment process.",
                ],
                "remediation": [
                    "Confirm which delivery boundary owns this ConfigMap or Secret before changing it.",
                ],
            },
        })
    return findings


def find_kubernetes_configuration_source_import_unknowns(conn: sqlite3.Connection) -> list[dict]:
    """Return low-confidence possible external ``envFrom`` dependencies."""
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT service_id, source_kind, source_name, prefix, container_role, optional, workload_kind, workload_name,
                  container_name, reference_file_path,
                  reference_start_line, reference_end_line
           FROM kubernetes_configuration_source_import_unknowns
           WHERE service_id IS NOT NULL
           ORDER BY service_id, source_name, prefix, reference_file_path, reference_start_line"""
    ).fetchall()
    grouped: dict[tuple[int, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((row["service_id"], row["source_kind"], row["source_name"]), []).append(row)
    findings: list[dict] = []
    for (service_id, source_kind, source_name), imports in sorted(grouped.items()):
        service = names[service_id]
        source_label = "ConfigMap" if source_kind == "config_map" else "Secret"
        evidence = sorted({
            (row["reference_file_path"], row["reference_start_line"], row["reference_end_line"])
            for row in imports
        })
        prefixes = sorted({row["prefix"] for row in imports if row["prefix"] is not None})
        observed_container_roles = {row["container_role"] for row in imports}
        container_roles = [
            role for role in ("initialization", "application") if role in observed_container_roles
        ]
        workloads = ordered_kubernetes_workloads(
            (
                row["workload_kind"], row["workload_name"], row["container_name"], row["container_role"],
                row["prefix"], row["reference_file_path"], row["reference_start_line"], row["reference_end_line"],
            )
            for row in imports
        )
        availability_values = {None if row["optional"] is None else bool(row["optional"]) for row in imports}
        availability = (
            "optional" if availability_values == {True}
            else "required" if availability_values == {False}
            else "unknown" if availability_values == {None}
            else "mixed"
        )
        detail = {
            "source_kind": source_kind, "source_name": source_name, "availability": availability, "confidence": 0.4,
            "evidence": [
                {"file": file_path, "start_line": start_line, "end_line": end_line}
                for file_path, start_line, end_line in evidence
            ],
            "unknowns": [
                "The source may be managed by another repository, Helm chart, controller, or deployment process.",
                "envFrom does not expose its imported environment keys as static facts.",
            ],
            "remediation": [
                "Confirm which delivery boundary owns this ConfigMap or Secret before relying on its imported keys.",
            ],
        }
        if prefixes:
            detail["prefixes"] = prefixes
        if any(row["prefix"] is None for row in imports):
            detail["includes_unprefixed_import"] = True
        if container_roles:
            detail["container_roles"] = container_roles
        if workloads:
            detail["workloads"] = workloads
        if availability == "optional":
            detail["unknowns"].append("The source is optional and may be absent at runtime.")
            detail["remediation"].append(
                f"Verify requested behavior remains safe when the optional {source_label} {source_name} is unavailable.",
            )
        elif availability in {"mixed", "unknown"}:
            detail["unknowns"].append("The imports do not establish one unambiguous source-availability requirement.")
            detail["remediation"].append("Confirm source availability before relying on imported configuration during rollout.")
        if "initialization" in container_roles:
            detail["unknowns"].append(
                "An initialization container using this source must complete before application containers start.",
            )
            detail["remediation"].append("Verify initialization completes before application containers start.")
        findings.append({
            "kind": "possible_kubernetes_configuration_source_import_not_declared_locally", "severity": "info",
            "services": [service],
            "reason": (
                f"{service} imports keys from {source_label} {source_name} through envFrom, "
                "but no matching source declaration was indexed locally."
            ),
            "detail": detail,
        })
    return findings


def find_shared_cloud_resource(conn: sqlite3.Connection) -> list[dict]:
    """Two or more different services whose code proves they talk to the same
    named cloud resource — coupling through a shared queue/topic/bucket, the
    cloud analog of find_shared_database."""
    names = _service_names(conn)
    rows = conn.execute(
        """SELECT provider, resource_type, target_name, GROUP_CONCAT(DISTINCT service_id) AS service_ids
           FROM static_cloud_facts WHERE target_name IS NOT NULL
           GROUP BY provider, resource_type, target_name HAVING COUNT(DISTINCT service_id) > 1"""
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        service_ids = [int(x) for x in row["service_ids"].split(",")]
        service_names = sorted(names[i] for i in service_ids if i in names)
        findings.append({
            "kind": "shared_cloud_resource", "severity": "info",
            "services": service_names,
            "reason": (
                f"{', '.join(service_names)} all talk to the same {row['provider']}:{row['resource_type']} "
                f"'{row['target_name']}' — coupling through a shared cloud resource."
            ),
            "detail": {
                "provider": row["provider"], "resource_type": row["resource_type"],
                "target_name": row["target_name"],
            },
        })
    return findings


# Presence key(s) that prove a setting was configured, per IaC dialect and (for
# Terraform S3) per detection path -- legacy inline block vs. the modern
# split-resource correlation orbitkb/iac/terraform.py itself performs (see its
# module docstring). Checking every key covers all three paths without
# needing to know which one a given file used.
_ENCRYPTION_PRESENCE_KEYS: dict[str, tuple[str, ...]] = {
    "aws_sqs_queue": ("kms_master_key_id",),
    "AWS::SQS::Queue": ("KmsMasterKeyId",),
    "aws_s3_bucket": ("server_side_encryption_configuration", "encryption_configured"),
    "AWS::S3::Bucket": ("BucketEncryption",),
}
_VERSIONING_PRESENCE_KEYS: dict[str, tuple[str, ...]] = {
    "aws_s3_bucket": ("versioning", "versioning_configured"),
    "AWS::S3::Bucket": ("VersioningConfiguration",),
}
# Attribute name -> the literal value(s) that mean "publicly accessible",
# vendor-sourced from each provider's own ACL/access-level vocabulary, one
# entry per IaC dialect's own spelling of the same attribute.
_PUBLIC_ACCESS_VALUES: dict[str, frozenset[str]] = {
    "acl": frozenset({"public-read", "public-read-write", "authenticated-read"}),
    "AccessControl": frozenset({"PublicRead", "PublicReadWrite", "AuthenticatedRead"}),
    "container_access_type": frozenset({"blob", "container"}),
}


def find_cloud_dead_letter_queue_missing(conn: sqlite3.Connection) -> list[dict]:
    """Flag an SQS queue with no source-proven dead-letter/redrive policy —
    the IaC analog of find_message_consumers_without_recovery_policy. A
    missing local declaration is not proof the queue truly lacks one (it
    could be set outside the indexed IaC), hence the low confidence and
    "worth checking" framing rather than a verdict."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT cir.service_id, cir.iac_resource_type, cir.logical_name, cir.physical_name,
               cir.attributes_json, cir.file_path, cir.start_line, cir.end_line
        FROM cloud_iac_resources cir
        JOIN services svc ON svc.id = cir.service_id
        WHERE cir.iac_resource_type IN ('aws_sqs_queue', 'AWS::SQS::Queue')
        ORDER BY cir.service_id, cir.logical_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        attributes = json.loads(row["attributes_json"] or "{}")
        redrive_key = "redrive_policy" if row["iac_resource_type"] == "aws_sqs_queue" else "RedrivePolicy"
        if attributes.get(redrive_key):
            continue
        service = names[row["service_id"]]
        queue = row["physical_name"] or row["logical_name"]
        findings.append({
            "kind": "possible_missing_dead_letter_queue", "severity": "warning",
            "services": [service],
            "reason": f"{service}'s SQS queue '{queue}' has no source-proven dead-letter/redrive policy; validate it.",
            "detail": {
                "queue": queue, "confidence": 0.45,
                "evidence": [_edge_evidence(row)],
                "unknowns": ["A redrive policy may be declared outside the indexed IaC, or attached to a shared DLQ module."],
                "remediation": ["Confirm a dead-letter queue and redrive policy are configured, or declare one near this queue."],
            },
        })
    return findings


def find_public_object_storage(conn: sqlite3.Connection) -> list[dict]:
    """A bucket/container whose IaC declaration literally sets a public ACL —
    the literal value is the proof, not an inference from the resource's
    name or purpose."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT cir.service_id, cir.provider, cir.iac_resource_type, cir.logical_name, cir.physical_name,
               cir.attributes_json, cir.file_path, cir.start_line, cir.end_line
        FROM cloud_iac_resources cir
        JOIN services svc ON svc.id = cir.service_id
        WHERE cir.resource_type = 'object_storage'
        ORDER BY cir.service_id, cir.logical_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        attributes = json.loads(row["attributes_json"] or "{}")
        public_attribute = next(
            (
                (attr_name, value) for attr_name, value in attributes.items()
                if value in _PUBLIC_ACCESS_VALUES.get(attr_name, frozenset())
            ),
            None,
        )
        if public_attribute is None:
            continue
        attr_name, value = public_attribute
        service = names[row["service_id"]]
        bucket = row["physical_name"] or row["logical_name"]
        findings.append({
            "kind": "possible_public_object_storage", "severity": "critical",
            "services": [service],
            "reason": f"{service}'s {row['provider']} storage '{bucket}' declares {attr_name}='{value}', a publicly accessible setting.",
            "detail": {
                "bucket": bucket, "attribute": attr_name, "value": value, "confidence": 0.85,
                "evidence": [_edge_evidence(row)],
                "unknowns": ["A bucket policy or public access block declared elsewhere in the IaC may still restrict access."],
                "remediation": ["Confirm public access is intentional, or set a private ACL and rely on explicit bucket policies instead."],
            },
        })
    return findings


def find_unencrypted_cloud_resource(conn: sqlite3.Connection) -> list[dict]:
    """S3/SQS specifically — GCS and Azure Storage encrypt by default, so an
    absent declaration there isn't informative the way it is for AWS."""
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT cir.service_id, cir.provider, cir.iac_resource_type, cir.logical_name, cir.physical_name,
               cir.attributes_json, cir.file_path, cir.start_line, cir.end_line
        FROM cloud_iac_resources cir
        JOIN services svc ON svc.id = cir.service_id
        WHERE cir.iac_resource_type IN ('aws_sqs_queue', 'AWS::SQS::Queue', 'aws_s3_bucket', 'AWS::S3::Bucket')
        ORDER BY cir.service_id, cir.logical_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        attributes = json.loads(row["attributes_json"] or "{}")
        presence_keys = _ENCRYPTION_PRESENCE_KEYS[row["iac_resource_type"]]
        if any(attributes.get(key) for key in presence_keys):
            continue
        service = names[row["service_id"]]
        resource = row["physical_name"] or row["logical_name"]
        findings.append({
            "kind": "possible_unencrypted_cloud_resource", "severity": "info",
            "services": [service],
            "reason": f"{service}'s {row['provider']} resource '{resource}' has no source-proven server-side encryption; validate it.",
            "detail": {
                "resource": resource, "confidence": 0.4,
                "evidence": [_edge_evidence(row)],
                "unknowns": ["Encryption may be enabled by an account/organization default outside the indexed IaC."],
                "remediation": ["Confirm server-side encryption is enabled, or declare it explicitly near this resource."],
            },
        })
    return findings


def find_missing_bucket_versioning(conn: sqlite3.Connection) -> list[dict]:
    names = _service_names(conn)
    rows = conn.execute(
        """
        SELECT cir.service_id, cir.provider, cir.iac_resource_type, cir.logical_name, cir.physical_name,
               cir.attributes_json, cir.file_path, cir.start_line, cir.end_line
        FROM cloud_iac_resources cir
        JOIN services svc ON svc.id = cir.service_id
        WHERE cir.iac_resource_type IN ('aws_s3_bucket', 'AWS::S3::Bucket')
        ORDER BY cir.service_id, cir.logical_name
        """
    ).fetchall()
    findings: list[dict] = []
    for row in rows:
        attributes = json.loads(row["attributes_json"] or "{}")
        presence_keys = _VERSIONING_PRESENCE_KEYS[row["iac_resource_type"]]
        if any(attributes.get(key) for key in presence_keys):
            continue
        service = names[row["service_id"]]
        bucket = row["physical_name"] or row["logical_name"]
        findings.append({
            "kind": "possible_missing_bucket_versioning", "severity": "info",
            "services": [service],
            "reason": f"{service}'s bucket '{bucket}' has no source-proven versioning configuration; validate it.",
            "detail": {
                "bucket": bucket, "confidence": 0.4,
                "evidence": [_edge_evidence(row)],
                "unknowns": ["Versioning may be enabled by an account/organization default outside the indexed IaC."],
                "remediation": ["Confirm versioning is enabled, or declare it explicitly for this bucket."],
            },
        })
    return findings


_DETECTORS = (
    find_cycles, find_fan_imbalance, find_component_cycles, find_component_fan_imbalance,
    find_shared_database, find_aggregate_ownership_overlap,
    find_duplicate_external_integrations,
    find_flow_hypotheses, find_read_entrypoint_side_effects, find_error_semantics_lost,
    find_unhandled_endpoint_errors, find_internal_error_exposures,
    find_static_http_calls_without_resilience_policy,
    find_retries_on_potentially_non_idempotent_http_calls,
    find_retries_on_non_retryable_errors,
    find_retries_on_downstream_client_errors,
    find_timeout_fallbacks_masking_failures,
    find_timeouts_mapped_as_internal_server_errors,
    find_timeouts_without_local_fallback,
    find_unmapped_downstream_errors,
    find_overbroad_exception_handlers,
    find_broad_handlers_that_can_swallow_timeouts,
    find_resilience_policies_on_write_flows,
    find_retries_on_write_publish_flows,
    find_retry_write_publish_flows_with_consumers,
    find_retry_write_publish_flows_with_persistent_consumers,
    find_retry_write_publish_flows_with_unrecovered_persistent_consumers,
    find_non_atomic_service_publish_flows,
    find_message_consumers_without_recovery_policy,
    find_cloud_code_without_iac, find_cloud_iac_unused_in_code, find_shared_cloud_resource,
    find_kubernetes_configuration_key_mismatches,
    find_kubernetes_configuration_source_unknowns,
    find_kubernetes_configuration_source_import_unknowns,
    find_cloud_dead_letter_queue_missing, find_public_object_storage,
    find_unencrypted_cloud_resource, find_missing_bucket_versioning,
)


def _findings_by_identity(conn: sqlite3.Connection, run_id: int) -> dict[tuple[str, tuple[str, ...], str], dict]:
    """Give findings a stable identity across recomputed runs.

    Structural findings are scoped by kind and services. Flow hypotheses additionally
    need their entrypoint: one service can legitimately expose several independently
    risky flows, and collapsing them would hide a newly detected one in `trend`.
    """
    identified: dict[tuple[str, tuple[str, ...], str], dict] = {}
    for f in architecture_repo.list_findings(conn, run_id):
        services = tuple(sorted(json.loads(f["services_json"])))
        detail = json.loads(f["detail_json"] or "{}")
        entrypoint = json.dumps(detail.get("entrypoint"), sort_keys=True, separators=(",", ":"))
        identified[(f["kind"], services, entrypoint)] = {
            "kind": f["kind"], "services": list(services), "detail": detail,
        }
    return identified


def diff_architecture_runs(conn: sqlite3.Connection, previous_run_id: int, current_run_id: int) -> dict:
    """Pure SQL/in-memory diff between two already-computed architecture runs — no
    LLM, no re-detection: new_findings/resolved_findings by (kind, services,
    entrypoint when present) identity, plus a numeric count_deltas entry for any fan_in/fan_out finding that
    persisted across both runs but whose count changed. Recomputed for free from
    data find_architecture_smells already has to read anyway.
    """
    previous = _findings_by_identity(conn, previous_run_id)
    current = _findings_by_identity(conn, current_run_id)

    new_findings = [finding for key, finding in current.items() if key not in previous]
    resolved_findings = [finding for key, finding in previous.items() if key not in current]

    count_deltas = []
    for key, current_finding in current.items():
        if key not in previous:
            continue
        previous_count = previous[key]["detail"].get("count")
        current_count = current_finding["detail"].get("count")
        if previous_count is not None and current_count is not None and previous_count != current_count:
            count_deltas.append({
                "kind": current_finding["kind"],
                "services": current_finding["services"],
                "previous_count": previous_count,
                "current_count": current_count,
            })

    return {"new_findings": new_findings, "resolved_findings": resolved_findings, "count_deltas": count_deltas}


def recompute_architecture_view(conn: sqlite3.Connection) -> int:
    """Recomputes every structural finding from scratch and persists a new run — 100%
    deterministic SQL + graph traversal over already-indexed facts, no LLM call."""
    services_indexed = conn.execute("SELECT COUNT(*) AS n FROM services").fetchone()["n"]
    run_id = architecture_repo.start_run(conn, services_indexed)
    for detector in _DETECTORS:
        for finding in detector(conn):
            architecture_repo.record_finding(
                conn, run_id, finding["kind"], finding["severity"], finding["services"],
                finding["reason"], finding["detail"],
            )
    return run_id
