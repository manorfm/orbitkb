"""Resolve Kotlin collection lambda parameters through proven local types."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import (
    find_calls,
    find_functions,
    find_matching_brace,
    find_matching_paren,
    mask_non_code,
    split_top_level,
)
from orbitkb.analysis.kotlin_dto_shapes import kotlin_data_class_shapes
from orbitkb.analysis.models import AnalysisResult, Symbol

_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*$")
_LAMBDA = re.compile(
    r"(?<![\w.])(?P<local>[A-Za-z_]\w*)\.(?P<field>[A-Za-z_]\w*)\."
    r"(?:map|mapNotNull)\s*\{\s*(?P<element>[A-Za-z_]\w*)\s*->"
)
_IMPLICIT_LAMBDA = re.compile(
    r"(?<![\w.])(?P<field>[A-Za-z_]\w*)\.(?P<operation>firstOrNull|map)\s*\{"
)
_LIST = re.compile(r"(?:List|MutableList|Collection)<\s*(?P<element>[A-Za-z_]\w*)\s*>")


def _local_type(name: str, file_path: str, imports: dict[str, dict[str, str]], packages: dict[str, str]) -> str:
    return imports[file_path].get(name, f"{packages[file_path]}.{name}")


def _list_element(
    owner_type: str, field: str, data_classes: dict[str, list[tuple[str, list[dict]]]],
    imports: dict[str, dict[str, str]], packages: dict[str, str],
) -> tuple[str, str, frozenset[str]] | None:
    owners = data_classes.get(owner_type, ())
    if len(owners) != 1:
        return None
    owner_path, fields = owners[0]
    property_types = [item["type"] for item in fields if item["name"] == field]
    if len(property_types) != 1 or (element_type := _LIST.fullmatch(property_types[0])) is None:
        return None
    element_name = element_type.group("element")
    elements = data_classes.get(_local_type(element_name, owner_path, imports, packages), ())
    if len(elements) != 1:
        return None
    element_path, element_fields = elements[0]
    return element_name, element_path, frozenset(item["name"] for item in element_fields)


def _copy_uses_declared_properties(
    source: str, visible: str, call_start: int, callee: str, property_names: frozenset[str],
) -> bool:
    call = re.match(rf"{re.escape(callee)}\s*\(", visible[call_start:])
    if call is None:
        return False
    opening = call_start + call.end() - 1
    closing = find_matching_paren(source, opening)
    if closing < 0:
        return False
    arguments = split_top_level(source[opening + 1:closing])
    if not arguments:
        return False
    names = [re.match(r"\s*([A-Za-z_]\w*)\s*=", argument) for argument in arguments]
    return all(name is not None and name.group(1) in property_names for name in names)


def _record_element_calls(
    symbol: Symbol, source: str, visible: str, body_start: int, closing: int, receiver: str,
    element: tuple[str, str, frozenset[str]], declarations: dict[str, list[Symbol]],
    edge_counts: Counter[tuple[str, str, int]], updates: dict[tuple[str, str, int], str],
    removals: set[tuple[str, str, int]], extension_copy_receivers: set[str], *, generated_copy: bool = False,
) -> None:
    element_name, element_path, property_names = element
    for callee_name, offset in find_calls(visible[body_start:closing]):
        call_receiver, separator, method = callee_name.rpartition(".")
        if not separator or call_receiver != receiver:
            continue
        prefix = visible[body_start:body_start + offset]
        if prefix.count("{") != prefix.count("}"):
            continue
        if re.search(rf"\b(?:val|var)\s+{re.escape(receiver)}\b", prefix):
            continue
        target = f"{element_name}.{method}"
        methods = declarations.get(target, ())
        line = source.count("\n", 0, body_start + offset) + 1
        key = (symbol.name, callee_name, line)
        if (generated_copy and method == "copy" and not methods and element_name not in extension_copy_receivers
                and edge_counts[key] == 1
                and _copy_uses_declared_properties(
                    source, visible, body_start + offset, callee_name, property_names,
                )):
            removals.add(key)
            continue
        if len(methods) != 1 or methods[0].evidence.file_path != element_path:
            continue
        if edge_counts[key] == 1:
            updates[key] = target


def resolve_kotlin_lambda_element_calls(result: AnalysisResult, files: list[Path], root: Path) -> None:
    """Link element calls only when a local return or member List property proves their type."""
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="ignore")
        for path in files if path.suffix == ".kt"
    }
    packages: dict[str, str] = {}
    imports: dict[str, dict[str, str]] = {}
    visible_sources: dict[str, str] = {}
    function_bodies: dict[tuple[str, int, str], list[int]] = {}
    data_classes: dict[str, list[tuple[str, list[dict]]]] = {}
    for file_path, source in sources.items():
        visible = mask_non_code(source)
        visible_sources[file_path] = visible
        for function in find_functions(visible, 0, len(visible), kotlin=True):
            function_bodies.setdefault((file_path, function.start_line, function.name), []).append(
                function.start_offset + function.body_offset,
            )
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
    extension_copy_receivers = {
        symbol.name.rsplit(".", 2)[-2] for symbol in result.symbols
        if symbol.member == "copy" and symbol.name.count(".") >= 2
    }
    updates: dict[tuple[str, str, int], str] = {}
    removals: set[tuple[str, str, int]] = set()
    edge_counts = Counter((edge.source, edge.target, edge.evidence.start_line) for edge in result.edges)

    for symbol in result.symbols:
        file_path = symbol.evidence.file_path
        source = sources.get(file_path)
        if source is None or file_path not in packages:
            continue
        lines = source.splitlines(keepends=True)
        start = sum(map(len, lines[:symbol.evidence.start_line - 1]))
        end = sum(map(len, lines[:symbol.evidence.end_line]))
        visible = visible_sources[file_path]
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
            element = _list_element(owner_type, match.group("field"), data_classes, imports, packages)
            if element is None:
                continue
            _record_element_calls(
                symbol, source, visible, match.end(), closing, match.group("element"),
                element, declarations, edge_counts, updates, removals, extension_copy_receivers,
            )

        owner_type = f"{packages[file_path]}.{symbol.owner}"
        owners = data_classes.get(owner_type, ())
        if len(owners) != 1 or owners[0][0] != file_path:
            continue
        body_positions = function_bodies.get((file_path, symbol.evidence.start_line, symbol.member), ())
        if len(body_positions) != 1:
            continue
        function_body = body_positions[0]
        base_depth = 1 if visible[function_body:function_body + 1] == "{" else 0
        for match in _IMPLICIT_LAMBDA.finditer(visible, start, end):
            preceding_body = visible[function_body:match.start()]
            if preceding_body.count("{") - preceding_body.count("}") != base_depth:
                continue
            opening = match.end() - 1
            closing = find_matching_brace(source, opening)
            if closing < 0 or closing >= end or re.match(r"\s*[A-Za-z_]\w*\s*->", visible[opening + 1:closing]):
                continue
            field = match.group("field")
            preceding = visible[start:match.start()]
            if re.search(rf"\b(?:val|var)\s+{re.escape(field)}\b|\b{re.escape(field)}\s*:", preceding):
                continue
            element = _list_element(owner_type, field, data_classes, imports, packages)
            if element is not None:
                _record_element_calls(
                    symbol, source, visible, opening + 1, closing, "it",
                    element, declarations, edge_counts, updates, removals, extension_copy_receivers,
                    generated_copy=match.group("operation") == "map",
                )

    result.edges = [
        replace(edge, target=updates[key], confidence="medium") if (
            key := (edge.source, edge.target, edge.evidence.start_line)
        ) in updates and edge.kind == "invokes" else edge
        for edge in result.edges
        if not ((edge.source, edge.target, edge.evidence.start_line) in removals
                and edge.kind == "invokes" and edge.boundary_kind is None)
    ]
