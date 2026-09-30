"""Source-proven outbound HTTP calls in one bounded API flow."""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

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


class RouteCallStatus(str, Enum):
    UNASSESSED = "unassessed"
    ASSESSED = "assessed"
    LIMITED = "limited"


@dataclass(frozen=True)
class RouteHttpCalls:
    calls: tuple[DeclaredHttpCall, ...]
    status: RouteCallStatus


def route_declared_http_calls(
    navigator: KnowledgeNavigator | None, method: str, path: str, represented_targets: Iterable[str],
) -> RouteHttpCalls:
    """Reach confirmed HTTP calls, omitting targets already represented by indexed calls."""
    if navigator is None:
        return RouteHttpCalls((), RouteCallStatus.UNASSESSED)
    entrypoints = route_entrypoints(navigator.snapshot, method, path)
    if not entrypoints:
        return RouteHttpCalls((), RouteCallStatus.UNASSESSED)
    represented = set(represented_targets)
    calls: set[DeclaredHttpCall] = set()
    truncated = False
    for entrypoint in entrypoints:
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
    ))), RouteCallStatus.LIMITED if truncated else RouteCallStatus.ASSESSED)
