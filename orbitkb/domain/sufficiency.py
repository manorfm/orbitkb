"""Conservative, dimension-specific assessment of canonical endpoint evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orbitkb.domain.canonical import FactStatus
from orbitkb.domain.reduction import ContextCapsule


class SufficiencyStatus(str, Enum):
    ENOUGH = "enough"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class DimensionAssessment:
    dimension: str
    status: SufficiencyStatus
    evidence_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class SufficiencyResult:
    dimensions: tuple[DimensionAssessment, ...]
    omitted_fact_ids: tuple[str, ...]

    def status(self, dimension: str) -> SufficiencyStatus:
        return self._dimension(dimension).status

    def evidence_ids(self, dimension: str) -> tuple[str, ...]:
        return self._dimension(dimension).evidence_ids

    def _dimension(self, name: str) -> DimensionAssessment:
        return next(item for item in self.dimensions if item.dimension == name)

    @property
    def overall(self) -> SufficiencyStatus:
        statuses = {item.status for item in self.dimensions}
        for status in (SufficiencyStatus.UNSUPPORTED, SufficiencyStatus.MISSING, SufficiencyStatus.AMBIGUOUS):
            if status in statuses:
                return status
        return SufficiencyStatus.ENOUGH


class DeterministicSufficiencyEvaluator:
    """Reports what current static facts establish, without authorizing LLM skips."""

    def evaluate(self, capsule: ContextCapsule) -> SufficiencyResult:
        if any(entrypoint.transport != "http" for entrypoint in capsule.entrypoints):
            return SufficiencyResult(tuple(
                DimensionAssessment(dimension, SufficiencyStatus.UNSUPPORTED, (),
                                    "deterministic endpoint assessment currently supports HTTP only")
                for dimension in (
                    "contract", "request_shape", "response_shape", "integrations",
                    "authorization", "flow", "business_behavior",
                )
            ), capsule.report.omitted_fact_ids)
        by_kind = {
            kind: tuple(fact for fact in capsule.facts if fact.kind == kind)
            for kind in ("entrypoint", "service_call", "security_requirement")
        }
        entrypoints = by_kind["entrypoint"]
        contracts = [fact.value.get("contract") for fact in entrypoints
                     if isinstance(fact.value.get("contract"), dict)]
        calls = by_kind["service_call"]
        security = by_kind["security_requirement"]

        dimensions = [
            DimensionAssessment(
                "contract", SufficiencyStatus.ENOUGH if contracts else SufficiencyStatus.MISSING,
                tuple(fact.id for fact in entrypoints),
                "static endpoint contract present" if contracts else "endpoint contract absent from retained evidence",
            ),
            DimensionAssessment(
                "request_shape",
                SufficiencyStatus.ENOUGH if any(
                    isinstance(contract.get("request"), dict) and contract["request"].get("fields")
                    for contract in contracts
                ) else SufficiencyStatus.MISSING,
                tuple(fact.id for fact in entrypoints),
                "request payload fields require explicit evidence",
            ),
            DimensionAssessment(
                "response_shape",
                SufficiencyStatus.ENOUGH if any(
                    isinstance(contract.get("returns"), dict) and contract["returns"].get("fields")
                    for contract in contracts
                ) else SufficiencyStatus.MISSING,
                tuple(fact.id for fact in entrypoints),
                "response fields require explicit evidence; a return type alone is insufficient",
            ),
            DimensionAssessment(
                "integrations",
                (SufficiencyStatus.AMBIGUOUS if capsule.truncated or not calls
                 else SufficiencyStatus.ENOUGH),
                tuple(fact.id for fact in calls),
                "limited or absent route call evidence" if capsule.truncated or not calls
                else "route-reachable calls have source evidence",
            ),
            DimensionAssessment(
                "authorization",
                (SufficiencyStatus.ENOUGH if security and all(
                    fact.status == FactStatus.CONFIRMED for fact in security
                ) and not capsule.truncated else SufficiencyStatus.AMBIGUOUS),
                tuple(fact.id for fact in security),
                "absence or uncertainty of an authorization fact does not prove public access",
            ),
            DimensionAssessment(
                "flow",
                SufficiencyStatus.AMBIGUOUS if capsule.truncated or any(
                    boundary.reason in {"unresolved", "depth_limit", "node_limit", "edge_limit"}
                    for boundary in capsule.boundaries
                ) else SufficiencyStatus.ENOUGH,
                tuple(fact.id for fact in capsule.facts if fact.kind == "flow_edge"),
                "unresolved or limited flow must remain explicit",
            ),
            DimensionAssessment(
                "business_behavior", SufficiencyStatus.MISSING, (),
                "canonical static facts do not establish a business description",
            ),
        ]
        return SufficiencyResult(tuple(dimensions), capsule.report.omitted_fact_ids)
