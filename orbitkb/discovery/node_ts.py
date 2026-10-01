from __future__ import annotations

import json
import re
from pathlib import Path

from orbitkb.discovery.base import (
    EndpointHint,
    MessagingHint,
    OutboundCallHint,
    PersistenceHint,
    ServiceHints,
)
from orbitkb.discovery.node_http import (
    express_receivers,
    express_route_prefixes,
    fastify_receivers,
)
from orbitkb.discovery.node_http_routes import find_literal_node_routes
from orbitkb.discovery.node_mounts import cross_file_express_mounts
from orbitkb.discovery.node_nest import find_nest_route_hints
from orbitkb.discovery.scan_helpers import (
    ENDPOINT_AFTER,
    ENDPOINT_BEFORE,
    component_hint_for,
    engine_hint_from_manifest,
    excerpt_around,
    find_matches,
    first_existing_file,
    iter_files,
    provider_from_match,
    resolve_local_calls,
)

_MANIFEST_FILES = ("package.json",)
_ENGINE_DRIVER_KEYWORDS = {
    "\"pg\"": "postgres",
    "mysql2": "mysql",
    "\"mysql\"": "mysql",
    "mongoose": "mongodb",
    "mongodb": "mongodb",
    "ioredis": "redis",
    "\"redis\"": "redis",
}
_PRISMA_PROVIDER_TO_ENGINE = {"postgresql": "postgres", "mysql": "mysql", "mongodb": "mongodb", "sqlite": "sqlite"}

EXTENSIONS = (".js", ".ts")

_EXPRESS_ROUTE_CHAIN_RE = re.compile(
    r"""\b([A-Za-z_]\w*)\.route\s*\(\s*(['"])([^'"]+)\2\s*\)\s*\.\s*(get|post|put|patch|delete)\s*\("""
)
_OUTBOUND_RE = re.compile(
    r"\b(axios\.(?:get|post|put|patch|delete)|fetch|got)\s*\(\s*['\"`]?([^'\"`)]*)"
)
_GRPC_CLIENT_RE = re.compile(r"new\s+\w*(?:Client|Stub)\s*\(")
_QUEUE_PUBLISH_RE = re.compile(
    r"""\b(?:(?P<kafka>producer\.send)|(?P<rabbitmq>channel\.publish)|"""
    r"""(?P<abstracted>client\.emit|\.emit)|(?P<sqs>\.sendMessage)|(?P<sns>\w*sns\w*\.publish))"""
    r"""\s*\(\s*['"`]?(?P<channel>[^'"`,)]*)"""
)
_QUEUE_CONSUME_RE = re.compile(
    r"""\b(?:(?P<kafka>consumer\.subscribe)|(?P<rabbitmq>channel\.consume)|"""
    r"""(?P<abstracted>@EventPattern|@MessagePattern)|(?P<sqs>\.receiveMessage))"""
    r"""\s*\(\s*['"`]?(?P<channel>[^'"`,)]*)"""
)

_ENTITY_RE = re.compile(r"@Entity\s*\(\s*['\"`]?([^'\"`)]*)['\"`]?\s*\)")
_PRISMA_MODEL_RE = re.compile(r"^model\s+(\w+)\s*\{", re.MULTILINE)
_PRISMA_DATASOURCE_RE = re.compile(r"datasource\s+\w+\s*\{[^}]*provider\s*=\s*\"(\w+)\"", re.DOTALL)
_MONGOOSE_SCHEMA_RE = re.compile(r"new\s+(?:mongoose\.)?Schema\s*\(")
_SEQUELIZE_DEFINE_RE = re.compile(r"\.define\s*\(\s*['\"`]([^'\"`]+)['\"`]")

_CLASS_RE = re.compile(r"^\s*(?:export\s+)?class\s+(\w+)")


