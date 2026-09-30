"""Source-proven HTTP targets for service and route documentation."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from orbitkb.domain.canonical import FactStatus
from orbitkb.domain.navigation import (
    KnowledgeNavigator,
    TraversalPolicy,
    route_entrypoints,
)


@dataclass(frozen=True)
class DeclaredHttpCall:
    target_service: str
    method: str | None
    path: str | None


@dataclass(frozen=True)
class RouteHttpCalls:
    calls: tuple[DeclaredHttpCall, ...]
    truncated: bool


def route_declared_http_calls(
    navigator: KnowledgeNavigator | None, method: str, path: str, represented_targets: Iterable[str],
) -> RouteHttpCalls:
    """Reach only source-confirmed HTTP calls from this route's bounded flow."""
    if navigator is None:
        return RouteHttpCalls((), False)
    represented = set(represented_targets)
    calls: set[DeclaredHttpCall] = set()
    truncated = False
    for entrypoint in route_entrypoints(navigator.snapshot, method, path):
        reached = navigator.reachable(entrypoint, TraversalPolicy())
        truncated |= reached.truncated
        for fact in reached.facts:
            target = fact.attributes.get("target_service")
            if (fact.kind != "service_call" or fact.status is not FactStatus.CONFIRMED
                    or fact.attributes.get("protocol") != "http" or not isinstance(target, str)
                    or not target or target in represented):
                continue
            calls.add(DeclaredHttpCall(
                target, fact.attributes.get("target_method"), fact.attributes.get("target_path"),
            ))
    return RouteHttpCalls(tuple(sorted(calls, key=lambda call: (
        call.target_service, call.method or "", call.path or "",
    ))), truncated)


def unresolved_declared_http_targets(
    static_calls: Iterable[Mapping], represented_targets: Iterable[str],
) -> tuple[str, ...]:
    """Keep a declared target once, without claiming its runtime destination is known."""
    represented = set(represented_targets)
    return tuple(sorted({
        call["target_service"] for call in static_calls
        if call["protocol"] == "http" and call["target_service"] not in represented
    }))
