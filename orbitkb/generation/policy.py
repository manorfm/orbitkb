"""Incremental generation decisions shared by aggregate units."""

from collections.abc import Set as AbstractSet
from dataclasses import dataclass


@dataclass(frozen=True)
class GenerationPolicy:
    force: bool
    is_new: bool
    changed: AbstractSet[str]
    removed: AbstractSet[str]

    def should_regenerate_aggregate(self, source_files: AbstractSet[str]) -> bool:
        return (
            self.force or self.is_new
            or not self.changed.isdisjoint(source_files)
            or not self.removed.isdisjoint(source_files)
        )
