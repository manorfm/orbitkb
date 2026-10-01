"""Shared Node/TS named-import parsing. Both `engine.py` (general symbol/
call resolution) and `cloud_detection.py` (SDK-import verification) need the
same "which module did this locally-bound name come from" fact — this is the
one place that regex lives, instead of two near-identical copies drifting
apart from each other.
"""
from __future__ import annotations

import re
from pathlib import Path

_IMPORT_RE = re.compile(r"import\s*\{([^}]+)\}\s*from\s*[\"']([^\"']+)[\"']")


def parse_node_named_import_declarations(source: str) -> list[tuple[str, str, str]]:
    """Return (local name, exact module, original name) for named imports."""
    parsed: list[tuple[str, str, str]] = []
    for names, module in _IMPORT_RE.findall(source):
        for item in names.split(","):
            original, _as, local = item.strip().partition(" as ")
            original = original.strip()
            if original:
                parsed.append(((local or original).strip(), module, original))
    return parsed


def parse_node_named_imports(source: str) -> list[tuple[str, str, str]]:
    """Return (local name, module basename, original name) for named imports."""
    return [
        (local, Path(module).name, original)
        for local, module, original in parse_node_named_import_declarations(source)
    ]
