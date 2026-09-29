from pathlib import Path

from orbitkb.analysis.models import (
    AnalysisResult,
    CloudFact,
    Evidence,
    FlowEdge,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.mermaid import (
    export_mermaid,
    generate_entrypoint_sequence,
    generate_er_diagram,
    generate_topology_diagram,
)
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


def test_topology_shows_only_confirmed_redis_publishers_without_a_channel(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    proven_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    inferred_id = services_repo.ensure_service(conn, "maybe-service", "/tmp/maybe", "jvm-spring")
    flows_repo.replace_analysis(conn, proven_id, AnalysisResult(edges=[
        FlowEdge("Publisher.send", "redis.convertAndSend", "publishes", Evidence("Publisher.kt", 4, 4),
                 boundary_kind="redis_pubsub"),
        FlowEdge("Other.send", "other.convertAndSend", "invokes", Evidence("Other.kt", 5, 5)),
    ]))
    flows_repo.replace_analysis(conn, inferred_id, AnalysisResult(edges=[
        FlowEdge("Publisher.send", "redis.convertAndSend", "publishes", Evidence("Publisher.kt", 4, 4),
                 confidence="medium", boundary_kind="redis_pubsub"),
    ]))

    diagram = generate_topology_diagram(conn)

    assert 'broker_orders_service_redis[("Redis Pub/Sub")]' in diagram
    assert 'svc_orders_service -.->|publish| broker_orders_service_redis' in diagram
    assert "broker_maybe_service_redis" not in diagram
    assert "redis.convertAndSend" not in diagram
    assert "unknown" not in diagram

    scoped = generate_topology_diagram(conn, root_services={"maybe-service"})
    assert "broker_orders_service_redis" not in scoped
    assert "broker_orders_service_redis" in generate_topology_diagram(
        conn, root_services={"orders-service"}, hops=0,
    )

    flows_repo.replace_analysis(conn, proven_id, AnalysisResult())
    assert "broker_orders_service_redis" not in generate_topology_diagram(conn)


def test_topology_shows_static_http_target_without_claiming_a_resolved_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    caller_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "jvm-spring")
    flows_repo.replace_analysis(conn, caller_id, AnalysisResult(static_service_calls=[
        StaticServiceCall("MenuClient.getItem", "catalog-service", "http", "GET", "/items/{id}",
                          Evidence("MenuClient.kt", 8, 9)),
        StaticServiceCall("MenuClient.getIngredient", "catalog-service", "http", "GET", "/ingredients/{id}",
                          Evidence("MenuClient.kt", 12, 13)),
    ]))

    diagram = generate_topology_diagram(conn)

    assert 'ext_catalog_service_declared_target(("catalog-service (declared target)"))' in diagram
    assert diagram.count('svc_orders_service -.->|http (unresolved)| ext_catalog_service_declared_target') == 1
    assert "svc_orders_service -->|http| svc_catalog_service" not in diagram
    assert "ext_catalog_service_declared_target" not in generate_topology_diagram(
        conn, root_services={"catalog-service"}, hops=0,
    )

    flows_repo.replace_analysis(conn, caller_id, AnalysisResult())
    assert "ext_catalog_service_declared_target" not in generate_topology_diagram(conn)


def test_topology_does_not_duplicate_a_reconciled_http_target(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    caller_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "jvm-spring")
    flows_repo.replace_analysis(conn, caller_id, AnalysisResult(static_service_calls=[
        StaticServiceCall("MenuClient.getItem", "catalog-service", "http", "GET", "/items/{id}",
                          Evidence("MenuClient.kt", 8, 9)),
    ]))
    api_id = apis_repo.upsert_api(conn, caller_id, "GET", "/orders", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(conn, caller_id, api_id, [
        {"to_service_name": "catalog-service", "call_kind": "http", "target_kind": "unknown"},
    ], EVIDENCE)

    diagram = generate_topology_diagram(conn)

    assert 'svc_orders_service -->|http| svc_catalog_service' in diagram
    assert "ext_catalog_service_declared_target" not in diagram


def test_generate_topology_diagram_keeps_unresolved_indexed_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    api_id = apis_repo.upsert_api(conn, service_id, "GET", "/menus", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(
        conn, service_id, api_id,
        [{"to_service_name": "RestaurantClient", "call_kind": "http", "reason": "lookup",
          "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.6,
          "target_kind": "unknown"}], EVIDENCE,
    )

    diagram = generate_topology_diagram(conn, root_services={"menu-service"})

    assert 'ext_restaurantclient(("RestaurantClient"))' in diagram
    assert 'svc_menu_service -.->|http (unresolved)| ext_restaurantclient' in diagram


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


def test_generate_topology_diagram_scoped_to_one_service_excludes_unrelated_ones(tmp_path: Path):
    """`root_services` + `hops` narrows the graph to one service's own neighborhood
    -- notification-service (1 hop via the order_created message link) is included,
    but a fourth, unrelated service must not leak into the scoped subgraph.
    """
    conn = open_db(tmp_path / "test.db")
    _seed_topology(conn)
    services_repo.ensure_service(conn, "unrelated-service", "/tmp/unrelated", "python")

    diagram = generate_topology_diagram(conn, root_services={"orders-service"}, hops=1)

    assert "orders-service" in diagram
    assert "payments-service" in diagram  # 1 hop via the http service_call
    assert "notification-service" in diagram  # 1 hop via the order_created message link
    assert "unrelated-service" not in diagram


def test_generate_topology_diagram_scoped_with_zero_hops_shows_only_the_root(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    _seed_topology(conn)

    diagram = generate_topology_diagram(conn, root_services={"orders-service"}, hops=0)

    assert "orders-service" in diagram
    assert "payments-service" not in diagram
    assert "notification-service" not in diagram


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


def test_generate_entrypoint_sequence_renders_calls_reads_and_writes_and_publishes():
    edges = [
        {"from_symbol": "OrdersController.create", "to_symbol": "OrdersService.create", "kind": "invokes"},
        {"from_symbol": "OrdersService.create", "to_symbol": "OrderRepository.save", "kind": "writes"},
        {"from_symbol": "OrdersService.create", "to_symbol": "kafka:order.created", "kind": "publishes"},
    ]

    diagram = generate_entrypoint_sequence(edges, "OrdersController.create", "POST /orders")

    assert diagram == "\n".join([
        "sequenceDiagram",
        "    participant p0 as POST /orders",
        "    participant p1 as OrdersService.create",
        "    p0->>p1: invokes",
        "    participant p2 as DB",
        "    p1->>p2: writes",
        "    participant p3 as kafka:order.created",
        "    p1->>p3: publishes",
    ])


def test_generate_entrypoint_sequence_reuses_one_db_participant_for_every_persistence_edge():
    edges = [
        {"from_symbol": "OrdersService.create", "to_symbol": "OrderRepository.save", "kind": "writes"},
        {"from_symbol": "OrdersService.create", "to_symbol": "OrderRepository.find", "kind": "reads"},
    ]

    diagram = generate_entrypoint_sequence(edges, "OrdersService.create", "POST /orders")

    assert diagram.count("participant p1 as DB") == 1
    assert diagram.count("->>p1:") == 2


def test_generate_entrypoint_sequence_with_no_edges_has_only_the_entrypoint_participant():
    diagram = generate_entrypoint_sequence([], "OrdersController.list", "GET /orders")

    assert diagram == "sequenceDiagram\n    participant p0 as GET /orders"


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
