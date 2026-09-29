import ast
from pathlib import Path

import pytest

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    ApiHeader,
    CloudFact,
    ConfigurationBinding,
    EntryPoint,
    ErrorContract,
    Evidence,
    FeatureFlag,
    FlowBoundary,
    FlowEdge,
    GrpcClientBinding,
    GrpcHandler,
    Injection,
    MessageContract,
    MigrationFact,
    PersistenceFact,
    ResiliencePolicy,
    SecurityRequirement,
    StaticServiceCall,
    Symbol,
)
from orbitkb.domain.canonical import (
    CloudResourceKey,
    FactStatus,
    MessageChannelKey,
    PersistenceResourceKey,
    RoutePatternKey,
    ServiceKey,
    SymbolKey,
)


def test_entrypoint_projection_has_stable_identity_and_preserves_sources():
    first = EntryPoint("http", "GET", "/menus", "MenuController.list", Evidence("Menu.kt", 10, 12),
                       {"response": "Menu"})
    duplicate = EntryPoint("http", "GET", "/menus", "MenuController.list", Evidence("Route.kt", 3, 4),
                           {"response": "Menu"})
    analysis = AnalysisResult(entrypoints=[first, duplicate])

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)
    fact = snapshot.facts[0]

    assert len(snapshot.facts) == 1
    assert fact.kind == "entrypoint" and fact.status is FactStatus.CONFIRMED
    assert fact.origin == "static"
    assert fact.subject.service == ServiceKey("menu-manager")
    assert (fact.subject.transport, fact.subject.method, fact.subject.name, fact.subject.symbol) == (
        "http", "GET", "/menus", "MenuController.list",
    )
    assert fact.attributes == {"contract": {"response": "Menu"}}
    assert [(source.file_path, source.start_line, source.end_line) for source in fact.sources] == [
        ("Menu.kt", 10, 12), ("Route.kt", 3, 4),
    ]
    assert project_analysis(ServiceKey("menu-manager"), AnalysisResult(entrypoints=[duplicate])).facts[0].id == fact.id
    assert project_analysis(ServiceKey("other-service"), analysis).facts[0].id != fact.id
    assert project_analysis(ServiceKey("menu-manager", repository="shop"), analysis).facts[0].id != (
        project_analysis(ServiceKey("menu-manager", repository="dining"), analysis).facts[0].id
    )


def test_entrypoint_projection_uses_existing_contract_when_entrypoint_has_none():
    entry = EntryPoint("http", "POST", "/orders", "Orders.create", Evidence("orders.go", 2, 5))
    analysis = AnalysisResult(entrypoints=[entry], contracts={"Orders.create": {"request": "Order"}})

    snapshot = project_analysis(ServiceKey("orders"), analysis)

    assert snapshot.facts[0].attributes == {"contract": {"request": "Order"}}


def test_entrypoint_projection_rejects_conflicting_contracts_for_one_identity():
    first = EntryPoint("http", "GET", "/items", "Items.list", Evidence("routes.go", 2, 3), {"response": "Item"})
    second = EntryPoint("http", "GET", "/items", "Items.list", Evidence("routes.go", 5, 6), {"response": "Other"})

    with pytest.raises(ValueError, match="conflicting values"):
        project_analysis(ServiceKey("catalog"), AnalysisResult(entrypoints=[first, second]))


def test_canonical_domain_does_not_import_adapters_or_storage():
    domain = Path(__file__).resolve().parents[1] / "orbitkb" / "domain"
    for path in domain.glob("*.py"):
        tree = ast.parse(path.read_text())
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
        assert not [module for module in imports if module.startswith(("orbitkb.analysis", "orbitkb.db",
                                                                "orbitkb.discovery", "orbitkb.generation"))], path.name


def test_entrypoint_projection_accepts_existing_go_and_kotlin_analysis(tmp_path: Path):
    corpus = Path(__file__).resolve().parents[1] / "verify" / "language_corpus"
    (tmp_path / "main.go").write_text(
        'package main\ntype Items struct{}\n'
        'func (i *Items) List() {}\n'
        'func main() { router.GET("/items", items.List) }\n'
    )
    for stack, directory, root in (
        ("go", "catalog-go-service", tmp_path),
        ("jvm-spring", "menu-kotlin-service", corpus / "menu-kotlin-service"),
    ):
        result = StaticAnalysisEngine().analyze(root, stack)

        snapshot = project_analysis(ServiceKey(directory), result)

        assert snapshot.facts
        assert all(fact.sources and fact.subject.service.value == directory for fact in snapshot.facts)
        if stack == "jvm-spring":
            assert {"entrypoint", "symbol", "injection", "flow_edge"} <= {
                fact.kind for fact in snapshot.facts
            }


