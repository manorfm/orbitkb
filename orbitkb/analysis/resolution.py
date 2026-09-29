"""Bounded symbol resolution for locally extracted flows.

This is deliberately not a repository-wide graph. It resolves only an observed
call when one unambiguous implementation is already part of the bounded static
flow, preferring constructor/field injection over a method-name fallback.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from orbitkb.analysis.models import AnalysisResult, FlowEdge, Symbol


class BoundedFlowResolver:
    """Links local call expressions to known symbols without inventing edges."""

    def resolve(self, result: AnalysisResult) -> AnalysisResult:
        declarations: dict[str, list[Symbol]] = {}
        for symbol in result.symbols:
            declarations.setdefault(symbol.name, []).append(symbol)
        symbols: dict[str, Symbol] = {}
        overloaded: set[str] = set()
        for name, group in declarations.items():
            if len(group) == 1:
                symbols[name] = group[0]
            elif len({(symbol.owner, symbol.evidence.file_path) for symbol in group}) == 1:
                symbols[name] = group[0]
                overloaded.add(name)
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
            self._resolve_edge(edge, symbols, implementations, injections, qualifiers, implementation_types,
                               overloaded)
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
        overloaded: set[str],
    ) -> FlowEdge:
        # Persistence operations are already classified from their direct, locally
        # proven receiver. Resolving them by a method-name fallback could replace
        # `db.Create` with an unrelated local `Create` function and corrupt the
        # compact operation projection exposed to agents.
        if edge.kind in {"injects", "reads", "writes"}:
            return edge
        if edge.target in implementations:
            return replace(edge, confidence="medium") if edge.target in overloaded else edge
        source_symbol = symbols.get(edge.source)
        imported_target = BoundedFlowResolver._imported_target(source_symbol, edge.target)
        if imported_target in implementations:
            return BoundedFlowResolver._link(edge, imported_target, overloaded)
        receiver, separator, method = edge.target.rpartition(".")
        receiver = receiver.removeprefix("this.")
        if separator and source_symbol is not None and edge.source not in overloaded:
            parameter_type = dict(source_symbol.parameters).get(receiver)
            if parameter_type:
                simple_type = parameter_type.split("<", 1)[0].rsplit(".", 1)[-1]
                parameter_candidate = f"{simple_type}.{method}"
                if parameter_candidate in implementations:
                    # A simple JVM type name can still refer to an imported class
                    # outside this index, so this link is a possible local path.
                    return replace(edge, target=parameter_candidate, confidence="medium")
        owner = edge.source.split(".", 1)[0]
        injected_type = injections.get(f"{owner}.{receiver}") if separator else None
        injected_candidate = f"{injected_type}.{method}" if injected_type else None
        if injected_candidate in implementations:
            return BoundedFlowResolver._link(edge, injected_candidate, overloaded)

        implementation_candidates = {
            f"{implementation}.{method}"
            for implementation in implementation_types.get(injected_type or "", set())
            if f"{implementation}.{method}" in implementations
        }
        if len(implementation_candidates) == 1:
            return BoundedFlowResolver._link(edge, implementation_candidates.pop(), overloaded)

        qualified_candidates = BoundedFlowResolver._qualified_candidates(
            implementation_candidates, symbols, qualifiers.get(f"{owner}.{receiver}"),
        )
        if len(qualified_candidates) == 1:
            return BoundedFlowResolver._link(edge, qualified_candidates.pop(), overloaded)

        primary_candidates = {
            candidate for candidate in implementation_candidates if symbols[candidate].primary
        }
        if len(primary_candidates) == 1:
            return BoundedFlowResolver._link(edge, primary_candidates.pop(), overloaded)

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
    def _link(edge: FlowEdge, target: str, overloaded: set[str]) -> FlowEdge:
        return replace(edge, target=target, confidence="medium" if target in overloaded else "high")

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
