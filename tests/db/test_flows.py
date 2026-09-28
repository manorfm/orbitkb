from orbitkb.analysis.models import (
    AnalysisResult,
    ApiHeader,
    CloudFact,
    ConfigurationBinding,
    EntryPoint,
    Evidence,
    FeatureFlag,
    FlowEdge,
    MessageContract,
    MigrationFact,
    SecurityRequirement,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import flows, services


def test_flow_snapshot_is_replaced_per_service(tmp_path):
    conn = open_db(tmp_path / "flows.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "go")
    evidence = Evidence("main.go", 10, 12)
    analysis = AnalysisResult(
        entrypoints=[EntryPoint("http", "POST", "/orders", "Orders.Create", evidence)],
        edges=[FlowEdge("Orders.Create", "orders.UseCase.Execute", "invokes", evidence)],
    )

    flows.replace_analysis(conn, service_id, analysis)

    entrypoint = flows.get_entrypoint(conn, service_id, "http", "post", "/orders")
    assert entrypoint is not None
    assert [edge["to_symbol"] for edge in flows.list_entrypoint_edges(conn, entrypoint["id"])] == [
        "orders.UseCase.Execute"
    ]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_entrypoints(conn, service_id) == []


def test_grpc_entrypoint_contract_is_persisted_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "grpc.db")
    service_id = services.ensure_service(conn, "inventory", "/repos/inventory", "go")
    evidence = Evidence("inventory.proto", 7, 7)

    flows.replace_analysis(
        conn,
        service_id,
        AnalysisResult(entrypoints=[
            EntryPoint(
                "grpc", "RPC", "inventory.v1.Inventory.Reserve",
                "proto.inventory.v1.Inventory.Reserve", evidence,
            ),
        ], contracts={
            "proto.inventory.v1.Inventory.Reserve": {"formal_contract": {"format": "protobuf"}},
        }),
    )

    entrypoint = flows.get_entrypoint(
        conn, service_id, "grpc", "rpc", "inventory.v1.Inventory.Reserve",
    )

    assert entrypoint is not None
    assert flows.get_entrypoint_contract(conn, entrypoint["id"]) == {
        "formal_contract": {"format": "protobuf"},
    }


def test_static_message_contracts_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "contracts.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-ts")
    evidence = Evidence("resolvers.ts", 10, 10)

    flows.replace_analysis(
        conn, service_id, AnalysisResult(message_contracts=[
            MessageContract("publishes", "orders", "created", None, evidence, "1"),
        ]),
    )

    assert [dict(row) for row in flows.list_static_message_contracts(conn, service_id)] == [{
        "direction": "publishes", "channel": "orders", "routing_key": "created", "payload_type": None,
        "message_version": "1",
        "file_path": "resolvers.ts", "start_line": 10, "end_line": 10,
    }]


def test_static_cloud_facts_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "cloud.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-ts")
    evidence = Evidence("publisher.ts", 4, 4)

    flows.replace_analysis(
        conn, service_id, AnalysisResult(cloud_facts=[
            CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk-js-v3", None, evidence),
        ]),
    )

    facts = flows.list_static_cloud_facts(conn, service_id)
    assert [dict(row) for row in facts] == [{
        "provider": "aws", "resource_type": "queue", "service_name": "sqs",
        "operation": "SendMessage", "operation_kind": "publish", "sdk": "aws-sdk-js-v3",
        "target_name": None, "file_path": "publisher.ts", "start_line": 4, "end_line": 4,
    }]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_cloud_facts(conn, service_id) == []


def test_static_migration_facts_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "migrations.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "jvm-spring")
    evidence = Evidence("db/migration/V4__orders.sql", 3, 3)

    flows.replace_analysis(conn, service_id, AnalysisResult(migration_facts=[
        MigrationFact("add_column", "orders", "external_id", False, evidence),
        MigrationFact("drop_column", "orders", "legacy_id", True, Evidence(evidence.file_path, 4, 4)),
    ]))

    assert [dict(row) for row in flows.list_static_migration_facts(conn, service_id)] == [
        {
            "operation": "add_column", "table_name": "orders", "column_name": "external_id",
            "destructive": 0, "file_path": "db/migration/V4__orders.sql", "start_line": 3, "end_line": 3,
        },
        {
            "operation": "drop_column", "table_name": "orders", "column_name": "legacy_id",
            "destructive": 1, "file_path": "db/migration/V4__orders.sql", "start_line": 4, "end_line": 4,
        },
    ]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_migration_facts(conn, service_id) == []


def test_static_configuration_bindings_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "configuration.db")
    service_id = services.ensure_service(conn, "orders", "/repos/orders", "node-ts")
    evidence = Evidence("publisher.ts", 3, 3)

    flows.replace_analysis(conn, service_id, AnalysisResult(configuration_bindings=[
        ConfigurationBinding("publisher.publish", "ORDERS_TOPIC", "environment", False, evidence),
        ConfigurationBinding("publisher.publish", "STRIPE_SECRET_KEY", "environment", True, Evidence("publisher.ts", 4, 4)),
        ConfigurationBinding("Client.timeout", "payments.timeout-ms", "property", False, Evidence("Client.java", 5, 5)),
    ]))

    assert [dict(row) for row in flows.list_static_configuration_bindings(conn, service_id)] == [
        {
            "source": "publisher.publish", "key": "ORDERS_TOPIC", "kind": "environment", "sensitive": 0,
            "file_path": "publisher.ts", "start_line": 3, "end_line": 3,
        },
        {
            "source": "publisher.publish", "key": "STRIPE_SECRET_KEY", "kind": "environment", "sensitive": 1,
            "file_path": "publisher.ts", "start_line": 4, "end_line": 4,
        },
        {
            "source": "Client.timeout", "key": "payments.timeout-ms", "kind": "property", "sensitive": 0,
            "file_path": "Client.java", "start_line": 5, "end_line": 5,
        },
    ]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_configuration_bindings(conn, service_id) == []