def test_relationship_projection_preserves_origin_confidence_and_all_sources():
    analysis = AnalysisResult(
        edges=[
            FlowEdge("Menu.list", "MenuService.find", "invokes", Evidence("Menu.kt", 10, 10)),
            FlowEdge("Menu.list", "MenuService.find", "invokes", Evidence("Service.kt", 20, 20)),
            FlowEdge("Menu.list", "MenuService.find", "invokes", Evidence("graph.json", 1, 1),
                     confidence="medium", origin="codegraph"),
        ],
        static_service_calls=[
            StaticServiceCall("Menu.list", "restaurant-service", "http", "GET", "/restaurants/{id}",
                              Evidence("Client.kt", 4, 6)),
            StaticServiceCall("Menu.list", "restaurant-service", "http", "GET", "/restaurants/{id}",
                              Evidence("Client.kt", 8, 9)),
        ],
    )

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    assert len(snapshot.facts) == 3
    static_edge, inferred_edge, call = snapshot.facts
    assert static_edge.subject == SymbolKey(ServiceKey("menu-manager"), "Menu.list")
    assert (static_edge.kind, static_edge.status, static_edge.origin, static_edge.attributes) == (
        "flow_edge", FactStatus.CONFIRMED, "static",
        {"relation": "invokes", "target": "MenuService.find", "confidence": "high"},
    )
    assert [source.file_path for source in static_edge.sources] == ["Menu.kt", "Service.kt"]
    assert (inferred_edge.status, inferred_edge.origin, inferred_edge.attributes["confidence"]) == (
        FactStatus.INFERRED, "codegraph", "medium",
    )
    assert inferred_edge.id != static_edge.id
    assert (call.kind, call.status, call.attributes) == (
        "service_call", FactStatus.CONFIRMED,
        {"target_service": "restaurant-service", "protocol": "http", "target_method": "GET",
         "target_path": "/restaurants/{id}"},
    )
    assert len(call.sources) == 2
    assert project_analysis(ServiceKey("menu-manager"), AnalysisResult(edges=[analysis.edges[1]])).facts[0].id == (
        static_edge.id
    )


def test_configuration_security_and_error_projection_preserves_meaning_and_sources():
    analysis = AnalysisResult(
        configuration_bindings=[
            ConfigurationBinding("MenuClient", "restaurant.base-url", "property", False, Evidence("Client.kt", 4, 4)),
            ConfigurationBinding("MenuClient", "restaurant.base-url", "property", False, Evidence("Config.kt", 8, 8)),
        ],
        security_requirements=[
            SecurityRequirement(None, None, "MenuController.list", "hasRole", ("ADMIN",),
                                Evidence("MenuController.kt", 10, 10)),
            SecurityRequirement("**", None, None, "custom:MenuPolicy", (), Evidence("Security.kt", 15, 17)),
        ],
        error_contracts=[
            ErrorContract("MenuController.list", "maps", "not_found", "MenuNotFound", "http", "404", "MENU_404",
                          False, "never", Evidence("Errors.kt", 20, 24)),
        ],
    )

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    assert len(snapshot.facts) == 4
    config, symbol_rule, route_rule, error = snapshot.facts
    assert (config.kind, config.subject, config.attributes, config.status) == (
        "configuration", SymbolKey(ServiceKey("menu-manager"), "MenuClient"),
        {"key": "restaurant.base-url", "binding_kind": "property", "sensitive": False}, FactStatus.CONFIRMED,
    )
    assert [source.file_path for source in config.sources] == ["Client.kt", "Config.kt"]
    assert (symbol_rule.kind, symbol_rule.subject, symbol_rule.attributes) == (
        "security_requirement", SymbolKey(ServiceKey("menu-manager"), "MenuController.list"),
        {"requirement": "hasRole", "roles": ["ADMIN"]},
    )
    assert (route_rule.subject, route_rule.status, route_rule.attributes) == (
        RoutePatternKey(ServiceKey("menu-manager"), None, "**"), FactStatus.UNKNOWN,
        {"requirement": "custom:MenuPolicy", "roles": []},
    )
    assert (error.kind, error.subject, error.status, error.attributes) == (
        "error_contract", SymbolKey(ServiceKey("menu-manager"), "MenuController.list"), FactStatus.CONFIRMED,
        {"role": "maps", "error_kind": "not_found", "internal_type": "MenuNotFound", "protocol": "http",
         "transport_code": "404", "public_code": "MENU_404", "exposes_internal_detail": False,
         "retryability": "never"},
    )
    assert project_analysis(
        ServiceKey("menu-manager"), AnalysisResult(configuration_bindings=[analysis.configuration_bindings[1]]),
    ).facts[0].id == config.id


