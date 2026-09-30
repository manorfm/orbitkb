"""Prompt excerpts and the source pointers actually shown to generation."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from orbitkb.security.redaction import redact_sensitive_values


class SourceExcerpt(Protocol):
    file_path: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class EvidenceSource:
    prompt_text: str
    pointers: list[dict]

    @classmethod
    def from_excerpts(cls, excerpts: Sequence[SourceExcerpt], max_chars: int) -> "EvidenceSource":
        if max_chars < 0:
            raise ValueError("excerpt budget must be nonnegative")
        parts: list[str] = []
        pointers: list[dict] = []
        total = 0
        last_file: str | None = None
        last_start = last_end = 0
        last_text = ""
        for excerpt in excerpts:
            safe_text = redact_sensitive_values(excerpt.text)
            adjacent = (
                bool(parts) and excerpt.file_path == last_file
                and excerpt.start_line == last_end + 1
            )
            block_text = f"{last_text}\n{safe_text}" if adjacent else safe_text
            block_start = last_start if adjacent else excerpt.start_line
            block = f"--- {excerpt.file_path} (lines {block_start}-{excerpt.end_line}) ---\n{block_text}\n"
            added_chars = len(block) - len(parts[-1]) if adjacent else len(block)
            if total + added_chars > max_chars:
                parts.append("... (truncated, excerpt budget reached)")
                break
            if adjacent:
                parts[-1] = block
            else:
                parts.append(block)
            pointers.append({
                "file": excerpt.file_path,
                "start_line": excerpt.start_line,
                "end_line": excerpt.end_line,
            })
            total += added_chars
            last_file = excerpt.file_path
            last_start = block_start
            last_end = excerpt.end_line
            last_text = block_text
        return cls("\n".join(parts) if parts else "(no excerpts found)", pointers)


@dataclass(frozen=True)
class AggregateEvidenceSource:
    primary: EvidenceSource
    configuration: EvidenceSource | None

    @classmethod
    def from_groups(
        cls, primary_excerpts: Sequence[SourceExcerpt], config_excerpts: Sequence[SourceExcerpt], max_chars: int,
    ) -> "AggregateEvidenceSource":
        return cls(
            EvidenceSource.from_excerpts(primary_excerpts, max_chars),
            EvidenceSource.from_excerpts(config_excerpts, max_chars) if config_excerpts else None,
        )

    @property
    def config_text(self) -> str:
        return self.configuration.prompt_text if self.configuration else "(none found)"

    @property
    def pointers(self) -> list[dict]:
        return [*self.primary.pointers, *(self.configuration.pointers if self.configuration else [])]
