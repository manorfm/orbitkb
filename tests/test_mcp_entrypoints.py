import sqlite3
from pathlib import Path

import pytest

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    ErrorContract,
    Evidence,
    FlowBoundary,
    FlowEdge,
    MessageContract,
    ResiliencePolicy,
    SecurityRequirement,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots, flows, repositories, services
from orbitkb.mcp import queries


def test_entrypoint_tools_keep_transport_and_flow_context_separate(tmp_path):
    conn = open_db(tmp_path / "entrypoints.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "node-ts")
    evidence = Evidence("resolvers.ts", 8, 12)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("graphql", "MUTATION", "createOrder", "Mutation.createOrder", evidence)],
            edges=[FlowEdge("Mutation.createOrder", "orders.create", "writes", evidence)],
        ),
    )

    listing = queries.list_entrypoints(conn, "checkout")
    detail = queries.describe_entrypoint(conn, "checkout", "graphql", "mutation", "createOrder")

    assert listing["entrypoints"] == [
        {
            "kind": "graphql", "method": "MUTATION", "name": "createOrder", "symbol": "Mutation.createOrder",
            "evidence": {"file": "resolvers.ts", "start_line": 8, "end_line": 12},
        }
    ]
    assert detail["flow"][0]["kind"] == "writes"
    assert detail["flow"][0]["origin"] == "static"


def test_describe_entrypoint_returns_the_reachable_bounded_flow(tmp_path):
    conn = open_db(tmp_path / "reachable-flow.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("OrdersController.kt", 8, 12)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "OrdersController.create", evidence)],
            edges=[
                FlowEdge("OrdersController.create", "CreateOrderUseCase.execute", "invokes", evidence),
                FlowEdge("CreateOrderUseCase.execute", "orderRepository.save", "writes", evidence),
            ],
            boundaries=[FlowBoundary("CreateOrderUseCase.execute", "transaction", evidence)],
        ),
    )

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert [(edge["from"], edge["to"]) for edge in detail["flow"]] == [
        ("OrdersController.create", "CreateOrderUseCase.execute"),
        ("CreateOrderUseCase.execute", "orderRepository.save"),
    ]
    assert detail["boundaries"][0]["kind"] == "transaction"
    assert detail["sequence_mermaid"] == "\n".join([
        "sequenceDiagram",
        "    participant p0 as POST /orders",
        "    participant p1 as CreateOrderUseCase.execute",
        "    p0->>p1: invokes",
        "    participant p2 as DB",
        "    p1->>p2: writes",
    ])


def test_describe_entrypoint_reports_only_reachable_message_destinations(tmp_path):
    conn = open_db(tmp_path / "message-operations.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-js")
    publish_evidence = Evidence("events.js", 7, 7)
    unrelated_evidence = Evidence("events.js", 12, 12)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[
            EntryPoint("http", "POST", "/orders", "Orders.create", Evidence("routes.js", 1, 1)),
            EntryPoint("http", "POST", "/cancel", "Orders.cancel", Evidence("routes.js", 2, 2)),
        ],
        edges=[
            FlowEdge("Orders.create", "publish", "invokes", Evidence("routes.js", 3, 3)),
            FlowEdge("publish", "producer.send", "publishes", publish_evidence),
            FlowEdge("Orders.cancel", "otherPublish", "invokes", Evidence("routes.js", 4, 4)),
            FlowEdge("otherPublish", "producer.send", "publishes", unrelated_evidence),
        ],
        message_contracts=[
            MessageContract("publishes", "orders.created", None, None, publish_evidence),
            MessageContract("publishes", "orders.cancelled", None, None, unrelated_evidence),
        ],
    ))

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert [(item["source"], item["target"], item["channel"], item["status"])
            for item in detail["message_operations"]] == [
        ("publish", "producer.send", "orders.created", "confirmed"),
    ]


def test_describe_entrypoint_uses_a_proven_contract_when_call_name_is_generic(tmp_path):
    conn = open_db(tmp_path / "generic-message-call.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-js")
    evidence = Evidence("events.js", 7, 7)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "Orders.create", Evidence("routes.js", 1, 1))],
        edges=[FlowEdge("Orders.create", "Events.emit", "invokes", evidence)],
        message_contracts=[MessageContract("publishes", "orders.created", None, None, evidence)],
    ))

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert [(item["target"], item["direction"], item["channel"], item["status"])
            for item in detail["message_operations"]] == [
        ("Events.emit", "publishes", "orders.created", "confirmed"),
    ]


