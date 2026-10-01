from dataclasses import replace
from pathlib import Path

import pytest

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.depth import NoopDepthProvider
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    SecurityRequirement,
    Symbol,
)
from orbitkb.analysis.resolution import BoundedFlowResolver
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots, messages, services
from orbitkb.discovery.base import CodeExcerpt, EndpointHint, ServiceHints
from orbitkb.domain.canonical import ServiceKey
from orbitkb.domain.sufficiency import SufficiencyStatus
from orbitkb.export.markdown import export_markdown
from orbitkb.generation.mock_backend import MockBackend
from orbitkb.generation.orchestrator import DiscoveryError, index_path, index_service
from orbitkb.generation.route_evidence import route_capsule
from orbitkb.mcp.queries import (
    describe_api,
    describe_entrypoint,
    describe_messages,
    list_entrypoints,
)


class FixtureFrontend:
    file_patterns = ("*.fixture",)
    supported_capabilities = frozenset()

    def analyze_file(self, path: Path, root: Path) -> AnalysisResult:
        route = path.read_text(encoding="utf-8").strip()
        return AnalysisResult(entrypoints=[
            EntryPoint("http", "GET", route, "Fixture.list", Evidence(path.relative_to(root).as_posix(), 1, 1)),
        ])


class FixtureFrameworkAdapter:
    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        assert [path.name for path in files] == ["routes.fixture"]
        result.security_requirements.append(SecurityRequirement(
            "/fixtures", "GET", None, "permitAll", (), Evidence("routes.fixture", 1, 1),
        ))


class FixtureFlowFrontend(FixtureFrontend):
    def analyze_file(self, path: Path, root: Path) -> AnalysisResult:
        result = super().analyze_file(path, root)
        result.edges.append(FlowEdge(
            "Fixture.list", "fixtureRepo.find", "invokes", Evidence(path.relative_to(root).as_posix(), 1, 1),
        ))
        return result


class FixtureFlowClassifier:
    def classify(self, result: AnalysisResult, files: list[Path]) -> None:
        assert [path.name for path in files] == ["routes.fixture"]
        result.edges = [replace(edge, kind="reads", boundary_kind="persistence") for edge in result.edges]


class FixtureDetector:
    id = "fixture"

    def matches(self, folder: Path) -> bool:
        return (folder / "routes.fixture").is_file()

    def collect_hints(self, folder: Path) -> ServiceHints:
        excerpt = CodeExcerpt("routes.fixture", 1, 1, (folder / "routes.fixture").read_text(encoding="utf-8"))
        return ServiceHints(
            endpoints=[EndpointHint("GET", "/fixtures", "Fixture", excerpt)],
            entry_excerpt=excerpt,
        )


class FixtureSupportedFrontend(FixtureFrontend):
    supported_capabilities = frozenset({"messaging"})


class FixtureWritePublishFrontend(FixtureFrontend):
    def analyze_file(self, path: Path, root: Path) -> AnalysisResult:
        result = super().analyze_file(path, root)
        evidence = Evidence(path.relative_to(root).as_posix(), 1, 1)
        result.symbols.extend([
            Symbol("FixtureStore.save", "FixtureStore", "save", evidence),
            Symbol("FixtureEvents.publish", "FixtureEvents", "publish", evidence),
        ])
        result.edges.extend([
            FlowEdge("Fixture.list", "FixtureStore.save", "writes", evidence),
            FlowEdge("Fixture.list", "FixtureEvents.publish", "publishes", evidence),
        ])
        return result


def test_new_language_frontend_uses_existing_analysis_and_canonical_projection(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("/ignored\n", encoding="utf-8")
    engine = StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()})

    analysis = engine.analyze(tmp_path, "fixture")
    snapshot = project_analysis(ServiceKey("fixture-service"), analysis)

    assert [path.name for path in engine.list_files(tmp_path, "fixture")] == ["routes.fixture"]
    assert [(entry.method, entry.name) for entry in analysis.entrypoints] == [("GET", "/fixtures")]
    assert [(fact.kind, fact.sources[0].file_path) for fact in snapshot.facts if fact.kind == "entrypoint"] == [
        ("entrypoint", "routes.fixture"),
    ]
    digest = engine.input_digest(tmp_path, "fixture")
    (tmp_path / "ignored.txt").write_text("/changed\n", encoding="utf-8")
    assert engine.input_digest(tmp_path, "fixture") == digest
    (tmp_path / "routes.fixture").write_text("/changed\n", encoding="utf-8")
    assert engine.input_digest(tmp_path, "fixture") != digest


