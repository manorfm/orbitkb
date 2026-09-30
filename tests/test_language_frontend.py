from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    SecurityRequirement,
)
from orbitkb.domain.canonical import ServiceKey


class FixtureFrontend:
    file_patterns = ("*.fixture",)

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
