from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    SecurityRequirement,
)
from orbitkb.domain.canonical import FactStatus, ServiceKey
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


def test_composed_route_evidence_preserves_feign_path_source_and_digest():
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)
    entrypoint = next(fact.subject for fact in snapshot.facts
                      if fact.kind == "entrypoint" and fact.subject.name == "/menus/{id}")
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({"entrypoint", "flow_edge", "service_call"}))

    evidence = composer.compose(entrypoint, profile, TraversalPolicy())
    repeated = composer.compose(entrypoint, profile, TraversalPolicy())

    call = next(fact for fact in evidence.facts if fact.kind == "service_call")
    assert call.value["target_service"] == "restaurant-service"
    assert call.path == ("MenuController.get", "MenuUseCase.get", "MenuGateway.fetch")
    assert call.sources[0].file_path.endswith("MenuGateway.kt")
    assert call.status == FactStatus.CONFIRMED
    assert call.confidence is None
    assert len(call.digest) == 64
    assert call.digest == next(fact.digest for fact in repeated.facts if fact.id == call.id)
    assert len({fact.id for fact in evidence.facts}) == len(evidence.facts)
    assert {fact.kind for fact in evidence.facts} == profile.kinds
    assert any(boundary.reason == "external_call" and boundary.target == "restaurantClient.getRestaurant"
               for boundary in evidence.boundaries)
    assert not any(boundary.reason == "unresolved" for boundary in evidence.boundaries)
    assert not evidence.truncated


def test_composer_reports_limited_flow_instead_of_silent_omission():
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)
    entrypoint = next(fact.subject for fact in snapshot.facts
                      if fact.kind == "entrypoint" and fact.subject.name == "/menus/{id}")
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))

    evidence = composer.compose(entrypoint, EvidenceProfile(frozenset({"service_call"})),
                                TraversalPolicy(max_edges=1))

    assert evidence.truncated
    assert any(boundary.reason == "edge_limit" for boundary in evidence.boundaries)
    assert not evidence.facts


def test_route_security_uses_method_and_first_matching_rule():
    source = Evidence("Security.kt", 1, 1)
    snapshot = project_analysis(ServiceKey("orders"), AnalysisResult(
        entrypoints=[
            EntryPoint("http", "GET", "/orders/{id}", "Orders.get", source),
            EntryPoint("http", "POST", "/orders/{id}", "Orders.post", source),
        ],
        security_requirements=[
            SecurityRequirement("/orders/*", "GET", None, "permitAll", (), source),
            SecurityRequirement("/orders/{orderId}", "POST", None, "hasRole", ("ADMIN",), source),
            SecurityRequirement("**", None, None, "authenticated", (), source),
        ],
    ))
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({"security_requirement"}))

    selected = {
        route.method: [fact.value["requirement"] for fact in composer.compose(
            route, profile, TraversalPolicy(),
        ).facts]
        for route in (fact.subject for fact in snapshot.facts if fact.kind == "entrypoint")
    }

    assert selected == {"GET": ["permitAll"], "POST": ["hasRole"]}
