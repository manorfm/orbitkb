"""Bounded navigation over language-neutral, evidence-bearing facts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    EntrypointKey,
    SourceReference,
    SymbolKey,
)


@dataclass(frozen=True)
class TraversalPolicy:
    max_depth: int = 8
    max_nodes: int = 100
    max_edges: int = 200
    relations: frozenset[str] = frozenset({"invokes", "injects", "validates", "reads", "writes", "publishes", "consumes",
                                           "uses_config"})

    def __post_init__(self) -> None:
        if self.max_depth < 0 or self.max_nodes < 1 or self.max_edges < 1:
            raise ValueError("traversal limits must be non-negative depth and positive node/edge counts")


@dataclass(frozen=True)
class TraversalBoundary:
    source: str
    target: str
    reason: str
    edge_id: str


@dataclass(frozen=True)
class FactPath:
    fact_id: str
    symbols: tuple[str, ...]


@dataclass(frozen=True)
class TraversalResult:
    facts: tuple[CanonicalFact, ...]
    paths: tuple[FactPath, ...]
    boundaries: tuple[TraversalBoundary, ...]
    truncated: bool

    def path_to(self, fact_id: str) -> tuple[str, ...]:
        for path in self.paths:
            if path.fact_id == fact_id:
                return path.symbols
        raise KeyError(fact_id)


class KnowledgeNavigator:
    """Visits only symbol links backed by facts in one canonical snapshot."""

    def __init__(self, snapshot: CanonicalSnapshot):
        self.snapshot = snapshot
        self._by_symbol: dict[str, list[CanonicalFact]] = {}
        for fact in snapshot.facts:
            if isinstance(fact.subject, SymbolKey) and fact.subject.service == snapshot.service:
                self._by_symbol.setdefault(fact.subject.name, []).append(fact)
        self._external_edges: set[str] = set()
        for attached in self._by_symbol.values():
            edges_by_source: dict[SourceReference, list[CanonicalFact]] = {}
            calls_by_source: dict[SourceReference, list[CanonicalFact]] = {}
            for fact in attached:
                if fact.kind == "flow_edge":
                    destination = edges_by_source
                elif fact.kind == "service_call":
                    destination = calls_by_source
                else:
                    continue
                for source in fact.sources:
                    destination.setdefault(source, []).append(fact)
            for fact in attached:
                if fact.kind == "flow_edge" and fact.sources and all(
                    len(edges_by_source[source]) == 1 and len(calls_by_source.get(source, ())) == 1
                    for source in fact.sources
                ):
                    self._external_edges.add(fact.id)

    def reachable(self, entrypoint: EntrypointKey, policy: TraversalPolicy) -> TraversalResult:
        if entrypoint.service != self.snapshot.service:
            raise KeyError("entrypoint belongs to another service")
        root = next((fact for fact in self.snapshot.facts
                     if fact.kind == "entrypoint" and fact.subject == entrypoint), None)
        if root is None:
            raise KeyError("entrypoint is absent from the snapshot")

        facts: dict[str, CanonicalFact] = {root.id: root}
        paths: dict[str, tuple[str, ...]] = {root.id: (entrypoint.symbol,)}
        boundaries: list[TraversalBoundary] = []
        pending = deque([(entrypoint.symbol, (entrypoint.symbol,))])
        visited = {entrypoint.symbol}
        edge_count = 0
        truncated = False

        while pending:
            symbol, path = pending.popleft()
            attached = self._by_symbol.get(symbol, ())
            for fact in attached:
                if fact.kind != "flow_edge":
                    facts.setdefault(fact.id, fact)
                    paths.setdefault(fact.id, path)
                    if fact.kind == "flow_boundary":
                        boundaries.append(TraversalBoundary(symbol, str(fact.attributes.get("boundary_kind", "")),
                                                            "known_boundary", fact.id))

            for edge in attached:
                if edge.kind != "flow_edge" or edge.attributes.get("relation") not in policy.relations:
                    continue
                target = edge.attributes.get("target")
                if not isinstance(target, str) or not target:
                    target = ""
                if edge_count >= policy.max_edges:
                    boundaries.append(TraversalBoundary(symbol, target, "edge_limit", edge.id))
                    truncated = True
                    break
                edge_count += 1
                facts.setdefault(edge.id, edge)
                paths.setdefault(edge.id, path)
                if not target or target not in self._by_symbol:
                    reason = "external_call" if edge.id in self._external_edges else "unresolved"
                    boundaries.append(TraversalBoundary(symbol, target, reason, edge.id))
                elif target in path:
                    boundaries.append(TraversalBoundary(symbol, target, "cycle", edge.id))
                elif target in visited:
                    boundaries.append(TraversalBoundary(symbol, target, "already_visited", edge.id))
                elif len(path) > policy.max_depth:
                    boundaries.append(TraversalBoundary(symbol, target, "depth_limit", edge.id))
                    truncated = True
                elif len(visited) >= policy.max_nodes:
                    boundaries.append(TraversalBoundary(symbol, target, "node_limit", edge.id))
                    truncated = True
                else:
                    visited.add(target)
                    pending.append((target, (*path, target)))

        return TraversalResult(tuple(facts.values()), tuple(FactPath(key, path) for key, path in paths.items()),
                               tuple(boundaries), truncated)
