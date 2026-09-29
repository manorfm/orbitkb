from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
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
    assert any(boundary.reason == "unresolved" for boundary in evidence.boundaries)
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
