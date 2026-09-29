"""Compact, route-proven outbound call hints for endpoint generation."""

from __future__ import annotations

from orbitkb.domain.canonical import CanonicalSnapshot, EntrypointKey
from orbitkb.domain.evidence import EvidenceComposer, EvidenceProfile
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.domain.reduction import EvidenceBudget, EvidenceReducer
from orbitkb.domain.sufficiency import (
    DeterministicSufficiencyEvaluator,
    SufficiencyResult,
)


def _entrypoints(snapshot: CanonicalSnapshot, method: str, path: str) -> tuple[EntrypointKey, ...]:
    return tuple(
        fact.subject for fact in snapshot.facts
        if fact.kind == "entrypoint" and isinstance(fact.subject, EntrypointKey)
        and fact.subject.method == method and fact.subject.name == path
    )


def route_sufficiency(snapshot: CanonicalSnapshot, method: str, path: str) -> SufficiencyResult | None:
    entrypoints = _entrypoints(snapshot, method, path)
    if not entrypoints:
        return None
    composer = EvidenceComposer(KnowledgeNavigator(snapshot))
    profile = EvidenceProfile(frozenset({
        "entrypoint", "flow_edge", "service_call", "security_requirement",
    }))
    capsule = EvidenceReducer().reduce(tuple(
        composer.compose(entrypoint, profile, TraversalPolicy()) for entrypoint in entrypoints
    ), EvidenceBudget(20_000))
    return DeterministicSufficiencyEvaluator().evaluate(capsule)


def route_outbound_hints(snapshot: CanonicalSnapshot, method: str, path: str,
                         *, max_chars: int = 4_000) -> str | None:
    entrypoints = _entrypoints(snapshot, method, path)
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
