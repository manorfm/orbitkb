from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.models import (
    AnalysisResult,
    CloudFact,
    Evidence,
    FlowEdge,
    Injection,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.canonical import ServiceKey
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


def test_topology_escapes_indexed_names_and_channels(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(
        conn, 'orders"\n  forged["node', "/tmp/orders", "python",
    )
    api_id = apis_repo.upsert_api(conn, service_id, "GET", "/orders", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
        {"to_service_name": 'vendor"X', "call_kind": "http", "reason": "lookup",
         "data_needed": [], "purpose_kind": "other", "confidence": 0.9,
         "target_kind": "external", "resource_type": "saas"},
    ], EVIDENCE)
    messages_repo.replace_messages(conn, service_id, [
        {"direction": "publishes", "channel": "orders|created\n  forged", "provider": "rabbitmq"},
    ], EVIDENCE)

    diagram = generate_topology_diagram(conn)

    assert 'orders#quot;#10;  forged[#quot;node' in diagram
    assert "vendor#quot;X" in diagram
    assert "orders#124;created#10;  forged" in diagram
    assert '\n  forged["node' not in diagram
    assert "orders|created\n  forged" not in diagram


def test_topology_keeps_distinct_names_with_the_same_mermaid_slug(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    for name in ("billing-a", "billing_a"):
        service_id = services_repo.ensure_service(conn, name, f"/tmp/{name}", "python")
        api_id = apis_repo.upsert_api(conn, service_id, "GET", "/health", "s", "d", [], EVIDENCE)
        service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
            {"to_service_name": f"vendor {name}", "call_kind": "http", "reason": "lookup",
             "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.9,
             "target_kind": "external", "resource_type": "saas"},
        ], EVIDENCE)
        persistence_repo.replace_persistence_entities(
            conn, service_id, [{"name": "records", "kind": "sql_table", "engine": "postgres",
                                "schema_json": []}], EVIDENCE,
        )

    diagram = generate_topology_diagram(conn)

    assert 'svc_billing_a["billing-a"]' in diagram
    assert 'svc_billing_a_2["billing_a"]' in diagram
    assert 'ext_vendor_billing_a(("vendor billing-a"))' in diagram
    assert 'ext_vendor_billing_a_2(("vendor billing_a"))' in diagram
    assert 'svc_billing_a -.->|saas| ext_vendor_billing_a' in diagram
    assert 'svc_billing_a_2 -.->|saas| ext_vendor_billing_a_2' in diagram
    assert 'db_billing_a_postgres[("postgres")]' in diagram
    assert 'db_billing_a_2_postgres[("postgres")]' in diagram


