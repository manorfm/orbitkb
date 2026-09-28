"""A zero-cost, no-subprocess LLMBackend for dry runs against a real repository --
exercises discovery, static analysis, prompt rendering and DB writes without
spending any LLM tokens or shelling out to `claude`/`codex`. Selected via
`orbitkb index --backend mock`. The canned, schema-shaped responses here are the
same ones `tests/test_orchestrator.py`'s FakeOrchestratorBackend uses to keep the
test suite deterministic; that class now builds on top of this one instead of
keeping its own separate copy.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.generation.backend_base import GenerationOutcome


def kind_for_schema(schema: dict) -> str:
    """Which unit `orchestrator.py` is asking for, detected from the schema's own
    top-level property names -- schemas don't otherwise carry a kind label.
    """
    props = schema.get("properties", {})
    if "short_desc" in props:
        return "service_overview"
    if "entities" in props:
        return "persistence"
    if "messages" in props:
        return "messaging"
    if set(props) == {"summary"}:
        return "component"
    return "api_detail"


def canned_response_for_schema(schema: dict) -> dict:
    kind = kind_for_schema(schema)
    if kind == "service_overview":
        return {"short_desc": "Mock short description.", "long_desc": "Mock long description."}
    if kind == "persistence":
        return {"entities": []}
    if kind == "messaging":
        return {"messages": []}
    if kind == "component":
        return {"summary": "Mock component summary."}
    return {
        "summary": "Mock summary.",
        "description": "Mock description.",
        "response_shape": [],
        "request_shape": [],
        "calls": [],
        "validations": [],
    }


class MockBackend:
    name = "mock"

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        return GenerationOutcome(structured=canned_response_for_schema(schema))
