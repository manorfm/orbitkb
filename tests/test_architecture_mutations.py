"""Fact-mutation regression checks for deterministic architecture-smell rules.

Each test creates the minimum evidence that should trigger a finding, then changes
one fact that invalidates that evidence. This guards rule boundaries without
pretending that a source-only heuristic proves a runtime architecture verdict.
"""
from pathlib import Path

from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    ErrorContract,
    Evidence,
    FlowBoundary,
    FlowEdge,
    MessageContract,
    ResiliencePolicy,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import architecture as architecture_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.architecture import (
    find_broad_handlers_that_can_swallow_timeouts,
    find_cycles,
    find_error_semantics_lost,
    find_fan_imbalance,
    find_internal_error_exposures,
    find_message_consumers_without_recovery_policy,
    find_non_atomic_service_publish_flows,
    find_overbroad_exception_handlers,
    find_read_entrypoint_side_effects,
    find_resilience_policies_on_write_flows,
    find_retries_on_downstream_client_errors,
    find_retries_on_non_retryable_errors,
    find_retries_on_potentially_non_idempotent_http_calls,
    find_retries_on_write_publish_flows,
    find_retry_write_publish_flows_with_consumers,
    find_retry_write_publish_flows_with_persistent_consumers,
    find_retry_write_publish_flows_with_unrecovered_persistent_consumers,
    find_shared_database,
    find_static_http_calls_without_resilience_policy,
    find_timeout_fallbacks_masking_failures,
    find_timeouts_mapped_as_internal_server_errors,
    find_timeouts_without_local_fallback,
    find_unhandled_endpoint_errors,
    find_unmapped_downstream_errors,
    recompute_architecture_view,
)

EVIDENCE = [{"file": "main.py", "start_line": 1, "end_line": 5}]
STATIC_EVIDENCE = Evidence("handler.ts", 4, 6)


def test_cycle_finding_disappears_when_one_return_edge_is_removed(tmp_path: Path):
    conn = open_db(tmp_path / "cycle.db")
    left = services_repo.ensure_service(conn, "left", "/tmp/left", "go")
    right = services_repo.ensure_service(conn, "right", "/tmp/right", "go")
    left_api = apis_repo.upsert_api(conn, left, "POST", "/left", "s", "d", [], EVIDENCE)
    right_api = apis_repo.upsert_api(conn, right, "POST", "/right", "s", "d", [], EVIDENCE)
    _replace_calls(conn, left, left_api, ["right"])
    _replace_calls(conn, right, right_api, ["left"])

    assert _kinds(find_cycles(conn)) == {"cycle"}

    _replace_calls(conn, right, right_api, [])
    assert find_cycles(conn) == []


def test_fan_out_finding_disappears_below_the_configured_boundary(tmp_path: Path):
    conn = open_db(tmp_path / "fan-out.db")
    source = services_repo.ensure_service(conn, "gateway", "/tmp/gateway", "go")
    for index in range(4):
        services_repo.ensure_service(conn, f"target-{index}", f"/tmp/target-{index}", "go")
    api = apis_repo.upsert_api(conn, source, "POST", "/work", "s", "d", [], EVIDENCE)
    _replace_calls(conn, source, api, [f"target-{index}" for index in range(4)])

    assert _kinds(find_fan_imbalance(conn)) == {"fan_out"}

    _replace_calls(conn, source, api, [f"target-{index}" for index in range(3)])
    assert find_fan_imbalance(conn) == []


def test_shared_database_finding_disappears_when_storage_engines_differ(tmp_path: Path):
    conn = open_db(tmp_path / "storage.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "go")
    reporting = services_repo.ensure_service(conn, "reporting", "/tmp/reporting", "node-ts")
    persistence_repo.replace_persistence_entities(
        conn, orders, [{"name": "orders", "kind": "sql_table", "engine": "postgres", "schema_json": []}], EVIDENCE,
    )
    persistence_repo.replace_persistence_entities(
        conn, reporting, [{"name": "orders", "kind": "sql_table", "engine": "postgres", "schema_json": []}], EVIDENCE,
    )

    assert _kinds(find_shared_database(conn)) == {"shared_database"}

    persistence_repo.replace_persistence_entities(
        conn, reporting, [{"name": "orders", "kind": "document", "engine": "mongodb", "schema_json": []}], EVIDENCE,
    )
    assert find_shared_database(conn) == []


def test_read_side_effect_finding_disappears_when_transport_becomes_a_command(tmp_path: Path):
    conn = open_db(tmp_path / "read-side-effect.db")
    service = services_repo.ensure_service(conn, "catalog", "/tmp/catalog", "node-ts")
    entrypoint = EntryPoint("http", "GET", "/catalog/refresh", "Catalog.refresh", STATIC_EVIDENCE)
    _replace_static_flow(conn, service, entrypoint)

    assert _kinds(find_read_entrypoint_side_effects(conn)) == {"possible_read_entrypoint_side_effect"}

    _replace_static_flow(conn, service, EntryPoint("http", "POST", "/catalog/refresh", "Catalog.refresh", STATIC_EVIDENCE))
    assert find_read_entrypoint_side_effects(conn) == []


def test_message_recovery_finding_disappears_when_a_dead_letter_route_is_proven(tmp_path: Path):
    conn = open_db(tmp_path / "consumer.db")
    service = services_repo.ensure_service(conn, "billing", "/tmp/billing", "node-ts")
    consumer = EntryPoint("message", "CONSUME", "billing.created", "message.consume:billing.created", STATIC_EVIDENCE)
    _replace_consumer_contract(conn, service, consumer, {})

    assert _kinds(find_message_consumers_without_recovery_policy(conn)) == {
        "possible_message_consumer_without_recovery_policy",
    }

    _replace_consumer_contract(conn, service, consumer, {"dead_letter_routing_key": "billing.dlq"})
    assert find_message_consumers_without_recovery_policy(conn) == []


def test_overbroad_exception_handler_disappears_when_the_mapping_becomes_specific(tmp_path: Path):
    conn = open_db(tmp_path / "error-handler.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    broad = ErrorContract(
        source="ApiExceptionHandler.handle", role="maps", error_kind="unexpected",
        internal_type="Exception", protocol="http", transport_code="500", public_code=None,
        exposes_internal_detail=False, retryability="unknown", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[broad]))

    assert _kinds(find_overbroad_exception_handlers(conn)) == {"possible_overbroad_exception_handler"}

    specific = ErrorContract(
        source="ApiExceptionHandler.handle", role="maps", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409", public_code=None,
        exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[specific]))

    assert find_overbroad_exception_handlers(conn) == []


