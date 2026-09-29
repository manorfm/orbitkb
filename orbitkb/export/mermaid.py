"""Mermaid diagram generation — text, not an image, so it's versionable, diffable in a
PR and renders natively in GitHub/GitLab/most editors. Generated 100% from the SQLite
index, the same way export/markdown.py is: no LLM call, no cost beyond what index/update
already paid for.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from orbitkb.db.repositories import architecture as architecture_repo
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.canonical import CanonicalSnapshot, FactStatus


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower() or "node"


def _sanitize_ident(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", text or "").strip("_") or "field"


def _cycle_service_names(conn: sqlite3.Connection) -> set[str]:
    run_id = architecture_repo.latest_run_id(conn)
    if run_id is None:
        return set()
    names: set[str] = set()
    for finding in architecture_repo.list_findings(conn, run_id):
        if finding["kind"] == "cycle":
            names.update(json.loads(finding["services_json"]))
    return names


def _neighbor_service_names(conn: sqlite3.Connection, service_id: int) -> set[str]:
    """Other services directly connected to this one, in either direction, by a
    resolved service_call or a matching publish/consume channel — external targets
    (unresolved to_service_id, or non-service message channels) never grow the
    reachable set, since they aren't a service to keep expanding from.
    """
    names = {
        row["to_service_name"] for row in service_calls_repo.list_calls_for_service(conn, service_id)
        if row["to_service_id"] is not None
    }
    names |= {row["from_service_name"] for row in service_calls_repo.list_inbound_calls(conn, service_id)}
    names |= {row["other_service"] for row in messages_repo.list_message_links(conn, service_id)}
    return names


def _reachable_service_names(conn: sqlite3.Connection, roots: set[str], hops: int) -> set[str]:
    """Every service within `hops` steps of `roots`, in either direction — a scoped
    subgraph for one service's neighborhood, so a caller isn't handed the whole
    system's topology when it only asked about one service's own context.
    """
    name_to_id = {svc["name"]: svc["id"] for svc in services_repo.list_services(conn)}
    reached = {name for name in roots if name in name_to_id}
    frontier = set(reached)
    for _ in range(max(hops, 0)):
        next_frontier: set[str] = set()
        for name in frontier:
            next_frontier |= _neighbor_service_names(conn, name_to_id[name]) - reached
        if not next_frontier:
            break
        reached |= next_frontier
        frontier = next_frontier
    return reached


def _has_confirmed_mongo_template_call(snapshot: CanonicalSnapshot) -> bool:
    receivers = {
        fact.subject.name
        for fact in snapshot.facts
        if fact.kind == "injection"
        and fact.status is FactStatus.CONFIRMED
        and fact.attributes.get("contract", "").split("<", 1)[0].rsplit(".", 1)[-1]
        in {"MongoTemplate", "ReactiveMongoTemplate"}
    }
    for fact in snapshot.facts:
        if (fact.kind != "flow_edge" or fact.status is not FactStatus.CONFIRMED
                or fact.attributes.get("boundary_kind") != "persistence"
                or fact.attributes.get("relation") not in {"reads", "writes", "invokes"}):
            continue
        target = fact.attributes.get("target")
        if not isinstance(target, str):
            continue
        owner, owner_separator, _ = fact.subject.name.rpartition(".")
        receiver, receiver_separator, _ = target.rpartition(".")
        if owner_separator and receiver_separator and f"{owner}.{receiver}" in receivers:
            return True
    return False


def generate_topology_diagram(
    conn: sqlite3.Connection, root_services: set[str] | None = None, hops: int = 1,
) -> str:
    """`graph TD` over every indexed service (or, with `root_services`, only the
    subgraph reachable within `hops` steps of them): each as a node, external
    vendors as rounded nodes, service_calls as solid edges, message links as dashed
    edges. Source-proven HTTP calls without a reconciled destination retain a
    declared target node, marked unresolved. A confirmed Redis Pub/Sub
    publication adds a broker node scoped to its producer service; the source
    does not prove a channel or shared instance. A confirmed call on an injected
    Mongo template adds a per-service MongoDB node, without assigning a collection
    or read/write direction.
    Services involved in a cycle (find_architecture_smells) are styled
    distinctly — the one piece of interpretation on top of otherwise purely
    structural facts. Cycle styling and DB nodes are scoped the same way: a service
    filtered out of the subgraph never contributes its own persistence nodes either.
    """
    cycle_names = _cycle_service_names(conn)
    included = _reachable_service_names(conn, root_services, hops) if root_services is not None else None
    lines = ["graph TD"]
    services = services_repo.list_services(conn)

    service_ids: dict[str, str] = {}
    for svc in services:
        if included is not None and svc["name"] not in included:
            continue
        node_id = f"svc_{_slug(svc['name'])}"
        service_ids[svc["name"]] = node_id
        lines.append(f'  {node_id}["{svc["name"]}"]')

    external_ids: dict[str, str] = {}

    def external_node(name: str) -> str:
        if name not in external_ids:
            node_id = f"ext_{_slug(name)}"
            external_ids[name] = node_id
            lines.append(f'  {node_id}(("{name}"))')
        return external_ids[name]

    rendered_targets: set[tuple[str, str]] = set()
    for edge in service_calls_repo.list_internal_edges(conn):
        from_id = service_ids.get(edge["from_name"])
        to_id = service_ids.get(edge["to_name"])
        if from_id is None or to_id is None:
            continue
        lines.append(f"  {from_id} -->|{edge['call_kind']}| {to_id}")
        rendered_targets.add((edge["from_name"], edge["to_name"]))

    for edge in service_calls_repo.list_external_edges(conn):
        from_id = service_ids.get(edge["from_name"])
        if from_id is None:
            continue
        target_id = external_node(edge["to_service_name"])
        label = edge["resource_type"] or "external"
        lines.append(f"  {from_id} -.->|{label}| {target_id}")
        rendered_targets.add((edge["from_name"], edge["to_service_name"]))

    for edge in service_calls_repo.list_unresolved_edges(conn):
        from_id = service_ids.get(edge["from_name"])
        if from_id is None:
            continue
        target_id = external_node(edge["to_service_name"])
        lines.append(f"  {from_id} -.->|{edge['call_kind']} (unresolved)| {target_id}")
        rendered_targets.add((edge["from_name"], edge["to_service_name"]))

    for svc in services:
        from_id = service_ids.get(svc["name"])
        if from_id is None:
            continue
        for call in flows_repo.list_static_service_calls(conn, svc["id"]):
            key = (svc["name"], call["target_service"])
            if call["protocol"] != "http" or key in rendered_targets:
                continue
            target_id = external_node(f"{call['target_service']} (declared target)")
            lines.append(f"  {from_id} -.->|http (unresolved)| {target_id}")
            rendered_targets.add(key)

    for fact in flows_repo.list_all_static_cloud_facts(conn):
        from_id = service_ids.get(fact["from_name"])
        if from_id is None:
            continue
        cloud_name = f"{fact['provider']}:{fact['service_name']}" + (
            f" {fact['target_name']}" if fact["target_name"] else ""
        )
        target_id = external_node(cloud_name)
        lines.append(f"  {from_id} -.->|{fact['resource_type']}| {target_id}")

    for link in messages_repo.list_all_message_links(conn):
        publisher_id = service_ids.get(link["publisher"])
        consumer_id = service_ids.get(link["consumer"])
        if publisher_id is None or consumer_id is None:
            continue
        lines.append(f'  {publisher_id} ==>|{link["channel"]}| {consumer_id}')

    for row in messages_repo.list_unmatched_message_channels(conn):
        from_id = service_ids.get(row["service"])
        if from_id is None:
            continue
        broker_id = external_node(f"{row['provider']}: {row['channel']}")
        if row["direction"] == "publishes":
            lines.append(f"  {from_id} -.->|{row['channel']}| {broker_id}")
        else:
            lines.append(f"  {broker_id} -.->|{row['channel']}| {from_id}")

    for svc in services:
        from_id = service_ids.get(svc["name"])
        if from_id is None:
            continue
        snapshot = snapshots_repo.read_snapshot(conn, svc["id"])
        if snapshot is not None and any(
            fact.kind == "flow_edge"
            and fact.status is FactStatus.CONFIRMED
            and fact.attributes.get("relation") == "publishes"
            and fact.attributes.get("boundary_kind") == "redis_pubsub"
            for fact in snapshot.facts
        ):
            broker_id = f"broker_{_slug(svc['name'])}_redis"
            lines.append(f'  {broker_id}[("Redis Pub/Sub")]')
            lines.append(f"  {from_id} -.->|publish| {broker_id}")
        engines = {entity["engine"] for entity in persistence_repo.list_persistence(conn, svc["id"])}
        if (snapshot is not None and _has_confirmed_mongo_template_call(snapshot)
                and not any(_slug(engine) in {"mongo", "mongodb"} for engine in engines)):
            node_id = f"db_{_slug(svc['name'])}_mongodb"
            lines.append(f'  {node_id}[("MongoDB")]')
            lines.append(f"  {from_id} -.->|accesses| {node_id}")
        for engine in sorted(engines):
            # Never shared across services — a same-named engine on two services isn't
            # evidence they're the same physical database, just the same technology.
            node_id = f"db_{_slug(svc['name'])}_{_slug(engine)}"
            lines.append(f'  {node_id}[("{engine}")]')
            lines.append(f"  {from_id} -.->|persists| {node_id}")

    if cycle_names:
        lines.append("  classDef cycle fill:#f88,stroke:#900,stroke-width:2px;")
        cycle_node_ids = ",".join(service_ids[name] for name in sorted(cycle_names) if name in service_ids)
        if cycle_node_ids:
            lines.append(f"  class {cycle_node_ids} cycle;")

    return "\n".join(lines)


def generate_entrypoint_sequence(edges: list, entrypoint_symbol: str, entrypoint_label: str) -> str:
    """Mermaid `sequenceDiagram` for one entrypoint's bounded flow -- the same
    edge rows `describe_entrypoint` already computes (`flows_repo.list_reachable_edges`,
    BFS-ordered), just rendered as a diagram instead of a flat list, no new data.
    Every persistence edge (`reads`/`writes`) collapses into one shared `DB`
    participant regardless of which table/repository it names, since a real flow
    can touch several without each needing its own lane; every other edge keeps
    its own real symbol as the participant, so the diagram shows exactly what the
    flow list does, only laid out as a sequence.
    """
    lines = ["sequenceDiagram"]
    participant_ids: dict[str, str] = {}

    def participant(label: str) -> str:
        if label not in participant_ids:
            participant_ids[label] = f"p{len(participant_ids)}"
            lines.append(f"    participant {participant_ids[label]} as {label}")
        return participant_ids[label]

    # The only symbol that ever needs a friendlier label than its own name is
    # the entrypoint itself -- every other symbol is shown exactly as resolved.
    symbol_labels = {entrypoint_symbol: entrypoint_label}
    participant(entrypoint_label)
    for edge in edges:
        from_id = participant(symbol_labels.get(edge["from_symbol"], edge["from_symbol"]))
        to_label = "DB" if edge["kind"] in {"reads", "writes"} else edge["to_symbol"]
        to_id = participant(to_label)
        lines.append(f"    {from_id}->>{to_id}: {edge['kind']}")
    return "\n".join(lines)


def generate_er_diagram(conn: sqlite3.Connection, service_name: str) -> str | None:
    """One service's `erDiagram`: an entity block per persisted table/collection, with
    its fields, plus a relationship line for every field whose LLM-inferred
    `references` names another entity present in this same diagram — a reference to
    an entity not in this diagram is skipped rather than fabricating a dangling node."""
    row = services_repo.get_service_by_name(conn, service_name)
    if row is None:
        return None
    entities = persistence_repo.list_persistence(conn, row["id"])
    entity_ids = {entity["name"]: _sanitize_ident(entity["name"]) for entity in entities}

    lines = ["erDiagram"]
    relationships: list[str] = []
    for entity in entities:
        entity_id = entity_ids[entity["name"]]
        lines.append(f"  {entity_id} {{")
        for field in json.loads(entity["schema_json"] or "[]"):
            type_token = _sanitize_ident((field.get("type_desc") or "string").split(",")[0].strip()) or "string"
            field_name = _sanitize_ident(field.get("field", "field"))
            lines.append(f"    {type_token} {field_name}")
        lines.append("  }")

        for field in json.loads(entity["schema_json"] or "[]"):
            reference = field.get("references")
            if not reference:
                continue
            target_id = entity_ids.get(reference.get("target_entity"))
            if target_id is None:
                continue
            crow_foot = "||--||" if reference.get("unique") else "}o--||"
            relationships.append(f'  {entity_id} {crow_foot} {target_id} : "{field.get("field", "field")}"')

    if relationships:
        lines.extend(relationships)
    else:
        lines.insert(1, "  %% No cross-entity relationships shown: none of the evidence resolved to one.")
    return "\n".join(lines)


def export_mermaid(conn: sqlite3.Connection, out_dir: Path, service_filter: str | None = None) -> list[Path]:
    """Writes `<out>/topology.mmd` (always) and `<out>/<service>/er.mmd` for every
    service that persists at least one entity — mirrors export/markdown.py's
    per-service directory layout."""
    written: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    if service_filter is None:
        topology_path = out_dir / "topology.mmd"
        topology_path.write_text(generate_topology_diagram(conn) + "\n", encoding="utf-8")
        written.append(topology_path)

    for svc in services_repo.list_services(conn):
        if service_filter and svc["name"] != service_filter:
            continue
        diagram = generate_er_diagram(conn, svc["name"])
        if not diagram or diagram.count("\n") <= 1:  # header lines only, no entities
            continue
        service_dir = out_dir / svc["name"]
        service_dir.mkdir(parents=True, exist_ok=True)
        er_path = service_dir / "er.mmd"
        er_path.write_text(diagram + "\n", encoding="utf-8")
        written.append(er_path)

    return written
