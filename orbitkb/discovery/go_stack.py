from __future__ import annotations

import re
from pathlib import Path

from orbitkb.discovery.base import (
    EndpointHint,
    MessagingHint,
    OutboundCallHint,
    PersistenceHint,
    ServiceHints,
)
from orbitkb.discovery.go_imports import has_standard_net_http_import
from orbitkb.discovery.scan_helpers import (
    ENDPOINT_AFTER,
    ENDPOINT_BEFORE,
    component_hint_for,
    engine_hint_from_manifest,
    excerpt_around,
    find_matches,
    first_existing_file,
    provider_from_match,
    read_text,
    resolve_local_calls,
)

_MANIFEST_FILES = ("go.mod",)
_ENGINE_DRIVER_KEYWORDS = {
    "lib/pq": "postgres",
    "jackc/pgx": "postgres",
    "go-sql-driver/mysql": "mysql",
    "mongo-driver": "mongodb",
    "go-redis": "redis",
    "redigo": "redis",
}

EXTENSIONS = (".go",)

_ROUTER_RE = re.compile(
    r"\b(?:router|r|mux|e|app)\.(GET|POST|PUT|PATCH|DELETE)\s*\(\s*\"([^\"]+)\"",
)
_NET_HTTP_HANDLE_RE = re.compile(
    r'\bhttp\.HandleFunc\s*\(\s*"(/[^"]*)"\s*,\s*([A-Za-z_]\w*)\s*,?\s*\)',
)
_TOP_LEVEL_FUNCTION_RE = re.compile(r"^[ \t]*func[ \t]+([A-Za-z_]\w*)[ \t]*\(", re.MULTILINE)
_GRPC_SERVER_METHOD_RE = re.compile(r"func\s+\(\w+\s+\*?\w*Server\)\s+(\w+)\s*\(")

_OUTBOUND_HTTP_RE = re.compile(r"\bhttp\.(Get|Post|NewRequest)\s*\(")
_GRPC_CLIENT_RE = re.compile(r"New\w*Client\s*\(\s*conn\s*\)")

_QUEUE_PUBLISH_RE = re.compile(
    r"\b(?:(?P<kafka>producer\.Produce|writer\.WriteMessages)|(?P<rabbitmq>ch\.Publish)|"
    r"(?P<sqs>\w*[Ss]qs\w*\.SendMessage))\s*\("
)
_QUEUE_CONSUME_RE = re.compile(
    r"\b(?:(?P<kafka>consumer\.Consume|reader\.ReadMessage)|(?P<rabbitmq>ch\.Consume)|"
    r"(?P<sqs>\w*[Ss]qs\w*\.ReceiveMessage))\s*\("
)

_GORM_MODEL_RE = re.compile(r"type\s+(\w+)\s+struct\s*\{[^}]*gorm\.Model", re.DOTALL)
_SQL_QUERY_RE = re.compile(r"\bdb\.(Query|Exec|QueryRow)\s*\(\s*\"([^\"]*)")

# Go has no classes; the closest equivalent grouping is the receiver type of a method
# (`func (s *Server) Handler(...)`) — the same shape _GRPC_SERVER_METHOD_RE already
# matches on. A route registered via router.GET(...) has no enclosing receiver, so it
# falls back to the file's stem like the other stacks' function-based routing does.
_RECEIVER_RE = re.compile(r"^\s*func\s*\(\w+\s+\*?(\w+)\)")


def _def_pattern(name: str) -> re.Pattern[str]:
    escaped = re.escape(name)
    return re.compile(rf"^\s*func\s+(?:\(\w+\s+\*?\w+\)\s+)?{escaped}\s*\(", re.MULTILINE)


def _endpoint_hint(method: str, path_value: str, file_path: Path, folder: Path, line_no: int) -> EndpointHint:
    excerpt = excerpt_around(file_path, folder, line_no, before=ENDPOINT_BEFORE, after=ENDPOINT_AFTER)
    component_hint = component_hint_for(file_path, line_no, _RECEIVER_RE)
    extra_excerpts = resolve_local_calls(
        file_path, folder, excerpt.text, _def_pattern, (excerpt.start_line, excerpt.end_line),
    )
    return EndpointHint(
        method=method, path=path_value, component_hint=component_hint,
        excerpt=excerpt, extra_excerpts=extra_excerpts,
    )


class GoDetector:
    id = "go"

    def matches(self, folder: Path) -> bool:
        if not (folder / "go.mod").is_file():
            return False
        return first_existing_file(folder, ("main.go",)) is not None or any(folder.glob("cmd/*/main.go"))

    def collect_hints(self, folder: Path) -> ServiceHints:
        hints = ServiceHints()
        engine_hint = engine_hint_from_manifest(folder, _MANIFEST_FILES, _ENGINE_DRIVER_KEYWORDS)

        entry = first_existing_file(folder, ("main.go",))
        if entry is None:
            candidates = list(folder.glob("cmd/*/main.go"))
            entry = candidates[0] if candidates else None
        if entry:
            hints.entry_excerpt = excerpt_around(entry, folder, 1, context=20)

        for path, line_no, match in find_matches(folder, EXTENSIONS, _ROUTER_RE):
            hints.endpoints.append(_endpoint_hint(match.group(1).upper(), match.group(2), path, folder, line_no))
        handlers_by_path: dict[Path, set[str]] = {}
        for path, line_no, match in find_matches(folder, EXTENSIONS, _NET_HTTP_HANDLE_RE):
            if path not in handlers_by_path:
                source = read_text(path) or ""
                handlers_by_path[path] = (
                    set(_TOP_LEVEL_FUNCTION_RE.findall(source))
                    if has_standard_net_http_import(source) else set()
                )
            if match.group(2) in handlers_by_path[path]:
                hints.endpoints.append(_endpoint_hint("ANY", match.group(1), path, folder, line_no))
        for path, line_no, match in find_matches(folder, EXTENSIONS, _GRPC_SERVER_METHOD_RE):
            hints.endpoints.append(_endpoint_hint("RPC", match.group(1), path, folder, line_no))

        for path, line_no, match in find_matches(folder, EXTENSIONS, _OUTBOUND_HTTP_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="http", target_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _GRPC_CLIENT_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="grpc", target_hint=match.group(0), excerpt=excerpt_around(path, folder, line_no))
            )

        for path, line_no, match in find_matches(folder, EXTENSIONS, _QUEUE_PUBLISH_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint="?", excerpt=excerpt_around(path, folder, line_no),
                    provider_hint=provider_from_match(match),
                )
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _QUEUE_CONSUME_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="consumes", channel_hint="?", excerpt=excerpt_around(path, folder, line_no),
                    provider_hint=provider_from_match(match),
                )
            )

        for path, line_no, match in find_matches(folder, EXTENSIONS, _GORM_MODEL_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _SQL_QUERY_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint="?", excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )

        return hints
