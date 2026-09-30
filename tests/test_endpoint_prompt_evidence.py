import json
from pathlib import Path
from shutil import copytree

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import index_runs as index_runs_repo
from orbitkb.discovery.base import (
    CodeExcerpt,
    EndpointHint,
    OutboundCallHint,
    ServiceHints,
)
from orbitkb.discovery.registry import detector_by_id
from orbitkb.domain.canonical import ServiceKey
from orbitkb.generation.backend_base import GenerationError
from orbitkb.generation.mock_backend import MockBackend, kind_for_schema
from orbitkb.generation.orchestrator import _render_api_detail_prompt, index_service
from orbitkb.generation.route_evidence import route_outbound_hints

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"
STATUS_CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/status-kotlin-service"
JAVA_STATUS_CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/status-java-service"
GOLDENS = Path(__file__).resolve().parents[1] / "verify/quality_goldens"


class RecordingBackend(MockBackend):
    def __init__(self):
        self.endpoint_prompts: list[str] = []

    def generate(self, prompt, schema, cwd):
        if kind_for_schema(schema) == "api_detail":
            self.endpoint_prompts.append(prompt)
        return super().generate(prompt, schema, cwd)


class FailingEndpointBackend(RecordingBackend):
    fail_endpoint = False

    def generate(self, prompt, schema, cwd):
        if self.fail_endpoint and kind_for_schema(schema) == "api_detail":
            self.endpoint_prompts.append(prompt)
            raise GenerationError("fixture endpoint failure")
        return super().generate(prompt, schema, cwd)


def test_endpoint_prompt_uses_route_proven_feign_evidence_without_another_model_call(tmp_path):
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")

    result = index_service(conn, "menu-manager", CORPUS, detector_by_id("jvm-spring"), backend)

    prompt = next(prompt for prompt in backend.endpoint_prompts
                  if "Endpoint: GET /menus/{id}\n" in prompt)
    assert result.status == "ok"
    assert result.llm_calls == 4
    assert result.sufficiency_shadow == {"missing": 2}
    assert {(item.method, item.path) for item in result.sufficiency_details} == {
        ("GET", "/menus/{id}"), ("GET", "/menus/by-restaurant/{id}"),
    }
    detail = next(item for item in result.sufficiency_details if item.method == "GET")
    assert detail.status == "missing"
    assert detail.assessment is not None
    behavior = next(item for item in detail.assessment.dimensions if item.dimension == "business_behavior")
    assert behavior.status.value == "missing"
    assert "business description" in behavior.reason
    assert "restaurant-service" in prompt
    assert "GET /restaurants/{id}" in prompt
    assert "MenuGateway.kt" in prompt
    assert "(none found)" not in prompt.split("Outbound-call hints", 1)[1].split("Return:", 1)[0]
    assert all(item.render_status == "ineligible" for item in result.sufficiency_details)


def test_simple_route_uses_deterministic_document_without_endpoint_model_call(tmp_path):
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")

    result = index_service(conn, "status", STATUS_CORPUS, detector_by_id("jvm-spring"), backend)

    assert result.status == "ok"
    assert backend.endpoint_prompts == []
    assert result.llm_calls == result.llm_invocations == 2
    assert result.sufficiency_shadow == {"enough": 1}
    detail = result.sufficiency_details[0]
    assert detail.render_status == "used"
    assert detail.status == "enough"
    golden = json.loads((GOLDENS / "status-kotlin.json").read_text(encoding="utf-8"))
    api = apis_repo.get_api_by_key(conn, result.service_id, "GET", "/status")
    assert api is not None
    assert api["summary"] == golden["summary"]
    assert api["description"] == golden["description"]
    assert json.loads(api["response_shape"]) == golden["response_shape"]
    run = index_runs_repo.recent_index_runs(conn, result.service_id)[0]
    endpoint_usage = next(row for row in index_runs_repo.list_unit_usage(conn, run["id"])
                          if row["unit_kind"] == "endpoint")
    assert endpoint_usage["generated_units"] == 1
    assert endpoint_usage["llm_invocations"] == 0


def test_java_simple_route_uses_deterministic_document_without_endpoint_model_call(tmp_path):
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")

    result = index_service(conn, "status-java", JAVA_STATUS_CORPUS, detector_by_id("jvm-spring"), backend)

    assert result.status == "ok"
    assert backend.endpoint_prompts == []
    assert result.sufficiency_details[0].render_status == "used"
    api = apis_repo.get_api_by_key(conn, result.service_id, "GET", "/health")
    golden = json.loads((GOLDENS / "status-java.json").read_text(encoding="utf-8"))
    assert api is not None
    assert api["summary"] == golden["summary"]
    assert api["description"] == golden["description"]
    assert json.loads(api["response_shape"]) == golden["response_shape"]


