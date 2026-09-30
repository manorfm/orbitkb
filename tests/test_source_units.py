from dataclasses import replace
from pathlib import Path
from shutil import copytree

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.domain.canonical import ServiceKey, SourceReference, SymbolKey
from orbitkb.generation.source_units import SourceUnitCollector

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"


def test_source_unit_collector_does_not_read_a_path_outside_service_root(tmp_path):
    root = tmp_path / "service"
    copytree(CORPUS, root)
    outside = tmp_path / "private.kt"
    outside.write_text("SECRET_OUTSIDE_SERVICE\n", encoding="utf-8")
    snapshot = project_analysis(
        ServiceKey("menu-manager"), StaticAnalysisEngine().analyze(root, "jvm-spring"),
    )
    facts = tuple(
        replace(fact, sources=(SourceReference("../private.kt", 1, 1),))
        if fact.kind == "symbol" and isinstance(fact.subject, SymbolKey)
        and fact.subject.name == "MenuUseCase.get" else fact
        for fact in snapshot.facts
    )
    snapshot = replace(snapshot, facts=facts)

    units = SourceUnitCollector(snapshot, root).for_route("GET", "/menus/{id}")

    assert all(unit.excerpt.file_path != "../private.kt" for unit in units)
    assert all("SECRET_OUTSIDE_SERVICE" not in unit.excerpt.text for unit in units)
