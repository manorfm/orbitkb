"""Source-proven Spring Feign calls and client URL configuration bindings."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from orbitkb.analysis.configuration_syntax import SENSITIVE_CONFIGURATION_KEY
from orbitkb.analysis.http_destination import literal_public_http_destination
from orbitkb.analysis.jvm_imports import parse_jvm_imports
from orbitkb.analysis.jvm_scanner import (
    find_matching_brace,
    mask_non_code,
    split_top_level,
)
from orbitkb.analysis.jvm_spring_syntax import (
    SPRING_ROUTE_ANNOTATION_TO_METHOD,
    spring_placeholder_literal,
    spring_route_prefix,
)
from orbitkb.analysis.models import (
    AnalysisResult,
    ConfigurationBinding,
    Evidence,
    ExternalHttpCall,
    StaticServiceCall,
)
from orbitkb.analysis.route_paths import join_route
from orbitkb.discovery.scan_helpers import find_matching_paren

_FEIGN_START = re.compile(r'@FeignClient\s*\(')
_ANNOTATION_NAME = re.compile(r'@[\w.]+')
_FEIGN_INTERFACE = re.compile(r'(?:public\s+)?interface\s+(?P<client>\w+)\s*\{')
_FEIGN_METHOD_ANNOTATION = re.compile(
    r'@(?P<mapping>GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping)\s*\('
)
_FEIGN_METHOD_PATH = re.compile(r'(?:value\s*=\s*)?"([^"\n]+)"')
_FEIGN_METHOD_SIGNATURE = re.compile(r'\s*(?:[\w<>?,\[\]\s]+\s+)?(?P<method>\w+)\s*\(')
_FEIGN_PROPERTY_URL = re.compile(spring_placeholder_literal("key"))
_FEIGN_EMPTY_URL = re.compile(r'\$*""')
_FEIGN_LITERAL_URL = re.compile(r'\$*"([^"\n]+)"')
_FEIGN_SERVICE_NAME = re.compile(r'"([^"\n]+)"')
_KOTLIN_INTERPOLATED_VALUE = re.compile(r'(?<!\\)\$[A-Za-z_]')
_PACKAGE_RE = re.compile(r"(?m)^[ \t]*package[ \t]+([\w.]+)[ \t]*;?")


@dataclass(frozen=True)
class _FeignEndpoint:
    service: str
    method: str
    path: str
    external_origin: tuple[str, str, int | None] | None = None


@dataclass(frozen=True)
class _FeignClientDeclaration:
    start: int
    end: int
    args: str
    annotations: str
    client: str


def _feign_clients(source: str, visible_source: str) -> Iterator[_FeignClientDeclaration]:
    for match in _FEIGN_START.finditer(visible_source):
        opening = match.end() - 1
        closing = find_matching_paren(source, opening)
        if closing < 0:
            continue
        cursor = closing + 1
        annotations_start = cursor
        while True:
            while cursor < len(source) and visible_source[cursor].isspace():
                cursor += 1
            annotation = _ANNOTATION_NAME.match(visible_source, cursor)
            if annotation is None:
                break
            cursor = annotation.end()
            while cursor < len(source) and visible_source[cursor].isspace():
                cursor += 1
            if cursor < len(source) and visible_source[cursor] == "(":
                annotation_end = find_matching_paren(source, cursor)
                if annotation_end < 0:
                    break
                cursor = annotation_end + 1
        interface = _FEIGN_INTERFACE.match(visible_source, cursor)
        if interface is not None:
            yield _FeignClientDeclaration(
                match.start(), interface.end(), source[opening + 1:closing],
                source[annotations_start:cursor], interface.group("client"),
            )


def _package_name(source: str) -> str:
    match = _PACKAGE_RE.search(mask_non_code(source))
    return match.group(1) if match else ""


def _qualified_client_name(package: str, client: str) -> str:
    return f"{package}.{client}" if package else client


def _feign_url_argument(extra_args: str) -> str | None:
    values = []
    for argument in split_top_level(extra_args):
        key, separator, value = argument.partition("=")
        if separator and key.strip() == "url":
            values.append(value.strip())
    if not values:
        return None
    return values[0] if len(values) == 1 else ""


def _feign_service_name(args: str) -> str | None:
    values = []
    for index, argument in enumerate(split_top_level(args)):
        key, separator, value = argument.partition("=")
        if separator and key.strip() in {"name", "value"}:
            values.append(value.strip())
        elif not separator and index == 0:
            values.append(argument.strip())
    names = []
    for value in values:
        literal = _FEIGN_SERVICE_NAME.fullmatch(value)
        if literal is None:
            return None
        names.append(literal.group(1))
    return names[0] if names and len(set(names)) == 1 else None


def _feign_mapped_methods(body: str, visible_body: str) -> Iterator[tuple[str, str, str]]:
    for annotation in _FEIGN_METHOD_ANNOTATION.finditer(visible_body):
        opening = annotation.end() - 1
        closing = find_matching_paren(body, opening)
        if closing < 0:
            continue
        arguments = split_top_level(body[opening + 1:closing])
        if not arguments or (path_match := _FEIGN_METHOD_PATH.fullmatch(arguments[0].strip())) is None:
            continue
        signature = _FEIGN_METHOD_SIGNATURE.match(body, closing + 1)
        if signature is None or visible_body[signature.start("method")] == " ":
            continue
        path = path_match.group(1)
        if "${" in path or "#{" in path:
            continue
        yield annotation.group("mapping"), path, signature.group("method")


class SpringFeignRecognizer:
    """Connect injected Feign calls to literal mappings and property keys."""

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        service_calls, external_calls = _spring_feign_calls(result, files, root)
        result.static_service_calls.extend(service_calls)
        result.external_http_calls.extend(external_calls)
        result.configuration_bindings.extend(_feign_client_url_bindings(files, root))


def _spring_feign_calls(
    result: AnalysisResult, files: list[Path], root: Path,
) -> tuple[list[StaticServiceCall], list[ExternalHttpCall]]:
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
    service_calls: list[StaticServiceCall] = []
    external_calls: list[ExternalHttpCall] = []
    seen: set[tuple[str, _FeignEndpoint]] = set()
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
        key = (edge.source, endpoint)
        if key in seen:
            continue
        seen.add(key)
        if endpoint.external_origin is not None:
            scheme, host, port = endpoint.external_origin
            external_calls.append(ExternalHttpCall(
                edge.source, scheme, host, port, endpoint.method, endpoint.path, edge.evidence,
            ))
        else:
            service_calls.append(StaticServiceCall(
                edge.source, endpoint.service, "http", endpoint.method, endpoint.path, edge.evidence,
            ))
    return service_calls, external_calls


def _injected_feign_type(
    contract: str, source: str | None, endpoints: dict[tuple[str, str], _FeignEndpoint],
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


def _feign_endpoints(files: list[Path]) -> dict[tuple[str, str], _FeignEndpoint]:
    """Return only literal method mappings declared in a local Feign interface."""
    candidates: dict[tuple[str, str], set[_FeignEndpoint]] = {}
    for path in files:
        if path.suffix not in {".java", ".kt"}:
            continue
        source = path.read_text(encoding="utf-8", errors="ignore")
        visible_source = mask_non_code(source)
        package = _package_name(source)
        for client_match in _feign_clients(source, visible_source):
            service = _feign_service_name(client_match.args)
            if not service or "$" in service or "#{" in service:
                continue
            client = _qualified_client_name(package, client_match.client)
            url_argument = _feign_url_argument(client_match.args)
            destination = None
            if url_argument is not None and (
                _FEIGN_EMPTY_URL.fullmatch(url_argument) is None
                and _FEIGN_PROPERTY_URL.fullmatch(url_argument) is None
            ):
                url_match = _FEIGN_LITERAL_URL.fullmatch(url_argument)
                if url_match is None:
                    continue
                url = url_match.group(1)
                if "${" in url or "#{" in url or (
                    path.suffix == ".kt" and _KOTLIN_INTERPOLATED_VALUE.search(url)
                ):
                    continue
                destination = literal_public_http_destination(url)
                if destination is None:
                    continue
            route_prefix, unresolved_route_prefix = spring_route_prefix(client_match.annotations)
            if unresolved_route_prefix:
                continue
            brace_open = client_match.end - 1
            brace_close = find_matching_brace(source, brace_open)
            body = source[brace_open + 1 : brace_close]
            visible_body = visible_source[brace_open + 1 : brace_close]
            for mapping, route, method_name in _feign_mapped_methods(body, visible_body):
                method_path = join_route(route_prefix, route)
                target_path = join_route(destination[3], method_path) if destination else method_path
                candidates.setdefault((client, method_name), set()).add(_FeignEndpoint(
                    service=service,
                    method=SPRING_ROUTE_ANNOTATION_TO_METHOD[mapping],
                    path=target_path,
                    external_origin=destination[:3] if destination else None,
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
            if _feign_service_name(client_match.args) is None:
                continue
            url_argument = _feign_url_argument(client_match.args)
            url_match = _FEIGN_PROPERTY_URL.fullmatch(url_argument) if url_argument is not None else None
            if url_match is None:
                continue
            key = url_match.group("key")
            line = source.count("\n", 0, client_match.start) + 1
            bindings.append(ConfigurationBinding(
                source=_qualified_client_name(package, client_match.client),
                key=key,
                kind="property",
                sensitive=SENSITIVE_CONFIGURATION_KEY.search(key) is not None,
                evidence=Evidence(path.relative_to(root).as_posix(), line, line),
            ))
    return bindings
