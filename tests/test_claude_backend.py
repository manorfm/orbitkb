"""Unit tests for ClaudeBackend.generate(): subprocess.run is monkeypatched so this
never shells out to a real `claude` CLI. Covers structured-output extraction (already
existing behavior) and the new best-effort usage/cost extraction from the CLI's
`--output-format json` payload — a shape this codebase has never verified against a
real invocation, so every usage field must degrade to None instead of raising when
it's absent or reshaped.
"""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbitkb.generation.backend_base import GenerationError
from orbitkb.generation.claude_backend import ClaudeBackend

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}}


def test_defaults_to_sonnet_when_no_model_is_given():
    """Without an explicit model, this used to shell out to `claude` with no --model
    flag at all, which falls back to whatever model the CLI's own ambient config
    defaults to (Opus, on an account configured that way) -- an expensive surprise
    for a headless batch job invoked once per unit generated across a whole service.
    """
    assert ClaudeBackend().model == "sonnet"


def test_an_explicit_model_overrides_the_default():
    assert ClaudeBackend(model="opus").model == "opus"


def test_generate_extracts_structured_output_and_full_usage(monkeypatch, tmp_path: Path):
    payload = {
        "structured_output": {"summary": "ok"},
        "is_error": False,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 500, "output_tokens": 80, "cache_read_input_tokens": 300},
    }
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))

    outcome = ClaudeBackend().generate("prompt", SCHEMA, tmp_path)

    assert outcome.structured == {"summary": "ok"}
    assert outcome.usage.input_tokens == 500
    assert outcome.usage.output_tokens == 80
    assert outcome.usage.cached_input_tokens == 300
    assert outcome.usage.cost_usd == 0.0123


def test_generate_leaves_usage_none_when_payload_has_no_usage_fields(monkeypatch, tmp_path: Path):
    payload = {"structured_output": {"summary": "ok"}, "is_error": False}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))

    outcome = ClaudeBackend().generate("prompt", SCHEMA, tmp_path)

    assert outcome.structured == {"summary": "ok"}
    assert outcome.usage.input_tokens is None
    assert outcome.usage.output_tokens is None
    assert outcome.usage.cached_input_tokens is None
    assert outcome.usage.cost_usd is None


def test_generate_raises_on_missing_structured_output(monkeypatch, tmp_path: Path):
    payload = {"is_error": False}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))

    with pytest.raises(GenerationError):
        ClaudeBackend().generate("prompt", SCHEMA, tmp_path)


def test_generate_raises_on_reported_error(monkeypatch, tmp_path: Path):
    payload = {"is_error": True, "result": "boom"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr=""))

    with pytest.raises(GenerationError):
        ClaudeBackend().generate("prompt", SCHEMA, tmp_path)
