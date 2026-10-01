"""Prove literal CommonJS Express router mounts across local source files."""

from __future__ import annotations

import re
from pathlib import Path

from orbitkb.discovery.node_http import express_receivers

_IMPORT_RE = re.compile(
    r"\b(?:const|let|var)\s+(\w+)\s*=\s*require\s*\(\s*['\"](\.{1,2}/[^'\"]+)['\"]\s*\)"
)
_MOUNT_RE = re.compile(r"\b(\w+)\.use\s*\(\s*(['\"])([^'\"]+)\2\s*,\s*(\w+)\s*\)")
_EXPORT_RE = re.compile(r"^\s*module\.exports\s*=\s*(\w+)\s*;?\s*$", re.MULTILINE)
_SOURCE_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")


def _imported_source(importer: Path, module: str, sources: frozenset[Path], root: Path) -> Path | None:
    base = (importer.parent / module).resolve()
    if not base.is_relative_to(root):
        return None
    candidates = (base,) if base.suffix in _SOURCE_SUFFIXES else tuple(Path(f"{base}{suffix}") for suffix in _SOURCE_SUFFIXES)
    return next((path for path in candidates if path in sources), None)


def cross_file_express_mounts(files: list[Path], root: Path) -> dict[tuple[Path, str], str]:
    """Map exported routers to their single proven external mount prefix."""
    root = root.resolve()
    sources = frozenset(path.resolve() for path in files if path.suffix in _SOURCE_SUFFIXES)
    mounts: dict[Path, list[str]] = {}
    exports: dict[Path, frozenset[str]] = {}
    for path in sources:
        source = path.read_text(encoding="utf-8", errors="ignore")
        exports[path] = frozenset(_EXPORT_RE.findall(source))
        applications, _ = express_receivers(source)
        if not applications:
            continue
        imports = {
            alias: imported
            for alias, module in _IMPORT_RE.findall(source)
            if (imported := _imported_source(path, module, sources, root)) is not None
        }
        for application, _quote, prefix, alias in _MOUNT_RE.findall(source):
            if application in applications and alias in imports:
                mounts.setdefault(imports[alias], []).append(prefix)

    proven: dict[tuple[Path, str], str] = {}
    for path, prefixes in mounts.items():
        if len(prefixes) != 1 or len(exports[path]) != 1:
            continue
        receiver = next(iter(exports[path]))
        proven[path, receiver] = prefixes[0]
    return proven