def test_framework_adapter_enriches_new_language_in_shared_pipeline(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    engine = StaticAnalysisEngine(
        frontends={"fixture": FixtureFrontend()},
        framework_adapters={"fixture": FixtureFrameworkAdapter()},
    )

    analysis = engine.analyze(tmp_path, "fixture")
    snapshot = project_analysis(ServiceKey("fixture-service"), analysis)

    assert [(requirement.route_pattern, requirement.requirement) for requirement in analysis.security_requirements] == [
        ("/fixtures", "permitAll"),
    ]
    assert any(fact.kind == "security_requirement" for fact in snapshot.facts)


def test_flow_classifier_runs_before_resolution_for_new_language(tmp_path: Path, monkeypatch):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    original_resolve = BoundedFlowResolver.resolve

    def check_order(self, result: AnalysisResult) -> AnalysisResult:
        assert [(edge.kind, edge.boundary_kind) for edge in result.edges] == [("reads", "persistence")]
        return original_resolve(self, result)

    monkeypatch.setattr(BoundedFlowResolver, "resolve", check_order)
    engine = StaticAnalysisEngine(
        frontends={"fixture": FixtureFlowFrontend()},
        flow_classifiers={"fixture": FixtureFlowClassifier()},
    )

    analysis = engine.analyze(tmp_path, "fixture")
    snapshot = project_analysis(ServiceKey("fixture-service"), analysis)

    assert [(edge.kind, edge.boundary_kind) for edge in analysis.edges] == [("reads", "persistence")]
    assert any(fact.kind == "flow_edge" and fact.attributes["relation"] == "reads" for fact in snapshot.facts)


def test_unsupported_messaging_survives_snapshot_and_is_visible_to_readers(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    analysis = StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()}).analyze(tmp_path, "fixture")
    snapshot = project_analysis(ServiceKey("fixture-service"), analysis)
    capability = next(fact for fact in snapshot.facts if fact.kind == "analysis_capability")
    assert capability.attributes["dimension"] == "messaging"
    assert capability.status.value == "unsupported"

    conn = open_db(tmp_path / "test.db")
    service_id = services.ensure_service(conn, "fixture-service", str(tmp_path), "fixture")
    canonical_snapshots.replace_snapshot(conn, service_id, snapshot)

    result = describe_messages(conn, "fixture-service")
    assert result["static_analysis_status"] == "unsupported"
    assert result["static_contracts"] == []
    assert result["messages"] == []

    export_markdown(conn, tmp_path / "docs")
    page = (tmp_path / "docs/fixture-service/index.md").read_text(encoding="utf-8")
    messaging = page.split("## Messaging", 1)[1].split("## Cloud", 1)[0]
    assert "Static analysis:** unsupported" in messaging
    assert messaging.count("- (not assessed)") == 2
    assert "none detected" not in messaging

    messages.replace_messages(conn, service_id, [{
        "direction": "publishes", "channel": "fixture-events", "provider": "rabbitmq",
        "shape_json": [], "description": "a documented event",
    }], [])
    export_markdown(conn, tmp_path / "docs")
    page = (tmp_path / "docs/fixture-service/index.md").read_text(encoding="utf-8")
    messaging = page.split("## Messaging", 1)[1].split("## Cloud", 1)[0]
    assert "fixture-events" in messaging
    assert "Static analysis:** unsupported" in messaging
    assert messaging.count("- (not assessed)") == 1


def test_existing_frontend_declares_messaging_analysis_support(tmp_path: Path):
    (tmp_path / "main.py").write_text("def main():\n    pass\n", encoding="utf-8")
    analysis = StaticAnalysisEngine().analyze(tmp_path, "python")
    snapshot = project_analysis(ServiceKey("python-service"), analysis)

    capability = next(fact for fact in snapshot.facts if fact.kind == "analysis_capability")
    assert capability.attributes["dimension"] == "messaging"
    assert capability.status.value == "confirmed"

    conn = open_db(tmp_path / "test.db")
    service_id = services.ensure_service(conn, "python-service", str(tmp_path), "python")
    canonical_snapshots.replace_snapshot(conn, service_id, snapshot)
    assert describe_messages(conn, "python-service")["static_analysis_status"] == "supported"