def test_describe_entrypoint_does_not_assign_a_channel_to_ambiguous_same_line_calls(tmp_path):
    conn = open_db(tmp_path / "ambiguous-messages.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-js")
    evidence = Evidence("events.js", 7, 7)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "Orders.create", Evidence("routes.js", 1, 1))],
        edges=[
            FlowEdge("Orders.create", "producer.send", "publishes", evidence),
            FlowEdge("Orders.create", "audit.send", "publishes", evidence),
        ],
        message_contracts=[MessageContract("publishes", "orders.created", None, None, evidence)],
    ))

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert {item["target"] for item in detail["message_operations"]} == {"producer.send", "audit.send"}
    assert all(item["status"] == "unknown" and item["channel"] is None
               for item in detail["message_operations"])


def test_describe_entrypoint_navigates_the_persisted_canonical_snapshot(tmp_path):
    conn = open_db(tmp_path / "canonical-flow.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("OrdersController.kt", 8, 12)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "OrdersController.create", evidence)],
        edges=[
            FlowEdge("OrdersController.create", "CreateOrder.execute", "invokes", evidence),
            FlowEdge("CreateOrder.execute", "Repository.save", "writes", evidence),
        ],
    ))
    assert canonical_snapshots.read_snapshot(conn, service_id) is not None
    conn.execute("DELETE FROM flow_edges WHERE service_id = ?", (service_id,))

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert [(edge["from"], edge["to"]) for edge in detail["flow"]] == [
        ("OrdersController.create", "CreateOrder.execute"),
        ("CreateOrder.execute", "Repository.save"),
    ]
    assert detail["flow_pagination"] == {"max_edges": 50, "truncated": False}


def test_describe_entrypoint_uses_canonical_attached_facts(tmp_path):
    conn = open_db(tmp_path / "canonical-attached.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("CheckoutService.kt", 12, 15)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "Checkout.create", evidence,
                                contract={"request": "Order"})],
        edges=[FlowEdge("Checkout.create", "CheckoutService.run", "invokes", evidence)],
        boundaries=[FlowBoundary("CheckoutService.run", "transaction", evidence)],
        error_contracts=[ErrorContract(
            "CheckoutService.run", "maps", "conflict", "StockError", "http", "409", "OUT_OF_STOCK",
            False, "not_retryable", evidence,
        )],
        static_service_calls=[StaticServiceCall(
            "CheckoutService.run", "inventory", "http", "POST", "/reservations", evidence,
        )],
        resilience_policies=[ResiliencePolicy(
            "CheckoutService.run", "timeout", "reactor", 2000, "milliseconds", evidence,
        )],
    ))
    for table in ("flow_boundaries", "static_error_contracts", "static_service_calls",
                  "static_resilience_policies", "entrypoint_contracts"):
        conn.execute(f"DELETE FROM {table}")

    detail = queries.describe_entrypoint(conn, "checkout", "http", "post", "/orders")

    assert detail["contract"] == {"request": "Order"}
    assert detail["boundaries"] == [{
        "source": "CheckoutService.run", "kind": "transaction",
        "evidence": {"file": "CheckoutService.kt", "start_line": 12, "end_line": 15},
    }]
    assert [(item["source"], item["transport_code"]) for item in detail["error_contracts"]] == [
        ("CheckoutService.run", "409"),
    ]
    assert [(item["source"], item["target_service"], item["resolved_target"]["status"])
            for item in detail["service_calls"]] == [("CheckoutService.run", "inventory", "not_indexed")]
    assert [(item["source"], item["kind"]) for item in detail["resilience_policies"]] == [
        ("CheckoutService.run", "timeout"),
    ]


def test_describe_entrypoint_uses_language_neutral_navigation_for_kotlin_and_go(tmp_path):
    go_root = tmp_path / "catalog-go"
    go_root.mkdir()
    (go_root / "main.go").write_text(
        'package main\ntype Items struct{}\nfunc (i *Items) List() {}\n'
        'func main() { router.GET("/items", items.List) }\n'
    )
    kotlin_root = Path(__file__).resolve().parents[1] / "verify/language_corpus/menu-kotlin-service"
    conn = open_db(tmp_path / "language-neutral.db")
    for name, stack, root in (("catalog-go", "go", go_root), ("menu-kotlin", "jvm-spring", kotlin_root)):
        service_id = services.ensure_service(conn, name, str(root), stack)
        analysis = StaticAnalysisEngine().analyze(root, stack)
        flows.replace_analysis(conn, service_id, analysis)
        entry = analysis.entrypoints[0]

        detail = queries.describe_entrypoint(conn, name, entry.kind, entry.method, entry.name)

        assert "error" not in detail
        assert detail["entrypoint"]["symbol"] == entry.symbol
        assert [(edge["from"], edge["to"]) for edge in detail["flow"]] == [
            (edge.source, edge.target) for edge in analysis.edges if edge.source == entry.symbol
        ]


