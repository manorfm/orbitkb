from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.domain.canonical import EntrypointKey, FactStatus, ServiceKey
from orbitkb.domain.evidence import (
    EvidenceComposer,
    EvidenceFact,
    EvidenceProfile,
    EvidenceSet,
)
from orbitkb.domain.navigation import (
    KnowledgeNavigator,
    TraversalBoundary,
    TraversalPolicy,
)
from orbitkb.domain.reduction import EvidenceBudget, EvidenceReducer
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyStatus,
)

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"
STATUS_CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/status-kotlin-service"


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
    assert result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS
    assert result.evidence_ids("integration_purpose") == result.evidence_ids("integrations")
    assert result.status("business_behavior") == SufficiencyStatus.MISSING
    assert result.status("request_shape") == SufficiencyStatus.MISSING
    assert result.status("response_shape") == SufficiencyStatus.ENOUGH
    assert result.status("authorization") == SufficiencyStatus.AMBIGUOUS
    assert result.overall != SufficiencyStatus.ENOUGH
    assert result.evidence_ids("integrations") == tuple(
        fact.id for fact in capsule.facts if fact.kind == "service_call"
    )


def test_local_kotlin_dto_construction_does_not_make_route_flow_ambiguous():
    capsule = _capsule()

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("flow") == SufficiencyStatus.ENOUGH
    assert not any(boundary.target == "Menu" for boundary in capsule.boundaries)


def test_explicit_openapi_description_proves_only_matching_route_business_behavior(tmp_path: Path):
    (tmp_path / "OrdersController.java").write_text(
        '''@RestController
class OrdersController {
  @GetMapping("/orders")
  Order list() { return new Order(); }
  @GetMapping("/orders/{id}")
  Order get(String id) { return new Order(); }
}
''', encoding="utf-8",
    )
    (tmp_path / "openapi.yaml").write_text(
        '''openapi: 3.0.3
paths:
  /orders:
    get:
      description: Returns orders available to the caller.
      responses:
        "200": {}
  /orders/{id}:
    get:
      summary: Finds an order
      responses:
        "200": {}
''', encoding="utf-8",
    )
    snapshot = project_analysis(ServiceKey("orders"), StaticAnalysisEngine().analyze(tmp_path, "jvm-spring"))
    profile = EvidenceProfile(frozenset({"entrypoint"}))
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    for path, expected in (("/orders", SufficiencyStatus.ENOUGH),
                           ("/orders/{id}", SufficiencyStatus.MISSING)):
        route = next(fact.subject for fact in snapshot.facts
                     if fact.kind == "entrypoint" and fact.subject.name == path)
        capsule = EvidenceReducer().reduce((composer.compose(route, profile, TraversalPolicy()),),
                                           EvidenceBudget(20_000))
        result = DeterministicSufficiencyEvaluator().evaluate(capsule)

        assert result.status("business_behavior") == expected
        assert result.status("request_shape") == SufficiencyStatus.ENOUGH
        assert result.overall != SufficiencyStatus.ENOUGH
        if expected == SufficiencyStatus.ENOUGH:
            assert result.evidence_ids("business_behavior") == (route.fact_id,)
            assert any(source.file_path == "openapi.yaml" for source in capsule.facts[0].sources)


def test_optional_or_unresolved_openapi_body_does_not_prove_empty_request_shape():
    route = EntrypointKey(ServiceKey("orders"), "http", "POST", "/orders", "Orders.create")

    def assess(present: bool | None) -> SufficiencyStatus:
        entry = EvidenceFact(
            "entry", "entrypoint", {"contract": {"formal_contract": {
                "format": "openapi", "request_body_present": present,
            }}}, FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "entry",
        )
        capsule = EvidenceReducer().reduce((EvidenceSet(route, (entry,), (), False),), EvidenceBudget(20_000))
        return DeterministicSufficiencyEvaluator().evaluate(capsule).status("request_shape")

    assert assess(False) == SufficiencyStatus.ENOUGH
    assert assess(True) == SufficiencyStatus.MISSING
    assert assess(None) == SufficiencyStatus.MISSING


