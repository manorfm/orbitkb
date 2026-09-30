"""Unit tests for generation.llm_harness.generate_with_retry: prompt/schema loading
is exercised indirectly elsewhere (orchestrator/change_surface tests); this file
covers retry behavior and token/cost usage accounting in isolation, with a scripted
fake backend that returns GenerationOutcome directly (no real claude/codex subprocess).
"""
from pathlib import Path

import pytest

from orbitkb.generation.backend_base import GenerationError, GenerationOutcome, LLMUsage
from orbitkb.generation.llm_harness import generate_with_retry

SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}


class ScriptedBackend:
    name = "fake"

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.prompts: list[str] = []

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        self.calls += 1
        self.prompts.append(prompt)
        item = self._outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_successful_first_attempt_returns_structured_result_and_usage(tmp_path: Path):
    outcome = GenerationOutcome(
        structured={"summary": "ok"}, usage=LLMUsage(input_tokens=100, output_tokens=20, cost_usd=0.01)
    )
    backend = ScriptedBackend([outcome])

    result = generate_with_retry(backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label")

    assert result.structured == {"summary": "ok"}
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cost_usd == 0.01
    assert backend.calls == 1


def test_usage_is_summed_across_a_validation_retry(tmp_path: Path):
    invalid = GenerationOutcome(
        structured={"wrong": "shape"}, usage=LLMUsage(input_tokens=50, output_tokens=10, cost_usd=0.005)
    )
    valid = GenerationOutcome(
        structured={"summary": "ok"}, usage=LLMUsage(input_tokens=60, output_tokens=12, cost_usd=0.006)
    )
    backend = ScriptedBackend([invalid, valid])
    attempts: list[None] = []

    result = generate_with_retry(
        backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label",
        on_attempt=lambda: attempts.append(None),
    )

    assert result.structured == {"summary": "ok"}
    assert result.usage.input_tokens == 110
    assert result.usage.output_tokens == 22
    assert result.usage.cost_usd == pytest.approx(0.011)
    assert backend.calls == 2
    assert len(attempts) == 2


def test_usage_stays_none_when_backend_never_reports_it(tmp_path: Path):
    outcome = GenerationOutcome(structured={"summary": "ok"}, usage=LLMUsage())
    backend = ScriptedBackend([outcome])

    result = generate_with_retry(backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label")

    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    assert result.usage.cost_usd is None


def test_exhausted_retries_returns_none_and_writes_failure_file(tmp_path: Path):
    backend = ScriptedBackend([GenerationError("boom"), GenerationError("boom again")])
    failures_dir = tmp_path / "failures"

    result = generate_with_retry(backend, "prompt", SCHEMA, tmp_path, failures_dir, "my-label")

    assert result is None
    assert failures_dir.exists()
    assert list(failures_dir.glob("my-label-*.txt"))


def test_invocation_observer_counts_failed_backend_attempts(tmp_path: Path):
    backend = ScriptedBackend([GenerationError("first"), GenerationError("second")])
    attempts: list[None] = []

    result = generate_with_retry(
        backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label",
        on_attempt=lambda: attempts.append(None),
    )

    assert result is None
    assert len(attempts) == backend.calls == 2


def test_prompt_observer_receives_each_redacted_attempt_including_retry(tmp_path: Path):
    secret = "production-secret-value"
    backend = ScriptedBackend([
        GenerationError("first"), GenerationOutcome(structured={"summary": "ok"}),
    ])
    observed: list[str] = []

    result = generate_with_retry(
        backend, f"API_TOKEN={secret}", SCHEMA, tmp_path, tmp_path / "failures", "label",
        on_prompt=observed.append,
    )

    assert result is not None
    assert observed == backend.prompts
    assert len(observed) == 2
    assert secret not in "".join(observed)
    assert len(observed[1]) > len(observed[0])


def test_backend_duration_observer_includes_failed_retry(tmp_path: Path, monkeypatch):
    backend = ScriptedBackend([GenerationError("first"), GenerationOutcome(structured={"summary": "ok"})])
    ticks = iter([10.0, 10.25, 11.0, 11.5])
    monkeypatch.setattr("orbitkb.generation.llm_harness.time.perf_counter", lambda: next(ticks))
    durations: list[float] = []

    result = generate_with_retry(
        backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label",
        on_duration_ms=durations.append,
    )

    assert result is not None
    assert durations == [250.0, 500.0]


def test_usage_observer_keeps_usage_from_invalid_responses(tmp_path: Path):
    backend = ScriptedBackend([
        GenerationOutcome(structured={"wrong": "shape"}, usage=LLMUsage(input_tokens=10, cost_usd=0.02)),
        GenerationOutcome(structured={"wrong": "again"}, usage=LLMUsage(input_tokens=15, cost_usd=0.03)),
    ])
    usage: list[LLMUsage] = []

    result = generate_with_retry(
        backend, "prompt", SCHEMA, tmp_path, tmp_path / "failures", "label",
        on_usage=usage.append,
    )

    assert result is None
    assert sum(item.input_tokens for item in usage) == 25
    assert sum(item.cost_usd for item in usage) == pytest.approx(0.05)


def test_generation_boundary_redacts_prompt_output_and_failure_log(tmp_path: Path):
    secret = "production-secret-value"
    backend = ScriptedBackend([
        GenerationOutcome(structured={"summary": f"token={secret}"}),
    ])

    result = generate_with_retry(
        backend, f"API_TOKEN={secret}", SCHEMA, tmp_path, tmp_path / "failures", "label"
    )

    assert secret not in backend.prompts[0]
    assert result is not None
    assert secret not in result.structured["summary"]

    failing = ScriptedBackend([GenerationError(secret), GenerationError(secret)])
    failures_dir = tmp_path / "failure-logs"
    generate_with_retry(failing, f"API_TOKEN={secret}", SCHEMA, tmp_path, failures_dir, "label")
    assert secret not in next(failures_dir.iterdir()).read_text(encoding="utf-8")
