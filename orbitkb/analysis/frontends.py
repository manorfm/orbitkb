"""Per-file source analysis contract used by the static analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from orbitkb.analysis.models import AnalysisResult


class LanguageFrontend(Protocol):
    file_patterns: tuple[str, ...]

    def analyze_file(self, path: Path, root: Path) -> AnalysisResult: ...


class FrameworkAdapter(Protocol):
    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None: ...


@dataclass(frozen=True)
class AnalyzerFrontend:
    """Connect an existing file analyzer to the shared source pipeline."""

    file_patterns: tuple[str, ...]
    analyze: Callable[[Path, Path], AnalysisResult]

    def analyze_file(self, path: Path, root: Path) -> AnalysisResult:
        return self.analyze(path, root)