def test_static_feature_flags_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "feature-flags.db")
    service_id = services.ensure_service(conn, "checkout", "/repos/checkout", "node-ts")
    evidence = Evidence("checkout.ts", 8, 8)

    flows.replace_analysis(conn, service_id, AnalysisResult(feature_flags=[
        FeatureFlag("checkout.checkout", "checkout.new-payment-flow", "launchdarkly", evidence),
    ]))

    assert [dict(row) for row in flows.list_static_feature_flags(conn, service_id)] == [{
        "source": "checkout.checkout", "key": "checkout.new-payment-flow", "provider": "launchdarkly",
        "file_path": "checkout.ts", "start_line": 8, "end_line": 8,
    }]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_feature_flags(conn, service_id) == []


def test_static_security_requirements_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "security-requirements.db")
    service_id = services.ensure_service(conn, "menu-manager", "/repos/menu-manager", "jvm-spring")
    route_evidence = Evidence("SecurityConfig.kt", 52, 55)
    method_evidence = Evidence("OrdersController.kt", 12, 12)

    flows.replace_analysis(conn, service_id, AnalysisResult(security_requirements=[
        SecurityRequirement(
            "/restaurants/{restaurantId}/**", "POST", None,
            "custom:RestaurantAccessAuthorizationManager", ("OWNER", "PARTNER"), route_evidence,
        ),
        SecurityRequirement(None, None, "OrdersController.cancel", "hasRole", ("ADMIN",), method_evidence),
    ]))

    assert [dict(row) for row in flows.list_static_security_requirements(conn, service_id)] == [
        {
            "route_pattern": None, "method": None, "symbol": "OrdersController.cancel",
            "requirement": "hasRole", "roles_json": '["ADMIN"]',
            "file_path": "OrdersController.kt", "start_line": 12, "end_line": 12,
        },
        {
            "route_pattern": "/restaurants/{restaurantId}/**", "method": "POST", "symbol": None,
            "requirement": "custom:RestaurantAccessAuthorizationManager", "roles_json": '["OWNER", "PARTNER"]',
            "file_path": "SecurityConfig.kt", "start_line": 52, "end_line": 55,
        },
    ]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_security_requirements(conn, service_id) == []


def test_static_security_requirements_in_declaration_order_are_not_sorted_alphabetically(tmp_path):
    """`/zebras/**` sorts alphabetically before `/apples/**`'s specific rule below
    it -- declaration order must survive that, since Spring Security evaluates
    authorizeHttpRequests rules in the order they're declared, not alphabetically.
    """
    conn = open_db(tmp_path / "security-requirements-order.db")
    service_id = services.ensure_service(conn, "menu-manager", "/repos/menu-manager", "jvm-spring")
    evidence = Evidence("SecurityConfig.kt", 10, 10)

    flows.replace_analysis(conn, service_id, AnalysisResult(security_requirements=[
        SecurityRequirement("/zebras/{id}/destinations", "POST", None, "hasRole", ("MANAGER",), evidence),
        SecurityRequirement("/apples/**", None, None, "authenticated", (), evidence),
    ]))

    rows = flows.list_static_security_requirements_in_declaration_order(conn, service_id)
    assert [row["route_pattern"] for row in rows] == ["/zebras/{id}/destinations", "/apples/**"]
    alphabetical = flows.list_static_security_requirements(conn, service_id)
    assert [row["route_pattern"] for row in alphabetical] == ["/apples/**", "/zebras/{id}/destinations"]


def test_static_api_headers_are_replaced_with_the_flow_snapshot(tmp_path):
    conn = open_db(tmp_path / "api-headers.db")
    service_id = services.ensure_service(conn, "menu-manager", "/repos/menu-manager", "jvm-spring")
    evidence = Evidence("MenuController.kt", 48, 48)

    flows.replace_analysis(conn, service_id, AnalysisResult(api_headers=[
        ApiHeader("GET", "/menus/active", "request", "Accept-Language", evidence),
        ApiHeader("GET", "/menus/active", "response", "ETag", evidence),
    ]))

    assert [dict(row) for row in flows.list_static_api_headers_for_route(conn, service_id, "GET", "/menus/active")] == [
        {"direction": "request", "name": "Accept-Language", "file_path": "MenuController.kt", "start_line": 48, "end_line": 48},
        {"direction": "response", "name": "ETag", "file_path": "MenuController.kt", "start_line": 48, "end_line": 48},
    ]

    flows.replace_analysis(conn, service_id, AnalysisResult())
    assert flows.list_static_api_headers_for_route(conn, service_id, "GET", "/menus/active") == []