def test_error_semantics_lost_disappears_when_a_conflict_is_mapped_to_409(tmp_path: Path):
    conn = open_db(tmp_path / "error-semantics.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    raised = ErrorContract(
        source="StockReservation.reserve", role="raises", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="internal", transport_code=None,
        public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    degraded = ErrorContract(
        source="ApiExceptionHandler.handleStock", role="maps", error_kind="unexpected",
        internal_type="InsufficientStockException", protocol="http", transport_code="500",
        public_code=None, exposes_internal_detail=False, retryability="unknown", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[raised, degraded]))

    assert _kinds(find_error_semantics_lost(conn)) == {"possible_error_semantics_lost"}

    preserved = ErrorContract(
        source="ApiExceptionHandler.handleStock", role="maps", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[raised, preserved]))

    assert find_error_semantics_lost(conn) == []


def test_unhandled_endpoint_error_disappears_when_a_local_mapping_is_indexed(tmp_path: Path):
    conn = open_db(tmp_path / "unhandled-endpoint-error.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    entrypoint = EntryPoint("http", "POST", "/orders", "OrdersController.create", STATIC_EVIDENCE)
    raised = ErrorContract(
        source="OrderService.create", role="raises", error_kind="validation",
        internal_type="InvalidOrderException", protocol="internal", transport_code=None,
        public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge("OrdersController.create", "OrderService.create", "invokes", STATIC_EVIDENCE)],
        error_contracts=[raised],
    ))

    findings = find_unhandled_endpoint_errors(conn)

    assert _kinds(findings) == {"possible_unhandled_endpoint_error"}
    assert findings[0]["detail"]["entrypoint"] == {
        "method": "POST", "path": "/orders", "symbol": "OrdersController.create",
    }
    assert findings[0]["detail"]["origin"] == {
        "symbol": "OrderService.create", "error_type": "InvalidOrderException", "kind": "validation",
    }
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_unhandled_endpoint_error"}

    mapped = ErrorContract(
        source="ApiExceptionHandler.invalidOrder", role="maps", error_kind="validation",
        internal_type="InvalidOrderException", protocol="http", transport_code="400",
        public_code="INVALID_ORDER", exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge("OrdersController.create", "OrderService.create", "invokes", STATIC_EVIDENCE)],
        error_contracts=[raised, mapped],
    ))

    assert find_unhandled_endpoint_errors(conn) == []


