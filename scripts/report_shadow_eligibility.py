"""Measure deterministic endpoint usage on repository-owned fixtures without a paid model."""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.discovery.registry import detector_by_id
from orbitkb.generation.mock_backend import MockBackend
from orbitkb.generation.orchestrator import IndexResult, index_path, index_service

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISCOVERED_SOURCES = (
    PROJECT_ROOT / "verify/sample_project",
    PROJECT_ROOT / "verify/language_corpus",
    PROJECT_ROOT / "verify/flow_corpus",
)
DIRECT_SOURCES = (
    ("flow-menu", PROJECT_ROOT / "verify/flow_corpus/menu-kotlin-service"),
    ("status", PROJECT_ROOT / "verify/flow_corpus/status-kotlin-service"),
    ("status-java", PROJECT_ROOT / "verify/flow_corpus/status-java-service"),
)


def summarize_routes(results: Sequence[tuple[str, IndexResult]]) -> dict:
    """Aggregate only non-sensitive counts from one fresh indexing run."""
    statuses: Counter[str] = Counter()
    by_stack: dict[str, dict[str, int]] = {}
    route_count = 0
    eligible_count = 0
    for stack, result in results:
        stack_count = by_stack.setdefault(stack, {"regenerated_routes": 0, "eligible_routes": 0})
        for detail in result.sufficiency_details:
            route_count += 1
            stack_count["regenerated_routes"] += 1
            statuses[detail.render_status] += 1
            if detail.render_status != "ineligible":
                eligible_count += 1
                stack_count["eligible_routes"] += 1
    return {
        "services": len(results),
        "regenerated_routes": route_count,
        "eligible_routes": eligible_count,
        "render_statuses": dict(sorted(statuses.items())),
        "by_stack": dict(sorted(by_stack.items())),
        "llm_calls": sum(result.llm_calls for _, result in results),
        "backend": "mock",
        "quality_evaluated": False,
    }


def collect_report(workspace: Path) -> dict:
    """Index the synthetic corpus in an isolated database and count outcomes."""
    conn = open_db(workspace / "shadow-report.db")
    backend = MockBackend()
    results: list[tuple[str, IndexResult]] = []
    try:
        for source in DISCOVERED_SOURCES:
            for result in index_path(conn, source, backend):
                if result.status != "ok":
                    raise RuntimeError(f"fixture indexing failed: {result.service_name}")
                row = conn.execute("SELECT stack FROM services WHERE id = ?", (result.service_id,)).fetchone()
                if row is None:
                    raise RuntimeError(f"indexed service disappeared: {result.service_name}")
                results.append((row["stack"], result))
        detector = detector_by_id("jvm-spring")
        if detector is None:
            raise RuntimeError("JVM Spring detector is unavailable")
        for name, source in DIRECT_SOURCES:
            result = index_service(conn, name, source, detector, backend)
            if result.status != "ok":
                raise RuntimeError(f"fixture indexing failed: {name}")
            results.append((detector.id, result))
        return summarize_routes(results)
    finally:
        conn.close()


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="orbitkb-shadow-") as directory:
        report = collect_report(Path(directory))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
