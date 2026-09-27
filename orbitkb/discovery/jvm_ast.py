"""Tree-sitter based navigation for Kotlin/Java, used only where a name-only regex
genuinely can't do the job: Kotlin's postfix extension-function syntax
(`fun Receiver.name(...)`) has no reliable text-regex anchor, and a function's exact
end can't be guessed by a fixed line count the way `scan_helpers.resolve_local_calls`
does for the other stacks. Still a one-hop heuristic, not a call graph: it follows an
explicitly imported name into the one file whose package matches, nothing more.
"""
from __future__ import annotations

import logging
from pathlib import Path

import tree_sitter_java
import tree_sitter_kotlin
from tree_sitter import Language, Node, Parser, Tree

from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.discovery.base import CodeExcerpt
from orbitkb.discovery.scan_helpers import CALL_RE, iter_files

logger = logging.getLogger(__name__)

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


def _parse(path: Path) -> tuple[Tree, Node, bytes] | None:
    """Returns the `Tree` alongside its root `Node`: `Node`s are views into memory
    owned by their `Tree`, so callers must keep the `Tree` referenced for as long as
    they walk nodes derived from it, or tree-sitter frees the backing memory out from
    under them (SIGSEGV/SIGBUS, often only surfacing later at GC time).
    """
    source = _read_bytes(path)
    if source is None:
        return None
    parser = _KOTLIN_PARSER if path.suffix == ".kt" else _JAVA_PARSER
    logger.debug("tree-sitter parsing: %s", path)
    tree = parser.parse(source)
    return tree, tree.root_node, source


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


class ParseCache:
    """Reused across every endpoint in one `collect_hints()` pass. Without it,
    `resolve_kotlin_java_calls()` re-parses the *same* candidate files, and re-walks
    the *same* folder via `iter_files()`, once per endpoint that can't resolve a call
    locally -- for N endpoints all calling one shared helper, that's N full
    tree-sitter parses of that one file, and N fresh `rglob()`s of the whole service.
    Real services can have dozens of endpoints, so this isn't just wasted CPU: it's
    thousands of avoidable native parse/free cycles on the exact code path (tree-sitter
    `Tree`/`Node` objects, walked by `iter_files`'s directory recursion in between) a
    real SIGBUS crash was found inside. Caching turns that into at most one parse and
    one directory walk per file, for the lifetime of one `collect_hints()` call.
    """

    def __init__(self) -> None:
        self._parsed: dict[Path, tuple[Tree, Node, bytes] | None] = {}
        self._files: dict[Path, list[Path]] = {}

    def parse(self, path: Path) -> tuple[Tree, Node, bytes] | None:
        if path not in self._parsed:
            self._parsed[path] = _parse(path)
        return self._parsed[path]

    def files(self, folder: Path) -> list[Path]:
        if folder not in self._files:
            self._files[folder] = list(iter_files(folder, EXTENSIONS))
        return self._files[folder]


def _find_in_package(
    folder: Path, package: str, name: str, exclude: Path, cache: ParseCache,
) -> tuple[Tree, Node, Path, bytes] | None:
    for candidate in cache.files(folder):
        if candidate == exclude:
            continue
        parsed = cache.parse(candidate)
        if parsed is None:
            continue
        candidate_tree, candidate_root, candidate_source = parsed
        if _package_of(candidate_root, candidate_source) != package:
            continue
        node = _find_function(candidate_root, candidate_source, name)
        if node is not None:
            return candidate_tree, node, candidate, candidate_source
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
    exact AST boundaries instead of a fixed line window); when a called name isn't
    defined in this file but is explicitly imported (Kotlin's `import a.b.out` for a
    top-level/extension function, or Java's `import a.b.Util` behind a `Util.method()`
    call), follows it into the one file under `folder` whose package matches.

    `cache` should be shared across every call made for the same `collect_hints()`
    pass (see `ParseCache`); a fresh one is created when called standalone.
    """
    if cache is None:
        cache = ParseCache()
    logger.debug("resolving JVM/Kotlin calls for endpoint: %s (lines %s-%s)", path, *exclude_line_range)
    parsed = cache.parse(path)
    if parsed is None:
        return []
    # `_tree` is unread but must stay bound: `root` is a view into its memory (see `_parse`).
    _tree, root, source = parsed
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
        found = _find_in_package(folder, fqn.rsplit(".", 1)[0], name, exclude=path, cache=cache)
        if found is None:
            continue
        _found_tree, found_node, found_path, found_source = found  # kept bound, see `_tree` above
        excerpts.append(_excerpt_from_node(found_node, found_path, folder, found_source))

    return excerpts
