from __future__ import annotations

import logging
import re
from pathlib import Path

from orbitkb.discovery.base import (
    EndpointHint,
    MessagingHint,
    OutboundCallHint,
    PersistenceHint,
    ServiceHints,
)
from orbitkb.discovery.jvm_ast import ParseCache, resolve_kotlin_java_calls
from orbitkb.discovery.scan_helpers import (
    ENDPOINT_AFTER,
    ENDPOINT_BEFORE,
    component_hint_for,
    engine_hint_from_manifest,
    excerpt_around,
    find_matches,
)

logger = logging.getLogger(__name__)

_MANIFEST_FILES = ("pom.xml", "build.gradle", "build.gradle.kts")
_ENGINE_DRIVER_KEYWORDS = {
    "postgresql": "postgres",
    "mysql-connector": "mysql",
    "mongodb-driver": "mongodb",
    "spring-boot-starter-data-mongodb": "mongodb",
    "lettuce": "redis",
    "jedis": "redis",
    "spring-boot-starter-data-cassandra": "cassandra",
    "cassandra-driver": "cassandra",
    "datastax": "cassandra",
}

EXTENSIONS = (".java", ".kt")

_MAPPING_RE = re.compile(
    r"@(GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)"
    r"\s*\(\s*(?:value\s*=\s*)?['\"]?([^'\")]*)['\"]?\s*\)?"
)
_REST_CONTROLLER_RE = re.compile(r"@RestController")

_METHOD_BY_ANNOTATION = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
    "RequestMapping": "REQUEST",
}

_OUTBOUND_RE = re.compile(r"\b(RestTemplate|WebClient)\b.*?\.(get|post|put|patch|delete|exchange)\s*\(", re.DOTALL)
_FEIGN_CLIENT_RE = re.compile(r"@FeignClient\s*\(\s*(?:name\s*=\s*)?['\"]?([^'\")]*)['\"]?")
_GRPC_STUB_RE = re.compile(r"(\w*Grpc\.\w*Stub)\s+\w+")
_STREAM_SEND_RE = re.compile(r"\bStreamBridge\.\w*send\s*\(\s*['\"]?([^'\")]*)")

_KAFKA_LISTENER_RE = re.compile(r"@KafkaListener\s*\(\s*(?:topics\s*=\s*)?['\"]?([^'\")]*)['\"]?")
_KAFKA_SEND_RE = re.compile(r"\bKafkaTemplate\b.*?\.send\s*\(\s*['\"]?([^'\")]*)", re.DOTALL)
_RABBIT_LISTENER_RE = re.compile(r"@RabbitListener\s*\(\s*(?:queues\s*=\s*)?['\"]?([^'\")]*)['\"]?")
_RABBIT_SEND_RE = re.compile(r"\bRabbitTemplate\b.*?\.convertAndSend\s*\(\s*['\"]?([^'\")]*)", re.DOTALL)
# JMS is a vendor-agnostic Java API (ActiveMQ, IBM MQ, Rabbit-via-JMS, ...) — the
# concrete broker is only visible in a ConnectionFactory bean/application.properties,
# never in the annotation/template call itself.
_JMS_LISTENER_RE = re.compile(r"@JmsListener\s*\(\s*(?:destination\s*=\s*)?['\"]?([^'\")]*)['\"]?")
_JMS_SEND_RE = re.compile(r"\bJmsTemplate\b.*?\.convertAndSend\s*\(\s*['\"]?([^'\")]*)", re.DOTALL)

_JPA_ENTITY_RE = re.compile(r"@Entity\b.*?\bclass\s+(\w+)", re.DOTALL)
_SPRING_DATA_REPO_RE = re.compile(r"interface\s+(\w+)\s+extends\s+\w*Repository")

_CLASS_RE = re.compile(r"^\s*(?:public\s+|private\s+)?(?:class|interface)\s+(\w+)")


def _endpoint_hint(
    method: str, path_value: str, file_path: Path, folder: Path, line_no: int, cache: ParseCache,
) -> EndpointHint:
    logger.debug("building endpoint hint: %s %s (%s:%s)", method, path_value, file_path, line_no)
    excerpt = excerpt_around(file_path, folder, line_no, before=ENDPOINT_BEFORE, after=ENDPOINT_AFTER)
    component_hint = component_hint_for(file_path, line_no, _CLASS_RE)
    extra_excerpts = resolve_kotlin_java_calls(
        file_path, folder, excerpt.text, (excerpt.start_line, excerpt.end_line), cache=cache,
    )
    return EndpointHint(
        method=method, path=path_value, component_hint=component_hint,
        excerpt=excerpt, extra_excerpts=extra_excerpts,
    )