def test_topology_keeps_same_named_services_in_separate_repositories(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    for repository in ("alpha", "beta"):
        repo_id = repositories_repo.ensure_repository(conn, repository, f"/tmp/{repository}")
        orders_id = services_repo.ensure_service(
            conn, "orders", f"/tmp/{repository}/orders", "python", repository_id=repo_id,
        )
        payments_id = services_repo.ensure_service(
            conn, "payments", f"/tmp/{repository}/payments", "python", repository_id=repo_id,
        )
        api_id = apis_repo.upsert_api(conn, orders_id, "POST", "/orders", "s", "d", [], EVIDENCE)
        service_calls_repo.replace_calls_for_api(conn, orders_id, api_id, [
            {"to_service_name": "payments", "call_kind": "http", "reason": "charge",
             "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.9,
             "target_kind": "unknown"},
            {"to_service_name": f"vendor-{repository}", "call_kind": "http", "reason": "notify",
             "data_needed": [], "purpose_kind": "other", "confidence": 0.9,
             "target_kind": "external", "resource_type": "saas"},
        ], EVIDENCE)
        persistence_repo.replace_persistence_entities(
            conn, orders_id, [{"name": "orders", "kind": "sql_table", "engine": "postgres",
                              "schema_json": []}], EVIDENCE,
        )
        messages_repo.replace_messages(conn, orders_id, [
            {"direction": "publishes", "channel": f"orders-{repository}", "description": "created"},
        ], EVIDENCE)
        messages_repo.replace_messages(conn, payments_id, [
            {"direction": "consumes", "channel": f"orders-{repository}", "description": "received"},
        ], EVIDENCE)
        flows_repo.replace_analysis(conn, orders_id, AnalysisResult(
            static_service_calls=[StaticServiceCall(
                "Orders.lookup", f"declared-{repository}", "http", "GET", "/items",
                Evidence("Orders.py", 1, 1),
            )],
            cloud_facts=[CloudFact(
                "aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk", repository,
                Evidence("Orders.py", 2, 2),
            )],
        ))

    diagram = generate_topology_diagram(conn)

    assert 'svc_orders["orders (alpha)"]' in diagram
    assert 'svc_orders_2["orders (beta)"]' in diagram
    assert 'svc_orders -->|http| svc_payments' in diagram
    assert 'svc_orders_2 -->|http| svc_payments_2' in diagram
    assert 'svc_orders -.->|saas| ext_vendor_alpha' in diagram
    assert 'svc_orders_2 -.->|saas| ext_vendor_beta' in diagram
    assert 'svc_orders -.->|persists| db_orders_postgres' in diagram
    assert 'svc_orders_2 -.->|persists| db_orders_2_postgres' in diagram
    assert 'svc_orders ==>|orders-alpha| svc_payments' in diagram
    assert 'svc_orders_2 ==>|orders-beta| svc_payments_2' in diagram
    assert 'svc_orders -.->|http (unresolved)| ext_declared_alpha_declared_target' in diagram
    assert 'svc_orders_2 -.->|http (unresolved)| ext_declared_beta_declared_target' in diagram
    assert 'svc_orders -.->|queue| ext_aws_sqs_alpha' in diagram
    assert 'svc_orders_2 -.->|queue| ext_aws_sqs_beta' in diagram


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

    scoped = generate_topology_diagram(conn, root_service_ids={inferred_id})
    assert "broker_orders_service_redis" not in scoped
    assert "broker_orders_service_redis" in generate_topology_diagram(
        conn, root_service_ids={proven_id}, hops=0,
    )

    flows_repo.replace_analysis(conn, proven_id, AnalysisResult())
    assert "broker_orders_service_redis" not in generate_topology_diagram(conn)


def test_topology_requires_a_confirmed_call_on_an_injected_mongo_template(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    proven_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    unused_id = services_repo.ensure_service(conn, "unused-service", "/tmp/unused", "jvm-spring")
    inferred_id = services_repo.ensure_service(conn, "maybe-service", "/tmp/maybe", "jvm-spring")
    other_id = services_repo.ensure_service(conn, "other-service", "/tmp/other", "jvm-spring")
    mongo = Injection("Store.mongo", "MongoTemplate", None, Evidence("Store.kt", 2, 2))
    proven_call = FlowEdge("Store.save", "mongo.execute", "invokes", Evidence("Store.kt", 4, 4),
                           boundary_kind="persistence")
    flows_repo.replace_analysis(conn, proven_id, AnalysisResult(injections=[mongo], edges=[proven_call]))
    flows_repo.replace_analysis(conn, unused_id, AnalysisResult(injections=[mongo]))
    flows_repo.replace_analysis(conn, inferred_id, AnalysisResult(injections=[mongo], edges=[
        FlowEdge("Store.save", "mongo.execute", "invokes", Evidence("Store.kt", 4, 4),
                 confidence="medium", boundary_kind="persistence"),
    ]))
    flows_repo.replace_analysis(conn, other_id, AnalysisResult(
        injections=[Injection("Store.mongo", "OtherClient", None, Evidence("Store.kt", 2, 2))],
        edges=[proven_call],
    ))

    diagram = generate_topology_diagram(conn)

    assert 'db_orders_service_mongodb[("MongoDB")]' in diagram
    assert 'svc_orders_service -.->|accesses| db_orders_service_mongodb' in diagram
    assert "db_unused_service_mongodb" not in diagram
    assert "db_maybe_service_mongodb" not in diagram
    assert "db_other_service_mongodb" not in diagram
    assert "mongo.execute" not in diagram

    flows_repo.replace_analysis(conn, proven_id, AnalysisResult(edges=[proven_call]))
    assert "db_orders_service_mongodb" not in generate_topology_diagram(conn)


def test_topology_does_not_duplicate_existing_mongo_engine(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(
        injections=[Injection("Store.mongo", "MongoTemplate", None, Evidence("Store.kt", 2, 2))],
        edges=[FlowEdge("Store.save", "mongo.save", "writes", Evidence("Store.kt", 4, 4),
                        boundary_kind="persistence")],
    ))
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [{"name": "orders", "kind": "document", "engine": "mongodb", "schema_json": []}], EVIDENCE,
    )

    diagram = generate_topology_diagram(conn)

    assert diagram.count("db_orders_service_mongodb") == 2  # declaration and edge


def test_topology_shows_static_http_target_without_claiming_a_resolved_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    caller_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    catalog_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "jvm-spring")
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
        conn, root_service_ids={catalog_id}, hops=0,
    )

    flows_repo.replace_analysis(conn, caller_id, AnalysisResult())
    assert "ext_catalog_service_declared_target" not in generate_topology_diagram(conn)


