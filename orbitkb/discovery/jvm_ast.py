"""Tree-sitter based navigation for Kotlin/Java, used only where a name-only regex
genuinely can't do the job: Kotlin's postfix extension-function syntax
(`fun Receiver.name(...)`) has no reliable text-regex anchor, and a function's exact
end can't be guessed by a fixed line count the way `scan_helpers.resolve_local_calls`
does for the other stacks. Still a one-hop heuristic, not a call graph: it follows an
explicitly imported name into the one file whose package matches, nothing more.
"""
from __future__ import annotations

from pathlib import Path

import tree_sitter_java
import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.discovery.base import CodeExcerpt
from orbitkb.discovery.scan_helpers import CALL_RE, iter_files

EXTENSIONS = (".kt", ".java")

_KOTLIN_PARSER = Parser(Language(tree_sitter_kotlin.language()))
_JAVA_PARSER = Parser(Language(tree_sitter_java.language()))

_DECLARATION_TYPES = {"function_declaration", "method_declaration"}
_PACKAGE_TYPES = {"package_header", "package_declaration"}


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _parse(path: Path) -> tuple[Node, bytes] | None:
    source = _read_bytes(path)
    if source is None:
        return None
    parser = _KOTLIN_PARSER if path.suffix == ".kt" else _JAVA_PARSER
    return parser.parse(source).root_node, source


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _walk(node: Node):
    yield node
    for child in node.named_children:
        yield from _walk(child)


def _package_of(root: Node, source: bytes) -> str | None:
    for node in _walk(root):
        if node.type in _PACKAGE_TYPES:
            for child in node.named_children:
                return _text(child, source)
    return None


def _find_function(root: Node, source: bytes, name: str) -> Node | None:
    for node in _walk(root):
        if node.type not in _DECLARATION_TYPES:
            continue
        name_node = node.child_by_field_name("name")
        if name_node is not None and _text(name_node, source) == name:
            return node
    return None


def _excerpt_from_node(node: Node, path: Path, folder: Path, source: bytes) -> CodeExcerpt:
    return CodeExcerpt(
        file_path=path.relative_to(folder).as_posix(),
        start_line=node.start_point.row + 1,
        end_line=node.end_point.row + 1,
        text=_text(node, source),
    )


def _find_in_package(folder: Path, package: str, name: str, exclude: Path) -> tuple[Node, Path, bytes] | None:
    for candidate in iter_files(folder, EXTENSIONS):
        if candidate == exclude:
            continue
        parsed = _parse(candidate)
        if parsed is None:
            continue
        candidate_root, candidate_source = parsed
        if _package_of(candidate_root, candidate_source) != package:
            continue
        node = _find_function(candidate_root, candidate_source, name)
        if node is not None:
            return node, candidate, candidate_source
    return None


def resolve_kotlin_java_calls(
    path: Path,
    folder: Path,
    excerpt_text: str,
    exclude_line_range: tuple[int, int],
    max_hops: int = 3,
) -> list[CodeExcerpt]:
    """Same-file first (mirrors `resolve_local_calls`'s one-hop posture, just with
    exact AST boundaries instead of a fixed line window); when a called name isn't
    defined in this file but is explicitly imported (Kotlin's `import a.b.out` for a
    top-level/extension function, or Java's `import a.b.Util` behind a `Util.method()`
    call), follows it into the one file under `folder` whose package matches.
    """
    parsed = _parse(path)
    if parsed is None:
        return []
    root, source = parsed
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

        node = _find_function(root, source, name)
        if node is not None:
            line_no = node.start_point.row + 1
            if range_start <= line_no <= range_end:
                continue
            excerpts.append(_excerpt_from_node(node, path, folder, source))
            continue

        if imports is None:
            imports = parse_jvm_imports(source.decode("utf-8", errors="ignore"))
        fqn = imports.get(name)
        if fqn is None or "." not in fqn:
            continue
        found = _find_in_package(folder, fqn.rsplit(".", 1)[0], name, exclude=path)
        if found is None:
            continue
        found_node, found_path, found_source = found
        excerpts.append(_excerpt_from_node(found_node, found_path, folder, found_source))

    return excerpts
