"""Persist and reconstruct one service's typed canonical static snapshot."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from orbitkb.db.repositories import services as services_repo
from orbitkb.db.repositories._util import now
from orbitkb.domain.canonical import (
    CanonicalFact,
    CanonicalSnapshot,
    CapabilityKey,
    CloudResourceKey,
    EntrypointKey,
    FactStatus,
    MessageChannelKey,
    PersistenceResourceKey,
    RoutePatternKey,
    ServiceKey,
    SourceReference,
    SymbolKey,
)

_SUBJECT_TYPES = {
    cls.__name__: cls for cls in (
        EntrypointKey, SymbolKey, RoutePatternKey, MessageChannelKey,
        CapabilityKey, PersistenceResourceKey, CloudResourceKey,
    )
}
_FORMAT_VERSION = 1


def service_key(conn: sqlite3.Connection, service_id: int) -> ServiceKey:
    row = services_repo.get_service_by_id(conn, service_id)
    if row is None:
        raise KeyError(f"service {service_id} does not exist")
    return ServiceKey(row["name"], repository=row["repository_name"])


def has_snapshot(conn: sqlite3.Connection, service_id: int) -> bool:
    row = conn.execute(
        "SELECT service_name, repository_name FROM canonical_snapshots WHERE service_id = ?", (service_id,),
    ).fetchone()
    return row is not None and ServiceKey(row["service_name"], row["repository_name"]) == service_key(conn, service_id)


def replace_snapshot(conn: sqlite3.Connection, service_id: int, snapshot: CanonicalSnapshot) -> None:
    if snapshot.service != service_key(conn, service_id):
        raise ValueError("canonical snapshot service identity does not match database service")
    if len({fact.id for fact in snapshot.facts}) != len(snapshot.facts):
        raise ValueError("canonical snapshot contains duplicate fact IDs")
    if any(fact.subject.service != snapshot.service for fact in snapshot.facts):
        raise ValueError("canonical fact service identity does not match snapshot")
    payload = {
        "format_version": _FORMAT_VERSION,
        "service": asdict(snapshot.service),
        "facts": [
            {**asdict(fact), "subject_type": type(fact.subject).__name__}
            for fact in snapshot.facts
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    conn.execute(
        """INSERT INTO canonical_snapshots (service_id, service_name, repository_name, payload_json, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(service_id) DO UPDATE SET
             service_name = excluded.service_name, repository_name = excluded.repository_name,
             payload_json = excluded.payload_json, updated_at = excluded.updated_at""",
        (service_id, snapshot.service.value, snapshot.service.repository, encoded, now()),
    )


def read_snapshot(conn: sqlite3.Connection, service_id: int) -> CanonicalSnapshot | None:
    row = conn.execute(
        "SELECT payload_json FROM canonical_snapshots WHERE service_id = ?", (service_id,),
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload_json"])
    if payload.get("format_version") != _FORMAT_VERSION:
        raise ValueError("unsupported canonical snapshot format")
    service = ServiceKey(**payload["service"])
    if service != service_key(conn, service_id):
        raise ValueError("canonical snapshot service identity does not match database service")
    facts: list[CanonicalFact] = []
    for item in payload["facts"]:
        subject_type = _SUBJECT_TYPES[item["subject_type"]]
        subject_data = dict(item["subject"])
        subject_data["service"] = ServiceKey(**subject_data["service"])
        fact = CanonicalFact(
            id=item["id"], kind=item["kind"], subject=subject_type(**subject_data),
            attributes=item["attributes"], status=FactStatus(item["status"]),
            origin=item["origin"], sources=tuple(SourceReference(**source) for source in item["sources"]),
        )
        if fact.subject.service != service:
            raise ValueError("canonical fact service identity does not match snapshot")
        facts.append(fact)
    if len({fact.id for fact in facts}) != len(facts):
        raise ValueError("canonical snapshot contains duplicate fact IDs")
    return CanonicalSnapshot(service, tuple(facts))
