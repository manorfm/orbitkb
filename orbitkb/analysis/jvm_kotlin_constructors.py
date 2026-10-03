"""Remove calls proven to construct a class declared in the Kotlin project."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import find_classes, find_functions, mask_non_code
from orbitkb.analysis.models import AnalysisResult

_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*$")


def remove_imported_constructor_calls(result: AnalysisResult, files: list[Path], root: Path) -> None:
    """An explicit import and unique local class prove an unqualified constructor call."""
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="ignore")
        for path in files if path.suffix == ".kt"
    }
    declared_classes: Counter[str] = Counter()
    function_names: set[str] = set()
    imports: dict[str, dict[str, str]] = {}
    for file_path, source in sources.items():
        visible = mask_non_code(source)
        package_match = _PACKAGE_RE.search(visible)
        if package_match is None:
            continue
        package = package_match.group(1)
        classes = find_classes(visible)
        declarations = re.finditer(r"\bclass\s+[A-Za-z_]\w*", visible)
        for declared, declaration in zip(classes, declarations, strict=True):
            if any(other is not declared and other.body_start <= declaration.start() <= other.body_end
                   for other in classes):
                continue
            declared_classes[f"{package}.{declared.name}"] += 1
        function_names.update(function.name for function in find_functions(visible, 0, len(visible), kotlin=True))
        imports[file_path] = parse_jvm_imports(visible)

    symbols = {(symbol.name, symbol.evidence.file_path): symbol for symbol in result.symbols}
    retained = []
    for edge in result.edges:
        source = sources.get(edge.evidence.file_path)
        imported = imports.get(edge.evidence.file_path, {}).get(edge.target)
        if (source is None or imported is None or edge.kind != "invokes" or edge.boundary_kind is not None
                or declared_classes[imported] != 1 or edge.target in function_names):
            retained.append(edge)
            continue
        symbol = symbols.get((edge.source, edge.evidence.file_path))
        if symbol is None or any(name == edge.target for name, _ in symbol.parameters):
            retained.append(edge)
            continue
        preceding = "\n".join(source.splitlines()[symbol.evidence.start_line - 1:edge.evidence.start_line])
        visible = mask_non_code(preceding)
        if (re.search(rf"\b(?:val|var)\s+{re.escape(edge.target)}\b", visible)
                or re.search(rf"\b{re.escape(edge.target)}\s*:", visible)):
            retained.append(edge)
            continue

    result.edges = retained
