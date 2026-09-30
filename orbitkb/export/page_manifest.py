"""Track generated API pages without claiming ownership of user-edited files."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

MANIFEST_NAME = ".orbitkb-pages.json"
_PAGE_NAME = re.compile(r"[a-z0-9][a-z0-9-]*\.md\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


def page_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def owned_pages(api_dir: Path) -> dict[str, str]:
    """Return manifest entries only while their regular files remain unchanged."""
    manifest = api_dir / MANIFEST_NAME
    if manifest.is_symlink():
        raise ValueError(f"API page manifest must not be a symlink: {manifest}")
    if not manifest.exists():
        return {}
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("pages"), dict):
        raise ValueError(f"invalid API page manifest: {manifest}")
    owned: dict[str, str] = {}
    for name, digest in payload["pages"].items():
        if not isinstance(name, str) or not _PAGE_NAME.fullmatch(name):
            continue
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            continue
        path = api_dir / name
        if path.is_file() and not path.is_symlink() and page_digest(path.read_bytes()) == digest:
            owned[name] = digest
    return owned


def save_owned_pages(api_dir: Path, previous: dict[str, str], current: dict[str, str]) -> None:
    """Remove only unchanged obsolete pages, then atomically record current pages."""
    for name in previous.keys() - current.keys():
        path = api_dir / name
        if path.is_file() and not path.is_symlink() and page_digest(path.read_bytes()) == previous[name]:
            path.unlink()
    payload = {"version": 1, "pages": dict(sorted(current.items()))}
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=api_dir, prefix=".orbitkb-pages-", suffix=".tmp", delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            json.dump(payload, temp, sort_keys=True)
            temp.write("\n")
        os.replace(temp_path, api_dir / MANIFEST_NAME)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
