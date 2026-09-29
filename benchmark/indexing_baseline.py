"""Reproducible public-output baseline for the repository-owned sample project.

The snapshot stores public facts from the synthetic fixture and digests of
exported files. It never records prompts, source excerpts, or local paths.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from benchmark.fixtures import build_pix_fixture
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import export_mermaid, generate_topology_diagram
from orbitkb.generation.mock_backend import MockBackend
from orbitkb.generation.orchestrator import index_path
from orbitkb.mcp import queries


def _export_digests(conn, output_dir: Path) -> dict[str, str]:
    written = export_markdown(conn, output_dir) + export_mermaid(conn, output_dir)
    return {
        path.relative_to(output_dir).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(written)
    }


def _integration_fixture(workspace: Path) -> dict:
    conn = build_pix_fixture(workspace / "integrations.db")
    try:
        return {
            "topology": generate_topology_diagram(conn),
            "exports": _export_digests(conn, workspace / "integration-exports"),
        }
    finally:
        conn.close()


def collect_baseline(sources: Sequence[Path], workspace: Path) -> dict:
    """Index synthetic fixtures with no paid provider and capture stable outputs."""
    if not sources:
        raise ValueError("baseline needs at least one source")
    for source in sources:
        if not source.is_dir():
            raise ValueError(f"baseline source is not a directory: {source}")
    workspace.mkdir(parents=True, exist_ok=True)
    db_path = workspace / "baseline.db"
    if db_path.exists():
        raise FileExistsError(f"baseline workspace already has a database: {db_path}")

    conn = open_db(db_path)
    try:
        results = []
        for source in sources:
            results.extend(index_path(conn, source, MockBackend()))
        services = []
        for row in services_repo.list_services(conn):
            name = row["name"]
            public = queries.describe_service(conn, name, limit=queries.MAX_LIST_LIMIT)
            if "error" in public:
                raise RuntimeError(public["error"])
            if any(public["pagination"][kind]["truncated"] for kind in
                   ("calls", "apis", "components", "persists", "messages")):
                raise RuntimeError(f"baseline service exceeds public list limit: {name}")
            topology = queries.describe_service_topology(conn, name)
            services.append({
                "name": name,
                "stack": public["stack"],
                "short_desc": public["short_desc"],
                "long_desc": public["long_desc"],
                "api_count": row["api_count"],
                "apis": public["apis"],
                "calls": public["calls"],
                "components": [
                    {"name": c["name"], "summary": c["summary"]}
                    for c in public["components"]
                ],
                "persists": public["persists"],
                "messages": public["messages"],
                "mermaid": topology["mermaid"],
            })

        stacks = sorted({service["stack"] for service in services})
        return {
            "schema_version": 1,
            "stacks": stacks,
            "quality_by_stack": {
                stack: {"false_positives": None, "omissions": None, "cost_usd": None}
                for stack in stacks
            },
            "services": services,
            "runs": sorted(({"service": result.service_name, "status": result.status,
                             "llm_calls": result.llm_calls, "llm_invocations": result.llm_invocations}
                            for result in results), key=lambda run: run["service"]),
            "exports": _export_digests(conn, workspace / "exports"),
            "integration_fixture": _integration_fixture(workspace),
        }
    finally:
        conn.close()


def compare_baseline(actual: dict, golden_path: Path) -> list[str]:
    """Return changed JSON paths; a clean result is an empty list."""
    expected = json.loads(golden_path.read_text(encoding="utf-8"))
    changes: list[str] = []

    def walk(left: object, right: object, path: str) -> None:
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(left.keys() | right.keys()):
                if key not in left or key not in right:
                    changes.append(f"{path}.{key}" if path else key)
                else:
                    walk(left[key], right[key], f"{path}.{key}" if path else key)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                changes.append(path)
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                walk(left_item, right_item, f"{path}[{index}]")
        elif left != right:
            changes.append(path)

    walk(expected, actual, "")
    return changes