def test_custom_frontend_runs_through_indexing_and_public_queries(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    first = index_service(
        conn, "fixture-service", tmp_path, FixtureDetector(), MockBackend(),
        analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()}),
    )

    assert first.status == "ok"
    assert [(entry["method"], entry["name"]) for entry in list_entrypoints(conn, "fixture-service")["entrypoints"]] == [
        ("GET", "/fixtures"),
    ]
    assert "error" not in describe_entrypoint(conn, "fixture-service", "http", "GET", "/fixtures")
    assert describe_api(conn, "fixture-service", "GET", "/fixtures")["summary"] == "Mock summary."
    assert describe_messages(conn, "fixture-service")["static_analysis_status"] == "unsupported"
    service_id = services.get_service_by_name(conn, "fixture-service")["id"]
    assert canonical_snapshots.read_snapshot(conn, service_id) is not None
    export_markdown(conn, tmp_path / "docs")
    assert "Static analysis:** unsupported" in (tmp_path / "docs/fixture-service/index.md").read_text()

    second = index_service(
        conn, "fixture-service", tmp_path, FixtureDetector(), MockBackend(),
        analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureSupportedFrontend()}),
    )

    assert second.status == "ok"
    assert describe_messages(conn, "fixture-service")["static_analysis_status"] == "supported"


def test_custom_analysis_engine_rejects_ambiguous_depth_provider(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    with pytest.raises(ValueError, match="cannot both be supplied"):
        index_service(
            conn, "fixture-service", tmp_path,
            FixtureDetector(), MockBackend(),
            depth_provider=NoopDepthProvider(),
            analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()}),
        )


def test_custom_detector_and_frontend_index_repository_through_public_path(tmp_path: Path):
    service_root = tmp_path / "sample-service"
    service_root.mkdir()
    (service_root / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    results = index_path(
        conn, tmp_path, MockBackend(),
        detectors=(FixtureDetector(),),
        analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()}),
    )

    assert [(item.service_name, item.status) for item in results] == [("sample-service", "ok")]
    assert describe_api(conn, "sample-service", "GET", "/fixtures")["summary"] == "Mock summary."
    assert describe_messages(conn, "sample-service")["static_analysis_status"] == "unsupported"
    assert [(entry["name"], entry["symbol"]) for entry in list_entrypoints(conn, "sample-service")["entrypoints"]] == [
        ("/fixtures", "Fixture.list"),
    ]


def test_custom_detector_is_available_to_explicit_stack_override(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    results = index_path(
        conn, tmp_path, MockBackend(),
        service_override="named-service", stack_override="fixture",
        detectors=(FixtureDetector(),),
        analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureFrontend()}),
    )

    assert [(item.service_name, item.status) for item in results] == [("named-service", "ok")]
    assert describe_messages(conn, "named-service")["static_analysis_status"] == "unsupported"


def test_custom_detector_without_frontend_fails_before_writing_service(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    with pytest.raises(DiscoveryError, match="no analysis frontend"):
        index_path(conn, tmp_path, MockBackend(), detectors=(FixtureDetector(),))

    assert services.list_services(conn) == []


def test_custom_detector_no_match_reports_configured_search(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")

    with pytest.raises(DiscoveryError, match="checked 1 configured detector"):
        index_path(conn, tmp_path, MockBackend(), detectors=(FixtureDetector(),))


def test_fake_language_publish_flow_reaches_composer_gate_and_smells(tmp_path: Path):
    (tmp_path / "routes.fixture").write_text("/fixtures\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    result = index_service(
        conn, "fixture-service", tmp_path, FixtureDetector(), MockBackend(),
        analysis_engine=StaticAnalysisEngine(frontends={"fixture": FixtureWritePublishFrontend()}),
    )

    snapshot = canonical_snapshots.read_snapshot(conn, result.service_id)
    capsule = route_capsule(snapshot, "GET", "/fixtures")
    assert capsule is not None
    assert {fact.value["relation"] for fact in capsule.facts if fact.kind == "flow_edge"} == {
        "writes", "publishes",
    }
    assert capsule.boundaries == ()
    assessment = result.sufficiency_details[0].assessment
    assert assessment.status("integrations") == SufficiencyStatus.AMBIGUOUS
    assert assessment.evidence_ids("integrations") == tuple(
        fact.id for fact in capsule.facts if fact.kind == "flow_edge" and fact.value["relation"] == "publishes"
    )
    public = describe_entrypoint(conn, "fixture-service", "http", "GET", "/fixtures")
    assert [smell["kind"] for smell in public["smells"]] == ["possible_non_atomic_publish"]
    assert public["smells"][0]["evidence_targets"] == ["FixtureStore.save", "FixtureEvents.publish"]