def test_unhandled_endpoint_error_uses_canonical_snapshot_without_legacy_flow_rows(tmp_path: Path):
    conn = open_db(tmp_path / "canonical-unhandled-error.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    entrypoint = EntryPoint("http", "POST", "/orders", "OrdersController.create", STATIC_EVIDENCE)
    edge = FlowEdge("OrdersController.create", "OrderService.create", "invokes", STATIC_EVIDENCE)
    raised = ErrorContract(
        source="OrderService.create", role="raises", error_kind="validation",
        internal_type="InvalidOrderException", protocol="internal", transport_code=None,
        public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    unrelated = ErrorContract(
        source="ReconciliationJob.run", role="raises", error_kind="conflict",
        internal_type="UnrelatedConflict", protocol="internal", transport_code=None,
        public_code=None, exposes_internal_detail=False, retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(
        entrypoints=[entrypoint], edges=[edge], error_contracts=[raised, unrelated],
    ))
    conn.execute("DELETE FROM flow_edges")
    conn.execute("DELETE FROM static_error_contracts")

    findings = find_unhandled_endpoint_errors(conn)

    assert [item["kind"] for item in findings] == ["possible_unhandled_endpoint_error"]
    assert findings[0]["detail"]["origin"]["symbol"] == "OrderService.create"

    mapped = ErrorContract(
        source="ApiExceptionHandler.invalidOrder", role="maps", error_kind="validation",
        internal_type="InvalidOrderException", protocol="http", transport_code="400",
        public_code="INVALID_ORDER", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(
        entrypoints=[entrypoint], edges=[edge], error_contracts=[raised, unrelated, mapped],
    ))
    conn.execute("DELETE FROM flow_edges")
    conn.execute("DELETE FROM static_error_contracts")

    assert find_unhandled_endpoint_errors(conn) == []


def test_unhandled_endpoint_error_marks_truncated_navigation(tmp_path: Path):
    conn = open_db(tmp_path / "bounded-unhandled-error.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    symbols = ["OrdersController.create", *(f"Node{i}.run" for i in range(201))]
    flows_repo.replace_analysis(conn, service, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", symbols[0], STATIC_EVIDENCE)],
        edges=[FlowEdge(source, target, "invokes", STATIC_EVIDENCE)
               for source, target in zip(symbols, symbols[1:])],
        error_contracts=[ErrorContract(
            source=symbols[0], role="raises", error_kind="validation",
            internal_type="InvalidOrderException", protocol="internal", transport_code=None,
            public_code=None, exposes_internal_detail=False, retryability="not_retryable",
            evidence=STATIC_EVIDENCE,
        )],
    ))

    findings = find_unhandled_endpoint_errors(conn)

    assert len(findings) == 1
    assert any("truncated" in item for item in findings[0]["detail"]["unknowns"])


def test_internal_error_exposure_disappears_when_mapping_stops_exposing_detail(tmp_path: Path):
    conn = open_db(tmp_path / "internal-error-exposure.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "node-ts")
    exposed = ErrorContract(
        source="orders.failOrder", role="maps", error_kind="unexpected",
        internal_type=None, protocol="http", transport_code="500", public_code=None,
        exposes_internal_detail=True, retryability="unknown", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[exposed]))

    findings = find_internal_error_exposures(conn)

    assert _kinds(findings) == {"possible_internal_error_exposure"}
    assert findings[0]["severity"] == "critical"
    assert findings[0]["detail"]["mapping"] == {
        "symbol": "orders.failOrder", "protocol": "http", "code": "500",
    }
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_internal_error_exposure"}

    safe = ErrorContract(
        source="orders.failOrder", role="maps", error_kind="unexpected",
        internal_type=None, protocol="http", transport_code="500", public_code="INTERNAL_ERROR",
        exposes_internal_detail=False, retryability="unknown", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[safe]))

    assert find_internal_error_exposures(conn) == []


def test_internal_error_exposure_includes_a_public_graphql_error(tmp_path: Path):
    conn = open_db(tmp_path / "graphql-internal-error-exposure.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "node-ts")
    exposed = ErrorContract(
        source="Mutation.createOrder", role="raises", error_kind="unexpected",
        internal_type="GraphQLError", protocol="graphql", transport_code=None,
        public_code="INTERNAL_SERVER_ERROR", exposes_internal_detail=True,
        retryability="not_retryable", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[exposed]))

    findings = find_internal_error_exposures(conn)

    assert _kinds(findings) == {"possible_internal_error_exposure"}
    assert findings[0]["detail"]["mapping"] == {
        "symbol": "Mutation.createOrder", "protocol": "graphql", "code": "INTERNAL_SERVER_ERROR",
    }


def test_unmapped_downstream_error_disappears_when_caller_maps_the_known_error(tmp_path: Path):
    conn = open_db(tmp_path / "downstream-error.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    inventory = services_repo.ensure_service(conn, "inventory", "/tmp/inventory", "jvm-spring")
    checkout_api = apis_repo.upsert_api(conn, checkout, "POST", "/orders", "", "", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(
        conn,
        checkout,
        checkout_api,
        [{
            "to_service_name": "inventory", "call_kind": "http", "reason": "reserve stock",
            "data_needed": [], "purpose_kind": "validation", "confidence": 1.0,
            "target_kind": "internal",
        }],
        EVIDENCE,
    )
    downstream = ErrorContract(
        source="InventoryExceptionHandler.stock", role="maps", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, inventory, AnalysisResult(error_contracts=[downstream]))

    assert _kinds(find_unmapped_downstream_errors(conn)) == {"possible_unmapped_downstream_error"}

    caller_mapping = ErrorContract(
        source="CheckoutExceptionHandler.stock", role="maps", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(error_contracts=[caller_mapping]))

    assert find_unmapped_downstream_errors(conn) == []


def test_unmapped_downstream_error_uses_a_proven_feign_client_call(tmp_path: Path):
    conn = open_db(tmp_path / "static-downstream-error.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    inventory = services_repo.ensure_service(conn, "inventory", "/tmp/inventory", "jvm-spring")
    feign_call = StaticServiceCall(
        source="CheckoutService.checkout", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    downstream = ErrorContract(
        source="InventoryExceptionHandler.stock", role="maps", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(static_service_calls=[feign_call]))
    flows_repo.replace_analysis(conn, inventory, AnalysisResult(error_contracts=[downstream]))
    conn.execute("DELETE FROM static_error_contracts WHERE service_id = ?", (inventory,))

    findings = find_unmapped_downstream_errors(conn)

    assert _kinds(findings) == {"possible_unmapped_downstream_error"}
    assert findings[0]["detail"]["caller"] == {
        "service": "checkout", "symbol": "CheckoutService.checkout",
        "method": "POST", "path": "/reservations",
    }
    assert findings[0]["detail"]["confidence"] == 0.6


def test_http_resilience_finding_disappears_when_a_literal_source_policy_is_present(tmp_path: Path):
    conn = open_db(tmp_path / "http-resilience.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    call = StaticServiceCall(
        source="CheckoutService.checkout", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(static_service_calls=[call]))

    findings = find_static_http_calls_without_resilience_policy(conn)

    assert _kinds(findings) == {"possible_missing_http_resilience_policy"}
    assert findings[0]["detail"]["caller"] == {
        "service": "checkout", "symbol": "CheckoutService.checkout",
    }
    assert findings[0]["detail"]["target"] == {
        "service": "inventory", "method": "POST", "path": "/reservations",
    }
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_missing_http_resilience_policy"}

    policy = ResiliencePolicy(
        source="CheckoutService.checkout", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[call], resilience_policies=[policy]),
    )

    assert find_static_http_calls_without_resilience_policy(conn) == []


def test_retry_risk_finding_disappears_when_the_http_method_is_idempotent(tmp_path: Path):
    conn = open_db(tmp_path / "retry-idempotency.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    retry = ResiliencePolicy(
        source="CheckoutService.reserve", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    post = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[post], resilience_policies=[retry]),
    )

    findings = find_retries_on_potentially_non_idempotent_http_calls(conn)

    assert _kinds(findings) == {"possible_retry_on_non_idempotent_http_call"}
    assert findings[0]["detail"]["retry_policies"] == [{
        "mechanism": "reactor", "value": 2, "unit": "retries",
    }]
    assert len(findings[0]["detail"]["evidence"]) == 2
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_retry_on_non_idempotent_http_call"}

    put = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="PUT", target_path="/reservations/1", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[put], resilience_policies=[retry]),
    )

    assert find_retries_on_potentially_non_idempotent_http_calls(conn) == []


def test_retry_on_non_retryable_error_disappears_without_the_retry_policy(tmp_path: Path):
    conn = open_db(tmp_path / "retry-non-retryable-error.db")
    service = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    retry = ResiliencePolicy(
        source="OrderService.create", kind="retry", mechanism="spring_annotation",
        value=3, unit="attempts", evidence=STATIC_EVIDENCE,
    )
    conflict = ErrorContract(
        source="OrderService.create", role="raises", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, service, AnalysisResult(error_contracts=[conflict], resilience_policies=[retry]),
    )

    findings = find_retries_on_non_retryable_errors(conn)

    assert _kinds(findings) == {"possible_retry_on_non_retryable_error"}
    assert findings[0]["detail"]["error"] == {
        "symbol": "OrderService.create", "type": "InsufficientStockException",
        "kind": "conflict", "status": "409",
    }
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_on_non_retryable_error" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    flows_repo.replace_analysis(conn, service, AnalysisResult(error_contracts=[conflict]))

    assert find_retries_on_non_retryable_errors(conn) == []


def test_retry_on_downstream_client_error_disappears_without_a_retry_policy(tmp_path: Path):
    conn = open_db(tmp_path / "retry-downstream-error.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    inventory = services_repo.ensure_service(conn, "inventory", "/tmp/inventory", "jvm-spring")
    call = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    retry = ResiliencePolicy(
        source="CheckoutService.reserve", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    conflict = ErrorContract(
        source="InventoryService.reserve", role="raises", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[call], resilience_policies=[retry]),
    )
    flows_repo.replace_analysis(
        conn,
        inventory,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/reservations", "InventoryController.reserve", STATIC_EVIDENCE)],
            edges=[FlowEdge("InventoryController.reserve", "InventoryService.reserve", "invokes", STATIC_EVIDENCE)],
            error_contracts=[conflict],
        ),
    )
    conn.execute("DELETE FROM flow_edges WHERE service_id = ?", (inventory,))
    conn.execute("DELETE FROM static_error_contracts WHERE service_id = ?", (inventory,))

    findings = find_retries_on_downstream_client_errors(conn)

    assert _kinds(findings) == {"possible_retry_on_downstream_client_error"}
    assert findings[0]["detail"]["downstream"] == {
        "service": "inventory", "symbol": "InventoryService.reserve",
        "error_type": "InsufficientStockException", "kind": "conflict", "status": "409",
    }
    assert findings[0]["detail"]["scope"] == "endpoint_flow"
    assert len(findings[0]["detail"]["evidence"]) == 3
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_on_downstream_client_error" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    timeout = ResiliencePolicy(
        source="CheckoutService.reserve", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[call], resilience_policies=[timeout]),
    )

    assert find_retries_on_downstream_client_errors(conn) == []


