"""Per-service limits checked between model attempts."""

from dataclasses import dataclass
from math import isfinite

from orbitkb.generation.backend_base import LLMUsage


@dataclass
class ModelBudget:
    max_invocations: int | None = None
    max_reported_cost_usd: float | None = None
    invocations: int = 0
    reported_cost_usd: float = 0.0
    pending_cost_reports: int = 0
    cost_unavailable: bool = False
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if self.max_invocations is not None and self.max_invocations < 0:
            raise ValueError("max_llm_invocations must be nonnegative")
        if self.max_reported_cost_usd is not None and (
            not isfinite(self.max_reported_cost_usd) or self.max_reported_cost_usd < 0
        ):
            raise ValueError("max_reported_cost_usd must be finite and nonnegative")

    def start_attempt(self) -> bool:
        if self.max_invocations is not None and self.invocations >= self.max_invocations:
            self.stop_reason = "invocation budget exhausted"
            return False
        if self.max_reported_cost_usd is not None:
            if self.pending_cost_reports or self.cost_unavailable:
                self.stop_reason = "cost unavailable"
                return False
            if self.reported_cost_usd >= self.max_reported_cost_usd:
                self.stop_reason = "cost budget exhausted"
                return False
            self.pending_cost_reports += 1
        self.invocations += 1
        return True

    def record_usage(self, usage: LLMUsage) -> None:
        if self.max_reported_cost_usd is None:
            return
        self.pending_cost_reports -= 1
        cost = usage.cost_usd
        if cost is None or not isinstance(cost, (int, float)) or not isfinite(cost) or cost < 0:
            self.cost_unavailable = True
        else:
            self.reported_cost_usd += cost
