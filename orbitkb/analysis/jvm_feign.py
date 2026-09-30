"""Source-proven Spring Feign calls and client URL configuration bindings."""

from __future__ import annotations

import re
from pathlib import Path

from orbitkb.analysis.configuration_syntax import SENSITIVE_CONFIGURATION_KEY
from orbitkb.analysis.jvm_scanner import find_matching_brace
from orbitkb.analysis.jvm_spring_syntax import (
    SPRING_ROUTE_ANNOTATION_TO_METHOD,
    spring_placeholder_literal,
    spring_route_prefix,
)
from orbitkb.analysis.models import (
    AnalysisResult,
    ConfigurationBinding,
    Evidence,
    StaticServiceCall,
)
from orbitkb.analysis.route_paths import join_route

# Stop at the interface's opening brace. A route may contain `{id}`, so the
# interface body itself must be delimited with find_matching_brace below.
_FEIGN_CLIENT_PATTERN = re.compile(
    r'@FeignClient\s*\(\s*(?:(?:name|value)\s*=\s*)?"(?P<service>[^"]+)"(?P<extra_args>[^)]*)\)\s*'
    r'(?P<annotations>(?:@\w+(?:\s*\([^)]*\))?\s*)*)'
    r'(?:public\s+)?interface\s+(?P<client>\w+)\s*\{',
)
_FEIGN_METHOD_PATTERN = re.compile(
    r'@(?P<mapping>GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping)\s*'
    r'\(\s*(?:value\s*=\s*)?"(?P<path>[^"]+)"[^)]*\)\s*'
    r'(?:[\w<>?,\[\]\s]+\s+)?(?P<method>\w+)\s*\(',
    re.DOTALL,
)
_FEIGN_CLIENT_URL = re.compile(r'\burl\s*=\s*' + spring_placeholder_literal("key"))


class SpringFeignRecognizer:
    """Connect injected Feign calls to literal mappings and property keys."""

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        result.static_service_calls.extend(_spring_feign_service_calls(result, files))
        result.configuration_bindings.extend(_feign_client_url_bindings(files, root))


def _spring_feign_service_calls(result: AnalysisResult, files: list[Path]) -> list[StaticServiceCall]:
    """Connect a Spring field injection to an explicitly declared Feign mapping."""
    endpoints = _feign_endpoints(files)
    injection_contracts = {
        (injection.consumer.split(".", 1)[0], injection.consumer.rsplit(".", 1)[-1]): injection.contract
        for injection in result.injections
    }
    injected_contracts_by_owner: dict[str, set[str]] = {}
    for injection in result.injections:
        owner = injection.consumer.split(".", 1)[0]
        injected_contracts_by_owner.setdefault(owner, set()).add(injection.contract)
    calls: list[StaticServiceCall] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for edge in result.edges:
        receiver, separator, member = edge.target.rpartition(".")
        if not separator:
            continue
        owner = edge.source.split(".", 1)[0]
        client = injection_contracts.get((owner, receiver))
        if client is None and receiver in injected_contracts_by_owner.get(owner, set()):
            client = receiver
        endpoint = endpoints.get((client or "", member))
        if endpoint is None:
            continue
        target_service, method, path = endpoint
        key = (edge.source, target_service, "http", method, path)
        if key in seen:
            continue
        seen.add(key)
        calls.append(StaticServiceCall(
            source=edge.source,
            target_service=target_service,
            protocol="http",
            target_method=method,
            target_path=path,
            evidence=edge.evidence,
        ))
    return calls


def _feign_endpoints(files: list[Path]) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Return only literal method mappings declared in a local Feign interface."""
    endpoints = {}
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for client_match in _FEIGN_CLIENT_PATTERN.finditer(source):
            service = client_match.group("service")
            client = client_match.group("client")
            route_prefix = spring_route_prefix(client_match.group("annotations"))
            brace_open = client_match.end() - 1
            brace_close = find_matching_brace(source, brace_open)
            body = source[brace_open + 1 : brace_close]
            for method_match in _FEIGN_METHOD_PATTERN.finditer(body):
                endpoints[(client, method_match.group("method"))] = (
                    service,
                    SPRING_ROUTE_ANNOTATION_TO_METHOD[method_match.group("mapping")],
                    join_route(route_prefix, method_match.group("path")),
                )
    return endpoints


def _feign_client_url_bindings(files: list[Path], root: Path) -> list[ConfigurationBinding]:
    """Bind a literal client URL property placeholder to its Feign interface."""
    bindings: list[ConfigurationBinding] = []
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        for client_match in _FEIGN_CLIENT_PATTERN.finditer(source):
            url_match = _FEIGN_CLIENT_URL.search(client_match.group("extra_args"))
            if url_match is None:
                continue
            key = url_match.group("key")
            line = source.count("\n", 0, client_match.start()) + 1
            bindings.append(ConfigurationBinding(
                source=client_match.group("client"),
                key=key,
                kind="property",
                sensitive=SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
                evidence=Evidence(path.relative_to(root).as_posix(), line, line),
            ))
    return bindings
