from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class GenerationError(Exception):
    """Raised when a backend fails to produce valid structured output."""


@dataclass
class LLMUsage:
    """Token/cost accounting for one backend call, reported best-effort: a field
    stays None (never guessed) when the backend's CLI output doesn't carry it —
    including when any attempt in an accumulated run omitted it. This follows
    the same honesty convention the project already uses for e.g.
    persistence_entities.engine = 'unknown'."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cost_usd: float | None = None
    observations: int = field(default=0, repr=False, compare=False)

    def observed(self) -> "LLMUsage":
        """Mark one backend attempt, even when its usage fields are absent."""
        if self.observations:
            return self
        return LLMUsage(
            self.input_tokens, self.output_tokens, self.cached_input_tokens, self.cost_usd, 1,
        )

    def __add__(self, other: "LLMUsage") -> "LLMUsage":
        def _sum(a: int | float | None, b: int | float | None) -> int | float | None:
            return a + b if a is not None and b is not None else None

        if not self.observations:
            return other
        if not other.observations:
            return self

        return LLMUsage(
            input_tokens=_sum(self.input_tokens, other.input_tokens),
            output_tokens=_sum(self.output_tokens, other.output_tokens),
            cached_input_tokens=_sum(self.cached_input_tokens, other.cached_input_tokens),
            cost_usd=_sum(self.cost_usd, other.cost_usd),
            observations=self.observations + other.observations,
        )


@dataclass
class GenerationOutcome:
    """What one LLMBackend.generate() call produced: the schema-shaped structured
    result plus whatever token/cost usage the backend could report for it."""

    structured: dict
    usage: LLMUsage = field(default_factory=LLMUsage)


class LLMBackend(Protocol):
    name: str

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        """Run the prompt through the model and return parsed JSON matching schema,
        plus best-effort token/cost usage for that call.

        Implementations must not allow the model to use tools/shell access — all
        context is pasted into the prompt already, so the model only needs to reason
        over text and return structured JSON.
        """
        ...
