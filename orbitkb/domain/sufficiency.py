"""Conservative, dimension-specific assessment of canonical endpoint evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orbitkb.domain.canonical import FactStatus
from orbitkb.domain.reduction import ContextCapsule


def _has_openapi_description(contract: object) -> bool:
    if not isinstance(contract, dict):
        return False
    formal = contract.get("formal_contract")
    return (isinstance(formal, dict) and formal.get("format") == "openapi"
            and isinstance(formal.get("description"), str) and bool(formal["description"].strip()))


def _has_proven_request_shape(contract: dict) -> bool:
    request = contract.get("request")
    if isinstance(request, dict):
        return bool(request.get("fields"))
    formal = contract.get("formal_contract")
    return isinstance(formal, dict) and formal.get("format") == "openapi" and (
        formal.get("request_body_present") is False
    )


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
                    "contract", "request_shape", "response_shape", "integrations", "integration_purpose",
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
        described = tuple(
            fact.id for fact in entrypoints
            if _has_openapi_description(fact.value.get("contract"))
        )
        behavior_enough = bool(described) and len(described) == len(entrypoints)
        request_enough = bool(contracts) and len(contracts) == len(entrypoints) and all(
            _has_proven_request_shape(contract) for contract in contracts
        )
        flow_incomplete = capsule.truncated or any(
            boundary.reason in {"unresolved", "depth_limit", "node_limit", "edge_limit"}
            for boundary in capsule.boundaries
        )
        authorization_limited = (
            capsule.navigation_truncated
            or "security_requirement" in capsule.report.omitted_fact_kinds
            or any(boundary.reason == "unresolved" for boundary in capsule.boundaries)
        )
        responses = [contract.get("returns") for contract in contracts]
        response_shapes = [response for response in responses
                           if isinstance(response, dict) and response.get("fields")]
        if not response_shapes:
            response_status = SufficiencyStatus.MISSING
            response_reason = "response fields require explicit evidence; a return type alone is insufficient"
        elif len(response_shapes) != len(contracts) or any(
            response.get("confidence") == "inferred" for response in response_shapes
        ):
            response_status = SufficiencyStatus.AMBIGUOUS
            response_reason = "response type inferred from an extension without proven receiver type"
        else:
            response_status = SufficiencyStatus.ENOUGH
            response_reason = "response fields have an explicit return type"

        dimensions = [
            DimensionAssessment(
                "contract", SufficiencyStatus.ENOUGH if contracts else SufficiencyStatus.MISSING,
                tuple(fact.id for fact in entrypoints),
                "static endpoint contract present" if contracts else "endpoint contract absent from retained evidence",
            ),
            DimensionAssessment(
                "request_shape",
                SufficiencyStatus.ENOUGH if request_enough else SufficiencyStatus.MISSING,
                tuple(fact.id for fact in entrypoints),
                "request payload fields or explicit absence of a body require evidence",
            ),
            DimensionAssessment(
                "response_shape",
                response_status,
                tuple(fact.id for fact in entrypoints),
                response_reason,
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
                "integration_purpose",
                SufficiencyStatus.ENOUGH if not calls and not flow_incomplete
                else SufficiencyStatus.AMBIGUOUS,
                tuple(fact.id for fact in calls),
                "static call targets do not establish purpose or exchanged data" if calls
                else "limited flow may omit calls" if flow_incomplete
                else "no route-reachable call requires a purpose",
            ),
            DimensionAssessment(
                "authorization",
                (SufficiencyStatus.ENOUGH if security and all(
                    fact.status == FactStatus.CONFIRMED for fact in security
                ) and not authorization_limited else SufficiencyStatus.AMBIGUOUS),
                tuple(fact.id for fact in security),
                "absence or uncertainty of an authorization fact does not prove public access",
            ),
            DimensionAssessment(
                "flow",
                SufficiencyStatus.AMBIGUOUS if flow_incomplete else SufficiencyStatus.ENOUGH,
                tuple(fact.id for fact in capsule.facts if fact.kind == "flow_edge"),
                "unresolved or limited flow must remain explicit",
            ),
            DimensionAssessment(
                "business_behavior",
                SufficiencyStatus.ENOUGH if behavior_enough else SufficiencyStatus.MISSING,
                described,
                "explicit OpenAPI operation description present" if behavior_enough
                else "canonical static facts do not establish a business description",
            ),
        ]
        return SufficiencyResult(tuple(dimensions), capsule.report.omitted_fact_ids)
