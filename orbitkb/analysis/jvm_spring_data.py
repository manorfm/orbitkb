"""Classify Spring Data calls using local repository and injection evidence."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.jvm_scanner import find_classes, split_top_level
from orbitkb.analysis.jvm_spring_syntax import kotlin_supertypes
from orbitkb.analysis.models import AnalysisResult, FlowEdge, Injection

SPRING_DATA_REPOSITORY_BASE_TYPES = frozenset({
    "CrudRepository", "JpaRepository", "ListCrudRepository", "ListPagingAndSortingRepository",
    "MongoRepository", "PagingAndSortingRepository", "ReactiveCrudRepository", "ReactiveMongoRepository",
})


class SpringDataClassifier:
    """Classify repository calls before bounded flow resolution."""

    def classify(self, result: AnalysisResult, files: list[Path]) -> None:
        result.edges = _classify_spring_data_derived_operations(
            result.edges, result.injections, _spring_data_repository_types(files),
        )
        result.edges = _classify_spring_data_query_operations(
            result.edges, result.injections, _spring_data_query_methods(files),
        )


def _spring_data_repository_types(files: list[Path]) -> frozenset[str]:
    """Find direct Spring Data interfaces and uniquely backed parent contracts."""
    parents_by_interface: dict[str, set[str]] = {}
    children_by_parent: dict[str, set[str]] = {}
    duplicate_interfaces: set[str] = set()

    def parent_names(declaration: str) -> set[str]:
        return {
            match.group(1).rsplit(".", 1)[-1]
            for parent in split_top_level(declaration)
            if (match := re.match(r"\s*([\w.]+)", parent))
        }

    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for match in re.finditer(
            r"\binterface\s+(?P<name>\w+)(?:[ \t]*(?:extends|:)[ \t]*(?P<parents>[^\{\n]+))?",
            source,
        ):
            name = match.group("name")
            if name in parents_by_interface:
                duplicate_interfaces.add(name)
            parents = parent_names(match.group("parents") or "")
            parents_by_interface[name] = parents
            for parent in parents:
                children_by_parent.setdefault(parent, set()).add(name)
        for declaration in find_classes(source):
            java_parents = re.search(r"\bimplements\s+([^\{]+)", declaration.header)
            parents = (parent_names(java_parents.group(1)) if java_parents
                       else set(kotlin_supertypes(declaration.header)))
            for parent in parents:
                children_by_parent.setdefault(parent.rsplit(".", 1)[-1], set()).add(declaration.name)
    types = {
        name for name, parents in parents_by_interface.items()
        if name not in duplicate_interfaces and parents & SPRING_DATA_REPOSITORY_BASE_TYPES
    }
    changed = True
    while changed:
        inferred = {
            parent for parent, children in children_by_parent.items()
            if parent in parents_by_interface and parent not in duplicate_interfaces
            and len(children) == 1 and next(iter(children)) in types
        }
        changed = bool(inferred - types)
        types.update(inferred)
    return frozenset(types)


def _classify_spring_data_derived_operations(
    edges: list[FlowEdge], injections: list[Injection], repository_types: frozenset[str],
) -> list[FlowEdge]:
    """Classify derived methods only from local interface and injection evidence."""
    injected_types = _injected_types(injections)
    classified = []
    for edge in edges:
        owner, separator, _member = edge.source.rpartition(".")
        receiver, target_separator, method = edge.target.rpartition(".")
        repository_type = injected_types.get(f"{owner}.{receiver}") if separator and target_separator else None
        kind = _spring_derived_operation_kind(method) if repository_type in repository_types else None
        classified.append(replace(edge, kind=kind, boundary_kind="persistence") if kind else edge)
    return classified


def _spring_derived_operation_kind(method: str) -> str | None:
    if method.startswith(("countBy", "existsBy", "findBy", "getBy", "queryBy", "readBy", "streamBy")):
        return "reads"
    if method.startswith(("deleteBy", "removeBy")):
        return "writes"
    return None


def _spring_data_query_methods(files: list[Path]) -> dict[tuple[str, str], str]:
    """Map local Spring Data `@Query` declarations to their proven operation kind."""
    methods = {}
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        for repository, parents, body in re.findall(
            r"\binterface\s+(\w+)\s*(?:extends|:)\s*([^\{]+)\{(.*?)\}", source, re.DOTALL,
        ):
            if not any(re.search(rf"\b{base}\b", parents) for base in SPRING_DATA_REPOSITORY_BASE_TYPES):
                continue
            for match in re.finditer(
                r"@Query\s*\((?:[^()]|\([^()]*\))*\)\s*"
                r"(?P<annotations>(?:@\w+(?:\s*\([^)]*\))?\s*)*)"
                r"(?:public\s+)?(?:[\w.<>,?\[\]]+\s+)?(?P<method>\w+)\s*\(",
                body,
                re.DOTALL,
            ):
                methods[(repository, match.group("method"))] = (
                    "writes" if "@Modifying" in match.group("annotations") else "reads"
                )
    return methods


def _classify_spring_data_query_operations(
    edges: list[FlowEdge], injections: list[Injection], query_methods: dict[tuple[str, str], str],
) -> list[FlowEdge]:
    """Apply only exact local `@Query` method declarations to observed calls."""
    injected_types = _injected_types(injections)
    classified = []
    for edge in edges:
        owner, separator, _member = edge.source.rpartition(".")
        receiver, target_separator, method = edge.target.rpartition(".")
        repository_type = injected_types.get(f"{owner}.{receiver}") if separator and target_separator else None
        kind = query_methods.get((repository_type, method))
        classified.append(replace(edge, kind=kind, boundary_kind="persistence") if kind else edge)
    return classified


def _injected_types(injections: list[Injection]) -> dict[str, str]:
    return {
        injection.consumer: injection.contract.split("<", 1)[0].rsplit(".", 1)[-1]
        for injection in injections
    }

