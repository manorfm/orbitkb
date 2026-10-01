from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    SecurityRequirement,
)
from orbitkb.analysis.resolution import BoundedFlowResolver
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots, services
from orbitkb.domain.canonical import ServiceKey
from orbitkb.mcp.queries import describe_messages


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
