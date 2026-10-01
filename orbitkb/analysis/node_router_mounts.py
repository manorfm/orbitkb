"""Promote locally parsed Express routes with proven external mounts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from orbitkb.analysis.models import AnalysisResult
from orbitkb.analysis.route_paths import join_route
from orbitkb.discovery.node_mounts import cross_file_express_mounts


def resolve_node_router_mounts(result: AnalysisResult, files: list[Path], root: Path) -> None:
    """Promote candidates only when import, export and mount identify one route prefix."""
    if not result.pending_node_routes:
        return
    mounts = cross_file_express_mounts(files, root)
    for candidate in result.pending_node_routes:
        path = (root / candidate.entrypoint.evidence.file_path).resolve()
        prefix = mounts.get((path, candidate.receiver))
        if prefix is None:
            continue
        route = join_route(prefix, candidate.entrypoint.name)
        result.entrypoints.append(replace(candidate.entrypoint, name=route))
    result.pending_node_routes.clear()