def test_openapi_change_refreshes_deterministic_endpoint_without_controller_change(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    spec = root / "openapi.yaml"
    spec.write_text(spec.read_text().replace(
        "Get service status", "Read current status",
    ), encoding="utf-8")

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.llm_calls == 2  # component and overview followed the endpoint change
    assert second.sufficiency_details[0].render_status == "used"
    assert backend.endpoint_prompts == []
    api = apis_repo.get_api_by_key(conn, second.service_id, "GET", "/status")
    assert api is not None and api["summary"] == "Read current status"


def test_security_change_replaces_public_document_with_model_output(tmp_path):
    root = tmp_path / "service"
    copytree(JAVA_STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status-java", root, detector, backend)
    assert first.status == "ok"
    security = root / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, "/health").authenticated()',
    ), encoding="utf-8")

    second = index_service(conn, "status-java", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.llm_calls == 3  # endpoint, component, overview
    assert second.sufficiency_details[0].render_status == "ineligible"
    assert len(backend.endpoint_prompts) == 1
    api = apis_repo.get_api_by_key(conn, second.service_id, "GET", "/health")
    assert api is not None and api["summary"] == "Mock summary."


def test_security_change_to_public_replaces_model_output_with_deterministic_document(tmp_path):
    root = tmp_path / "service"
    copytree(JAVA_STATUS_CORPUS, root)
    security = root / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, "/health").authenticated()',
    ), encoding="utf-8")
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status-java", root, detector, backend)
    assert first.status == "ok" and len(backend.endpoint_prompts) == 1
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").authenticated()',
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
    ), encoding="utf-8")

    second = index_service(conn, "status-java", root, detector, backend)

    assert second.status == "ok"
    assert second.llm_calls == 2
    assert len(backend.endpoint_prompts) == 1
    assert second.sufficiency_details[0].render_status == "used"
    api = apis_repo.get_api_by_key(conn, second.service_id, "GET", "/health")
    assert api is not None and api["summary"] == "Read health status"


def test_unrelated_openapi_operation_does_not_regenerate_existing_route(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    spec = root / "openapi.yaml"
    spec.write_text(spec.read_text() + (
        "  /unrelated:\n    get:\n      summary: Different operation\n"
        "      responses:\n        '200': {}\n"
    ), encoding="utf-8")

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.sufficiency_details == ()
    assert second.llm_calls == 0
    assert backend.endpoint_prompts == []


def test_openapi_comment_does_not_regenerate_unchanged_route(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    spec = root / "openapi.yaml"
    spec.write_text("# This comment does not change the operation\n" + spec.read_text(), encoding="utf-8")

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.sufficiency_details == ()
    assert second.llm_calls == 0


def test_removed_openapi_contract_replaces_deterministic_endpoint_with_model_output(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    (root / "openapi.yaml").unlink()

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.llm_calls == 3
    assert second.sufficiency_details[0].render_status == "ineligible"
    assert len(backend.endpoint_prompts) == 1
    api = apis_repo.get_api_by_key(conn, second.service_id, "GET", "/status")
    assert api is not None and api["summary"] == "Mock summary."


def test_removed_public_security_rule_replaces_deterministic_endpoint_with_model_output(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    (root / "SecurityConfig.kt").unlink()

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.llm_calls == 3
    assert second.sufficiency_details[0].render_status == "ineligible"
    assert len(backend.endpoint_prompts) == 1
    api = apis_repo.get_api_by_key(conn, second.service_id, "GET", "/status")
    assert api is not None and api["summary"] == "Mock summary."


def test_removed_controller_prunes_endpoint_and_refreshes_overview(tmp_path):
    root = tmp_path / "service"
    copytree(STATUS_CORPUS, root)
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status", root, detector, backend)
    assert first.status == "ok"
    (root / "StatusController.kt").unlink()

    second = index_service(conn, "status", root, detector, backend)

    assert second.status == "ok"
    assert second.files_changed == 1
    assert second.llm_calls == 1
    assert apis_repo.get_api_by_key(conn, second.service_id, "GET", "/status") is None
    unchanged = index_service(conn, "status", root, detector, backend)
    assert unchanged.status == "ok"
    assert unchanged.files_changed == unchanged.llm_calls == 0


def test_failed_regeneration_after_security_change_retries_without_another_file_edit(tmp_path):
    root = tmp_path / "service"
    copytree(JAVA_STATUS_CORPUS, root)
    backend = FailingEndpointBackend()
    conn = open_db(tmp_path / "index.db")
    detector = detector_by_id("jvm-spring")
    first = index_service(conn, "status-java", root, detector, backend)
    assert first.status == "ok"
    security = root / "SecurityConfig.java"
    security.write_text(security.read_text().replace(
        '.requestMatchers(HttpMethod.GET, "/health").permitAll()',
        '.requestMatchers(HttpMethod.GET, "/health").authenticated()',
    ), encoding="utf-8")
    backend.fail_endpoint = True
    failed = index_service(
        conn, "status-java", root, detector, backend, failures_root=tmp_path / "failures",
    )
    assert failed.status == "partial"
    assert len(backend.endpoint_prompts) == 2
    backend.fail_endpoint = False

    retried = index_service(conn, "status-java", root, detector, backend)

    assert retried.status == "ok"
    assert len(backend.endpoint_prompts) == 3
    api = apis_repo.get_api_by_key(conn, retried.service_id, "GET", "/health")
    assert api is not None and api["summary"] == "Mock summary."


def test_limited_route_evidence_reports_omitted_calls_instead_of_claiming_none():
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    snapshot = project_analysis(ServiceKey("menu-manager"), analysis)

    hints = route_outbound_hints(snapshot, "GET", "/menus/{id}", max_chars=1)

    assert hints is not None
    assert "limited" in hints
    assert "omitted" in hints


def test_limited_static_evidence_preserves_existing_outbound_hint():
    excerpt = CodeExcerpt("MenuController.kt", 1, 3, "fun get() = service.get()")
    endpoint = EndpointHint("GET", "/menus", "MenuController", excerpt)
    hints = ServiceHints(outbound_calls=[OutboundCallHint("http", "legacy-client", excerpt)])

    prompt = _render_api_detail_prompt(
        "menu-manager", "jvm-spring", endpoint, hints,
        outbound_evidence="- (evidence budget limited: 1 route call omitted)",
    )

    assert "legacy-client" in prompt
    assert "route call omitted" in prompt
