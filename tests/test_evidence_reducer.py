from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.domain.canonical import ServiceKey
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.domain.reduction import EvidenceBudget, EvidenceReducer

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


def _route_evidence():
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({"entrypoint", "flow_edge", "service_call"}))
    routes = sorted((fact.subject for fact in snapshot.facts if fact.kind == "entrypoint"),
                    key=lambda entrypoint: entrypoint.name)
    return tuple(composer.compose(route, profile, TraversalPolicy()) for route in routes)


def test_shared_feign_fact_is_sent_once_with_both_route_paths():
    evidence = _route_evidence()

    capsule = EvidenceReducer().reduce(evidence, EvidenceBudget(max_chars=100_000))

    calls = [fact for fact in capsule.facts if fact.kind == "service_call"]
    assert len(calls) == 1
    assert calls[0].value["target_service"] == "restaurant-service"
    assert calls[0].sources[0].file_path.endswith("MenuGateway.kt")
    uses = [use for use in capsule.uses if use.fact_id == calls[0].id]
    assert {use.entrypoint.name for use in uses} == {"/menus/{id}", "/menus/by-restaurant/{id}"}
    assert {use.path[0] for use in uses} == {"MenuController.get", "MenuController.byRestaurant"}
    assert all(use.path[-1] == "MenuGateway.fetch" for use in uses)
    assert capsule.report.input_facts > capsule.report.unique_facts
    assert capsule.report.omitted_fact_ids == ()
    assert capsule.report.estimated_chars < capsule.report.estimated_input_chars
    assert not capsule.truncated


def test_budget_reports_every_omitted_fact_and_keeps_navigation_limits():
    evidence = _route_evidence()

    capsule = EvidenceReducer().reduce(evidence, EvidenceBudget(max_chars=1))

    assert capsule.report.estimated_chars <= 1
    assert capsule.report.omitted_fact_ids
    assert set(capsule.report.omitted_fact_ids) == {
        fact.id for route in evidence for fact in route.facts
    }
    assert capsule.truncated
    assert any(boundary.reason == "unresolved" for boundary in capsule.boundaries)


def test_source_truncation_remains_visible_without_budget_omission():
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)
    route = next(fact.subject for fact in snapshot.facts
                 if fact.kind == "entrypoint" and fact.subject.name == "/menus/{id}")
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(
        route, EvidenceProfile(frozenset({"entrypoint", "service_call"})),
        TraversalPolicy(max_edges=1),
    )

    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(max_chars=100_000))

    assert capsule.truncated
    assert capsule.report.omitted_fact_ids == ()
    assert any(boundary.reason == "edge_limit" for boundary in capsule.boundaries)


def test_budget_keeps_http_destination_before_optional_flow_edges():
    evidence = _route_evidence()
    full = EvidenceReducer().reduce(evidence, EvidenceBudget(max_chars=100_000))

    budget = full.report.estimated_chars // 2
    capsule = EvidenceReducer().reduce(evidence, EvidenceBudget(max_chars=budget))

    assert any(fact.kind == "service_call" for fact in capsule.facts)
    assert capsule.report.omitted_fact_ids
    assert capsule.report.estimated_chars <= budget
