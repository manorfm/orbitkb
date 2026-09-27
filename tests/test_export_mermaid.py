from pathlib import Path

from orbitkb.analysis.models import AnalysisResult, CloudFact, Evidence
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.mermaid import export_mermaid, generate_er_diagram, generate_topology_diagram
from orbitkb.generation.architecture import recompute_architecture_view

EVIDENCE = [{"file": "main.py", "start_line": 1, "end_line": 5}]


def _seed_topology(conn):
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    payments_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "node-ts")
    notif_id = services_repo.ensure_service(conn, "notification-service", "/tmp/notif", "python")
    api_id = apis_repo.upsert_api(conn, orders_id, "POST", "/orders", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(
        conn, orders_id, api_id,
        [
            {"to_service_name": "payments-service", "call_kind": "http", "reason": "charge",
             "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.9, "target_kind": "unknown"},
            {"to_service_name": "Stripe API", "call_kind": "http", "reason": "vendor charge",
             "data_needed": [], "purpose_kind": "other", "confidence": 0.8, "target_kind": "external",
             "resource_type": "saas"},
        ],
        EVIDENCE,
    )
    service_calls_repo.reconcile_service_call_targets(conn)
    messages_repo.replace_messages(
        conn, orders_id, [{"direction": "publishes", "channel": "order_created", "shape_json": {}, "description": "d"}], EVIDENCE,
    )
    messages_repo.replace_messages(
        conn, notif_id, [{"direction": "consumes", "channel": "order_created", "shape_json": {}, "description": "d"}], EVIDENCE,
    )
    return orders_id, payments_id, notif_id


def test_generate_topology_diagram_includes_services_and_edges(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    _seed_topology(conn)

    diagram = generate_topology_diagram(conn)

    assert diagram.startswith("graph TD")
    assert "orders-service" in diagram
    assert "payments-service" in diagram
    assert "notification-service" in diagram
    assert "Stripe API" in diagram
    assert "order_created" in diagram


def test_generate_topology_diagram_includes_cloud_nodes(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id, _payments_id, _notif_id = _seed_topology(conn)
    flows_repo.replace_analysis(conn, orders_id, AnalysisResult(cloud_facts=[
        CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk-js-v3", "orders-queue", Evidence("a.ts", 1, 1)),
    ]))

    diagram = generate_topology_diagram(conn)

    assert "sqs" in diagram
    assert "orders-queue" in diagram


def test_generate_topology_diagram_shows_unmatched_messaging_and_persistence(tmp_path: Path):
    """A single-service repo that both publishes and consumes on the same channel (no
    other indexed service on it) and persists to a DB shouldn't render an empty
    topology just because there's no second service to pair the channel with."""
    conn = open_db(tmp_path / "test.db")
    lone_id = services_repo.ensure_service(conn, "lone-service", "/tmp/lone", "jvm-spring")
    messages_repo.replace_messages(
        conn, lone_id,
        [
            {"direction": "publishes", "channel": "order.created", "shape_json": {}, "description": "d", "provider": "rabbitmq"},
            {"direction": "consumes", "channel": "order.created", "shape_json": {}, "description": "d", "provider": "rabbitmq"},
        ],
        EVIDENCE,
    )
    persistence_repo.replace_persistence_entities(
        conn, lone_id,
        [{"name": "orders", "kind": "sql_table", "engine": "postgres", "schema_json": []}],
        EVIDENCE,
    )

    diagram = generate_topology_diagram(conn)

    assert "rabbitmq: order.created" in diagram
    assert diagram.count("order.created") >= 2  # one edge in each direction
    assert 'db_lone_service_postgres[("postgres")]' in diagram
    assert "persists" in diagram


def test_generate_topology_diagram_highlights_cycle_services(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    a_id = services_repo.ensure_service(conn, "a-service", "/tmp/a", "python")
    b_id = services_repo.ensure_service(conn, "b-service", "/tmp/b", "python")
    a_api = apis_repo.upsert_api(conn, a_id, "GET", "/a", "s", "d", [], EVIDENCE)
    b_api = apis_repo.upsert_api(conn, b_id, "GET", "/b", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(
        conn, a_id, a_api,
        [{"to_service_name": "b-service", "call_kind": "http", "reason": "r", "data_needed": [],
          "purpose_kind": "other", "confidence": 0.9, "target_kind": "unknown"}],
        EVIDENCE,
    )
    service_calls_repo.replace_calls_for_api(
        conn, b_id, b_api,
        [{"to_service_name": "a-service", "call_kind": "http", "reason": "r", "data_needed": [],
          "purpose_kind": "other", "confidence": 0.9, "target_kind": "unknown"}],
        EVIDENCE,
    )
    service_calls_repo.reconcile_service_call_targets(conn)
    recompute_architecture_view(conn)

    diagram = generate_topology_diagram(conn)

    assert "classDef cycle" in diagram
    assert "cycle" in diagram.lower()


def test_generate_er_diagram_lists_entity_fields(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, orders_id,
        [{
            "name": "orders", "kind": "sql_table", "engine": "postgres",
            "schema_json": [{"field": "order_id", "type_desc": "string, order id"}, {"field": "total", "type_desc": "number"}],
        }],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, "orders-service")

    assert diagram.startswith("erDiagram")
    assert "orders {" in diagram
    assert "order_id" in diagram
    assert "total" in diagram


def test_generate_er_diagram_includes_relationship_lines_from_field_references(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, orders_id,
        [
            {
                "name": "orders", "kind": "sql_table", "engine": "postgres",
                "schema_json": [
                    {"field": "order_id", "type_desc": "string, order id"},
                    {
                        "field": "customer_id", "type_desc": "string, foreign key",
                        "references": {"target_entity": "customers", "unique": False},
                    },
                ],
            },
            {
                "name": "customers", "kind": "sql_table", "engine": "postgres",
                "schema_json": [{"field": "id", "type_desc": "string, primary key"}],
            },
        ],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, "orders-service")

    assert 'orders }o--|| customers : "customer_id"' in diagram
    assert "No cross-entity relationships shown" not in diagram


def test_generate_er_diagram_skips_a_reference_to_an_entity_not_in_this_diagram(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, orders_id,
        [{
            "name": "orders", "kind": "sql_table", "engine": "postgres",
            "schema_json": [{
                "field": "customer_id", "type_desc": "string",
                "references": {"target_entity": "customers", "unique": False},
            }],
        }],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, "orders-service")

    assert "customers" not in diagram  # never fabricate a node for an unresolved reference
    assert "No cross-entity relationships shown" in diagram


def test_generate_er_diagram_for_unknown_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    assert generate_er_diagram(conn, "does-not-exist") is None


def test_export_mermaid_writes_topology_and_er_files(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id, _payments_id, _notif_id = _seed_topology(conn)
    persistence_repo.replace_persistence_entities(
        conn, orders_id,
        [{"name": "orders", "kind": "sql_table", "engine": "postgres",
          "schema_json": [{"field": "order_id", "type_desc": "string"}]}],
        EVIDENCE,
    )
    out_dir = tmp_path / "docs"

    written = export_mermaid(conn, out_dir)

    assert out_dir / "topology.mmd" in written
    assert (out_dir / "topology.mmd").exists()
    assert (out_dir / "orders-service" / "er.mmd").exists()
    assert not (out_dir / "payments-service").exists()  # no persistence, nothing written
