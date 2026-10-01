"""Shared local module resolution and Node/TS named-import parsing.

Analysis and cloud detection use the same named-import facts. Route handlers
and router mounts use the same bounded local path resolution.
"""
from __future__ import annotations

import re
from collections.abc import Collection
from pathlib import Path

_IMPORT_RE = re.compile(r"import\s*\{([^}]+)\}\s*from\s*[\"']([^\"']+)[\"']")
_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")


def resolve_local_source(
    importer: Path, module: str, root: Path, sources: Collection[Path] | None = None,
    suffixes: tuple[str, ...] = _SOURCE_SUFFIXES,
) -> Path | None:
    """Resolve one local source path, rejecting escapes and ambiguous extensions."""
    if not module.startswith(("./", "../")):
        return None
    root = root.resolve()
    base = importer.parent / module
    candidates = (base,) if base.suffix in suffixes else tuple(Path(f"{base}{suffix}") for suffix in suffixes)
    matches = [
        resolved for candidate in candidates
        if (resolved := candidate.resolve()).is_relative_to(root)
        and resolved.is_file()
        and (sources is None or resolved in sources)
    ]
    return matches[0] if len(matches) == 1 else None


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
