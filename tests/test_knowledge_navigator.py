import pytest

from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    EntrypointKey,
    FactStatus,
    ServiceKey,
    SourceReference,
    SymbolKey,
)
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy

SERVICE = ServiceKey("menu-manager")
ENTRY = EntrypointKey(SERVICE, "http", "GET", "/menus", "MenuController.list")


def fact(identifier, kind, subject, attributes=None, source="Menu.kt"):
    return CanonicalFact(identifier, kind, subject, attributes or {}, FactStatus.CONFIRMED,
                         "static", (SourceReference(source, 1, 2),))


def menu_snapshot():
    controller = SymbolKey(SERVICE, "MenuController.list")
    use_case = SymbolKey(SERVICE, "MenuUseCase.list")
    gateway = SymbolKey(SERVICE, "MenuGateway.fetch")
    return CanonicalSnapshot(SERVICE, (
        fact(ENTRY.fact_id, "entrypoint", ENTRY),
        fact("controller", "symbol", controller),
        fact("use-case", "symbol", use_case),
        fact("gateway", "symbol", gateway),
        fact("controller-use-case", "flow_edge", controller,
             {"relation": "invokes", "target": use_case.name}, "Controller.kt"),
        fact("use-case-gateway", "flow_edge", use_case,
             {"relation": "invokes", "target": gateway.name}, "UseCase.kt"),
        fact("gateway-http", "service_call", gateway,
             {"target_service": "restaurant-service", "protocol": "http", "target_path": "/restaurants"},
             "Gateway.kt"),
        fact("gateway-cycle", "flow_edge", gateway,
             {"relation": "invokes", "target": controller.name}, "Gateway.kt"),
        fact("gateway-unknown", "flow_edge", gateway,
             {"relation": "invokes", "target": "UnknownClient.call"}, "Gateway.kt"),
        fact("unrelated", "symbol", SymbolKey(SERVICE, "Other.list")),
    ))


def test_reachable_returns_only_evidenced_path_facts_and_explicit_boundaries():
    result = KnowledgeNavigator(menu_snapshot()).reachable(ENTRY, TraversalPolicy())

    assert "gateway-http" in {item.id for item in result.facts}
    assert "unrelated" not in {item.id for item in result.facts}
    assert result.path_to("gateway-http") == (
        "MenuController.list", "MenuUseCase.list", "MenuGateway.fetch",
    )
    assert next(item for item in result.facts if item.id == "gateway-http").sources == (
        SourceReference("Gateway.kt", 1, 2),
    )
    assert {(item.reason, item.target) for item in result.boundaries} == {
        ("cycle", "MenuController.list"), ("unresolved", "UnknownClient.call"),
    }
    assert result.truncated is False


def test_policy_limits_depth_nodes_and_edge_types_without_silent_truncation():
    navigator = KnowledgeNavigator(menu_snapshot())
    depth = navigator.reachable(ENTRY, TraversalPolicy(max_depth=1))
    nodes = navigator.reachable(ENTRY, TraversalPolicy(max_nodes=2))
    filtered = navigator.reachable(ENTRY, TraversalPolicy(relations=frozenset({"injects"})))

    assert "gateway-http" not in {item.id for item in depth.facts}
    assert (depth.truncated, {item.reason for item in depth.boundaries}) == (True, {"depth_limit"})
    assert (nodes.truncated, {item.reason for item in nodes.boundaries}) == (True, {"node_limit"})
    assert {item.id for item in filtered.facts} == {ENTRY.fact_id, "controller"}
    assert filtered.truncated is False


def test_edge_limit_reports_truncation_and_unknown_entrypoint_fails():
    navigator = KnowledgeNavigator(menu_snapshot())
    limited = navigator.reachable(ENTRY, TraversalPolicy(max_edges=1))

    assert limited.truncated is True
    assert {item.reason for item in limited.boundaries} == {"edge_limit"}
    with pytest.raises(KeyError, match="entrypoint"):
        navigator.reachable(EntrypointKey(SERVICE, "http", "POST", "/missing", "Missing.call"), TraversalPolicy())


def test_known_boundary_is_reported_without_fabricating_a_destination():
    snapshot = menu_snapshot()
    gateway = SymbolKey(SERVICE, "MenuGateway.fetch")
    snapshot = CanonicalSnapshot(SERVICE, (*snapshot.facts,
        fact("dynamic-client", "flow_boundary", gateway, {"boundary_kind": "dynamic_dispatch"}),
    ))

    result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())

    assert ("known_boundary", "dynamic_dispatch") in {(item.reason, item.target) for item in result.boundaries}
    assert result.path_to("dynamic-client")[-1] == "MenuGateway.fetch"


