"""Source-backed method units shared by every route that reaches them."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from orbitkb.discovery.base import CodeExcerpt
from orbitkb.domain.canonical import CanonicalFact, CanonicalSnapshot, SymbolKey
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.generation.route_evidence import route_entrypoints


@dataclass(frozen=True)
class SourceUnit:
    unit_key: str
    fact_id: str
    excerpt: CodeExcerpt
    digest: str


class SourceUnitCollector:
    def __init__(self, snapshot: CanonicalSnapshot, root: Path):
        self.navigator = KnowledgeNavigator(snapshot)
        self.root = root.resolve()
        self._units: dict[str, SourceUnit] = {}
        self._lines: dict[Path, list[str]] = {}

    def for_route(self, method: str, path: str) -> tuple[SourceUnit, ...]:
        units: dict[str, SourceUnit] = {}
        for entrypoint in route_entrypoints(self.navigator.snapshot, method, path):
            for fact in self.navigator.reachable(entrypoint, TraversalPolicy()).facts:
                if fact.kind != "symbol" or not fact.sources:
                    continue
                unit = self._units.get(fact.id)
                if unit is None:
                    unit = self._read_unit(fact)
                    if unit is not None:
                        self._units[fact.id] = unit
                if unit is not None:
                    units[fact.id] = unit
        return tuple(units.values())

    def _read_unit(self, fact: CanonicalFact) -> SourceUnit | None:
        if not isinstance(fact.subject, SymbolKey):
            return None
        source = fact.sources[0]
        path = (self.root / source.file_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            return None
        if source.start_line < 1 or source.end_line < source.start_line:
            return None
        if path not in self._lines:
            self._lines[path] = path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = self._lines[path]
        if source.end_line > len(lines):
            return None
        text = "\n".join(lines[source.start_line - 1:source.end_line])
        identity = json.dumps([fact.subject.name, source.file_path], ensure_ascii=False, separators=(",", ":"))
        unit_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        payload = json.dumps([unit_key, text], ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return SourceUnit(
            unit_key, fact.id, CodeExcerpt(source.file_path, source.start_line, source.end_line, text), digest,
        )