def test_topology_shows_canonical_http_target_without_legacy_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    caller_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    catalog_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "jvm-spring")
    snapshot = project_analysis(ServiceKey("orders-service"), AnalysisResult(static_service_calls=[
        StaticServiceCall("MenuClient.getItem", "catalog-service", "http", "GET", "/items/{id}",
                          Evidence("MenuClient.kt", 8, 9)),
    ]))
    snapshots_repo.replace_snapshot(conn, caller_id, snapshot)

    diagram = generate_topology_diagram(conn)

    assert 'ext_catalog_service_declared_target(("catalog-service (declared target)"))' in diagram
    assert diagram.count('svc_orders_service -.->|http (unresolved)| ext_catalog_service_declared_target') == 1
    assert "svc_orders_service -->|http| svc_catalog_service" not in diagram
    assert "ext_catalog_service_declared_target" not in generate_topology_diagram(
        conn, root_service_ids={catalog_id}, hops=0,
    )


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

    diagram = generate_topology_diagram(conn, root_service_ids={service_id})

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
    """`root_service_ids` + `hops` narrows the graph to one service's own neighborhood
    -- notification-service (1 hop via the order_created message link) is included,
    but a fourth, unrelated service must not leak into the scoped subgraph.
    """
    conn = open_db(tmp_path / "test.db")
    orders_id, _payments_id, _notif_id = _seed_topology(conn)
    services_repo.ensure_service(conn, "unrelated-service", "/tmp/unrelated", "python")

    diagram = generate_topology_diagram(conn, root_service_ids={orders_id}, hops=1)

    assert "orders-service" in diagram
    assert "payments-service" in diagram  # 1 hop via the http service_call
    assert "notification-service" in diagram  # 1 hop via the order_created message link
    assert "unrelated-service" not in diagram


