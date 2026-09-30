from pathlib import Path
from shutil import copytree

import jsonschema

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.domain.canonical import ServiceKey
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.domain.reduction import EvidenceBudget, EvidenceReducer
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyStatus,
)
from orbitkb.generation.deterministic_endpoint import render_simple_endpoint
from orbitkb.generation.llm_harness import load_schema
from orbitkb.generation.route_evidence import route_capsule

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/status-kotlin-service"
JAVA_CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/status-java-service"


def test_simple_route_renders_schema_valid_source_backed_documentation():
    capsule, assessment = _status_route_from_existing(CORPUS)

    document = render_simple_endpoint(capsule, assessment)

    assert assessment.overall == SufficiencyStatus.ENOUGH
    assert document == {
        "summary": "Get service status",
        "description": "Returns the current service status.",
        "request_shape": [],
        "response_shape": [{"field": "value", "type_desc": "String"}],
        "calls": [],
        "validations": [{"kind": "authorization", "description": "Public access is permitted."}],
    }
    jsonschema.validate(document, load_schema("api_detail"))


def test_java_route_uses_the_same_evidence_based_renderer():
    snapshot = project_analysis(
        ServiceKey("status-java"), StaticAnalysisEngine().analyze(JAVA_CORPUS, "jvm-spring"),
    )
    capsule = route_capsule(snapshot, "GET", "/health")
    assert capsule is not None
    assessment = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert render_simple_endpoint(capsule, assessment) == {
        "summary": "Read health status",
        "description": "Returns the service health status.",
        "request_shape": [],
        "response_shape": [{"field": "value", "type_desc": "String"}],
        "calls": [],
        "validations": [{"kind": "authorization", "description": "Public access is permitted."}],
    }


def test_java_route_with_restricted_filter_rule_is_not_rendered(tmp_path: Path):
    copytree(JAVA_CORPUS, tmp_path, dirs_exist_ok=True)
    security = tmp_path / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, "/health").authenticated()',
    ), encoding="utf-8")
    snapshot = project_analysis(
        ServiceKey("status-java"), StaticAnalysisEngine().analyze(tmp_path, "jvm-spring"),
    )
    capsule = route_capsule(snapshot, "GET", "/health")
    assert capsule is not None
    assessment = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert render_simple_endpoint(capsule, assessment) is None


def test_java_method_permit_all_without_public_http_rule_is_not_rendered(tmp_path: Path):
    copytree(JAVA_CORPUS, tmp_path, dirs_exist_ok=True)
    (tmp_path / "SecurityConfig.java").unlink()
    controller = tmp_path / "HealthController.java"
    controller.write_text(controller.read_text().replace(
        '@GetMapping("/health")', '@GetMapping("/health")\n    @PreAuthorize("permitAll()")',
    ), encoding="utf-8")
    snapshot = project_analysis(
        ServiceKey("status-java"), StaticAnalysisEngine().analyze(tmp_path, "jvm-spring"),
    )
    capsule = route_capsule(snapshot, "GET", "/health")
    assert capsule is not None

    assert render_simple_endpoint(capsule, DeterministicSufficiencyEvaluator().evaluate(capsule)) is None


def test_public_wildcard_rule_does_not_prove_exact_route_access(tmp_path: Path):
    copytree(JAVA_CORPUS, tmp_path, dirs_exist_ok=True)
    security = tmp_path / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, "/**").permitAll()',
    ), encoding="utf-8")
    snapshot = project_analysis(ServiceKey("java"), StaticAnalysisEngine().analyze(tmp_path, "jvm-spring"))
    capsule = route_capsule(snapshot, "GET", "/health")
    assert capsule is not None

    assert render_simple_endpoint(capsule, DeterministicSufficiencyEvaluator().evaluate(capsule)) is None