def test_invalid_canonical_projection_keeps_previous_static_analysis(tmp_path):
    conn = open_db(tmp_path / "atomic-projection.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("Orders.kt", 1, 2)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "GET", "/orders", "Orders.list", evidence)],
    ))

    with pytest.raises(ValueError, match="security requirement subject"):
        flows.replace_analysis(conn, service_id, AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "Orders.create", evidence)],
            security_requirements=[SecurityRequirement("/orders", "POST", "Orders.create", "hasRole",
                                                       ("ADMIN",), evidence)],
        ))

    assert flows.get_entrypoint(conn, service_id, "http", "GET", "/orders") is not None
    assert flows.get_entrypoint(conn, service_id, "http", "POST", "/orders") is None
    assert len(canonical_snapshots.read_snapshot(conn, service_id).facts) == 1


def test_static_write_failure_rolls_back_flow_and_canonical_snapshot(tmp_path):
    conn = open_db(tmp_path / "atomic-write.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("Orders.kt", 1, 2)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "GET", "/orders", "Orders.list", evidence)],
    ))
    conn.execute("""CREATE TRIGGER block_entrypoint BEFORE INSERT ON entrypoints
                    WHEN NEW.name = '/blocked' BEGIN SELECT RAISE(ABORT, 'blocked'); END""")

    with pytest.raises(sqlite3.IntegrityError, match="blocked"):
        flows.replace_analysis(conn, service_id, AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/blocked", "Orders.blocked", evidence)],
        ))

    assert flows.get_entrypoint(conn, service_id, "http", "GET", "/orders") is not None
    assert flows.get_entrypoint(conn, service_id, "http", "POST", "/blocked") is None
    assert canonical_snapshots.read_snapshot(conn, service_id).facts[0].subject.name == "/orders"


def test_describe_entrypoint_includes_a_deterministic_graphql_contract(tmp_path):
    conn = open_db(tmp_path / "graphql-contract.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "node-ts")
    evidence = Evidence("schema.graphql", 1, 1)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("graphql", "MUTATION", "checkout", "Mutation.checkout", evidence)],
            contracts={"Mutation.checkout": {"arguments": [], "returns": {"type": "Receipt", "required": True}}},
        ),
    )

    detail = queries.describe_entrypoint(conn, "checkout", "graphql", "mutation", "checkout")

    assert detail["contract"] == {"arguments": [], "returns": {"type": "Receipt", "required": True}}


def test_describe_entrypoint_keeps_route_middleware_with_its_exact_route(tmp_path):
    conn = open_db(tmp_path / "route-middleware-contract.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-ts")
    evidence = Evidence("orders.ts", 8, 8)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[
                EntryPoint(
                    "http", "POST", "/orders", "orders.create", evidence,
                    contract={"route_middlewares": [{"symbol": "requireAuthentication"}]},
                ),
                EntryPoint(
                    "http", "POST", "/internal/orders", "orders.create", evidence,
                    contract={"route_middlewares": [{"symbol": "requireEmployee"}]},
                ),
            ],
        ),
    )

    public = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")
    internal = queries.describe_entrypoint(conn, "orders", "http", "post", "/internal/orders")

    assert public["contract"] == {"route_middlewares": [{"symbol": "requireAuthentication"}]}
    assert internal["contract"] == {"route_middlewares": [{"symbol": "requireEmployee"}]}


