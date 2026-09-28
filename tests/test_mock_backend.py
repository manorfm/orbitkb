"""MockBackend: a zero-cost, no-subprocess LLMBackend for dry runs against a real
repository (crash/regression testing on discovery + static analysis) without
spending any LLM tokens -- exactly the backend `orbitkb index --backend mock`
should use instead of `claude`/`codex` for that purpose.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.generation.mock_backend import MockBackend, canned_response_for_schema

ENTRYPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "description": {"type": "string"},
        "response_shape": {"type": "array"},
        "request_shape": {"type": "array"},
        "calls": {"type": "array"},
        "validations": {"type": "array"},
    },
}
OVERVIEW_SCHEMA = {"type": "object", "properties": {"short_desc": {"type": "string"}, "long_desc": {"type": "string"}}}
PERSISTENCE_SCHEMA = {"type": "object", "properties": {"entities": {"type": "array"}}}
MESSAGING_SCHEMA = {"type": "object", "properties": {"messages": {"type": "array"}}}
COMPONENT_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def test_generate_never_shells_out_and_returns_a_schema_shaped_response(tmp_path: Path):
    outcome = MockBackend().generate("prompt", ENTRYPOINT_SCHEMA, tmp_path)

    assert outcome.structured["summary"]
    assert outcome.usage.cost_usd is None


def test_canned_response_matches_each_known_unit_schema():
    assert set(canned_response_for_schema(OVERVIEW_SCHEMA)) == {"short_desc", "long_desc"}
    assert canned_response_for_schema(PERSISTENCE_SCHEMA) == {"entities": []}
    assert canned_response_for_schema(MESSAGING_SCHEMA) == {"messages": []}
    assert set(canned_response_for_schema(COMPONENT_SCHEMA)) == {"summary"}
    assert set(canned_response_for_schema(ENTRYPOINT_SCHEMA)) >= {"summary", "description", "calls"}
