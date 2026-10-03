"""Source-proven Spring Feign calls and client URL configuration bindings."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from orbitkb.analysis.configuration_syntax import SENSITIVE_CONFIGURATION_KEY
from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import find_matching_brace, mask_non_code
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
_PACKAGE_RE = re.compile(r"(?m)^[ \t]*package[ \t]+([\w.]+)[ \t]*;?")


def _feign_clients(source: str, visible_source: str) -> Iterator[re.Match[str]]:
    for match in _FEIGN_CLIENT_PATTERN.finditer(source):
        if visible_source[match.start()] == "@":
            yield match


def _package_name(source: str) -> str:
    match = _PACKAGE_RE.search(mask_non_code(source))
    return match.group(1) if match else ""


def _qualified_client_name(package: str, client: str) -> str:
    return f"{package}.{client}" if package else client


class SpringFeignRecognizer:
    """Connect injected Feign calls to literal mappings and property keys."""

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        result.static_service_calls.extend(_spring_feign_service_calls(result, files, root))
        result.configuration_bindings.extend(_feign_client_url_bindings(files, root))


def _spring_feign_service_calls(result: AnalysisResult, files: list[Path], root: Path) -> list[StaticServiceCall]:
    """Connect a Spring field injection to an explicitly declared Feign mapping."""
    endpoints = _feign_endpoints(files)
    files_by_name = {
        path.relative_to(root).as_posix(): path for path in files if path.suffix in {".java", ".kt"}
    }
    sources = {
        name: files_by_name[name].read_text(encoding="utf-8", errors="ignore")
        for name in {injection.evidence.file_path for injection in result.injections}
        if name in files_by_name
    }
    injection_contracts = {
        (injection.consumer.split(".", 1)[0], injection.consumer.rsplit(".", 1)[-1]): _injected_feign_type(
            injection.contract, sources.get(injection.evidence.file_path), endpoints,
        )
        for injection in result.injections
    }
    injected_contracts_by_owner: dict[str, set[str]] = {}
    for (owner, _field), contract in injection_contracts.items():
        if contract is not None:
            injected_contracts_by_owner.setdefault(owner, set()).add(contract)
    calls: list[StaticServiceCall] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    for edge in result.edges:
        receiver, separator, member = edge.target.rpartition(".")
        if not separator:
            continue
        owner = edge.source.split(".", 1)[0]
        client = injection_contracts.get((owner, receiver))
        if client is None:
            matches = {
                contract for contract in injected_contracts_by_owner.get(owner, set())
                if receiver in {contract, contract.rsplit(".", 1)[-1]}
            }
            if len(matches) == 1:
                client = next(iter(matches))
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


def _injected_feign_type(
    contract: str, source: str | None, endpoints: dict[tuple[str, str], tuple[str, str, str]],
) -> str | None:
    if "." in contract:
        return contract if any(client == contract for client, _method in endpoints) else None
    if source is not None:
        imported = parse_jvm_imports(mask_non_code(source)).get(contract)
        if imported is not None:
            return imported
        package = _package_name(source)
        local = f"{package}.{contract}" if package else contract
        if any(client == local for client, _method in endpoints):
            return local
        return None
    matches = {client for client, _method in endpoints if client.rsplit(".", 1)[-1] == contract}
    return next(iter(matches)) if len(matches) == 1 else None


def _feign_endpoints(files: list[Path]) -> dict[tuple[str, str], tuple[str, str, str]]:
    """Return only literal method mappings declared in a local Feign interface."""
    candidates: dict[tuple[str, str], set[tuple[str, str, str]]] = {}
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        visible_source = mask_non_code(source)
        package = _package_name(source)
        for client_match in _feign_clients(source, visible_source):
            service = client_match.group("service")
            if not service or "$" in service or "#{" in service:
                continue
            client = _qualified_client_name(package, client_match.group("client"))
            route_prefix, unresolved_route_prefix = spring_route_prefix(client_match.group("annotations"))
            if unresolved_route_prefix:
                continue
            brace_open = client_match.end() - 1
            brace_close = find_matching_brace(source, brace_open)
            body = source[brace_open + 1 : brace_close]
            visible_body = visible_source[brace_open + 1 : brace_close]
            for method_match in _FEIGN_METHOD_PATTERN.finditer(body):
                if visible_body[method_match.start()] != "@" or visible_body[method_match.start("method")] == " ":
                    continue
                if "${" in method_match.group("path") or "#{" in method_match.group("path"):
                    continue
                candidates.setdefault((client, method_match.group("method")), set()).add((
                    service,
                    SPRING_ROUTE_ANNOTATION_TO_METHOD[method_match.group("mapping")],
                    join_route(route_prefix, method_match.group("path")),
                ))
    return {key: next(iter(routes)) for key, routes in candidates.items() if len(routes) == 1}


def _feign_client_url_bindings(files: list[Path], root: Path) -> list[ConfigurationBinding]:
    """Bind a literal client URL property placeholder to its Feign interface."""
    bindings: list[ConfigurationBinding] = []
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        visible_source = mask_non_code(source)
        package = _package_name(source)
        for client_match in _feign_clients(source, visible_source):
            url_match = _FEIGN_CLIENT_URL.search(client_match.group("extra_args"))
            if url_match is None:
                continue
            key = url_match.group("key")
            line = source.count("\n", 0, client_match.start()) + 1
            bindings.append(ConfigurationBinding(
                source=_qualified_client_name(package, client_match.group("client")),
                key=key,
                kind="property",
                sensitive=SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
                evidence=Evidence(path.relative_to(root).as_posix(), line, line),
            ))
    return bindings
