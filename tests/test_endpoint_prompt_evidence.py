from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.db.connection import open_db
from orbitkb.discovery.base import (
    CodeExcerpt,
    EndpointHint,
    OutboundCallHint,
    ServiceHints,
)
from orbitkb.discovery.registry import detector_by_id
from orbitkb.domain.canonical import ServiceKey
from orbitkb.generation.mock_backend import MockBackend, kind_for_schema
from orbitkb.generation.orchestrator import _render_api_detail_prompt, index_service
from orbitkb.generation.route_evidence import route_outbound_hints

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


class RecordingBackend(MockBackend):
    def __init__(self):
        self.endpoint_prompts: list[str] = []

    def generate(self, prompt, schema, cwd):
        if kind_for_schema(schema) == "api_detail":
            self.endpoint_prompts.append(prompt)
        return super().generate(prompt, schema, cwd)


def test_endpoint_prompt_uses_route_proven_feign_evidence_without_another_model_call(tmp_path):
    backend = RecordingBackend()
    conn = open_db(tmp_path / "index.db")

    result = index_service(conn, "menu-manager", CORPUS, detector_by_id("jvm-spring"), backend)

    prompt = next(prompt for prompt in backend.endpoint_prompts
                  if "Endpoint: GET /menus/{id}\n" in prompt)
    assert result.status == "ok"
    assert result.llm_calls == 6
    assert result.sufficiency_shadow == {"missing": 2, "unassessed": 1}
    assert "restaurant-service" in prompt
    assert "GET /restaurants/{id}" in prompt
    assert "MenuGateway.kt" in prompt
    assert "(none found)" not in prompt.split("Outbound-call hints", 1)[1].split("Return:", 1)[0]


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
