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
