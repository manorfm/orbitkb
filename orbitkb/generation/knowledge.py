"""Read contract for composing generated knowledge without storage details."""

from dataclasses import dataclass
from typing import Protocol

RouteKey = tuple[str, str]


@dataclass(frozen=True)
class ComponentSummary:
    name: str
    file_path: str
    summary: str


class KnowledgeReader(Protocol):
    def api_summaries(self, service_id: int) -> dict[RouteKey, str]: ...
    def component_summaries(self, service_id: int) -> list[ComponentSummary]: ...


def compose_endpoint_summaries(routes: list[RouteKey], summaries: dict[RouteKey, str]) -> str:
    """Describe each available route once, in discovery order."""
    lines: list[str] = []
    seen: set[RouteKey] = set()
    for method, path in routes:
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)
        if key in summaries:
            lines.append(f"- {method} {path}: {summaries[key]}")
    return "\n".join(lines) or "(no endpoint summaries available yet)"


def compose_component_summaries(components: list[ComponentSummary]) -> str:
    return "\n".join(
        f"- {component.name} ({component.file_path}): {component.summary}"
        for component in components
    ) or (
        "(no classes/controllers detected — this service's routing is likely function-based, "
        "or it exposes no HTTP endpoints at all)"
    )