def test_describe_entrypoint_includes_only_reachable_static_error_contracts(tmp_path):
    conn = open_db(tmp_path / "error-contracts.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("OrdersController.java", 12, 15)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "OrdersController.create", evidence)],
            edges=[FlowEdge("OrdersController.create", "CreateOrder.execute", "invokes", evidence)],
            error_contracts=[
                ErrorContract(
                    source="CreateOrder.execute", role="raises", error_kind="conflict",
                    internal_type="InsufficientStockException", protocol="internal", transport_code=None,
                    public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=evidence,
                ),
                ErrorContract(
                    source="Unrelated.reconcile", role="raises", error_kind="dependency",
                    internal_type="PartnerUnavailable", protocol="internal", transport_code=None,
                    public_code=None, exposes_internal_detail=False, retryability="retryable", evidence=evidence,
                ),
            ],
        ),
    )

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders")

    assert detail["error_contracts"] == [{
        "source": "CreateOrder.execute", "role": "raises", "error_kind": "conflict",
        "internal_type": "InsufficientStockException", "protocol": "internal", "transport_code": None,
        "public_code": None, "exposes_internal_detail": False, "retryability": "not_retryable",
        "evidence": {"file": "OrdersController.java", "start_line": 12, "end_line": 15},
    }]


def test_describe_error_flow_returns_a_proven_downstream_409_mapping(tmp_path):
    conn = open_db(tmp_path / "error-flow.db")
    checkout = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    inventory = services.ensure_service(conn, "inventory", "/repos/inventory", "jvm-spring")
    checkout_evidence = Evidence("CheckoutService.java", 20, 24)
    inventory_evidence = Evidence("InventoryController.java", 14, 18)
    flows.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", checkout_evidence)],
            edges=[FlowEdge("CheckoutController.create", "CheckoutService.checkout", "invokes", checkout_evidence)],
            boundaries=[FlowBoundary("CheckoutService.checkout", "transaction", checkout_evidence)],
            static_service_calls=[StaticServiceCall(
                source="CheckoutService.checkout", target_service="inventory", protocol="http",
                target_method="POST", target_path="/reservations", evidence=checkout_evidence,
            )],
            error_contracts=[ErrorContract(
                source="CheckoutService.checkout", role="maps", error_kind="conflict",
                internal_type="InsufficientStock", protocol="http", transport_code="409",
                public_code="OUT_OF_STOCK", exposes_internal_detail=False,
                retryability="not_retryable", evidence=checkout_evidence,
            )],
        ),
    )
    flows.replace_analysis(
        conn,
        inventory,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/reservations", "InventoryController.reserve", inventory_evidence)],
            error_contracts=[ErrorContract(
                source="InventoryController.reserve", role="maps", error_kind="conflict",
                internal_type="InsufficientStock", protocol="http", transport_code="409",
                public_code="OUT_OF_STOCK", exposes_internal_detail=False,
                retryability="not_retryable", evidence=inventory_evidence,
            )],
        ),
    )
    for table in ("flow_edges", "static_error_contracts", "static_service_calls"):
        conn.execute(f"DELETE FROM {table}")

    result = queries.describe_error_flow(conn, "checkout", "http", "post", "/orders")

    assert result == {
        "service": "checkout",
        "repository": None,
        "entrypoint": {"kind": "http", "method": "POST", "name": "/orders", "symbol": "CheckoutController.create"},
        "error_flows": [{
            "origin": {
                "service": "inventory", "symbol": "InventoryController.reserve",
                "transport": {"protocol": "http", "status": "409", "public_code": "OUT_OF_STOCK"},
                "evidence": {"file": "InventoryController.java", "start_line": 14, "end_line": 18},
            },
            "handling": [{
                "service": "checkout", "symbol": "CheckoutService.checkout", "action": "maps_to_http_409",
                "evidence": {"file": "CheckoutService.java", "start_line": 20, "end_line": 24},
            }],
            "outcome": {"protocol": "http", "status": "409", "public_code": "OUT_OF_STOCK"},
            "confidence": 1.0,
            "evidence": [
                {"file": "CheckoutService.java", "start_line": 20, "end_line": 24},
                {"file": "InventoryController.java", "start_line": 14, "end_line": 18},
            ],
        }],
        "unknowns": [],
    }


