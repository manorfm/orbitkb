"""A fictional Kotlin/Spring sample exercises one complex route end to end."""

from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.discovery.jvm_stack import JvmSpringDetector
from orbitkb.domain.canonical import ServiceKey
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.generation.route_evidence import route_sufficiency

SAMPLE = Path(__file__).resolve().parents[1] / "verify/flow_corpus/sample-order-kotlin-service"
ROUTE = "/venues/{restaurantId}/spots/{tableId}/checks/{billId}/items"


def test_sample_uses_fictitious_identifiers_and_has_typed_redis_event():
    files = tuple(SAMPLE.rglob("*.kt"))
    source = "\n".join(path.read_text() for path in files)

    assert files
    assert all("package com.example.sampleorder" in path.read_text().splitlines()[0] for path in files)
    assert '"/venues/{restaurantId}/spots/{tableId}/checks/{billId}"' in source
    assert '@PostMapping("/items")' in source
    assert "events:spot:$tableId" in source
    assert "sealed class TableSseEvent" in source
    assert "data class ItemAdded(" in source
    assert "val price: BigDecimal" in source
    assert "val timestamp: Instant = Instant.now()" in source
    assert "price = item.item.price" in source
    assert "response.price" in source
    assert "jsonMapper.writeValueAsString(event)" in source


def test_sample_exposes_a_real_controller_route_without_feign_routes():
    detector = JvmSpringDetector()
    hints = detector.collect_hints(SAMPLE)
    analysis = StaticAnalysisEngine().analyze(SAMPLE, "jvm-spring")

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("POST", ROUTE)]
    assert [(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"] == [
        ("POST", ROUTE),
    ]
    assert hints.entry_excerpt is not None
    assert "fun main(args: Array<String>)" in hints.entry_excerpt.text
    assert "@EnableFeignClients" in hints.entry_excerpt.text
    assert "@EnableRabbit" in hints.entry_excerpt.text
    assert not analysis.message_contracts  # @EnableRabbit does not prove an actual publisher/consumer.


def test_sample_has_source_proven_security_and_catalog_calls():
    analysis = StaticAnalysisEngine().analyze(SAMPLE, "jvm-spring")

    assert any(
        requirement.requirement == "authenticated" and requirement.route_pattern == "**"
        for requirement in analysis.security_requirements
    )
    assert {
        (call.target_method, call.target_path)
        for call in analysis.static_service_calls if call.target_service == "catalog-service"
    } == {
        ("GET", "/venues/{restaurantId}/catalogs/{menuId}/products/{itemId}/summary"),
        ("GET", "/venues/{restaurantId}/ingredients/{ingredientId}"),
    }
    assert {(fact.name, fact.kind) for fact in analysis.persistence_facts} == {("checks", "document")}


def test_sample_route_reaches_catalog_but_remains_ineligible_for_zero_call():
    analysis = StaticAnalysisEngine().analyze(SAMPLE, "jvm-spring")
    request = analysis.contracts["BillOrderController.addItem"]["request"]
    assert request["fields"] == [
        {"name": "id", "type": "ULID", "required": True, "validations": []},
        {"name": "menuId", "type": "ULID", "required": True, "validations": []},
        {"name": "annotation", "type": "String", "required": False, "validations": []},
        {"name": "ingredientsAdded", "type": "List<IngredientIn>", "required": False, "validations": []},
        {"name": "ingredientsRemoved", "type": "List<IngredientIn>", "required": False, "validations": []},
    ]
    snapshot = project_analysis(ServiceKey("sample-order"), analysis)
    route = next(
        fact.subject for fact in snapshot.facts
        if fact.kind == "entrypoint" and fact.subject.name == ROUTE
    )
    reached = KnowledgeNavigator(snapshot).reachable(route, TraversalPolicy())

    catalog_calls = [fact for fact in reached.facts if fact.kind == "service_call"]
    assert {fact.attributes["target_path"] for fact in catalog_calls} == {
        "/venues/{restaurantId}/catalogs/{menuId}/products/{itemId}/summary",
        "/venues/{restaurantId}/ingredients/{ingredientId}",
    }
    assert all("FetchItemMediator.get" in reached.path_to(call.id) for call in catalog_calls)

    sufficiency = route_sufficiency(snapshot, "POST", ROUTE)
    assert sufficiency is not None
    assert sufficiency.overall.value == "missing"
    assert sufficiency.status("request_shape").value == "enough"
    assert sufficiency.status("response_shape").value == "missing"
    assert sufficiency.status("business_behavior").value == "missing"
    assert sufficiency.status("flow").value == "ambiguous"
