"""Shared LLM invocation harness: prompt/schema loading, retry-with-validation, and
failure logging. Used by both the indexing orchestrator and change_surface — the one
place that knows how to turn a prompt + schema into a validated dict via an
LLMBackend, so neither caller re-implements retry/validation logic.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from functools import cache
from importlib import resources
from pathlib import Path
from string import Template

import jsonschema

from orbitkb.generation.backend_base import (
    GenerationError,
    GenerationOutcome,
    LLMBackend,
    LLMUsage,
)
from orbitkb.security.redaction import redact_sensitive_values, redact_structured_values

logger = logging.getLogger(__name__)

PROMPTS_PKG = "orbitkb.generation.prompts"
SCHEMAS_PKG = "orbitkb.generation.schemas"


@cache
def load_prompt(name: str) -> Template:
    text = resources.files(PROMPTS_PKG).joinpath(f"{name}.md").read_text(encoding="utf-8")
    return Template(text)


@cache
def load_schema(name: str) -> dict:
    text = resources.files(SCHEMAS_PKG).joinpath(f"{name}.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def generate_with_retry(
    backend: LLMBackend, prompt: str, schema: dict, cwd: Path, failures_dir: Path, label: str,
    on_attempt: Callable[[], None] | None = None,
    on_usage: Callable[[LLMUsage], None] | None = None,
    on_duration_ms: Callable[[float], None] | None = None,
) -> GenerationOutcome | None:
    last_error: Exception | None = None
    safe_prompt = redact_sensitive_values(prompt)
    current_prompt = safe_prompt
    total_usage = LLMUsage()
    for _attempt in range(2):
        try:
            if on_attempt is not None:
                on_attempt()
            started_at = time.perf_counter()
            try:
                outcome = backend.generate(current_prompt, schema, cwd)
            finally:
                if on_duration_ms is not None:
                    on_duration_ms((time.perf_counter() - started_at) * 1000)
            total_usage = total_usage + outcome.usage  # a retried call is still a billed call
            if on_usage is not None:
                on_usage(outcome.usage)
            jsonschema.validate(outcome.structured, schema)
            return GenerationOutcome(structured=redact_structured_values(outcome.structured), usage=total_usage)
        except (GenerationError, jsonschema.ValidationError) as exc:
            last_error = exc
            current_prompt = (
                f"{safe_prompt}\n\nYour previous response did not meet generation requirements. "
                "Return ONLY valid JSON matching the schema, no prose, no markdown fences."
            )

    # Failure capture is diagnostic only. A read-only home directory, common in
    # containers and test sandboxes, must not turn one failed model call into an
    # indexing crash or conceal the partial-result status from the caller.
    safe_label = label.replace("/", "_")
    fail_file = failures_dir / f"{safe_label}-{int(time.time())}.txt"
    try:
        failures_dir.mkdir(parents=True, exist_ok=True)
        fail_file.write_text(
            f"Prompt:\n{safe_prompt}\n\nLast error type:\n{type(last_error).__name__}", encoding="utf-8"
        )
    except OSError as write_error:
        logger.warning("generation failed for %s; failure log unavailable: %s", label, write_error)
    else:
        logger.warning("generation failed for %s (see %s)", label, fail_file)
    return None
