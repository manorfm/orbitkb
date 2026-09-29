"""Bounded, loss-reporting reduction of source-proven route evidence."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from orbitkb.domain.canonical import EntrypointKey
from orbitkb.domain.evidence import EvidenceFact, EvidenceSet
from orbitkb.domain.navigation import TraversalBoundary


@dataclass(frozen=True)
class EvidenceBudget:
    max_chars: int

    def __post_init__(self) -> None:
        if self.max_chars < 0:
            raise ValueError("evidence character budget must be nonnegative")


@dataclass(frozen=True)
class EvidenceUse:
    entrypoint: EntrypointKey
    fact_id: str
    path: tuple[str, ...]


@dataclass(frozen=True)
class ReductionReport:
    input_facts: int
    unique_facts: int
    retained_facts: int
    omitted_fact_ids: tuple[str, ...]
    estimated_input_chars: int
    estimated_chars: int
    omitted_fact_kinds: tuple[str, ...]


@dataclass(frozen=True)
class ContextCapsule:
    entrypoints: tuple[EntrypointKey, ...]
    facts: tuple[EvidenceFact, ...]
    uses: tuple[EvidenceUse, ...]
    boundaries: tuple[TraversalBoundary, ...]
    report: ReductionReport
    truncated: bool
    navigation_truncated: bool


def _estimated_size(fact: EvidenceFact, uses: Sequence[EvidenceUse]) -> int:
    payload = {
        "id": fact.id, "kind": fact.kind, "value": fact.value,
        "status": fact.status.value, "origin": fact.origin,
        "confidence": fact.confidence, "sources": [asdict(source) for source in fact.sources],
        "digest": fact.digest,
        "uses": [{"entrypoint": asdict(use.entrypoint), "path": use.path} for use in uses],
    }
    return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


class EvidenceReducer:
    """Keeps one fact payload per digest and reports budget omissions."""

    def reduce(self, evidence: Sequence[EvidenceSet], budget: EvidenceBudget) -> ContextCapsule:
        if not evidence:
            raise ValueError("at least one route evidence set is required")
        service = evidence[0].entrypoint.service
        if any(item.entrypoint.service != service for item in evidence):
            raise ValueError("evidence sets must belong to one service")

        facts_by_digest: dict[str, EvidenceFact] = {}
        uses_by_digest: dict[str, list[EvidenceUse]] = {}
        digests_by_id: dict[str, str] = {}
        boundaries: dict[tuple[str, str, str, str], TraversalBoundary] = {}
        input_facts = input_chars = 0
        for route in evidence:
            for boundary in route.boundaries:
                boundaries.setdefault((boundary.source, boundary.target, boundary.reason, boundary.edge_id), boundary)
            for fact in route.facts:
                previous = digests_by_id.setdefault(fact.id, fact.digest)
                if previous != fact.digest:
                    raise ValueError(f"conflicting evidence for fact {fact.id}")
                use = EvidenceUse(route.entrypoint, fact.id, fact.path)
                input_facts += 1
                input_chars += _estimated_size(fact, (use,))
                facts_by_digest.setdefault(fact.digest, fact)
                uses = uses_by_digest.setdefault(fact.digest, [])
                if use not in uses:
                    uses.append(use)

        retained: list[EvidenceFact] = []
        retained_uses: list[EvidenceUse] = []
        omitted: list[str] = []
        omitted_kinds: list[str] = []
        selected_chars = 0
        priority = {"entrypoint": 0, "service_call": 1, "security_requirement": 2}
        for digest, fact in sorted(facts_by_digest.items(), key=lambda item: priority.get(item[1].kind, 3)):
            uses = uses_by_digest[digest]
            size = _estimated_size(fact, uses)
            if selected_chars + size > budget.max_chars:
                omitted.append(fact.id)
                omitted_kinds.append(fact.kind)
                continue
            retained.append(fact)
            retained_uses.extend(uses)
            selected_chars += size

        report = ReductionReport(input_facts, len(facts_by_digest), len(retained),
                                 tuple(omitted), input_chars, selected_chars, tuple(omitted_kinds))
        entrypoints = tuple(dict.fromkeys(item.entrypoint for item in evidence))
        navigation_truncated = any(item.truncated for item in evidence)
        return ContextCapsule(entrypoints, tuple(retained), tuple(retained_uses), tuple(boundaries.values()), report,
                              navigation_truncated or bool(omitted), navigation_truncated)
