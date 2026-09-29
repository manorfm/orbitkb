"""Bounded symbol resolution for locally extracted flows.

This is deliberately not a repository-wide graph. It resolves only an observed
call when one unambiguous implementation is already part of the bounded static
flow, preferring constructor/field injection over a method-name fallback.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import replace

from orbitkb.analysis.models import AnalysisResult, FlowEdge, Symbol


class BoundedFlowResolver:
    """Links local call expressions to known symbols without inventing edges."""

    def resolve(self, result: AnalysisResult) -> AnalysisResult:
        counts = Counter(symbol.name for symbol in result.symbols)
        symbols = {symbol.name: symbol for symbol in result.symbols if counts[symbol.name] == 1}
        implementations = set(symbols)
        implementation_types = self._implementation_types(symbols.values())
        injections = {
            edge.source: edge.target
            for edge in result.edges
            if edge.kind == "injects" and "." in edge.source
        }
        qualifiers = {injection.consumer: injection.qualifier for injection in result.injections}
        injections.update({injection.consumer: injection.contract for injection in result.injections})
        result.edges = [
            self._resolve_edge(edge, symbols, implementations, injections, qualifiers, implementation_types)
            for edge in result.edges
        ]
        return result

    @staticmethod
    def _implementation_types(symbols: Iterable[Symbol]) -> dict[str, set[str]]:
        implementations: dict[str, set[str]] = {}
        for symbol in symbols:
            for contract in symbol.implements:
                implementations.setdefault(contract, set()).add(symbol.owner)
        return implementations

    @staticmethod
    def _resolve_edge(
        edge: FlowEdge,
        symbols: dict[str, Symbol],
        implementations: set[str],
        injections: dict[str, str],
        qualifiers: dict[str, str | None],
        implementation_types: dict[str, set[str]],
    ) -> FlowEdge:
        # Persistence operations are already classified from their direct, locally
        # proven receiver. Resolving them by a method-name fallback could replace
        # `db.Create` with an unrelated local `Create` function and corrupt the
        # compact operation projection exposed to agents.
        if edge.kind in {"injects", "reads", "writes"} or edge.target in implementations:
            return edge
        source_symbol = symbols.get(edge.source)
        imported_target = BoundedFlowResolver._imported_target(source_symbol, edge.target)
        if imported_target in implementations:
            return replace(edge, target=imported_target, confidence="high")
        receiver, separator, method = edge.target.rpartition(".")
        receiver = receiver.removeprefix("this.")
        owner = edge.source.split(".", 1)[0]
        injected_type = injections.get(f"{owner}.{receiver}") if separator else None
        injected_candidate = f"{injected_type}.{method}" if injected_type else None
        if injected_candidate in implementations:
            return replace(edge, target=injected_candidate, confidence="high")

        implementation_candidates = {
            f"{implementation}.{method}"
            for implementation in implementation_types.get(injected_type or "", set())
            if f"{implementation}.{method}" in implementations
        }
        if len(implementation_candidates) == 1:
            return replace(edge, target=implementation_candidates.pop(), confidence="high")

        qualified_candidates = BoundedFlowResolver._qualified_candidates(
            implementation_candidates, symbols, qualifiers.get(f"{owner}.{receiver}"),
        )
        if len(qualified_candidates) == 1:
            return replace(edge, target=qualified_candidates.pop(), confidence="high")

        primary_candidates = {
            candidate for candidate in implementation_candidates if symbols[candidate].primary
        }
        if len(primary_candidates) == 1:
            return replace(edge, target=primary_candidates.pop(), confidence="high")

        # A receiver-qualified call that did not resolve through an import or an
        # injection remains an observed external/local-object boundary. Falling back
        # to its method name alone can turn `this.client.create()` into an unrelated
        # local `Controller.create()` symbol.
        if separator:
            return edge
        candidates = sorted(symbol for symbol in implementations if symbol.endswith(f".{method}"))
        if len(candidates) == 1:
            return replace(edge, target=candidates[0], confidence="medium")
        return edge

    @staticmethod
    def _imported_target(symbol: Symbol | None, target: str) -> str | None:
        if symbol is None:
            return None
        imports = dict(symbol.imports)
        if target in imports:
            return imports[target]
        receiver, separator, member = target.partition(".")
        module = imports.get(receiver)
        return f"{module}.{member}" if module and separator else None

    @staticmethod
    def _qualified_candidates(
        candidates: set[str], symbols: dict[str, Symbol], qualifier: str | None,
    ) -> set[str]:
        if qualifier is None:
            return set()
        return {candidate for candidate in candidates if qualifier in symbols[candidate].qualifiers}
