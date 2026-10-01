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


def fact_id(service: ServiceKey, kind: str, *identity: str | int | bool | None) -> str:
    fields = [service.repository, service.value, kind, *identity]
    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return kind + ":" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EntrypointKey:
    service: ServiceKey
    transport: str
    method: str
    name: str
    symbol: str

    @property
    def fact_id(self) -> str:
        return fact_id(self.service, "entrypoint", self.transport, self.method, self.name, self.symbol)


@dataclass(frozen=True)
class SymbolKey:
    service: ServiceKey
    name: str


@dataclass(frozen=True)
class RoutePatternKey:
    service: ServiceKey
    method: str | None
    pattern: str


@dataclass(frozen=True)
class MessageChannelKey:
    service: ServiceKey
    channel: str


@dataclass(frozen=True)
class CapabilityKey:
    service: ServiceKey
    dimension: str


@dataclass(frozen=True)
class PersistenceResourceKey:
    service: ServiceKey
    kind: str
    name: str


@dataclass(frozen=True)
class CloudResourceKey:
    service: ServiceKey
    provider: str
    resource_type: str
    target_name: str | None


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


def _json_value(value):
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical attribute keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError(f"unsupported canonical attribute: {type(value).__name__}")


@dataclass(frozen=True)
class CanonicalFact:
    id: str
    kind: str
    subject: EntrypointKey | SymbolKey | RoutePatternKey | MessageChannelKey | CapabilityKey | PersistenceResourceKey | CloudResourceKey
    attributes: dict
    status: FactStatus
    origin: str
    sources: tuple[SourceReference, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "attributes", _json_value(self.attributes))


@dataclass(frozen=True)
class CanonicalSnapshot:
    service: ServiceKey
    facts: tuple[CanonicalFact, ...]