def test_describe_error_flow_keeps_a_proven_409_to_500_translation_visible(tmp_path):
    conn = open_db(tmp_path / "degraded-error-flow.db")
    checkout = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    inventory = services.ensure_service(conn, "inventory", "/repos/inventory", "jvm-spring")
    evidence = Evidence("CheckoutService.java", 20, 24)
    flows.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", evidence)],
            edges=[FlowEdge("CheckoutController.create", "CheckoutService.checkout", "invokes", evidence)],
            static_service_calls=[StaticServiceCall(
                source="CheckoutService.checkout", target_service="inventory", protocol="http",
                target_method="POST", target_path="/reservations", evidence=evidence,
            )],
            error_contracts=[ErrorContract(
                source="CheckoutService.checkout", role="maps", error_kind="conflict",
                internal_type="InsufficientStock", protocol="http", transport_code="500",
                public_code="INTERNAL_ERROR", exposes_internal_detail=False,
                retryability="unknown", evidence=evidence,
            )],
        ),
    )
    flows.replace_analysis(
        conn,
        inventory,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/reservations", "InventoryController.reserve", evidence)],
            error_contracts=[ErrorContract(
                source="InventoryController.reserve", role="maps", error_kind="conflict",
                internal_type="InsufficientStock", protocol="http", transport_code="409",
                public_code="OUT_OF_STOCK", exposes_internal_detail=False,
                retryability="not_retryable", evidence=evidence,
            )],
        ),
    )

    result = queries.describe_error_flow(conn, "checkout", "http", "post", "/orders")

    assert result["error_flows"][0]["origin"]["transport"]["status"] == "409"
    assert result["error_flows"][0]["handling"][0]["action"] == "maps_to_http_500"
    assert result["error_flows"][0]["outcome"] == {
        "protocol": "http", "status": "500", "public_code": "INTERNAL_ERROR",
    }
    assert result["unknowns"] == []


def test_describe_error_flow_exposes_unresolved_flow_boundaries(tmp_path):
    conn = open_db(tmp_path / "unknown-error-flow.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("Checkout.kt", 4, 5)
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "Checkout.create", evidence)],
        edges=[FlowEdge("Checkout.create", "DynamicClient.call", "invokes", evidence)],
        boundaries=[FlowBoundary("Checkout.create", "async", evidence)],
    ))

    result = queries.describe_error_flow(conn, "checkout", "http", "post", "/orders")

    assert result["error_flows"] == []
    assert any("DynamicClient.call" in item and "unresolved" in item for item in result["unknowns"])
    assert any("async" in item for item in result["unknowns"])


def test_describe_error_flow_reports_incomplete_bounded_navigation(tmp_path):
    conn = open_db(tmp_path / "bounded-error-flow.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("Checkout.kt", 4, 5)
    symbols = ["Checkout.create", *(f"Node{i}.run" for i in range(201))]
    flows.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", symbols[0], evidence)],
        edges=[FlowEdge(source, target, "invokes", evidence)
               for source, target in zip(symbols, symbols[1:])],
    ))

    result = queries.describe_error_flow(conn, "checkout", "http", "post", "/orders")

    assert result["error_flows"] == []
    assert any("truncated" in item for item in result["unknowns"])


def test_describe_entrypoint_includes_only_reachable_static_service_calls(tmp_path):
    conn = open_db(tmp_path / "service-calls.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("CheckoutService.java", 12, 15)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", evidence)],
            edges=[FlowEdge("CheckoutController.create", "CheckoutService.checkout", "invokes", evidence)],
            static_service_calls=[
                StaticServiceCall(
                    source="CheckoutService.checkout", target_service="inventory", protocol="http",
                    target_method="POST", target_path="/reservations", evidence=evidence,
                ),
                StaticServiceCall(
                    source="ReconciliationJob.reconcile", target_service="payments", protocol="http",
                    target_method="GET", target_path="/settlements", evidence=evidence,
                ),
            ],
        ),
    )
    inventory_id = services.ensure_service(conn, "inventory", "/repos/inventory", "jvm-spring")
    flows.replace_analysis(
        conn,
        inventory_id,
        AnalysisResult(entrypoints=[
            EntryPoint("http", "POST", "/reservations", "InventoryController.reserve", evidence),
        ]),
    )

    detail = queries.describe_entrypoint(conn, "checkout", "http", "post", "/orders")

    assert detail["service_calls"] == [{
        "source": "CheckoutService.checkout", "target_service": "inventory", "protocol": "http",
        "method": "POST", "path": "/reservations",
        "evidence": {"file": "CheckoutService.java", "start_line": 12, "end_line": 15},
        "resolved_target": {
            "status": "endpoint_indexed", "service": "inventory", "repository": None,
            "entrypoint": {
                "kind": "http", "method": "POST", "name": "/reservations",
                "symbol": "InventoryController.reserve",
                "evidence": {"file": "CheckoutService.java", "start_line": 12, "end_line": 15},
            },
        },
    }]


