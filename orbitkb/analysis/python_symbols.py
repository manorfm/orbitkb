"""Stable identities for Python declarations inside an indexed source root."""
from __future__ import annotations

from pathlib import Path


def module_name(path: Path, root: Path) -> str:
    return ".".join(path.relative_to(root).with_suffix("").parts)
