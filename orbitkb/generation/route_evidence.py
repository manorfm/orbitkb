"""Compact, route-proven outbound call hints for endpoint generation."""

from __future__ import annotations

from orbitkb.domain.canonical import CanonicalSnapshot
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile
from orbitkb.domain.navigation import (
    KnowledgeNavigator,
    TraversalPolicy,
    route_entrypoints,
)
from orbitkb.domain.reduction import ContextCapsule, EvidenceBudget, EvidenceReducer
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyResult,
)


def route_capsule(snapshot: CanonicalSnapshot, method: str, path: str) -> ContextCapsule | None:
    entrypoints = route_entrypoints(snapshot, method, path)
    if not entrypoints:
        return None
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({
        "entrypoint", "flow_edge", "service_call", "security_requirement",
    }))
    capsule = EvidenceReducer().reduce(tuple(
        composer.compose(entrypoint, profile, TraversalPolicy()) for entrypoint in entrypoints
    ), EvidenceBudget(20_000))
    return capsule


def route_sufficiency(snapshot: CanonicalSnapshot, method: str, path: str) -> SufficiencyResult | None:
    capsule = route_capsule(snapshot, method, path)
    return DeterministicSufficiencyEvaluator().evaluate(capsule) if capsule else None


def route_documentation_state(capsule: ContextCapsule | None) -> tuple | None:
    """Compare route meaning while ignoring source locations and budget size."""
    if capsule is None:
        return None
    facts = []
    for fact in capsule.facts:
        value = fact.value
        if fact.kind == "entrypoint":
            contract = value.get("contract")
            formal = contract.get("formal_contract") if isinstance(contract, dict) else None
            if isinstance(formal, dict):
                value = {
                    **value,
                    "contract": {
                        **contract,
                        "formal_contract": {key: item for key, item in formal.items() if key != "evidence"},
                    },
                }
        facts.append((fact.id, fact.kind, value, fact.status, fact.origin, fact.path))
    return (
        capsule.entrypoints, tuple(facts), capsule.uses, capsule.boundaries,
        capsule.report.omitted_fact_ids, capsule.report.omitted_fact_kinds,
        capsule.truncated, capsule.navigation_truncated, capsule.selected_kinds,
    )


def route_outbound_hints(snapshot: CanonicalSnapshot, method: str, path: str,
                         *, max_chars: int = 4_000) -> str | None:
    entrypoints = route_entrypoints(snapshot, method, path)
    if not entrypoints:
        return None
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({"service_call"}))
    route_evidence = tuple(
        composer.compose(entrypoint, profile, TraversalPolicy()) for entrypoint in entrypoints
    )
    capsule = EvidenceReducer().reduce(
        route_evidence,
        EvidenceBudget(max_chars),
    )
    calls = [fact for fact in capsule.facts if fact.kind == "service_call"]
    lines: list[str] = []
    for call in calls:
        destination = call.value.get("target_service") or "unknown target"
        protocol = call.value.get("protocol") or "call"
        operation = " ".join(str(value) for value in (
            call.value.get("target_method"), call.value.get("target_path"),
        ) if value)
        source = call.sources[0] if call.sources else None
        location = f"{source.file_path}:{source.start_line}" if source else "source unknown"
        uses = [use for use in capsule.uses if use.fact_id == call.id]
        paths = "; ".join(" → ".join(use.path) for use in uses)
        lines.append(f"- [route-reachable {protocol}] {destination} {operation} ({location}; via {paths})")
    limitations = []
    if capsule.report.omitted_fact_ids:
        limitations.append(f"evidence budget limited: {len(capsule.report.omitted_fact_ids)} route call(s) omitted")
    if any(item.truncated for item in route_evidence):
        limitations.append("static flow limited")
    if limitations:
        lines.append(f"- ({'; '.join(limitations)}; other calls may exist)")
    return "\n".join(lines) if lines else None
