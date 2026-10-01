"""Locally proven Express and Fastify receiver declarations."""

from __future__ import annotations

import re


def express_receivers(source: str) -> tuple[frozenset[str], frozenset[str]]:
    imported = re.search(
        r"(?:import\s+(?:\*\s+as\s+)?express(?:\s*,\s*\{[^}]*\})?\s+from\s*|"
        r"(?:const|let)\s+express\s*=\s*require\s*\()"
        r"[\"']express[\"']",
        source,
    )
    if imported is None:
        return frozenset(), frozenset()
    factories = re.findall(
        r"\b(?:const|let|var)\s+(\w+)\s*=\s*express(?:(\.Router))?\s*\(", source,
    )
    applications = frozenset(name for name, router_factory in factories if not router_factory)
    routers = frozenset(name for name, router_factory in factories if router_factory)
    return applications, routers


def express_route_prefixes(source: str) -> dict[str, str]:
    """Return direct applications and routers mounted once by a literal local app."""
    applications, routers = express_receivers(source)
    mounts: dict[str, list[str]] = {}
    for application, _quote, prefix, router in re.findall(
        r"\b(\w+)\.use\s*\(\s*([\"'])([^\"']+)\2\s*,\s*(\w+)\s*\)", source,
    ):
        if application in applications and router in routers:
            mounts.setdefault(router, []).append(prefix)
    return {
        **{application: "" for application in applications},
        **{router: prefixes[0] for router, prefixes in mounts.items() if len(prefixes) == 1},
    }


def fastify_receivers(source: str) -> frozenset[str]:
    factories = {
        *re.findall(r"\bimport\s+(\w+)\s+from\s*[\"']fastify[\"']", source),
        *re.findall(
            r"\b(?:const|let)\s+(\w+)\s*=\s*require\s*\(\s*[\"']fastify[\"']\s*\)", source,
        ),
    }
    receivers: set[str] = set()
    for factory in factories:
        receivers.update(re.findall(
            rf"\b(?:const|let|var)\s+(\w+)\s*=\s*{re.escape(factory)}\s*\(", source,
        ))
    return frozenset(receivers)
