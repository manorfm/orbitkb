from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.domain.canonical import EntrypointKey, ServiceKey
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile, EvidenceSet
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.domain.reduction import EvidenceBudget, EvidenceReducer
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyStatus,
)

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


def _capsule(max_chars=100_000):
    snapshot = project_analysis(ServiceKey("menu-manager"), StaticAnalysisEngine().analyze(CORPUS, "jvm-spring"))
    route = next(fact.subject for fact in snapshot.facts
                 if fact.kind == "entrypoint" and fact.subject.name == "/menus/{id}")
    profile = EvidenceProfile(frozenset({"entrypoint", "flow_edge", "service_call", "security_requirement"}))
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(route, profile, TraversalPolicy())
    return EvidenceReducer().reduce((evidence,), EvidenceBudget(max_chars))


def test_spring_route_has_proven_call_and_response_but_missing_semantic_documentation_fields():
    capsule = _capsule()

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("contract") == SufficiencyStatus.ENOUGH
    assert result.status("integrations") == SufficiencyStatus.ENOUGH
    assert result.status("business_behavior") == SufficiencyStatus.MISSING
    assert result.status("request_shape") == SufficiencyStatus.MISSING
    assert result.status("response_shape") == SufficiencyStatus.ENOUGH
    assert result.status("authorization") == SufficiencyStatus.AMBIGUOUS
    assert result.overall != SufficiencyStatus.ENOUGH
    assert result.evidence_ids("integrations") == tuple(
        fact.id for fact in capsule.facts if fact.kind == "service_call"
    )


def test_omitted_call_cannot_be_marked_sufficient():
    capsule = _capsule(max_chars=1)

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert result.status("contract") != SufficiencyStatus.ENOUGH
    assert result.omitted_fact_ids == capsule.report.omitted_fact_ids


def test_unsupported_transport_stays_explicit_even_with_no_retained_facts():
    route = EntrypointKey(ServiceKey("orders"), "consumer", "CONSUME", "orders.created", "Orders.onCreated")
    capsule = EvidenceReducer().reduce((EvidenceSet(route, (), (), False),), EvidenceBudget(0))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.overall == SufficiencyStatus.UNSUPPORTED
    assert result.status("contract") == SufficiencyStatus.UNSUPPORTED
