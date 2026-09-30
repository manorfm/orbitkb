"""Source-backed persistence access shared by document and diagram exports."""

import re
from collections.abc import Iterable

from orbitkb.domain.canonical import CanonicalSnapshot, FactStatus


def has_unrepresented_mongo_access(snapshot: CanonicalSnapshot | None, engines: Iterable[str]) -> bool:
    """Require a confirmed call on an injected Mongo template and no known Mongo engine."""
    if snapshot is None or any(
        re.sub(r"[^a-z0-9]+", "_", engine.lower()).strip("_") in {"mongo", "mongodb"}
        for engine in engines
    ):
        return False
    receivers = {
        fact.subject.name
        for fact in snapshot.facts
        if fact.kind == "injection"
        and fact.status is FactStatus.CONFIRMED
        and fact.attributes.get("contract", "").split("<", 1)[0].rsplit(".", 1)[-1]
        in {"MongoTemplate", "ReactiveMongoTemplate"}
    }
    for fact in snapshot.facts:
        if (fact.kind != "flow_edge" or fact.status is not FactStatus.CONFIRMED
                or fact.attributes.get("boundary_kind") != "persistence"
                or fact.attributes.get("relation") not in {"reads", "writes", "invokes"}):
            continue
        target = fact.attributes.get("target")
        if not isinstance(target, str):
            continue
        owner, owner_separator, _ = fact.subject.name.rpartition(".")
        receiver, receiver_separator, _ = target.rpartition(".")
        if owner_separator and receiver_separator and f"{owner}.{receiver}" in receivers:
            return True
    return False
