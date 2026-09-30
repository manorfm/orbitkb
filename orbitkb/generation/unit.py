"""Accounting for one indexing unit, independent of concrete providers and storage."""

from dataclasses import dataclass, field

from orbitkb.generation.backend_base import LLMUsage


@dataclass
class IndexUnit:
    """A planned generation unit and the backend usage observed for it."""

    kind: str
    identity: tuple[str, ...]
    status: str = "skipped"
    llm_invocations: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)
    backend_duration_ms: float = 0.0
    prompt_chars: int = 0

    def record_attempt(self) -> None:
        self.llm_invocations += 1

    def record_usage(self, usage: LLMUsage) -> None:
        self.usage = self.usage + usage.observed()

    def record_duration(self, duration_ms: float) -> None:
        self.backend_duration_ms += duration_ms

    def record_prompt(self, prompt: str) -> None:
        self.prompt_chars += len(prompt)