def _def_pattern(name: str) -> re.Pattern[str]:
    escaped = re.escape(name)
    return re.compile(
        rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped}\s*\(|^\s*{escaped}\s*\([^)]*\)\s*\{{",
        re.MULTILINE,
    )


def _endpoint_hint(method: str, path_value: str, file_path: Path, folder: Path, line_no: int) -> EndpointHint:
    excerpt = excerpt_around(file_path, folder, line_no, before=ENDPOINT_BEFORE, after=ENDPOINT_AFTER)
    component_hint = component_hint_for(file_path, line_no, _CLASS_RE)
    extra_excerpts = resolve_local_calls(
        file_path, folder, excerpt.text, _def_pattern, (excerpt.start_line, excerpt.end_line),
    )
    return EndpointHint(
        method=method, path=path_value, component_hint=component_hint,
        excerpt=excerpt, extra_excerpts=extra_excerpts,
    )


def _route_with_prefix(prefix: str, route: str) -> str:
    return f"{prefix.rstrip('/')}/{route.lstrip('/')}" if prefix else route


class NodeTsDetector:
    id = "node-ts"

    def matches(self, folder: Path) -> bool:
        pkg = folder / "package.json"
        if not pkg.is_file():
            return False
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            return False
        scripts = data.get("scripts", {})
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        has_entry = first_existing_file(folder, ("main.ts", "app.ts", "server.js", "server.ts", "index.js", "index.ts"))
        return bool(scripts.get("start") or scripts.get("start:prod") or has_entry or "express" in deps or "@nestjs/core" in deps)

    def collect_hints(self, folder: Path) -> ServiceHints:
        hints = ServiceHints()
        engine_hint = engine_hint_from_manifest(folder, _MANIFEST_FILES, _ENGINE_DRIVER_KEYWORDS)

        entry = first_existing_file(folder, ("src/main.ts", "src/app.ts", "main.ts", "app.ts", "server.js", "index.js"))
        if entry:
            hints.entry_excerpt = excerpt_around(entry, folder, 1, context=20)

        node_files = list(iter_files(folder, EXTENSIONS))
        cross_file_mounts = cross_file_express_mounts(node_files, folder)
        mounted_receivers: dict[Path, dict[str, str]] = {}
        for (mounted_path, receiver), prefix in cross_file_mounts.items():
            mounted_receivers.setdefault(mounted_path, {})[receiver] = prefix
        route_prefixes: dict[Path, dict[str, str]] = {}
        local_express_prefixes: dict[Path, dict[str, str]] = {}
        fastify_by_file: dict[Path, frozenset[str]] = {}

        def prefixes_for(path: Path) -> dict[str, str]:
            if path not in route_prefixes:
                source = path.read_text(encoding="utf-8", errors="ignore")
                applications, _ = express_receivers(source)
                local_express_prefixes[path] = express_route_prefixes(source)
                fastify_by_file[path] = fastify_receivers(source)
                route_prefixes[path] = {
                    **{receiver: "" for receiver in applications | fastify_by_file[path]},
                    **mounted_receivers.get(path.resolve(), {}),
                    **local_express_prefixes[path],
                }
            return route_prefixes[path]

        for path in node_files:
            prefixes = prefixes_for(path)
            for receiver, method, route, line_no in find_literal_node_routes(
                path, frozenset(prefixes), fastify_by_file[path],
            ):
                hints.endpoints.append(_endpoint_hint(
                    method, _route_with_prefix(prefixes[receiver], route), path, folder, line_no,
                ))

        for path, line_no, match in find_matches(folder, EXTENSIONS, _EXPRESS_ROUTE_CHAIN_RE):
            prefixes_for(path)
            receiver = match.group(1)
            if receiver not in local_express_prefixes[path]:
                continue
            route = _route_with_prefix(local_express_prefixes[path][receiver], match.group(3))
            hints.endpoints.append(_endpoint_hint(match.group(4).upper(), route, path, folder, line_no))

        for path in node_files:
            if path.suffix != ".ts":
                continue
            for method, route, line_no in find_nest_route_hints(path):
                hints.endpoints.append(_endpoint_hint(method, route, path, folder, line_no))

        for path, line_no, match in find_matches(folder, EXTENSIONS, _OUTBOUND_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="http", target_hint=match.group(2), excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _GRPC_CLIENT_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="grpc", target_hint=match.group(0), excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _QUEUE_PUBLISH_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint=match.group("channel") or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint=provider_from_match(match),
                )
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _QUEUE_CONSUME_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="consumes", channel_hint=match.group("channel") or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint=provider_from_match(match),
                )
            )

        for path, line_no, match in find_matches(folder, EXTENSIONS, _ENTITY_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint=match.group(1) or "?", excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _MONGOOSE_SCHEMA_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="document", name_hint="?", excerpt=excerpt_around(path, folder, line_no),
                    engine_hint="mongodb",  # unambiguous: this is Mongoose's own schema constructor
                )
            )
        for path, line_no, match in find_matches(folder, EXTENSIONS, _SEQUELIZE_DEFINE_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )
        prisma_schema = folder / "prisma" / "schema.prisma"
        if prisma_schema.is_file():
            text = prisma_schema.read_text(encoding="utf-8", errors="ignore")
            datasource_match = _PRISMA_DATASOURCE_RE.search(text)
            prisma_engine = _PRISMA_PROVIDER_TO_ENGINE.get(datasource_match.group(1)) if datasource_match else None
            for match in _PRISMA_MODEL_RE.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                hints.persistence.append(
                    PersistenceHint(
                        kind="sql_table", name_hint=match.group(1),
                        excerpt=excerpt_around(prisma_schema, folder, line_no), engine_hint=prisma_engine,
                    )
                )

        return hints
