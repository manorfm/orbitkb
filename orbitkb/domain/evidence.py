"""Source-proven facts selected from a bounded canonical route flow."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from orbitkb.domain.canonical import (
    CanonicalFact,
    EntrypointKey,
    FactStatus,
    SourceReference,
)
from orbitkb.domain.navigation import (
    KnowledgeNavigator,
    TraversalBoundary,
    TraversalPolicy,
)


@dataclass(frozen=True)
class EvidenceProfile:
    kinds: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "kinds", frozenset(self.kinds))
        if not self.kinds or any(not kind.strip() for kind in self.kinds):
            raise ValueError("evidence profile must select at least one fact kind")


@dataclass(frozen=True)
class EvidenceFact:
    id: str
    kind: str
    value: dict
    status: FactStatus
    origin: str
    confidence: str | None
    sources: tuple[SourceReference, ...]
    path: tuple[str, ...]
    digest: str

    @classmethod
    def from_canonical(cls, fact: CanonicalFact, path: tuple[str, ...]) -> EvidenceFact:
        confidence = fact.attributes.get("confidence")
        confidence = confidence if isinstance(confidence, str) else None
        content = {
            "id": fact.id, "kind": fact.kind, "value": fact.attributes,
            "status": fact.status.value, "origin": fact.origin, "confidence": confidence,
            "sources": [asdict(source) for source in fact.sources],
        }
        encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return cls(fact.id, fact.kind, json.loads(encoded)["value"], fact.status, fact.origin,
                   confidence, fact.sources, path, hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class EvidenceSet:
    entrypoint: EntrypointKey
    facts: tuple[EvidenceFact, ...]
    boundaries: tuple[TraversalBoundary, ...]
    truncated: bool


class EvidenceComposer:
    """Selects a profile from one route without rereading source or calling a model."""

    def __init__(self, navigator: KnowledgeNavigator):
        self._navigator = navigator

    def compose(self, entrypoint: EntrypointKey, profile: EvidenceProfile,
                policy: TraversalPolicy) -> EvidenceSet:
        traversal = self._navigator.reachable(entrypoint, policy)
        facts = tuple(
            EvidenceFact.from_canonical(fact, traversal.path_to(fact.id))
            for fact in traversal.facts if fact.kind in profile.kinds
        )
        return EvidenceSet(entrypoint, facts, traversal.boundaries, traversal.truncated)
