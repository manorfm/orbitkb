"""Shared projection of source-proven HTTP targets not represented by indexed calls."""

from collections.abc import Iterable, Mapping


def unresolved_declared_http_targets(
    static_calls: Iterable[Mapping], represented_targets: Iterable[str],
) -> tuple[str, ...]:
    """Keep a declared target once, without claiming its runtime destination is known."""
    represented = set(represented_targets)
    return tuple(sorted({
        call["target_service"] for call in static_calls
        if call["protocol"] == "http" and call["target_service"] not in represented
    }))
