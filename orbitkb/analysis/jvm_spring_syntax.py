"""Spring syntax shared by source parsing and cross-file recognizers."""

from __future__ import annotations

import re

from orbitkb.analysis.configuration_syntax import PROPERTY_CONFIGURATION_KEY

SPRING_ROUTE_ANNOTATION_TO_METHOD: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "PatchMapping": "PATCH",
    "DeleteMapping": "DELETE",
}

def spring_route_prefix(annotations: str) -> str | None:
    match = re.search(r'@RequestMapping\s*\(\s*(?:value\s*=\s*)?"([^"]+)"', annotations)
    return match.group(1) if match else None


def spring_placeholder_literal(group_name: str) -> str:
    """One whole-string `${key}` or `${key:default}` placeholder.

    Accept Java's plain string, Kotlin's backslash escape, and Kotlin's
    multi-dollar string. Composed values do not prove a single configuration key.
    """
    return rf'\$*"\\?\$\{{(?P<{group_name}>{PROPERTY_CONFIGURATION_KEY.pattern})(?::[^{{}}"]*)?\}}"'
