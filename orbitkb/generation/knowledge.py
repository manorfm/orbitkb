"""Read contract for composing generated knowledge without storage details."""

from typing import Protocol

RouteKey = tuple[str, str]


class KnowledgeReader(Protocol):
    def api_summaries(self, service_id: int) -> dict[RouteKey, str]: ...


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
