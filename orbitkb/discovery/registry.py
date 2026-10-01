from __future__ import annotations

from collections.abc import Sequence

from orbitkb.discovery.base import StackDetector
from orbitkb.discovery.go_stack import GoDetector
from orbitkb.discovery.jvm_stack import JvmSpringDetector
from orbitkb.discovery.node_ts import NodeTsDetector
from orbitkb.discovery.python_stack import PythonDetector

# Order matters only in that the first matching detector "claims" a folder.
# Add a new stack by writing one StackDetector implementation and appending it here.
DETECTORS: list[StackDetector] = [
    NodeTsDetector(),
    PythonDetector(),
    JvmSpringDetector(),
    GoDetector(),
]


def detector_for(folder, detectors: Sequence[StackDetector] | None = None) -> StackDetector | None:
    available = DETECTORS if detectors is None else detectors
    for detector in available:
        if detector.matches(folder):
            return detector
    return None


def detector_by_id(stack_id: str, detectors: Sequence[StackDetector] | None = None) -> StackDetector | None:
    """Explicit lookup by stack id, bypassing matches() entirely — the escape hatch
    for a folder whose shape no heuristic recognizes (e.g. a library/CLI package
    with its manifest at the repo root and source in a subdirectory) but whose stack
    the caller already knows for certain. See `orbitkb index --stack`."""
    available = DETECTORS if detectors is None else detectors
    return next((detector for detector in available if detector.id == stack_id), None)