def test_unresolved_flow_cannot_prove_that_no_call_purpose_is_needed():
    route = EntrypointKey(ServiceKey("orders"), "http", "GET", "/orders", "Orders.list")
    unresolved = EvidenceSet(route, (), (
        TraversalBoundary(route.symbol, "client.fetch", "unresolved", "flow"),
    ), False)
    capsule = EvidenceReducer().reduce((unresolved,), EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS


def test_simple_local_kotlin_route_has_known_empty_integrations():
    snapshot = project_analysis(ServiceKey("status"), StaticAnalysisEngine().analyze(STATUS_CORPUS, "jvm-spring"))
    route = next(fact.subject for fact in snapshot.facts if fact.kind == "entrypoint")
    profile = EvidenceProfile(frozenset({
        "entrypoint", "flow_edge", "service_call", "security_requirement", "analysis_capability",
    }))
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(route, profile, TraversalPolicy())
    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert not capsule.boundaries
    assert result.status("integrations") == SufficiencyStatus.ENOUGH
    assert result.status("integration_purpose") == SufficiencyStatus.ENOUGH
    assert result.evidence_ids("integrations") == (route.fact_id,)
    assert result.overall == SufficiencyStatus.ENOUGH

    entrypoint_only = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(
        route, EvidenceProfile(frozenset({"entrypoint"})), TraversalPolicy(),
    )
    partial_capsule = EvidenceReducer().reduce((entrypoint_only,), EvidenceBudget(20_000))
    partial_result = DeterministicSufficiencyEvaluator().evaluate(partial_capsule)

    assert partial_result.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert partial_result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS

    mixed_capsule = EvidenceReducer().reduce((evidence, entrypoint_only), EvidenceBudget(20_000))
    assert DeterministicSufficiencyEvaluator().evaluate(mixed_capsule).status(
        "integrations",
    ) == SufficiencyStatus.AMBIGUOUS


def test_missing_messaging_capability_cannot_prove_empty_integrations():
    snapshot = project_analysis(ServiceKey("status"), StaticAnalysisEngine().analyze(STATUS_CORPUS, "jvm-spring"))
    route = next(fact.subject for fact in snapshot.facts if fact.kind == "entrypoint")
    profile = EvidenceProfile(frozenset({"entrypoint", "flow_edge", "service_call"}))
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(route, profile, TraversalPolicy())
    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert not capsule.boundaries
    assert not capsule.truncated
    assert result.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS
    assert result.evidence_ids("integrations") == ()


def test_incomplete_snapshot_cannot_prove_empty_integrations():
    complete = project_analysis(ServiceKey("status"), StaticAnalysisEngine().analyze(STATUS_CORPUS, "jvm-spring"))
    snapshot = replace(complete, facts=tuple(
        fact for fact in complete.facts if fact.kind != "analysis_capability"
    ))
    route = next(fact.subject for fact in snapshot.facts if fact.kind == "entrypoint")
    profile = EvidenceProfile(frozenset({
        "entrypoint", "flow_edge", "service_call", "analysis_capability",
    }))
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(route, profile, TraversalPolicy())
    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS


def test_known_boundary_cannot_prove_empty_integrations():
    route = EntrypointKey(ServiceKey("orders"), "http", "GET", "/orders", "Orders.list")
    entry = EvidenceFact("entry", "entrypoint", {"contract": {}}, FactStatus.CONFIRMED,
                         "static", None, (), (route.symbol,), "entry")
    boundary = TraversalBoundary(route.symbol, "dynamic_dispatch", "known_boundary", "flow")
    capsule = EvidenceReducer().reduce((EvidenceSet(route, (entry,), (boundary,), False),),
                                       EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert result.status("integration_purpose") == SufficiencyStatus.AMBIGUOUS
    assert result.status("flow") == SufficiencyStatus.AMBIGUOUS


def test_openapi_body_absence_cannot_override_source_request_type_without_fields():
    route = EntrypointKey(ServiceKey("orders"), "http", "POST", "/orders", "Orders.create")
    entry = EvidenceFact(
        "entry", "entrypoint", {"contract": {
            "request": {"type": "OrderRequest"},
            "formal_contract": {"format": "openapi", "request_body_present": False},
        }}, FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "entry",
    )
    capsule = EvidenceReducer().reduce((EvidenceSet(route, (entry,), (), False),), EvidenceBudget(20_000))

    result = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert result.status("request_shape") == SufficiencyStatus.AMBIGUOUS


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
    assert result.status("integration_purpose") == SufficiencyStatus.UNSUPPORTED


def test_authorization_ignores_only_unrelated_budget_omissions():
    route = EntrypointKey(ServiceKey("orders"), "http", "GET", "/orders", "Orders.list")
    facts = (
        EvidenceFact("entry", "entrypoint", {"contract": {"returns": {"fields": [{"name": "id"}]}}},
                     FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "entry"),
        EvidenceFact("security", "security_requirement", {"requirement": "authenticated"},
                     FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "security"),
        EvidenceFact("flow", "flow_edge", {"detail": "x" * 10_000},
                     FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "flow"),
    )
    evidence = EvidenceSet(route, facts, (), False)
    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(2_000))

    assert capsule.truncated
    assert capsule.report.omitted_fact_ids == ("flow",)
    assert capsule.report.omitted_fact_kinds == ("flow_edge",)
    assert not capsule.navigation_truncated
    assert DeterministicSufficiencyEvaluator().evaluate(capsule).status("authorization") == SufficiencyStatus.ENOUGH

    unresolved = EvidenceSet(route, facts, (TraversalBoundary(route.symbol, "unknown.call", "unresolved", "flow"),), False)
    with_unresolved = EvidenceReducer().reduce((unresolved,), EvidenceBudget(2_000))
    assert DeterministicSufficiencyEvaluator().evaluate(with_unresolved).status("authorization") == SufficiencyStatus.AMBIGUOUS

    limited = EvidenceSet(route, facts, (), True)
    with_limit = EvidenceReducer().reduce((limited,), EvidenceBudget(2_000))
    assert DeterministicSufficiencyEvaluator().evaluate(with_limit).status("authorization") == SufficiencyStatus.AMBIGUOUS

    omitted_security = EvidenceFact("security-extra", "security_requirement", {"detail": "x" * 10_000},
                                    FactStatus.CONFIRMED, "static", None, (), (route.symbol,), "security-extra")
    with_omitted_security = EvidenceReducer().reduce(
        (EvidenceSet(route, (*facts, omitted_security), (), False),), EvidenceBudget(2_000),
    )
    assert "security_requirement" in with_omitted_security.report.omitted_fact_kinds
    assert DeterministicSufficiencyEvaluator().evaluate(with_omitted_security).status(
        "authorization",
    ) == SufficiencyStatus.AMBIGUOUS