def test_timeout_fallback_finding_disappears_when_the_source_handles_timeout(tmp_path: Path):
    conn = open_db(tmp_path / "timeout-fallback.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    call = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    timeout = ResiliencePolicy(
        source="CheckoutService.reserve", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[call], resilience_policies=[timeout]),
    )

    findings = find_timeouts_without_local_fallback(conn)

    assert _kinds(findings) == {"possible_timeout_without_local_fallback"}
    assert findings[0]["detail"]["timeout_policies"] == [{
        "mechanism": "reactor", "value": 2_000, "unit": "milliseconds",
    }]
    assert findings[0]["detail"]["target"] == {
        "service": "inventory", "method": "POST", "path": "/reservations",
    }
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_timeout_without_local_fallback"}

    handled_timeout = ErrorContract(
        source="CheckoutService.reserve", role="handles", error_kind="timeout",
        internal_type="TimeoutException", protocol="internal", transport_code=None,
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            static_service_calls=[call], resilience_policies=[timeout], error_contracts=[handled_timeout],
        ),
    )

    assert find_timeouts_without_local_fallback(conn) == []


def test_timeout_success_fallback_finding_disappears_when_the_endpoint_returns_an_error(tmp_path: Path):
    conn = open_db(tmp_path / "timeout-success-fallback.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    entrypoint = EntryPoint("http", "POST", "/checkout", "CheckoutController.reserve", STATIC_EVIDENCE)
    call = StaticServiceCall(
        source="CheckoutController.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    successful_fallback = ErrorContract(
        source="CheckoutController.reserve", role="handles", error_kind="timeout",
        internal_type="TimeoutException", protocol="http", transport_code="200",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[entrypoint], static_service_calls=[call], error_contracts=[successful_fallback],
        ),
    )

    findings = find_timeout_fallbacks_masking_failures(conn)

    assert _kinds(findings) == {"possible_timeout_fallback_masks_failure"}
    assert findings[0]["detail"]["entrypoint"] == {
        "method": "POST", "path": "/checkout", "symbol": "CheckoutController.reserve",
    }
    assert findings[0]["detail"]["fallback"] == {
        "error_type": "TimeoutException", "status": "200",
    }
    run_id = recompute_architecture_view(conn)
    assert "possible_timeout_fallback_masks_failure" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    error_fallback = ErrorContract(
        source="CheckoutController.reserve", role="handles", error_kind="timeout",
        internal_type="TimeoutException", protocol="http", transport_code="503",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            entrypoints=[entrypoint], static_service_calls=[call], error_contracts=[error_fallback],
        ),
    )

    assert find_timeout_fallbacks_masking_failures(conn) == []