def test_security_projection_rejects_rule_without_exactly_one_subject():
    invalid = SecurityRequirement("/menus", "GET", "MenuController.list", "hasRole", ("ADMIN",),
                                  Evidence("Security.kt", 1, 1))

    with pytest.raises(ValueError, match="security requirement subject"):
        project_analysis(ServiceKey("menu-manager"), AnalysisResult(security_requirements=[invalid]))


def test_message_and_persistence_projection_preserves_contracts_and_sources():
    analysis = AnalysisResult(
        message_contracts=[
            MessageContract("publishes", "menus", "created", "MenuCreated", Evidence("Publisher.kt", 5, 5), "2"),
            MessageContract("publishes", "menus", "created", "MenuCreated", Evidence("Publisher.kt", 8, 8), "2"),
            MessageContract("consumes", "menus", None, "MenuCreated", Evidence("Listener.kt", 3, 3), "2"),
        ],
        persistence_facts=[
            PersistenceFact("menu", "sql_table", "Menu", Evidence("Menu.kt", 1, 4)),
            PersistenceFact("menu", "sql_table", "Menu", Evidence("Schema.sql", 10, 12)),
        ],
        migration_facts=[
            MigrationFact("add_column", "menu", "published", False, Evidence("V2.sql", 2, 2)),
            MigrationFact("drop_column", "menu", "legacy", True, Evidence("V3.sql", 3, 3)),
        ],
    )

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    assert len(snapshot.facts) == 5
    published, consumed, resource, added, dropped = snapshot.facts
    assert (published.kind, published.subject, published.attributes) == (
        "message_contract", MessageChannelKey(ServiceKey("menu-manager"), "menus"),
        {"direction": "publishes", "routing_key": "created", "payload_type": "MenuCreated", "message_version": "2"},
    )
    assert len(published.sources) == 2
    assert consumed.attributes["direction"] == "consumes" and consumed.id != published.id
    assert (resource.kind, resource.subject, resource.attributes) == (
        "persistence_resource", PersistenceResourceKey(ServiceKey("menu-manager"), "sql_table", "menu"),
        {"owner": "Menu"},
    )
    assert len(resource.sources) == 2
    assert (added.kind, added.subject, added.attributes) == (
        "migration", resource.subject,
        {"operation": "add_column", "column_name": "published", "destructive": False},
    )
    assert dropped.attributes["destructive"] is True and dropped.id != added.id
    assert project_analysis(ServiceKey("menu-manager"), AnalysisResult(message_contracts=[analysis.message_contracts[1]])
                            ).facts[0].id == published.id


