"""Regex-based Java/Kotlin gRPC handler and client-binding detection, replacing
tree-sitter-kotlin/-java: the last of three tree-sitter usage points in the
JVM/Kotlin analysis path (see `jvm_scanner.py` and `orbitkb/discovery/jvm_ast.py`'s
module docstrings for the other two, and the crash history behind removing it
everywhere -- SIGSEGV/SIGBUS/hangs reproduced on a real Kotlin/Spring service).
"""
from __future__ import annotations

import re
from pathlib import Path

from orbitkb.analysis.jvm_scanner import find_classes, find_functions, mask_ranges, split_top_level
from orbitkb.analysis.models import Evidence, GrpcClientBinding, GrpcHandler

_GRPC_SERVICE_IMPORT = re.compile(
    r"^\s*import\s+net\.devh\.boot\.grpc\.server\.service\.GrpcService(?=\s|;|$)\s*;?", re.MULTILINE,
)
_JAVA_IMPL_BASE = re.compile(
    r"\bextends\s+(?:[\w.]+\.)?(?P<service>[A-Za-z_]\w*)Grpc\.\w*ImplBase\b",
)
_KOTLIN_COROUTINE_IMPL_BASE = re.compile(
    r":\s+(?:[\w.]+\.)?(?P<service>[A-Za-z_]\w*)GrpcKt\.\w*CoroutineImplBase\s*\(",
)
_KOTLIN_JAVA_IMPL_BASE = re.compile(
    r":\s+(?:[\w.]+\.)?(?P<service>[A-Za-z_]\w*)Grpc\.\w*ImplBase\s*\(",
)
_JAVA_STUB_FIELD = re.compile(
    r"\b(?P<service>[A-Za-z_]\w*)Grpc\.[A-Za-z_]\w*Stub\s+(?P<member>[A-Za-z_]\w*)\b",
)
_KOTLIN_STUB_PROPERTY = re.compile(
    r"\b(?:val|var)\s+(?P<member>[A-Za-z_]\w*)\s*:\s*(?:[A-Za-z_]\w*\.)*"
    r"(?P<service>[A-Za-z_]\w*)Grpc(?:Kt)?\.[A-Za-z_]\w*Stub\b",
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _evidence(path: Path, root: Path, start_line: int, end_line: int) -> Evidence:
    return Evidence(path.relative_to(root).as_posix(), start_line, end_line)


def _function_ranges_relative_to_body(text: str, body_start: int, body_end: int, kotlin: bool) -> list[tuple[int, int]]:
    return [
        (f.start_offset - body_start, f.end_offset - body_start)
        for f in find_functions(text, body_start, body_end, kotlin)
    ]


def jvm_grpc_handlers(files: list[Path], root: Path) -> list[GrpcHandler]:
    """Direct Java `@GrpcService` implementations of generated bases."""
    handlers: list[GrpcHandler] = []
    for path in files:
        if path.suffix != ".java":
            continue
        text = _read_text(path)
        if _GRPC_SERVICE_IMPORT.search(text) is None:
            continue
        for class_match in find_classes(text):
            base = _JAVA_IMPL_BASE.search(class_match.header)
            if "@GrpcService" not in class_match.annotations or base is None:
                continue
            service = base.group("service")
            for function_match in find_functions(text, class_match.body_start, class_match.body_end, kotlin=False):
                if "@Override" not in function_match.modifiers:
                    continue
                handlers.append(GrpcHandler(
                    service, function_match.name, f"{class_match.name}.{function_match.name}",
                    _evidence(path, root, function_match.start_line, function_match.end_line),
                ))
    return handlers


def kotlin_grpc_handlers(files: list[Path], root: Path) -> list[GrpcHandler]:
    """Direct Kotlin `@GrpcService` implementations of generated bases."""
    handlers: list[GrpcHandler] = []
    for path in files:
        if path.suffix != ".kt":
            continue
        text = _read_text(path)
        if _GRPC_SERVICE_IMPORT.search(text) is None:
            continue
        for class_match in find_classes(text):
            base = _KOTLIN_COROUTINE_IMPL_BASE.search(class_match.header) or _KOTLIN_JAVA_IMPL_BASE.search(class_match.header)
            if "@GrpcService" not in class_match.annotations or base is None:
                continue
            service = base.group("service")
            for function_match in find_functions(text, class_match.body_start, class_match.body_end, kotlin=True):
                if "override" not in function_match.modifiers:
                    continue
                handlers.append(GrpcHandler(
                    service, function_match.name, f"{class_match.name}.{function_match.name}",
                    _evidence(path, root, function_match.start_line, function_match.end_line),
                ))
    return handlers


def jvm_grpc_client_bindings(files: list[Path], root: Path) -> list[GrpcClientBinding]:
    """Direct Java fields typed as generated gRPC stubs."""
    bindings: list[GrpcClientBinding] = []
    for path in files:
        if path.suffix != ".java":
            continue
        text = _read_text(path)
        for class_match in find_classes(text):
            class_body_text = text[class_match.body_start : class_match.body_end + 1]
            function_ranges = _function_ranges_relative_to_body(text, class_match.body_start, class_match.body_end, kotlin=False)
            masked = mask_ranges(class_body_text, function_ranges)
            for match in _JAVA_STUB_FIELD.finditer(masked):
                line = class_match.start_line + masked.count("\n", 0, match.start())
                bindings.append(GrpcClientBinding(
                    class_match.name, match.group("member"), match.group("service"), _evidence(path, root, line, line),
                ))
    return bindings


def _kotlin_direct_superclass(header: str) -> str | None:
    """One unqualified superclass invoked directly by a Kotlin class -- i.e. the
    class declares exactly one supertype and it's a class delegation (a constructor
    call, `: Base(...)`), not a bare interface name or multiple supertypes.
    """
    parts = split_top_level(header, ":")
    if len(parts) != 2:
        return None
    candidates = [candidate for candidate in split_top_level(parts[1], ",") if candidate.strip()]
    if len(candidates) != 1:
        return None
    match = re.fullmatch(r"\s*(?P<name>[A-Za-z_]\w*)\s*\([^()]*\)\s*", candidates[0])
    return match.group("name") if match else None


def kotlin_grpc_client_bindings(files: list[Path], root: Path) -> list[GrpcClientBinding]:
    """Direct Kotlin properties typed as generated Java or coroutine gRPC stubs."""
    bindings: list[GrpcClientBinding] = []
    inheritances: list[tuple[str, str, Evidence]] = []
    for path in files:
        if path.suffix != ".kt":
            continue
        text = _read_text(path)
        for class_match in find_classes(text):
            superclass = _kotlin_direct_superclass(class_match.header)
            if superclass is not None:
                inheritances.append((
                    class_match.name, superclass, _evidence(path, root, class_match.start_line, class_match.end_line),
                ))
            # Constructor-injected stubs (`class Foo(private val stub: XGrpc.XStub)`) live in
            # the header; body-declared ones (`private val stub: XGrpc.XStub = ...`) live in
            # the body -- masked here the same way field detection is, so a stub-typed local
            # `val` inside a method body is never mistaken for a class member.
            for match in _KOTLIN_STUB_PROPERTY.finditer(class_match.header):
                line = class_match.start_line + class_match.header.count("\n", 0, match.start())
                bindings.append(GrpcClientBinding(
                    class_match.name, match.group("member"), match.group("service"), _evidence(path, root, line, line),
                ))
            class_body_text = text[class_match.body_start : class_match.body_end + 1]
            function_ranges = _function_ranges_relative_to_body(text, class_match.body_start, class_match.body_end, kotlin=True)
            masked_body = mask_ranges(class_body_text, function_ranges)
            for match in _KOTLIN_STUB_PROPERTY.finditer(masked_body):
                line = class_match.start_line + masked_body.count("\n", 0, match.start())
                bindings.append(GrpcClientBinding(
                    class_match.name, match.group("member"), match.group("service"), _evidence(path, root, line, line),
                ))
    parent_members: dict[tuple[str, str], list[GrpcClientBinding]] = {}
    for binding in bindings:
        parent_members.setdefault((binding.owner, binding.member), []).append(binding)
    direct_members = set(parent_members)
    for child, parent, evidence in inheritances:
        for (owner, member), candidates in parent_members.items():
            if owner == parent and (child, member) not in direct_members and len(candidates) == 1:
                binding = candidates[0]
                bindings.append(GrpcClientBinding(child, member, binding.service, evidence))
    return bindings
