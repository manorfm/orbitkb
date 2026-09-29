"""Resolve a narrow, source-backed Kotlin expression return through an imported mapper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from orbitkb.analysis.jvm_scanner import (
    find_classes,
    find_functions,
    find_matching_brace,
    find_matching_paren,
)
from orbitkb.analysis.kotlin_dto_shapes import kotlin_data_class_shapes
from orbitkb.analysis.models import AnalysisResult

_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*$")
_IMPORT = re.compile(r"(?m)^\s*import\s+([\w.]+)\s*$")
_EXTENSION = re.compile(r"^fun\s+([\w.<>?]+)\.([A-Za-z_]\w*)\s*\(")
_BASE_CALL = re.compile(r"^(?P<receiver>[A-Za-z_]\w*)\.(?P<method>[A-Za-z_]\w*)\s*\(")
_CHAIN_CALL = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(")
_CONSTRUCTOR = re.compile(r"^([A-Z][A-Za-z_0-9]*)\s*\(")
_INTERFACE = re.compile(r"\binterface\s+([A-Za-z_]\w*)\b")
_RETURN_TYPE = re.compile(r"^\s*:\s*([A-Za-z_]\w*)\s*$")


@dataclass(frozen=True)
class _ExtensionReturn:
    type_name: str
    receiver_type: str | None
    package: str
    file_path: str
    line: int


@dataclass(frozen=True)
class _InterfaceMethod:
    return_type: str | None
    file_path: str
    line: int


def _imported_type(source: str, name: str) -> str | None:
    matches = [item for item in _IMPORT.findall(source) if item.endswith(f".{name}")]
    return matches[0] if len(matches) == 1 else None


def _direct_constructor(body: str) -> str | None:
    match = _CONSTRUCTOR.match(body.strip())
    if match is None:
        return None
    expression = body.strip()
    opening = match.end() - 1
    closing = find_matching_paren(expression, opening)
    return match.group(1) if closing >= 0 and not expression[closing + 1:].strip() else None


def _terminal_extension(body: str) -> str | None:
    expression = body.strip()
    base = _BASE_CALL.match(expression)
    if base is None:
        return None
    closing = find_matching_paren(expression, base.end() - 1)
    if closing < 0:
        return None
    cursor = closing + 1
    terminal = None
    while cursor < len(expression):
        while cursor < len(expression) and expression[cursor].isspace():
            cursor += 1
        if cursor == len(expression):
            break
        call = _CHAIN_CALL.match(expression, cursor)
        if call is None:
            return None
        closing = find_matching_paren(expression, call.end() - 1)
        if closing < 0:
            return None
        terminal = call.group(1)
        cursor = closing + 1
    return terminal


def _direct_receiver_call(body: str) -> tuple[str, str] | None:
    expression = body.strip()
    base = _BASE_CALL.match(expression)
    if base is None:
        return None
    base_end = find_matching_paren(expression, base.end() - 1)
    if base_end < 0:
        return None
    tail = expression[base_end + 1:].strip()
    mapper = _CHAIN_CALL.match(tail)
    if mapper is None:
        return None
    mapper_end = find_matching_paren(tail, mapper.end() - 1)
    if mapper_end < 0 or tail[mapper_end + 1:].strip():
        return None
    return base.group("receiver"), base.group("method")


def _interface_methods(sources: dict[str, str]) -> dict[str, list[dict[str, list[_InterfaceMethod]]]]:
    interfaces: dict[str, list[dict[str, list[_InterfaceMethod]]]] = {}
    for file_path, source in sources.items():
        package_match = _PACKAGE.search(source)
        if package_match is None:
            continue
        for declaration in _INTERFACE.finditer(source):
            opening = source.find("{", declaration.end())
            if opening < 0:
                continue
            closing = find_matching_brace(source, opening)
            if closing < 0:
                continue
            methods: dict[str, list[_InterfaceMethod]] = {}
            for function in find_functions(source, opening + 1, closing, kotlin=True):
                signature = function.text[:function.body_offset]
                params_open = signature.find("(")
                params_close = find_matching_paren(signature, params_open) if params_open >= 0 else -1
                if params_close < 0:
                    continue
                type_match = _RETURN_TYPE.match(signature[params_close + 1:])
                return_type = _imported_type(source, type_match.group(1)) if type_match else None
                methods.setdefault(function.name, []).append(
                    _InterfaceMethod(return_type, file_path, function.start_line),
                )
            interfaces.setdefault(f"{package_match.group(1)}.{declaration.group(1)}", []).append(methods)
    return interfaces


def _receiver_evidence(
    result: AnalysisResult, class_name: str, body: str, controller_source: str,
    extension: _ExtensionReturn, interfaces: dict[str, list[dict[str, list[_InterfaceMethod]]]],
) -> dict | None:
    receiver_call = _direct_receiver_call(body)
    if receiver_call is None or extension.receiver_type is None:
        return None
    receiver, method = receiver_call
    injections = [injection for injection in result.injections
                  if injection.consumer == f"{class_name}.{receiver}"]
    if len(injections) != 1:
        return None
    interface_name = _imported_type(controller_source, injections[0].contract)
    definitions = interfaces.get(interface_name, []) if interface_name else []
    if len(definitions) != 1:
        return None
    methods = definitions[0].get(method, [])
    if len(methods) != 1 or methods[0].return_type != extension.receiver_type:
        return None
    return {"file": methods[0].file_path, "start_line": methods[0].line}


def enrich_kotlin_expression_returns(result: AnalysisResult, files: list[Path], root: Path) -> None:
    """Infer only a unique imported extension whose body directly builds one DTO."""
    kotlin_sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="ignore")
        for path in files if path.suffix == ".kt"
    }
    dto_packages: dict[str, list[str]] = {}
    extensions: dict[str, list[_ExtensionReturn]] = {}
    interfaces = _interface_methods(kotlin_sources)
    for file_path, source in kotlin_sources.items():
        package_match = _PACKAGE.search(source)
        if package_match is None:
            continue
        package = package_match.group(1)
        for name in kotlin_data_class_shapes(source):
            dto_packages.setdefault(name, []).append(package)
        for function in find_functions(source, 0, len(source), kotlin=True):
            match = _EXTENSION.match(function.text)
            if match is None:
                continue
            type_name = _direct_constructor(function.text[function.body_offset:])
            if type_name is None:
                continue
            extensions.setdefault(f"{package}.{match.group(2)}", []).append(
                _ExtensionReturn(type_name, _imported_type(source, match.group(1)),
                                 package, file_path, function.start_line),
            )

    for entrypoint in result.entrypoints:
        if entrypoint.kind != "http":
            continue
        contract = result.contracts.get(entrypoint.symbol)
        source = kotlin_sources.get(entrypoint.evidence.file_path)
        if contract is None or contract.get("returns") is not None or source is None:
            continue
        class_name, _, method_name = entrypoint.symbol.partition(".")
        function = next((function for declaration in find_classes(source) if declaration.name == class_name
                         for function in find_functions(source, declaration.body_start, declaration.body_end, kotlin=True)
                         if function.name == method_name), None)
        if function is None or function.body_offset >= len(function.text):
            continue
        mapper_name = _terminal_extension(function.text[function.body_offset:])
        if mapper_name is None:
            continue
        imports = set(_IMPORT.findall(source))
        candidates = [extension for fq_name, definitions in extensions.items()
                      if fq_name in imports and fq_name.endswith(f".{mapper_name}")
                      for extension in definitions]
        if len(candidates) != 1:
            continue
        extension = candidates[0]
        if dto_packages.get(extension.type_name) != [extension.package]:
            continue
        receiver_evidence = _receiver_evidence(
            result, class_name, function.text[function.body_offset:], source, extension, interfaces,
        )
        contract["returns"] = {
            "type": extension.type_name,
            "required": True,
            "confidence": "confirmed" if receiver_evidence else "inferred",
            "derived_from": {"file": extension.file_path, "start_line": extension.line},
        }
        if receiver_evidence:
            contract["returns"]["receiver_evidence"] = receiver_evidence
