"""Per-service limits checked between model attempts."""

from dataclasses import dataclass
from math import isfinite

from orbitkb.generation.backend_base import LLMUsage


@dataclass
class ModelBudget:
    max_invocations: int | None = None
    max_reported_cost_usd: float | None = None
    max_reported_tokens: int | None = None
    invocations: int = 0
    reported_cost_usd: float = 0.0
    reported_tokens: int = 0
    pending_cost_reports: int = 0
    pending_token_reports: int = 0
    cost_unavailable: bool = False
    tokens_unavailable: bool = False
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.max_invocations is not None and self.max_invocations < 0:
            raise ValueError("max_llm_invocations must be nonnegative")
        if self.max_reported_cost_usd is not None and (
            not isfinite(self.max_reported_cost_usd) or self.max_reported_cost_usd < 0
        ):
            raise ValueError("max_reported_cost_usd must be finite and nonnegative")
        if self.max_reported_tokens is not None and (
            not isinstance(self.max_reported_tokens, int)
            or isinstance(self.max_reported_tokens, bool)
            or self.max_reported_tokens < 0
        ):
            raise ValueError("max_reported_tokens must be a nonnegative integer")

    def start_attempt(self) -> bool:
        if self.max_invocations is not None and self.invocations >= self.max_invocations:
            self.stop_reason = "invocation budget exhausted"
            return False
        if self.max_reported_tokens is not None:
            if self.pending_token_reports or self.tokens_unavailable:
                self.stop_reason = "token usage unavailable"
                return False
            if self.reported_tokens >= self.max_reported_tokens:
                self.stop_reason = "token budget exhausted"
                return False
        if self.max_reported_cost_usd is not None:
            if self.pending_cost_reports or self.cost_unavailable:
                self.stop_reason = "cost unavailable"
                return False
            if self.reported_cost_usd >= self.max_reported_cost_usd:
                self.stop_reason = "cost budget exhausted"
                return False
            self.pending_cost_reports += 1
        if self.max_reported_tokens is not None:
            self.pending_token_reports += 1
        self.invocations += 1
        return True

    def record_usage(self, usage: LLMUsage) -> None:
        if self.max_reported_cost_usd is not None:
            self.pending_cost_reports -= 1
            cost = usage.cost_usd
            if cost is None or not isinstance(cost, (int, float)) or not isfinite(cost) or cost < 0:
                self.cost_unavailable = True
            else:
                self.reported_cost_usd += cost
        if self.max_reported_tokens is not None:
            self.pending_token_reports -= 1
            input_tokens, output_tokens = usage.input_tokens, usage.output_tokens
            if any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in (input_tokens, output_tokens)
            ):
                self.tokens_unavailable = True
            else:
                self.reported_tokens += input_tokens + output_tokens
