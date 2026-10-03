"""Resolve a named Kotlin collection lambda parameter through a proven local type."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import find_calls, find_matching_brace, mask_non_code
from orbitkb.analysis.kotlin_dto_shapes import kotlin_data_class_shapes
from orbitkb.analysis.models import AnalysisResult, Symbol

_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*$")
_LAMBDA = re.compile(
    r"(?<![\w.])(?P<local>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)\."
    r"(?:map|mapNotNull)\s*\{\s*(?P<element>[A-Za-z_]\w*)\s*->"
)
_LIST = re.compile(r"(?:List|MutableList|Collection)<\s*(?P<element>[A-Za-z_]\w*)\s*>")


def _local_type(name: str, file_path: str, imports: dict[str, dict[str, str]], packages: dict[str, str]) -> str:
    return imports[file_path].get(name, f"{packages[file_path]}.{name}")


def resolve_kotlin_lambda_element_calls(result: AnalysisResult, files: list[Path], root: Path) -> None:
    """Link `element.method()` only when a local return and List property prove its type."""
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="ignore")
        for path in files if path.suffix == ".kt"
    }
    packages: dict[str, str] = {}
    imports: dict[str, dict[str, str]] = {}
    data_classes: dict[str, list[tuple[str, list[dict]]]] = {}
    for file_path, source in sources.items():
        visible = mask_non_code(source)
        package = _PACKAGE.search(visible)
        if package is None:
            continue
        packages[file_path] = package.group(1)
        imports[file_path] = parse_jvm_imports(visible)
        for name, fields in kotlin_data_class_shapes(source).items():
            data_classes.setdefault(f"{package.group(1)}.{name}", []).append((file_path, fields))

    declarations: dict[str, list[Symbol]] = {}
    for symbol in result.symbols:
        declarations.setdefault(symbol.name, []).append(symbol)
    updates: dict[tuple[str, str, int], str] = {}
    edge_counts = Counter((edge.source, edge.target, edge.evidence.start_line) for edge in result.edges)

    for symbol in result.symbols:
        file_path = symbol.evidence.file_path
        source = sources.get(file_path)
        if source is None or file_path not in packages or not symbol.local_assignments:
            continue
        lines = source.splitlines(keepends=True)
        start = sum(map(len, lines[:symbol.evidence.start_line - 1]))
        end = sum(map(len, lines[:symbol.evidence.end_line]))
        visible = mask_non_code(source)
        for match in _LAMBDA.finditer(visible, start, end):
            opening = visible.find("{", match.start(), match.end())
            closing = find_matching_brace(source, opening)
            if closing < 0 or closing >= end:
                continue
            lambda_line = source.count("\n", 0, match.start()) + 1
            assignments = [(callee, line) for name, callee, line in symbol.local_assignments
                           if name == match.group("local") and line < lambda_line]
            if len(assignments) != 1:
                continue
            callee, assignment_line = assignments[0]
            calls = [edge for edge in result.edges
                     if edge.source == symbol.name and edge.evidence.file_path == file_path
                     and edge.evidence.start_line == assignment_line
                     and edge.target.rpartition(".")[2] == callee.rpartition(".")[2]]
            if len(calls) != 1:
                continue
            if calls[0].kind != "invokes" or calls[0].boundary_kind is not None or calls[0].confidence != "high":
                continue
            candidates = declarations.get(calls[0].target, ())
            if len(candidates) != 1 or not candidates[0].return_type:
                continue
            returned = candidates[0]
            if returned.evidence.file_path not in packages:
                continue
            owner_type = _local_type(returned.return_type, returned.evidence.file_path, imports, packages)
            owner_classes = data_classes.get(owner_type, ())
            if len(owner_classes) != 1:
                continue
            owner_path, fields = owner_classes[0]
            property_types = [field["type"] for field in fields if field["name"] == match.group("field")]
            if len(property_types) != 1 or (element_type := _LIST.fullmatch(property_types[0])) is None:
                continue
            element_name = element_type.group("element")
            element_fqn = _local_type(element_name, owner_path, imports, packages)
            element_classes = data_classes.get(element_fqn, ())
            if len(element_classes) != 1:
                continue
            element_path, _ = element_classes[0]
            for callee_name, offset in find_calls(visible[match.end():closing]):
                receiver, separator, method = callee_name.rpartition(".")
                if not separator or receiver != match.group("element"):
                    continue
                if re.search(rf"\b(?:val|var)\s+{re.escape(receiver)}\b", visible[match.end():match.end() + offset]):
                    continue
                target = f"{element_name}.{method}"
                methods = declarations.get(target, ())
                if len(methods) != 1 or methods[0].evidence.file_path != element_path:
                    continue
                line = source.count("\n", 0, match.end() + offset) + 1
                key = (symbol.name, callee_name, line)
                if edge_counts[key] == 1:
                    updates[key] = target

    result.edges = [
        replace(edge, target=updates[key], confidence="medium") if (
            key := (edge.source, edge.target, edge.evidence.start_line)
        ) in updates and edge.kind == "invokes" else edge
        for edge in result.edges
    ]