def test_unique_source_matched_service_call_is_an_external_boundary():
    gateway = SymbolKey(SERVICE, "MenuGateway.fetch")
    snapshot = CanonicalSnapshot(SERVICE, (
        fact(ENTRY.fact_id, "entrypoint", ENTRY),
        fact("to-gateway", "flow_edge", SymbolKey(SERVICE, ENTRY.symbol),
             {"relation": "invokes", "target": gateway.name}, "Controller.kt"),
        fact("remote-edge", "flow_edge", gateway,
             {"relation": "invokes", "target": "client.fetch"}, "Client.kt"),
        fact("remote-call", "service_call", gateway,
             {"target_service": "catalog", "protocol": "http", "target_path": "/items"}, "Client.kt"),
    ))

    result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())

    assert ("external_call", "client.fetch") in {(item.reason, item.target) for item in result.boundaries}
    assert result.path_to("remote-call") == (ENTRY.symbol, gateway.name)


def test_external_boundary_requires_unique_edge_and_same_source():
    gateway = SymbolKey(SERVICE, "MenuGateway.fetch")
    base = (
        fact(ENTRY.fact_id, "entrypoint", ENTRY),
        fact("to-gateway", "flow_edge", SymbolKey(SERVICE, ENTRY.symbol),
             {"relation": "invokes", "target": gateway.name}, "Controller.kt"),
        fact("remote-edge", "flow_edge", gateway,
             {"relation": "invokes", "target": "client.fetch"}, "Client.kt"),
    )
    different_source = CanonicalSnapshot(SERVICE, (*base,
        fact("remote-call", "service_call", gateway,
             {"target_service": "catalog", "protocol": "http"}, "Other.kt"),
    ))
    competing_edge = CanonicalSnapshot(SERVICE, (*base,
        fact("other-edge", "flow_edge", gateway,
             {"relation": "invokes", "target": "client.other"}, "Client.kt"),
        fact("remote-call", "service_call", gateway,
             {"target_service": "catalog", "protocol": "http"}, "Client.kt"),
    ))

    for snapshot in (different_source, competing_edge):
        result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())
        assert ("unresolved", "client.fetch") in {(item.reason, item.target) for item in result.boundaries}


def test_unproven_read_does_not_become_a_persistence_boundary():
    snapshot = CanonicalSnapshot(SERVICE, (
        fact(ENTRY.fact_id, "entrypoint", ENTRY),
        fact("unproven-read", "flow_edge", SymbolKey(SERVICE, ENTRY.symbol),
             {"relation": "reads", "target": "repository.findByStatus"}),
    ))

    result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())

    assert [(boundary.reason, boundary.target) for boundary in result.boundaries] == [
        ("unresolved", "repository.findByStatus"),
    ]


def test_inferred_persistence_marker_does_not_prove_a_boundary():
    edge = CanonicalFact(
        "inferred-read", "flow_edge", SymbolKey(SERVICE, ENTRY.symbol),
        {"relation": "reads", "target": "repository.findByStatus", "boundary_kind": "persistence"},
        FactStatus.INFERRED, "codegraph", (SourceReference("Menu.kt", 1, 2),),
    )
    snapshot = CanonicalSnapshot(SERVICE, (fact(ENTRY.fact_id, "entrypoint", ENTRY), edge))

    result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())

    assert [(boundary.reason, boundary.target) for boundary in result.boundaries] == [
        ("unresolved", "repository.findByStatus"),
    ]


def test_validation_relation_reaches_its_evidence():
    validator = SymbolKey(SERVICE, "MenuValidator.validate")
    controller = SymbolKey(SERVICE, ENTRY.symbol)
    snapshot = CanonicalSnapshot(SERVICE, (
        fact(ENTRY.fact_id, "entrypoint", ENTRY),
        fact("validation-edge", "flow_edge", controller,
             {"relation": "validates", "target": validator.name}),
        fact("validation-error", "error_contract", validator, {"error_kind": "validation"}),
    ))

    result = KnowledgeNavigator(snapshot).reachable(ENTRY, TraversalPolicy())

    assert "validation-error" in {item.id for item in result.facts}


@pytest.mark.parametrize("values", [{"max_depth": -1}, {"max_nodes": 0}, {"max_edges": 0}])
def test_policy_rejects_invalid_bounds(values):
    with pytest.raises(ValueError):
        TraversalPolicy(**values)
