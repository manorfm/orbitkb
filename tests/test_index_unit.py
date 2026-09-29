"""The indexing unit contract stays independent of storage and discovery."""

import ast
from pathlib import Path

from orbitkb.generation.backend_base import LLMUsage
from orbitkb.generation.unit import IndexUnit


def test_index_unit_tracks_attempts_usage_and_duration_without_index_context():
    unit = IndexUnit("endpoint", ("GET", "/items"))

    assert (unit.kind, unit.identity, unit.status, unit.llm_invocations) == (
        "endpoint", ("GET", "/items"), "skipped", 0,
    )
    unit.record_attempt()
    unit.record_attempt()
    unit.record_usage(LLMUsage(input_tokens=10, cached_input_tokens=3))
    unit.record_usage(LLMUsage(output_tokens=4, cost_usd=0.25))
    unit.record_duration(12.5)
    unit.record_duration(7.5)
    unit.status = "success"

    assert unit.llm_invocations == 2
    assert unit.usage == LLMUsage(input_tokens=10, output_tokens=4, cached_input_tokens=3, cost_usd=0.25)
    assert unit.backend_duration_ms == 20


def test_generation_contracts_do_not_import_storage_discovery_or_concrete_providers():
    root = Path(__file__).resolve().parents[1] / "orbitkb" / "generation"
    forbidden = ("orbitkb.db", "orbitkb.discovery", "orbitkb.cli", "orbitkb.generation.claude_backend",
                 "orbitkb.generation.codex_backend", "orbitkb.generation.mock_backend")
    for name in ("unit.py", "knowledge.py", "evidence.py", "policy.py"):
        tree = ast.parse((root / name).read_text())
        imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
        imports += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
        assert not [module for module in imports if module.startswith(forbidden)], name
