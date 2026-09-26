"""Idempotent, non-destructive "replace only what's between two marker lines"
in a text file — the shared rule behind the git hook (`git_hook.py`) and the
agent instruction block (`agent_instructions.py`): both are regenerated freely
by `orbitkb setup`, but never at the cost of a foreign file's own content.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.setup.actions import SetupAction


def apply_marked_block(
    path: Path, block_text: str, marker_begin: str, marker_end: str,
    *, category: str, client: str, header: str = "", dry_run: bool = False,
) -> SetupAction:
    """Ensure `path` contains exactly `block_text` between `marker_begin` and
    `marker_end`. Creates the file (with `header` prepended) when it doesn't
    exist yet. When it exists and already has both markers, replaces only the
    text between them — everything else in the file is preserved byte-for-byte.
    When it exists without the markers, does nothing and reports "conflict"
    with the block ready to paste manually: a file the caller doesn't own
    (a hand-written hook, a team's own AGENTS.md) is never overwritten.
    """
    wrapped = f"{marker_begin}\n{block_text.rstrip()}\n{marker_end}\n"

    if not path.exists():
        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{header}{wrapped}", encoding="utf-8")
        return SetupAction(category=category, client=client, scope="", path=path, status="created")

    existing = path.read_text(encoding="utf-8")
    begin_idx = existing.find(marker_begin)
    end_idx = existing.find(marker_end)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        return SetupAction(
            category=category, client=client, scope="", path=path, status="conflict",
            detail=f"{path} already exists without the orbitkb-managed block; paste this in manually:\n{wrapped}",
        )

    end_idx_after = end_idx + len(marker_end)
    current_block = existing[begin_idx:end_idx_after]
    if current_block == wrapped.rstrip("\n"):
        return SetupAction(category=category, client=client, scope="", path=path, status="unchanged")

    new_content = existing[:begin_idx] + wrapped.rstrip("\n") + existing[end_idx_after:]
    if not dry_run:
        path.write_text(new_content, encoding="utf-8")
    return SetupAction(category=category, client=client, scope="", path=path, status="updated")


def remove_marked_block(
    path: Path, marker_begin: str, marker_end: str,
    *, category: str, client: str, header: str = "", dry_run: bool = False,
) -> SetupAction:
    """Remove the orbitkb-managed block from `path`, preserving everything
    else. Deletes the file entirely when nothing but `header` (or nothing at
    all) would be left. Never touches a file that doesn't exist or doesn't
    have the marker: both mean this file wasn't ours to remove from.
    """
    if not path.exists():
        return SetupAction(category=category, client=client, scope="", path=path, status="skipped", detail="nothing to remove")

    existing = path.read_text(encoding="utf-8")
    begin_idx = existing.find(marker_begin)
    end_idx = existing.find(marker_end)
    if begin_idx == -1 or end_idx == -1 or end_idx < begin_idx:
        return SetupAction(
            category=category, client=client, scope="", path=path, status="skipped",
            detail=f"{path} has no orbitkb-managed block; left untouched",
        )

    remainder_start = end_idx + len(marker_end)
    if remainder_start < len(existing) and existing[remainder_start] == "\n":
        remainder_start += 1
    remaining = existing[:begin_idx] + existing[remainder_start:]
    body = remaining[len(header):] if header and remaining.startswith(header) else remaining
    if body.strip() == "":
        if not dry_run:
            path.unlink()
        return SetupAction(category=category, client=client, scope="", path=path, status="removed", detail="file removed (only orbitkb-managed content remained)")

    if not dry_run:
        path.write_text(remaining, encoding="utf-8")
    return SetupAction(category=category, client=client, scope="", path=path, status="removed")