def test_dynamic_rule_before_public_java_route_blocks_rendering(tmp_path: Path):
    copytree(JAVA_CORPUS, tmp_path, dirs_exist_ok=True)
    security = tmp_path / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, route()).authenticated()\n'
        '            .requestMatchers(HttpMethod.GET, "/health").permitAll()',
    ), encoding="utf-8")
    snapshot = project_analysis(ServiceKey("java"), StaticAnalysisEngine().analyze(tmp_path, "jvm-spring"))
    capsule = route_capsule(snapshot, "GET", "/health")
    assert capsule is not None
    assessment = DeterministicSufficiencyEvaluator().evaluate(capsule)

    assert assessment.status("authorization") == SufficiencyStatus.AMBIGUOUS
    assert render_simple_endpoint(capsule, assessment) is None


def test_dynamic_rule_before_public_kotlin_route_blocks_rendering(tmp_path: Path):
    copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    security = tmp_path / "SecurityConfig.kt"
    security.write_text(security.read_text().replace(
        'authorize(HttpMethod.GET, "/status", permitAll)',
        'authorize(HttpMethod.GET, computedPath, authenticated)\n'
        '                authorize(HttpMethod.GET, "/status", permitAll)',
    ), encoding="utf-8")
    capsule, assessment = _status_route_from_existing(tmp_path)

    assert assessment.status("authorization") == SufficiencyStatus.AMBIGUOUS
    assert render_simple_endpoint(capsule, assessment) is None


def test_simple_route_without_declared_summary_is_not_rendered(tmp_path: Path):
    copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    spec = tmp_path / "openapi.yaml"
    spec.write_text(spec.read_text().replace("      summary: Get service status\n", ""), encoding="utf-8")
    capsule, assessment = _status_route_from_existing(tmp_path)

    assert assessment.overall == SufficiencyStatus.ENOUGH
    assert render_simple_endpoint(capsule, assessment) is None


def test_conflicting_openapi_security_is_not_rendered_as_public(tmp_path: Path):
    copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    spec = tmp_path / "openapi.yaml"
    spec.write_text(spec.read_text().replace(
        "paths:\n", "security:\n  - bearerAuth: []\npaths:\n",
    ), encoding="utf-8")
    capsule, assessment = _status_route_from_existing(tmp_path)

    assert assessment.overall == SufficiencyStatus.ENOUGH
    assert render_simple_endpoint(capsule, assessment) is None


def test_declared_request_body_is_not_rendered_as_empty(tmp_path: Path):
    copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    handler = tmp_path / "StatusController.kt"
    handler.write_text(handler.read_text().replace(
        "fun get(): Status", "fun get(@RequestBody request: Status): Status",
    ), encoding="utf-8")
    capsule, assessment = _status_route_from_existing(tmp_path)

    assert assessment.status("request_shape") == SufficiencyStatus.AMBIGUOUS
    assert render_simple_endpoint(capsule, assessment) is None


def test_local_helper_flow_is_not_flattened_into_direct_response(tmp_path: Path):
    copytree(CORPUS, tmp_path, dirs_exist_ok=True)
    handler = tmp_path / "StatusController.kt"
    handler.write_text(handler.read_text().replace(
        'fun get(): Status = Status("ok")',
        'fun get(): Status = status()\n    fun status(): Status = Status("ok")',
    ), encoding="utf-8")
    capsule, assessment = _status_route_from_existing(tmp_path)

    assert assessment.overall == SufficiencyStatus.ENOUGH
    assert any(fact.kind == "flow_edge" for fact in capsule.facts)
    assert render_simple_endpoint(capsule, assessment) is None


def _status_route_from_existing(root: Path):
    snapshot = project_analysis(ServiceKey("status"), StaticAnalysisEngine().analyze(root, "jvm-spring"))
    route = next(fact.subject for fact in snapshot.facts if fact.kind == "entrypoint")
    profile = EvidenceProfile(frozenset({"entrypoint", "flow_edge", "service_call", "security_requirement"}))
    evidence = EvidenceComposer(KnowledgeNavigator(snapshot)).compose(route, profile, TraversalPolicy())
    capsule = EvidenceReducer().reduce((evidence,), EvidenceBudget(20_000))
    return capsule, DeterministicSufficiencyEvaluator().evaluate(capsule)