def test_timeout_mapping_finding_disappears_when_status_becomes_unavailable(tmp_path: Path):
    conn = open_db(tmp_path / "timeout-mapping.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    internal_error = ErrorContract(
        source="ApiExceptionHandler.timeout", role="maps", error_kind="timeout",
        internal_type="TimeoutException", protocol="http", transport_code="500",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(error_contracts=[internal_error]))

    findings = find_timeouts_mapped_as_internal_server_errors(conn)

    assert _kinds(findings) == {"possible_timeout_mapped_as_internal_server_error"}
    assert findings[0]["detail"]["mapping"] == {
        "symbol": "ApiExceptionHandler.timeout", "error_type": "TimeoutException", "status": "500",
    }
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_timeout_mapped_as_internal_server_error"}

    unavailable = ErrorContract(
        source="ApiExceptionHandler.timeout", role="maps", error_kind="timeout",
        internal_type="TimeoutException", protocol="http", transport_code="503",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(error_contracts=[unavailable]))

    assert find_timeouts_mapped_as_internal_server_errors(conn) == []


def test_broad_timeout_handler_risk_disappears_when_the_handler_becomes_specific(tmp_path: Path):
    conn = open_db(tmp_path / "broad-timeout-handler.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    broad_handler = ErrorContract(
        source="ApiExceptionHandler.handle", role="maps", error_kind="unexpected",
        internal_type="Exception", protocol="http", transport_code="500",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    call = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    timeout = ResiliencePolicy(
        source="CheckoutService.reserve", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            error_contracts=[broad_handler], static_service_calls=[call], resilience_policies=[timeout],
        ),
    )

    findings = find_broad_handlers_that_can_swallow_timeouts(conn)

    assert _kinds(findings) == {"possible_broad_handler_swallows_timeout"}
    assert findings[0]["detail"]["handler"] == {
        "symbol": "ApiExceptionHandler.handle", "error_type": "Exception", "status": "500",
    }
    assert findings[0]["detail"]["timeout_flow"] == {
        "symbol": "CheckoutService.reserve", "target_service": "inventory",
        "method": "POST", "path": "/reservations",
    }
    run_id = recompute_architecture_view(conn)
    assert "possible_broad_handler_swallows_timeout" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    specific_handler = ErrorContract(
        source="ApiExceptionHandler.handle", role="maps", error_kind="timeout",
        internal_type="TimeoutException", protocol="http", transport_code="504",
        public_code=None, exposes_internal_detail=False, retryability="unknown",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(
            error_contracts=[specific_handler], static_service_calls=[call], resilience_policies=[timeout],
        ),
    )

    assert find_broad_handlers_that_can_swallow_timeouts(conn) == []