def endpoint_matches(scan_root: Path) -> list[tuple[str, str, Path, int]]:
    """(method, route, path, line_no) for every `@XMapping` annotation -- pure regex,
    no tree-sitter, always safe to run directly (never needs isolating).
    """
    return [
        (_METHOD_BY_ANNOTATION[match.group(1)], match.group(2) or "/", path, line_no)
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _MAPPING_RE)
    ]


def endpoint_hints_for_matches(
    matches: list[tuple[str, str, Path, int]], folder: Path, cache: ParseCache | None = None,
) -> list[EndpointHint]:
    """The half of building endpoint hints that follows a call into another file
    (`resolve_kotlin_java_calls`, pure regex since `jvm_ast.py`'s tree-sitter
    removal -- no longer the crash-prone half it once was). Still run per batch via
    `orchestrator.py` for jvm-spring, so one endpoint's resolution failing is
    isolated to its own batch's hints rather than the whole service's. `cache`
    should be shared across one batch's matches (see `ParseCache`); a fresh one is
    created when called standalone.
    """
    if cache is None:
        cache = ParseCache()
    return [
        _endpoint_hint(method, route, path, folder, line_no, cache)
        for method, route, path, line_no in matches
    ]


def _has_spring_boot_dependency(folder: Path) -> bool:
    pom = folder / "pom.xml"
    if pom.is_file() and "spring-boot" in pom.read_text(encoding="utf-8", errors="ignore"):
        return True
    for gradle_name in ("build.gradle", "build.gradle.kts"):
        gradle = folder / gradle_name
        if gradle.is_file() and "spring-boot" in gradle.read_text(encoding="utf-8", errors="ignore"):
            return True
    return False


class JvmSpringDetector:
    id = "jvm-spring"

    def matches(self, folder: Path) -> bool:
        has_build_file = (folder / "pom.xml").is_file() or (folder / "build.gradle").is_file() or (folder / "build.gradle.kts").is_file()
        if not has_build_file:
            return False
        if _has_spring_boot_dependency(folder):
            return True
        src = folder / "src" / "main"
        if src.is_dir():
            for path, _line_no, _m in find_matches(src, EXTENSIONS, re.compile(r"@SpringBootApplication")):
                return True
        return False

    @staticmethod
    def scan_root(folder: Path) -> Path:
        src = folder / "src" / "main"
        return src if src.is_dir() else folder

    def collect_hints(self, folder: Path) -> ServiceHints:
        hints = self.collect_hints_without_endpoints(folder)
        hints.endpoints = endpoint_hints_for_matches(endpoint_matches(self.scan_root(folder)), folder)
        return hints

    def collect_hints_without_endpoints(self, folder: Path) -> ServiceHints:
        """Everything `collect_hints()` builds except `hints.endpoints` -- pure regex,
        so it never needs isolating. Split out so a caller (see `orchestrator.py`'s
        isolation for jvm-spring) can always get this part, even when endpoint
        resolution -- run in its own isolated batch regardless -- fails.
        """
        logger.debug("scanning JVM/Spring hints: %s", folder)
        hints = ServiceHints()
        engine_hint = engine_hint_from_manifest(folder, _MANIFEST_FILES, _ENGINE_DRIVER_KEYWORDS)
        scan_root = self.scan_root(folder)

        entry_matches = find_matches(scan_root, EXTENSIONS, re.compile(r"@SpringBootApplication"))
        if entry_matches:
            path, line_no, _m = entry_matches[0]
            hints.entry_excerpt = excerpt_around(path, folder, line_no, context=20)

        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _OUTBOUND_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="http", target_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _FEIGN_CLIENT_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="http", target_hint=match.group(1) or "?", excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _GRPC_STUB_RE):
            hints.outbound_calls.append(
                OutboundCallHint(call_kind="grpc", target_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no))
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _STREAM_SEND_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="abstracted",
                )
            )

        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _KAFKA_LISTENER_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="consumes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="kafka",
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _KAFKA_SEND_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="kafka",
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _RABBIT_LISTENER_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="consumes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="rabbitmq",
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _RABBIT_SEND_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="rabbitmq",
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _JMS_LISTENER_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="consumes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="abstracted",
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _JMS_SEND_RE):
            hints.messaging.append(
                MessagingHint(
                    direction="publishes", channel_hint=match.group(1) or "?",
                    excerpt=excerpt_around(path, folder, line_no), provider_hint="abstracted",
                )
            )

        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _JPA_ENTITY_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )
        for path, line_no, match in find_matches(scan_root, EXTENSIONS, _SPRING_DATA_REPO_RE):
            hints.persistence.append(
                PersistenceHint(
                    kind="sql_table", name_hint=match.group(1), excerpt=excerpt_around(path, folder, line_no),
                    engine_hint=engine_hint,
                )
            )

        return hints