def test_generate_topology_diagram_scoped_with_zero_hops_shows_only_the_root(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id, _payments_id, _notif_id = _seed_topology(conn)

    diagram = generate_topology_diagram(conn, root_service_ids={orders_id}, hops=0)

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


def test_topology_highlights_only_the_repository_with_a_cycle(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    for repository in ("alpha", "beta"):
        repo_id = repositories_repo.ensure_repository(conn, repository, f"/tmp/{repository}")
        a_id = services_repo.ensure_service(conn, "a", f"/tmp/{repository}/a", "python", repository_id=repo_id)
        b_id = services_repo.ensure_service(conn, "b", f"/tmp/{repository}/b", "python", repository_id=repo_id)
        if repository == "alpha":
            for source_id, target_name in ((a_id, "b"), (b_id, "a")):
                api_id = apis_repo.upsert_api(conn, source_id, "GET", "/check", "s", "d", [], EVIDENCE)
                service_calls_repo.replace_calls_for_api(conn, source_id, api_id, [
                    {"to_service_name": target_name, "call_kind": "http", "reason": "lookup",
                     "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.9,
                     "target_kind": "unknown"},
                ], EVIDENCE)
    recompute_architecture_view(conn)

    diagram = generate_topology_diagram(conn)

    assert 'svc_a["a (alpha)"]' in diagram
    assert 'svc_a_2["a (beta)"]' in diagram
    assert "  class svc_a,svc_b cycle;" in diagram
    assert "svc_a_2,svc_b_2 cycle" not in diagram


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


def test_generate_entrypoint_sequence_escapes_indexed_labels():
    edges = [{
        "from_symbol": "OrdersController.create",
        "to_symbol": "Worker; participant p8 as fake\n    p8->>p0: injected",
        "kind": "invokes; p0->>p9: injected\n    Note over p0: fake",
    }]

    diagram = generate_entrypoint_sequence(
        edges, "OrdersController.create", "POST /orders\n    participant p9 as forged",
    )

    assert diagram.splitlines() == [
        "sequenceDiagram",
        "    participant p0 as POST /orders#10;    participant p9 as forged",
        "    participant p1 as Worker#59; participant p8 as fake#10;    p8-#62;#62;p0: injected",
        "    p0->>p1: invokes#59; p0-#62;#62;p9: injected#10;    Note over p0: fake",
    ]


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

    diagram = generate_er_diagram(conn, orders_id)

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

    diagram = generate_er_diagram(conn, orders_id)

    assert 'orders }o--|| customers : "customer_id"' in diagram
    assert "No cross-entity relationships shown" not in diagram


def test_generate_er_diagram_keeps_colliding_entity_names_and_relationships_distinct(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [
            {"name": "order-items", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "legacy_code", "type_desc": "string"}]},
            {"name": "order_items", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "sku", "type_desc": "string"}]},
            {"name": "invoices", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "item_id", "type_desc": "string",
                              "references": {"target_entity": "order_items", "unique": False}}]},
        ],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, service_id)

    assert "  order_items {\n    string legacy_code\n  }" in diagram
    assert "  order_items_2 {\n    string sku\n  }" in diagram
    assert '  invoices }o--|| order_items_2 : "item_id"' in diagram


def test_generate_er_diagram_sanitizes_relationship_field_labels(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    injected_field = 'customer_id"\n  forged ||--|| orders : "invented'
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [
            {"name": "orders", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": injected_field, "type_desc": "string",
                              "references": {"target_entity": "customers", "unique": False}}]},
            {"name": "customers", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "id", "type_desc": "string"}]},
        ],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, service_id)

    assert 'orders }o--|| customers : "customer_id_forged_orders_invented"' in diagram
    assert "\n  forged ||--||" not in diagram
    assert diagram.count("}o--||") == 1


def test_generate_er_diagram_keeps_colliding_field_names_distinct(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [
            {"name": "orders", "kind": "sql_table", "engine": "postgres",
             "schema_json": [
                 {"field": "customer-id", "type_desc": "string"},
                 {"field": "customer_id", "type_desc": "string",
                  "references": {"target_entity": "customers", "unique": False}},
             ]},
            {"name": "customers", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "id", "type_desc": "string"}]},
        ],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, service_id)

    assert "  orders {\n    string customer_id\n    string customer_id_2\n  }" in diagram
    assert '  orders }o--|| customers : "customer_id_2"' in diagram


def test_generate_er_diagram_uses_valid_attribute_tokens_for_numeric_names(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [
            {"name": "orders", "kind": "sql_table", "engine": "postgres",
             "schema_json": [
                 {"field": "1st_item", "type_desc": "64bit, item identifier",
                  "references": {"target_entity": "items", "unique": False}},
                 {"field": "field_1st_item", "type_desc": "string"},
             ]},
            {"name": "items", "kind": "sql_table", "engine": "postgres",
             "schema_json": [{"field": "id", "type_desc": "string"}]},
        ],
        EVIDENCE,
    )

    diagram = generate_er_diagram(conn, service_id)

    assert "  orders {\n    type_64bit field_1st_item\n    string field_1st_item_2\n  }" in diagram
    assert '  orders }o--|| items : "field_1st_item"' in diagram


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

    diagram = generate_er_diagram(conn, orders_id)

    assert "customers" not in diagram  # never fabricate a node for an unresolved reference
    assert "No cross-entity relationships shown" in diagram


def test_generate_er_diagram_for_unknown_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    assert generate_er_diagram(conn, -1) is None


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
