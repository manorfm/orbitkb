from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    ErrorContract,
    Evidence,
    FlowBoundary,
    FlowEdge,
    ResiliencePolicy,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import flows, repositories, services
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
