"""Source-proven HTTP targets for service and route documentation."""

from collections.abc import Iterable, Mapping
from itertools import chain

from orbitkb.domain.canonical import CanonicalSnapshot, FactStatus


def unresolved_declared_http_targets(
    static_calls: Iterable[Mapping], indexed_calls: Iterable[Mapping],
    snapshot: CanonicalSnapshot | None = None,
) -> tuple[str, ...]:
    """Keep a declared target once, without claiming its runtime destination is known."""
    represented = {
        call["to_service_name"] for call in indexed_calls if call["call_kind"] == "http"
    }
    canonical_calls = (
        fact.attributes for fact in (snapshot.facts if snapshot is not None else ())
        if fact.kind == "service_call" and fact.status is FactStatus.CONFIRMED
    )
    return tuple(sorted({
        call["target_service"] for call in chain(static_calls, canonical_calls)
        if call["protocol"] == "http" and isinstance(call["target_service"], str)
        and call["target_service"].strip() and call["target_service"] not in represented
    }))