def test_describe_entrypoint_includes_only_reachable_resilience_policies(tmp_path):
    conn = open_db(tmp_path / "resilience-policies.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("CheckoutService.java", 12, 15)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", evidence)],
            edges=[FlowEdge("CheckoutController.create", "CheckoutService.checkout", "invokes", evidence)],
            resilience_policies=[
                ResiliencePolicy(
                    source="CheckoutService.checkout", kind="timeout", mechanism="reactor",
                    value=2_000, unit="milliseconds", evidence=evidence,
                ),
                ResiliencePolicy(
                    source="ReconciliationJob.reconcile", kind="retry", mechanism="spring_annotation",
                    value=3, unit="attempts", evidence=evidence,
                ),
            ],
        ),
    )

    detail = queries.describe_entrypoint(conn, "checkout", "http", "post", "/orders")

    assert detail["resilience_policies"] == [{
        "source": "CheckoutService.checkout", "kind": "timeout", "mechanism": "reactor",
        "value": 2_000, "unit": "milliseconds",
        "evidence": {"file": "CheckoutService.java", "start_line": 12, "end_line": 15},
    }]


def test_describe_entrypoint_reports_an_ambiguous_static_service_call_target(tmp_path):
    conn = open_db(tmp_path / "ambiguous-service-call.db")
    checkout = services.ensure_service(conn, "checkout", "/repos/checkout", "jvm-spring")
    evidence = Evidence("CheckoutService.java", 12, 15)
    flows.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", evidence)],
            static_service_calls=[
                StaticServiceCall(
                    source="CheckoutController.create", target_service="inventory", protocol="http",
                    target_method="POST", target_path="/reservations", evidence=evidence,
                ),
            ],
        ),
    )
    first_repository = repositories.ensure_repository(conn, "first", "/repos/first")
    second_repository = repositories.ensure_repository(conn, "second", "/repos/second")
    services.ensure_service(conn, "inventory", "/repos/first/inventory", "jvm-spring", first_repository)
    services.ensure_service(conn, "inventory", "/repos/second/inventory", "jvm-spring", second_repository)

    detail = queries.describe_entrypoint(conn, "checkout", "http", "post", "/orders")

    assert detail["service_calls"][0]["resolved_target"] == {
        "status": "ambiguous", "repositories": ["first", "second"],
    }


def test_describe_entrypoint_prefers_a_static_service_call_target_in_its_repository(tmp_path):
    conn = open_db(tmp_path / "same-repository-service-call.db")
    shop_repository = repositories.ensure_repository(conn, "shop", "/repos/shop")
    other_repository = repositories.ensure_repository(conn, "other", "/repos/other")
    checkout = services.ensure_service(
        conn, "checkout", "/repos/shop/checkout", "jvm-spring", shop_repository,
    )
    evidence = Evidence("CheckoutService.java", 12, 15)
    flows.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "CheckoutController.create", evidence)],
            static_service_calls=[
                StaticServiceCall(
                    source="CheckoutController.create", target_service="inventory", protocol="http",
                    target_method="POST", target_path="/reservations", evidence=evidence,
                ),
            ],
        ),
    )
    services.ensure_service(conn, "inventory", "/repos/shop/inventory", "jvm-spring", shop_repository)
    services.ensure_service(conn, "inventory", "/repos/other/inventory", "jvm-spring", other_repository)

    detail = queries.describe_entrypoint(conn, "checkout", "http", "post", "/orders", repository="shop")

    assert detail["service_calls"][0]["resolved_target"] == {
        "status": "service_indexed", "service": "inventory", "repository": "shop",
    }


def test_describe_entrypoint_bounds_flow_context_and_reports_truncation(tmp_path):
    conn = open_db(tmp_path / "flow-budget.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("OrdersController.kt", 8, 12)
    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/orders", "OrdersController.create", evidence)],
            edges=[
                FlowEdge("OrdersController.create", "UseCase.execute", "invokes", evidence),
                FlowEdge("UseCase.execute", "Repository.save", "writes", evidence),
            ],
        ),
    )

    detail = queries.describe_entrypoint(conn, "orders", "http", "post", "/orders", max_edges=1)

    assert len(detail["flow"]) == 1
    assert detail["flow_pagination"] == {"max_edges": 1, "truncated": True}
    assert any(item["kind"] == "edge_limit" and item["target"] == "Repository.save"
               for item in detail["boundaries"])
