"""Language-neutral identities and evidence-bearing facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ServiceKey:
    value: str
    repository: str | None = None

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("service key cannot be empty")
        if self.repository is not None and not self.repository.strip():
            raise ValueError("repository key cannot be empty")


@dataclass(frozen=True)
class EntrypointKey:
    service: ServiceKey
    transport: str
    method: str
    name: str
    symbol: str

    @property
    def fact_id(self) -> str:
        identity = [self.service.repository, self.service.value, "entrypoint", self.transport,
                    self.method, self.name, self.symbol]
        encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return "entrypoint:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SourceReference:
    file_path: str
    start_line: int
    end_line: int


class FactStatus(str, Enum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class CanonicalFact:
    id: str
    kind: str
    subject: EntrypointKey
    attributes: dict
    status: FactStatus
    origin: str
    sources: tuple[SourceReference, ...]


@dataclass(frozen=True)
class CanonicalSnapshot:
    service: ServiceKey
    facts: tuple[CanonicalFact, ...]
