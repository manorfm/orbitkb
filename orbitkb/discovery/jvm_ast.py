"""Regex-based navigation for Kotlin/Java cross-file call resolution, replacing
tree-sitter-kotlin/-java: that native parser had a proven, reproducible
memory-corruption bug (SIGSEGV/SIGBUS, and even indefinite hangs) on real
Kotlin/Spring services -- subprocess isolation could only contain the damage, not
eliminate it. This was the second of three tree-sitter usage points removed from
the JVM/Kotlin analysis path (see `orbitkb/analysis/jvm_scanner.py`/
`jvm_spring_analyzer.py` for the main per-file analyzer, and
`orbitkb/analysis/jvm_grpc_analyzer.py` for the third, gRPC detection); reusing
`jvm_scanner.find_functions` here removes it. Still a one-hop heuristic, not a
call graph: it follows an explicitly imported name into the one file whose
package matches, nothing more.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import FunctionMatch, find_functions
from orbitkb.discovery.base import CodeExcerpt
from orbitkb.discovery.scan_helpers import CALL_RE, iter_files

logger = logging.getLogger(__name__)

EXTENSIONS = (".kt", ".java")

_PACKAGE_RE = re.compile(r"(?m)^\s*package\s+(?P<name>[\w.]+)")

# A scanned file: its decoded text (imports/package are parsed from this) and every
# function/method found directly in it, in source order.
_ScannedFile = tuple[str, list[FunctionMatch]]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def _package_of(text: str) -> str | None:
    match = _PACKAGE_RE.search(text)
    return match.group("name") if match else None


def _find_function(functions: list[FunctionMatch], name: str) -> FunctionMatch | None:
    return next((function for function in functions if function.name == name), None)


def _excerpt_from_function(function_match: FunctionMatch, path: Path, folder: Path) -> CodeExcerpt:
    return CodeExcerpt(
        file_path=path.relative_to(folder).as_posix(),
        start_line=function_match.start_line,
        end_line=function_match.end_line,
        text=function_match.text,
    )


def _scan(path: Path) -> _ScannedFile | None:
    text = _read_text(path)
    if text is None:
        return None
    logger.debug("scanning JVM/Kotlin source: %s", path)
    functions = find_functions(text, 0, len(text), kotlin=path.suffix == ".kt")
    return text, functions


class ParseCache:
    """Reused across every endpoint in one `collect_hints()` pass. Without it,
    `resolve_kotlin_java_calls()` re-scans the *same* candidate files, and re-walks
    the *same* folder via `iter_files()`, once per endpoint that can't resolve a call
    locally -- for N endpoints all calling one shared helper, that's N re-scans of
    that one file, and N fresh `rglob()`s of the whole service. Real services can
    have dozens of endpoints, so caching still matters for throughput even now that
    there's no native parser call to also isolate exposure to.
    """

    def __init__(self) -> None:
        self._scanned: dict[Path, _ScannedFile | None] = {}
        self._files: dict[Path, list[Path]] = {}

    def scan(self, path: Path) -> _ScannedFile | None:
        if path not in self._scanned:
            self._scanned[path] = _scan(path)
        return self._scanned[path]

    def files(self, folder: Path) -> list[Path]:
        if folder not in self._files:
            self._files[folder] = list(iter_files(folder, EXTENSIONS))
        return self._files[folder]


def _find_in_package(
    folder: Path, package: str, name: str, exclude: Path, cache: ParseCache,
) -> tuple[FunctionMatch, Path] | None:
    for candidate in cache.files(folder):
        if candidate == exclude:
            continue
        scanned = cache.scan(candidate)
        if scanned is None:
            continue
        candidate_text, candidate_functions = scanned
        if _package_of(candidate_text) != package:
            continue
        function_match = _find_function(candidate_functions, name)
        if function_match is not None:
            return function_match, candidate
    return None


def resolve_kotlin_java_calls(
    path: Path,
    folder: Path,
    excerpt_text: str,
    exclude_line_range: tuple[int, int],
    max_hops: int = 3,
    cache: ParseCache | None = None,
) -> list[CodeExcerpt]:
    """Same-file first (mirrors `resolve_local_calls`'s one-hop posture, just with
    exact function boundaries instead of a fixed line window); when a called name
    isn't defined in this file but is explicitly imported (Kotlin's `import a.b.out`
    for a top-level/extension function, or Java's `import a.b.Util` behind a
    `Util.method()` call), follows it into the one file under `folder` whose package
    matches.

    `cache` should be shared across every call made for the same `collect_hints()`
    pass (see `ParseCache`); a fresh one is created when called standalone.
    """
    if cache is None:
        cache = ParseCache()
    logger.debug("resolving JVM/Kotlin calls for endpoint: %s (lines %s-%s)", path, *exclude_line_range)
    scanned = cache.scan(path)
    if scanned is None:
        return []
    text, functions = scanned
    excerpts: list[CodeExcerpt] = []
    seen: set[str] = set()
    range_start, range_end = exclude_line_range
    imports: dict[str, str] | None = None

    for match in CALL_RE.finditer(excerpt_text):
        if len(excerpts) >= max_hops:
            break
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)

        function_match = _find_function(functions, name)
        if function_match is not None:
            if range_start <= function_match.start_line <= range_end:
                continue
            excerpts.append(_excerpt_from_function(function_match, path, folder))
            continue

        if imports is None:
            imports = parse_jvm_imports(text)
        fqn = imports.get(name)
        if fqn is None or "." not in fqn:
            continue
        found = _find_in_package(folder, fqn.rsplit(".", 1)[0], name, exclude=path, cache=cache)
        if found is None:
            continue
        found_function, found_path = found
        excerpts.append(_excerpt_from_function(found_function, found_path, folder))

    return excerpts
