"""Read contract for composing generated knowledge without storage details."""

from dataclasses import dataclass
from typing import Protocol

RouteKey = tuple[str, str]


@dataclass(frozen=True)
class ComponentSummary:
    name: str
    file_path: str
    summary: str


@dataclass(frozen=True)
class EndpointSummary:
    summary: str
    evidence: list[dict]


@dataclass(frozen=True)
class EndpointContext:
    text: str
    evidence: list[dict]


@dataclass(frozen=True)
class EndpointDocumentation:
    method: str
    path: str
    summary: str
    description: str
    response_shape: list[dict]
    request_shape: list[dict]
    validations: list[dict]
    calls: list[dict]
    evidence: list[dict]


@dataclass(frozen=True)
class ComponentDocumentation:
    name: str
    file_path: str
    summary: str
    evidence: list[dict]


@dataclass(frozen=True)
class OverviewDocumentation:
    short_desc: str
    long_desc: str


@dataclass(frozen=True)
class PersistenceEntity:
    name: str
    kind: str
    engine: str
    fields: list[dict]


@dataclass(frozen=True)
class PersistenceDocumentation:
    entities: list[PersistenceEntity]
    evidence: list[dict]


@dataclass(frozen=True)
class MessageDocumentation:
    direction: str
    channel: str
    provider: str
    shape: list[dict]
    description: str


@dataclass(frozen=True)
class MessagingDocumentation:
    messages: list[MessageDocumentation]
    evidence: list[dict]


class KnowledgeReader(Protocol):
    def endpoint_keys(self, service_id: int) -> set[RouteKey]: ...
    def api_summaries(self, service_id: int) -> dict[RouteKey, EndpointSummary]: ...
    def component_summaries(self, service_id: int) -> list[ComponentSummary]: ...


class KnowledgeWriter(Protocol):
    def save_endpoint(self, service_id: int, documentation: EndpointDocumentation) -> None: ...
    def prune_endpoints(self, service_id: int, keep_keys: set[RouteKey]) -> None: ...
    def save_component(self, service_id: int, documentation: ComponentDocumentation) -> None: ...
    def prune_components(self, service_id: int, keep_keys: set[tuple[str, str]]) -> None: ...
    def save_overview(self, service_id: int, documentation: OverviewDocumentation) -> None: ...
    def replace_persistence(self, service_id: int, documentation: PersistenceDocumentation) -> None: ...
    def replace_messaging(self, service_id: int, documentation: MessagingDocumentation) -> None: ...


def compose_endpoint_context(
    routes: list[RouteKey], summaries: dict[RouteKey, EndpointSummary],
) -> EndpointContext:
    """Use only stored summaries and their source pointers, in discovery order."""
    lines: list[str] = []
    evidence: list[dict] = []
    seen_routes: set[RouteKey] = set()
    seen_pointers: set[tuple[str, int, int]] = set()
    for method, path in routes:
        key = (method, path)
        if key in seen_routes:
            continue
        seen_routes.add(key)
        if key not in summaries:
            continue
        endpoint = summaries[key]
        lines.append(f"- {method} {path}: {endpoint.summary}")
        for pointer in endpoint.evidence:
            identity = (pointer["file"], pointer["start_line"], pointer["end_line"])
            if identity not in seen_pointers:
                seen_pointers.add(identity)
                evidence.append(pointer)
    return EndpointContext("\n".join(lines) or "(no endpoint summaries available yet)", evidence)


def compose_endpoint_summaries(routes: list[RouteKey], summaries: dict[RouteKey, EndpointSummary]) -> str:
    return compose_endpoint_context(routes, summaries).text


def compose_component_summaries(components: list[ComponentSummary]) -> str:
    return "\n".join(
        f"- {component.name} ({component.file_path}): {component.summary}"
        for component in components
    ) or (
        "(no classes/controllers detected — this service's routing is likely function-based, "
        "or it exposes no HTTP endpoints at all)"
    )
