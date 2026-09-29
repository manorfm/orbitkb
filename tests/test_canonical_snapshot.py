import ast
from pathlib import Path

import pytest

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    StaticServiceCall,
)
from orbitkb.domain.canonical import FactStatus, ServiceKey, SymbolKey


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
