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


@pytest.mark.parametrize("values", [{"max_depth": -1}, {"max_nodes": 0}, {"max_edges": 0}])
def test_policy_rejects_invalid_bounds(values):
    with pytest.raises(ValueError):
        TraversalPolicy(**values)