def test_flow_structure_projection_keeps_declarations_and_unresolved_boundaries_distinct():
    first_symbol = Symbol(
        "MenuController.list", "MenuController", "list", Evidence("Menu.kt", 8, 12),
        implements=("MenuApi",), imports=(("MenuService", "app.MenuService"),),
        qualifiers=("primary",), primary=True,
    )
    duplicate_symbol = Symbol(
        "MenuController.list", "MenuController", "list", Evidence("Menu.kt", 20, 24),
        implements=("MenuApi",), imports=(("MenuService", "app.MenuService"),),
        qualifiers=("primary",), primary=True,
    )
    analysis = AnalysisResult(
        symbols=[first_symbol, duplicate_symbol],
        injections=[
            Injection("MenuController", "MenuService", "primary", Evidence("Menu.kt", 3, 3)),
            Injection("MenuController", "MenuService", "primary", Evidence("Config.kt", 9, 9)),
        ],
        boundaries=[
            FlowBoundary("MenuController.list", "branch", Evidence("Menu.kt", 11, 11)),
            FlowBoundary("MenuController.list", "branch", Evidence("Menu.kt", 22, 22)),
        ],
    )

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    assert len(snapshot.facts) == 3
    symbol, injection, boundary = snapshot.facts
    assert (symbol.kind, symbol.subject, symbol.attributes) == (
        "symbol", SymbolKey(ServiceKey("menu-manager"), "MenuController.list"),
        {"owner": "MenuController", "member": "list", "implements": ["MenuApi"],
         "imports": [["MenuService", "app.MenuService"]], "qualifiers": ["primary"], "primary": True},
    )
    assert len(symbol.sources) == 2
    assert (injection.kind, injection.subject, injection.attributes) == (
        "injection", SymbolKey(ServiceKey("menu-manager"), "MenuController"),
        {"contract": "MenuService", "qualifier": "primary"},
    )
    assert len(injection.sources) == 2
    assert (boundary.kind, boundary.subject, boundary.status, boundary.attributes) == (
        "flow_boundary", SymbolKey(ServiceKey("menu-manager"), "MenuController.list"),
        FactStatus.CONFIRMED, {"boundary_kind": "branch"},
    )
    assert len(boundary.sources) == 2
    assert not any(fact.kind == "flow_edge" for fact in snapshot.facts)


def test_remaining_static_facts_preserve_identity_and_unresolved_targets():
    analysis = AnalysisResult(
        grpc_handlers=[
            GrpcHandler("menu.v1.Menu", "Get", "MenuHandler.Get", Evidence("menu.go", 3, 5)),
        ],
        grpc_client_bindings=[
            GrpcClientBinding("MenuClient", "stub", "restaurant.v1.Restaurant", Evidence("client.go", 4, 4)),
        ],
        resilience_policies=[
            ResiliencePolicy("MenuClient.Get", "timeout", "grpc_deadline", 500, "ms", Evidence("client.go", 8, 8)),
        ],
        feature_flags=[
            FeatureFlag("MenuController.list", "menu.new-flow", "launchdarkly", Evidence("flags.kt", 7, 7)),
        ],
        cloud_facts=[
            CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk", None,
                      Evidence("publisher.ts", 10, 10)),
            CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk", None,
                      Evidence("publisher.ts", 12, 12)),
        ],
        api_headers=[
            ApiHeader("GET", "/menus", "request", "X-Trace", Evidence("Menu.kt", 11, 11)),
            ApiHeader("GET", "/menus", "response", "X-Trace", Evidence("Menu.kt", 19, 19)),
        ],
    )

    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    assert len(snapshot.facts) == 7
    handler, client, resilience, flag, cloud, request_header, response_header = snapshot.facts
    assert (handler.kind, handler.subject, handler.attributes) == (
        "grpc_handler", SymbolKey(ServiceKey("menu-manager"), "MenuHandler.Get"),
        {"grpc_service": "menu.v1.Menu", "rpc": "Get"},
    )
    assert (client.kind, client.attributes) == (
        "grpc_client_binding", {"member": "stub", "grpc_service": "restaurant.v1.Restaurant"},
    )
    assert (resilience.kind, resilience.attributes) == (
        "resilience_policy", {"policy_kind": "timeout", "mechanism": "grpc_deadline", "value": 500, "unit": "ms"},
    )
    assert (flag.kind, flag.attributes) == ("feature_flag", {"key": "menu.new-flow", "provider": "launchdarkly"})
    assert (cloud.kind, cloud.subject, cloud.status, cloud.attributes) == (
        "cloud_operation", CloudResourceKey(ServiceKey("menu-manager"), "aws", "queue", None),
        FactStatus.CONFIRMED,
        {"service_name": "sqs", "operation": "SendMessage", "operation_kind": "publish", "sdk": "aws-sdk"},
    )
    assert len(cloud.sources) == 2
    assert (request_header.kind, request_header.subject, request_header.attributes) == (
        "api_header", RoutePatternKey(ServiceKey("menu-manager"), "GET", "/menus"),
        {"direction": "request", "name": "X-Trace"},
    )
    assert response_header.id != request_header.id
    assert not any("value" in fact.attributes for fact in (request_header, response_header))
