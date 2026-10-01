from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from orbitkb.discovery.base import StackDetector
from orbitkb.discovery.registry import detector_for
from orbitkb.discovery.scan_helpers import SKIP_DIRS

logger = logging.getLogger(__name__)


@dataclass
class ServiceCandidate:
    name: str
    path: Path
    detector: StackDetector


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return slug or "service"


def discover_services(root: Path, detectors: Sequence[StackDetector] | None = None) -> list[ServiceCandidate]:
    """Find every microservice under root.

    If root itself is a service (single-repo mode), returns just that one.
    Otherwise walks top-down and treats the first matching folder on each branch
    as a service boundary, without descending further into it (so nested vendored
    code never gets mistaken for a second service).
    """
    logger.debug("detecting stack: %s", root)
    root = root.resolve()
    detector = detector_for(root, detectors)
    if detector is not None:
        logger.debug("detected stack: %s (%s)", detector.id, root)
        return [ServiceCandidate(name=slugify(root.name), path=root, detector=detector)]

    candidates: list[ServiceCandidate] = []
    for dirpath, dirnames, _filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if current == root:
            continue
        found = detector_for(current, detectors)
        if found is not None:
            logger.debug("detected stack: %s (%s)", found.id, current)
            candidates.append(ServiceCandidate(name=slugify(current.name), path=current, detector=found))
            dirnames[:] = []

    return _dedupe_names(candidates)


def _dedupe_names(candidates: list[ServiceCandidate]) -> list[ServiceCandidate]:
    seen: dict[str, int] = {}
    for c in candidates:
        seen[c.name] = seen.get(c.name, 0) + 1
    if all(count == 1 for count in seen.values()):
        return candidates
    result: list[ServiceCandidate] = []
    used: set[str] = set()
    for c in candidates:
        name = c.name
        if seen[c.name] > 1:
            name = f"{c.path.parent.name}-{c.name}".strip("-").lower()
            name = slugify(name)
        base = name
        i = 2
        while name in used:
            name = f"{base}-{i}"
            i += 1
        used.add(name)
        result.append(ServiceCandidate(name=name, path=c.path, detector=c.detector))
    return result