def test_resilience_write_flow_risk_disappears_when_the_local_write_is_removed(tmp_path: Path):
    conn = open_db(tmp_path / "resilience-write-flow.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    call = StaticServiceCall(
        source="CheckoutService.reserve", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    retry = ResiliencePolicy(
        source="CheckoutService.reserve", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    write = FlowEdge(
        "CheckoutService.reserve", "OrderRepository.save", "writes", STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(static_service_calls=[call], resilience_policies=[retry], edges=[write]),
    )

    findings = find_resilience_policies_on_write_flows(conn)

    assert _kinds(findings) == {"possible_resilience_policy_on_partial_write_flow"}
    assert findings[0]["detail"]["writes"] == [{"target": "OrderRepository.save"}]
    assert findings[0]["detail"]["resilience_policies"] == [{
        "kind": "retry", "mechanism": "reactor", "value": 2, "unit": "retries",
    }]
    run_id = recompute_architecture_view(conn)
    assert "possible_resilience_policy_on_partial_write_flow" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    flows_repo.replace_analysis(
        conn, checkout, AnalysisResult(static_service_calls=[call], resilience_policies=[retry]),
    )

    assert find_resilience_policies_on_write_flows(conn) == []


def test_retry_write_publish_risk_disappears_when_retry_is_removed(tmp_path: Path):
    conn = open_db(tmp_path / "retry-write-publish.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    retry = ResiliencePolicy(
        source="CheckoutService.reserve", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    write = FlowEdge(
        "CheckoutService.reserve", "OrderRepository.save", "writes", STATIC_EVIDENCE,
    )
    publish = FlowEdge(
        "CheckoutService.reserve", "order.created", "publishes", STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(resilience_policies=[retry], edges=[write, publish]),
    )

    findings = find_retries_on_write_publish_flows(conn)

    assert _kinds(findings) == {"possible_retry_on_write_publish_flow"}
    assert findings[0]["detail"]["writes"] == [{"target": "OrderRepository.save"}]
    assert findings[0]["detail"]["publishes"] == [{"target": "order.created"}]
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_on_write_publish_flow" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    timeout = ResiliencePolicy(
        source="CheckoutService.reserve", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn,
        checkout,
        AnalysisResult(resilience_policies=[timeout], edges=[write, publish]),
    )

    assert find_retries_on_write_publish_flows(conn) == []


def test_service_publish_finding_disappears_when_a_transaction_boundary_is_proven(tmp_path: Path):
    conn = open_db(tmp_path / "service-publish.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    write = FlowEdge(
        "OrderService.create", "OrderRepository.save", "writes", STATIC_EVIDENCE,
    )
    publish = FlowEdge(
        "OrderService.create", "order.created", "publishes", STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, orders, AnalysisResult(edges=[write, publish]))

    findings = find_non_atomic_service_publish_flows(conn)

    assert _kinds(findings) == {"possible_non_atomic_service_publish"}
    assert findings[0]["detail"]["flow"] == {"symbol": "OrderService.create"}
    assert findings[0]["detail"]["writes"] == [{"target": "OrderRepository.save"}]
    assert findings[0]["detail"]["publishes"] == [{"target": "order.created"}]
    run_id = recompute_architecture_view(conn)
    assert {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    } == {"possible_non_atomic_service_publish"}

    transaction = FlowBoundary("OrderService.create", "transaction", STATIC_EVIDENCE)
    flows_repo.replace_analysis(
        conn, orders, AnalysisResult(edges=[write, publish], boundaries=[transaction]),
    )

    assert find_non_atomic_service_publish_flows(conn) == []


def test_retry_write_publish_consumer_risk_disappears_when_retry_is_removed(tmp_path: Path):
    conn = open_db(tmp_path / "retry-write-publish-consumer.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    billing = services_repo.ensure_service(conn, "billing", "/tmp/billing", "jvm-spring")
    retry = ResiliencePolicy(
        source="OrderService.create", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    write = FlowEdge("OrderService.create", "OrderRepository.save", "writes", STATIC_EVIDENCE)
    publish = FlowEdge("OrderService.create", "order.created", "publishes", STATIC_EVIDENCE)
    consumer = MessageContract(
        direction="consumes", channel="order.created", routing_key=None,
        payload_type="OrderCreated", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, orders, AnalysisResult(resilience_policies=[retry], edges=[write, publish]),
    )
    flows_repo.replace_analysis(conn, billing, AnalysisResult(message_contracts=[consumer]))

    findings = find_retry_write_publish_flows_with_consumers(conn)

    assert _kinds(findings) == {"possible_retry_write_publish_reaches_consumer"}
    assert findings[0]["detail"]["consumers"] == [{
        "service": "billing", "channel": "order.created",
    }]
    assert findings[0]["detail"]["consumer_count"] == 1
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_write_publish_reaches_consumer" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    timeout = ResiliencePolicy(
        source="OrderService.create", kind="timeout", mechanism="reactor",
        value=2_000, unit="milliseconds", evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, orders, AnalysisResult(resilience_policies=[timeout], edges=[write, publish]),
    )

    assert find_retry_write_publish_flows_with_consumers(conn) == []


def test_persistent_consumer_risk_disappears_when_consumer_write_is_removed(tmp_path: Path):
    conn = open_db(tmp_path / "persistent-consumer.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    billing = services_repo.ensure_service(conn, "billing", "/tmp/billing", "jvm-spring")
    retry = ResiliencePolicy(
        source="OrderService.create", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    producer_write = FlowEdge("OrderService.create", "OrderRepository.save", "writes", STATIC_EVIDENCE)
    publish = FlowEdge("OrderService.create", "order.created", "publishes", STATIC_EVIDENCE)
    consumer_entrypoint = EntryPoint(
        "message", "CONSUME", "order.created", "BillingConsumer.consume", STATIC_EVIDENCE,
    )
    consumer_contract = MessageContract(
        direction="consumes", channel="order.created", routing_key=None,
        payload_type="OrderCreated", evidence=STATIC_EVIDENCE,
    )
    consumer_write = FlowEdge(
        "BillingConsumer.consume", "BillingRepository.save", "writes", STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, orders, AnalysisResult(resilience_policies=[retry], edges=[producer_write, publish]),
    )
    flows_repo.replace_analysis(
        conn,
        billing,
        AnalysisResult(
            entrypoints=[consumer_entrypoint], message_contracts=[consumer_contract], edges=[consumer_write],
        ),
    )

    findings = find_retry_write_publish_flows_with_persistent_consumers(conn)

    assert _kinds(findings) == {"possible_retry_write_publish_reaches_persistent_consumer"}
    assert findings[0]["detail"]["consumers"] == [{
        "service": "billing", "symbol": "BillingConsumer.consume",
        "writes": [{"target": "BillingRepository.save"}], "write_count": 1,
    }]
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_write_publish_reaches_persistent_consumer" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    flows_repo.replace_analysis(
        conn,
        billing,
        AnalysisResult(entrypoints=[consumer_entrypoint], message_contracts=[consumer_contract]),
    )

    assert find_retry_write_publish_flows_with_persistent_consumers(conn) == []


def test_unrecovered_persistent_consumer_risk_reports_declared_idempotency_without_hiding_recovery_gap(tmp_path: Path):
    conn = open_db(tmp_path / "unrecovered-persistent-consumer.db")
    orders = services_repo.ensure_service(conn, "orders", "/tmp/orders", "jvm-spring")
    billing = services_repo.ensure_service(conn, "billing", "/tmp/billing", "jvm-spring")
    retry = ResiliencePolicy(
        source="OrderService.create", kind="retry", mechanism="reactor",
        value=2, unit="retries", evidence=STATIC_EVIDENCE,
    )
    producer_write = FlowEdge("OrderService.create", "OrderRepository.save", "writes", STATIC_EVIDENCE)
    publish = FlowEdge("OrderService.create", "order.created", "publishes", STATIC_EVIDENCE)
    consumer_entrypoint = EntryPoint(
        "message", "CONSUME", "order.created", "BillingConsumer.consume", STATIC_EVIDENCE,
    )
    consumer_contract = MessageContract(
        direction="consumes", channel="order.created", routing_key=None,
        payload_type="OrderCreated", evidence=STATIC_EVIDENCE,
    )
    consumer_write = FlowEdge(
        "BillingConsumer.consume", "BillingRepository.save", "writes", STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(
        conn, orders, AnalysisResult(resilience_policies=[retry], edges=[producer_write, publish]),
    )
    flows_repo.replace_analysis(
        conn,
        billing,
        AnalysisResult(
            entrypoints=[consumer_entrypoint], message_contracts=[consumer_contract], edges=[consumer_write],
            contracts={consumer_entrypoint.symbol: {
                "transport": "rabbitmq", "direction": "consumes", "queue": "order.created",
            }},
        ),
    )

    findings = find_retry_write_publish_flows_with_unrecovered_persistent_consumers(conn)

    assert _kinds(findings) == {"possible_retry_write_publish_reaches_unrecovered_persistent_consumer"}
    assert findings[0]["detail"]["consumers"] == [{
        "service": "billing", "symbol": "BillingConsumer.consume", "queue": "order.created",
        "writes": [{"target": "BillingRepository.save"}], "write_count": 1,
    }]
    run_id = recompute_architecture_view(conn)
    assert "possible_retry_write_publish_reaches_unrecovered_persistent_consumer" in {
        row["kind"] for row in architecture_repo.list_findings(conn, run_id)
    }

    flows_repo.replace_analysis(
        conn,
        billing,
        AnalysisResult(
            entrypoints=[consumer_entrypoint], message_contracts=[consumer_contract], edges=[consumer_write],
            contracts={consumer_entrypoint.symbol: {
                "transport": "rabbitmq", "direction": "consumes", "queue": "order.created",
                "idempotency": "detected",
            }},
        ),
    )

    idempotency_findings = find_retry_write_publish_flows_with_unrecovered_persistent_consumers(conn)
    assert _kinds(idempotency_findings) == {
        "possible_retry_write_publish_reaches_unrecovered_persistent_consumer",
    }
    assert idempotency_findings[0]["detail"]["consumers"][0]["idempotency"] == "declared"

    flows_repo.replace_analysis(
        conn,
        billing,
        AnalysisResult(
            entrypoints=[consumer_entrypoint], message_contracts=[consumer_contract], edges=[consumer_write],
            contracts={consumer_entrypoint.symbol: {
                "transport": "rabbitmq", "direction": "consumes", "queue": "order.created",
                "dead_letter_routing_key": "orders.dlq",
            }},
        ),
    )

    assert find_retry_write_publish_flows_with_unrecovered_persistent_consumers(conn) == []


def test_unmapped_downstream_error_scopes_static_call_to_the_target_endpoint_flow(tmp_path: Path):
    conn = open_db(tmp_path / "endpoint-scoped-downstream-error.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    inventory = services_repo.ensure_service(conn, "inventory", "/tmp/inventory", "jvm-spring")
    feign_call = StaticServiceCall(
        source="CheckoutService.checkout", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )
    reservation_error = ErrorContract(
        source="InventoryService.reserve", role="raises", error_kind="conflict",
        internal_type="InsufficientStockException", protocol="http", transport_code="409",
        public_code="OUT_OF_STOCK", exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    unrelated_error = ErrorContract(
        source="InventoryService.reconcile", role="raises", error_kind="not_found",
        internal_type="SettlementNotFoundException", protocol="http", transport_code="404",
        public_code=None, exposes_internal_detail=False, retryability="not_retryable",
        evidence=STATIC_EVIDENCE,
    )
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(static_service_calls=[feign_call]))
    flows_repo.replace_analysis(
        conn,
        inventory,
        AnalysisResult(
            entrypoints=[EntryPoint("http", "POST", "/reservations", "InventoryController.reserve", STATIC_EVIDENCE)],
            edges=[FlowEdge("InventoryController.reserve", "InventoryService.reserve", "invokes", STATIC_EVIDENCE)],
            error_contracts=[reservation_error, unrelated_error],
        ),
    )
    conn.execute("DELETE FROM flow_edges WHERE service_id = ?", (inventory,))
    conn.execute("DELETE FROM static_error_contracts WHERE service_id = ?", (inventory,))

    findings = find_unmapped_downstream_errors(conn)

    assert len(findings) == 1
    assert findings[0]["detail"]["downstream"]["error_type"] == "InsufficientStockException"
    assert findings[0]["detail"]["scope"] == "endpoint_flow"
    assert findings[0]["detail"]["confidence"] == 0.75


def test_downstream_error_finding_reports_truncated_target_flow(tmp_path: Path):
    conn = open_db(tmp_path / "bounded-downstream-error.db")
    checkout = services_repo.ensure_service(conn, "checkout", "/tmp/checkout", "jvm-spring")
    inventory = services_repo.ensure_service(conn, "inventory", "/tmp/inventory", "jvm-spring")
    symbols = ["InventoryController.reserve", *(f"Node{i}.run" for i in range(201))]
    flows_repo.replace_analysis(conn, checkout, AnalysisResult(static_service_calls=[StaticServiceCall(
        source="CheckoutService.checkout", target_service="inventory", protocol="http",
        target_method="POST", target_path="/reservations", evidence=STATIC_EVIDENCE,
    )]))
    flows_repo.replace_analysis(conn, inventory, AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/reservations", symbols[0], STATIC_EVIDENCE)],
        edges=[FlowEdge(source, target, "invokes", STATIC_EVIDENCE)
               for source, target in zip(symbols, symbols[1:])],
        error_contracts=[ErrorContract(
            source=symbols[0], role="raises", error_kind="conflict",
            internal_type="InsufficientStockException", protocol="http", transport_code="409",
            public_code="OUT_OF_STOCK", exposes_internal_detail=False,
            retryability="not_retryable", evidence=STATIC_EVIDENCE,
        )],
    ))

    findings = find_unmapped_downstream_errors(conn)

    assert len(findings) == 1
    assert any("truncated" in item for item in findings[0]["detail"]["unknowns"])


def _replace_calls(conn, service_id: int, api_id: int, targets: list[str]) -> None:
    service_calls_repo.replace_calls_for_api(
        conn,
        service_id,
        api_id,
        [
            {
                "to_service_name": target,
                "call_kind": "http",
                "reason": "test relation",
                "data_needed": [],
                "purpose_kind": "other",
                "confidence": 0.9,
                "target_kind": "internal",
            }
            for target in targets
        ],
        EVIDENCE,
    )
    service_calls_repo.reconcile_service_call_targets(conn)


def _replace_static_flow(conn, service_id: int, entrypoint: EntryPoint) -> None:
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(entrypoints=[entrypoint], edges=[FlowEdge(entrypoint.symbol, "repository.save", "writes", STATIC_EVIDENCE)]),
    )


def _replace_consumer_contract(conn, service_id: int, consumer: EntryPoint, recovery: dict) -> None:
    flows_repo.replace_analysis(
        conn,
        service_id,
        AnalysisResult(
            entrypoints=[consumer],
            contracts={
                consumer.symbol: {
                    "transport": "rabbitmq",
                    "direction": "consumes",
                    "queue": consumer.name,
                    **recovery,
                },
            },
        ),
    )


def _kinds(findings: list[dict]) -> set[str]:
    return {finding["kind"] for finding in findings}
