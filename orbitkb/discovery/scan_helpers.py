"""Small shared helpers used by every stack detector so each one stays a short list
of regex patterns rather than reimplementing file walking / excerpt slicing."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Iterator

from orbitkb.discovery.base import CodeExcerpt

SKIP_DIRS = {
    "node_modules", ".venv", "venv", "env", "dist", "build", ".git", "target",
    "__pycache__", ".idea", ".gradle", "vendor", "bin", "obj", ".mypy_cache",
    ".pytest_cache", "coverage", ".next", ".turbo",
    # Test code: never a real HTTP entrypoint or production persistence call (a
    # mocked `repo.save()` is evidence of test isolation, not a real operation),
    # and a real source of static-analysis crashes this project's own fixtures
    # never exercised (backtick-named Kotlin test functions, long MockMvc
    # assertion chains). "test"/"tests" is the Maven/Gradle/Python/Node
    # convention; "__tests__" is Jest's.
    "test", "tests", "__tests__",
}

MAX_FILE_BYTES = 300_000  # skip generated/huge files

# Endpoint hints anchor on the route decorator/annotation line, whose interesting
# content (the handler body) lies almost entirely after it — so use a small
# look-back and a generous look-ahead rather than a symmetric window.
ENDPOINT_BEFORE = 3
ENDPOINT_AFTER = 60


def iter_files(folder: Path, extensions: tuple[str, ...]) -> Iterator[Path]:
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in extensions:
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(folder).parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


def excerpt_around(
    path: Path,
    folder: Path,
    line_no: int,
    context: int = 15,
    before: int | None = None,
    after: int | None = None,
) -> CodeExcerpt:
    """Slice a window of lines around line_no.

    context sets a symmetric window by default. Pass before/after explicitly for an
    asymmetric window — e.g. a route decorator/annotation line has its interesting
    content (the whole handler body) almost entirely AFTER it, so endpoint hints use a
    small `before` and a large `after` rather than wasting half the budget on the
    (usually irrelevant) lines above the decorator.
    """
    text = read_text(path) or ""
    lines = text.splitlines()
    b = context if before is None else before
    a = context if after is None else after
    start = max(1, line_no - b)
    end = min(len(lines), line_no + a)
    snippet = "\n".join(lines[start - 1 : end])
    rel = path.relative_to(folder).as_posix()
    return CodeExcerpt(file_path=rel, start_line=start, end_line=end, text=snippet)


def find_matches(folder: Path, extensions: tuple[str, ...], pattern: re.Pattern[str]) -> list[tuple[Path, int, re.Match[str]]]:
    hits: list[tuple[Path, int, re.Match[str]]] = []
    for path in iter_files(folder, extensions):
        text = read_text(path)
        if not text:
            continue
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            hits.append((path, line_no, match))
    return hits


def first_existing_file(folder: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def component_hint_for(path: Path, line_no: int, class_pattern: re.Pattern[str] | None) -> str:
    """The name an endpoint's class/controller/module layer (`components`) should be
    grouped under: the nearest enclosing class definition above `line_no` in `path` when
    one exists, otherwise the file's own stem — most Python/Node routing is function-based
    with no wrapping class, so the file itself is the natural cluster in that common case.
    """
    if class_pattern is not None:
        text = read_text(path)
        if text:
            name = None
            for line in text.splitlines()[:line_no]:
                match = class_pattern.match(line)
                if match:
                    name = match.group(1)
            if name:
                return name
    return path.stem


_CONFIG_FILE_NAMES = (
    "application.properties", "application.yml", "application.yaml",
    "docker-compose.yml", "docker-compose.yaml",
)
MAX_CONFIG_FILE_CHARS = 4_000
_SECRET_CONFIG_LINE = re.compile(
    r"(?im)^(?P<prefix>\s*[^#\n:=]*?(?:password|secret|token|api[_-]?key|credential|private[_-]?key)[^:=]*?\s*[:=]\s*)(?P<value>[^\n]*)$"
)


def _redact_config(text: str) -> str:
    """Keep topology-bearing settings while never passing credential values to an LLM."""
    return _SECRET_CONFIG_LINE.sub(r"\g<prefix>[REDACTED]", text)


def collect_config_excerpts(folder: Path) -> list[CodeExcerpt]:
    """Whole (small, capped) content of any known config file at the service root.

    Used to resolve the concrete broker/vendor behind a transport-agnostic abstraction
    (JMS, Celery, NestJS microservices) that the application code alone never names —
    the connection factory / broker URL lives here, not in a source file. Only the
    service's own root is checked (not the whole tree), matching the "propositalmente
    simples" scope every other heuristic in this module keeps.
    """
    excerpts: list[CodeExcerpt] = []
    for filename in _CONFIG_FILE_NAMES:
        path = folder / filename
        if not path.is_file():
            continue
        text = read_text(path)
        if not text:
            continue
        truncated = _redact_config(text)[:MAX_CONFIG_FILE_CHARS]
        line_count = truncated.count("\n") + 1
        excerpts.append(CodeExcerpt(file_path=filename, start_line=1, end_line=line_count, text=truncated))
    return excerpts


def engine_hint_from_manifest(folder: Path, manifest_names: tuple[str, ...], driver_keywords: dict[str, str]) -> str | None:
    """Best-effort database engine guess from the service's own dependency manifest
    (requirements.txt, package.json, pom.xml, go.mod, ...) — an ORM model/entity
    definition (SQLAlchemy, JPA, GORM) rarely names its engine, but the driver package a
    project depends on almost always does, and declaring a dependency is a much stronger,
    cheaper signal than scanning source text for it. `driver_keywords` maps a substring
    to look for -> the engine it implies (e.g. {"psycopg2": "postgres"}); the first match
    found, in the order given, wins.
    """
    for name in manifest_names:
        path = folder / name
        if not path.is_file():
            continue
        text = read_text(path)
        if not text:
            continue
        lowered = text.lower()
        for keyword, engine in driver_keywords.items():
            if keyword.lower() in lowered:
                return engine
    return None


def provider_from_match(match: re.Match[str], exclude: str = "channel") -> str | None:
    """Which named alternative fired in a regex built from `(?P<name>...)` branches —
    used instead of a side lookup table (matched text -> provider) so the provider tag
    can never drift out of sync with the pattern, and so this module's own source never
    spells out one of these patterns' matched text as a plain, unescaped string constant
    elsewhere in the same file (which would self-match here on this module's own source
    the next time it scans itself, the way an earlier version of this exact helper did).
    `exclude` skips a trailing group unrelated to classification (e.g. a channel name
    captured by the same pattern), since `Match.lastgroup` only reports the right-most
    group, not the one that drove which alternative matched.
    """
    for name, value in match.groupdict().items():
        if name != exclude and value is not None:
            return name
    return None


_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def resolve_local_calls(
    path: Path,
    folder: Path,
    excerpt_text: str,
    definition_for: Callable[[str], re.Pattern[str]],
    exclude_line_range: tuple[int, int],
    exclude_names: frozenset[str] = frozenset(),
    max_hops: int = 3,
) -> list[CodeExcerpt]:
    """One-hop local navigation: for each name called inside `excerpt_text`, look for a
    matching definition (built per-name by `definition_for`) in the same file, and return
    its excerpt as extra evidence. Never crosses into another file and never chases more
    than `max_hops` distinct names — a cheap heuristic, not a call-graph traversal (the
    project's README lists a full AST/LSP code graph as a deliberate non-goal).

    `exclude_line_range` is the endpoint's own excerpt (start_line, end_line): a name whose
    definition falls inside it is the handler itself (its own `def`/signature line reads as
    a "call" to `_CALL_RE`), not a real one-hop dependency, so it is skipped.
    """
    text = read_text(path)
    if not text:
        return []
    excerpts: list[CodeExcerpt] = []
    seen: set[str] = set(exclude_names)
    range_start, range_end = exclude_line_range
    for match in _CALL_RE.finditer(excerpt_text):
        if len(excerpts) >= max_hops:
            break
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        definition_match = definition_for(name).search(text)
        if not definition_match:
            continue
        line_no = text.count("\n", 0, definition_match.start()) + 1
        if range_start <= line_no <= range_end:
            continue
        excerpts.append(excerpt_around(path, folder, line_no, before=1, after=30))
    return excerpts
