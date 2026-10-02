"""Per-file source analysis contract used by the static analysis pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from orbitkb.analysis.models import AnalysisResult

BUILTIN_MESSAGING_STACKS = frozenset({"go", "jvm-spring", "node-ts", "node-js"})


class LanguageFrontend(Protocol):
    file_patterns: tuple[str, ...]
    supported_capabilities: frozenset[str]

    def analyze_file(self, path: Path, root: Path) -> AnalysisResult: ...


class FlowClassifier(Protocol):
    """Classify source-proven edges before shared symbol resolution."""

    def classify(self, result: AnalysisResult, files: list[Path]) -> None: ...


class FrameworkAdapter(Protocol):
    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None: ...


@dataclass(frozen=True)
class CombinedFrameworkAdapter:
    adapters: tuple[FrameworkAdapter, ...]

    def enrich(self, result: AnalysisResult, files: list[Path], root: Path) -> None:
        for adapter in self.adapters:
            adapter.enrich(result, files, root)


@dataclass(frozen=True)
class AnalyzerFrontend:
    """Connect an existing file analyzer to the shared source pipeline."""

    file_patterns: tuple[str, ...]
    analyze: Callable[[Path, Path], AnalysisResult]
    supported_capabilities: frozenset[str] = frozenset()

    def analyze_file(self, path: Path, root: Path) -> AnalysisResult:
        return self.analyze(path, root)
