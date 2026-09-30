"""Stable, confined output directories shared by documentation exporters."""

import re
import sqlite3
from collections import Counter
from pathlib import Path


def _component(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        return value
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.") or "service"


def service_output_dirs(out_dir: Path, services: list[sqlite3.Row]) -> dict[int, Path]:
    """Preserve simple names; qualify homonyms and avoid path traversal/collisions."""
    counts = Counter(service["name"] for service in services)
    used = {"topology.mmd"}
    paths: dict[int, Path] = {}
    for service in services:
        name = service["name"]
        base = _component(name)
        if counts[name] > 1:
            base = f"{_component(service['repository_name'] or 'standalone')}--{base}"
        candidate = base
        suffix = 1
        while candidate.casefold() in used:
            candidate = f"{base}--{service['id']}" + (f"-{suffix}" if suffix > 1 else "")
            suffix += 1
        used.add(candidate.casefold())
        paths[service["id"]] = out_dir / candidate
    return paths
