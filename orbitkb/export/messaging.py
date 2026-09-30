"""Source-backed messaging facts shared by document and diagram exports."""

from orbitkb.domain.canonical import CanonicalSnapshot, FactStatus


def has_confirmed_redis_publication(snapshot: CanonicalSnapshot | None) -> bool:
    return snapshot is not None and any(
        fact.kind == "flow_edge"
        and fact.status is FactStatus.CONFIRMED
        and fact.attributes.get("relation") == "publishes"
        and fact.attributes.get("boundary_kind") == "redis_pubsub"
        for fact in snapshot.facts
    )
