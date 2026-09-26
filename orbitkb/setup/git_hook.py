"""Installs a non-blocking `post-commit`/`post-merge` git hook that runs
`orbitkb update --repository <name>` in the background after every commit or
merge, so an indexed repository's knowledge stays close to current without a
human remembering to run it by hand.

The hook never blocks `git commit`/`git merge` (reindexing can involve real
LLM cost and time even with the incremental file-hash skip already in place),
and it never overwrites a hook a team already has (husky, pre-commit
framework, a hand-written script): see `apply_marked_block`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from orbitkb.setup.actions import SetupAction
from orbitkb.setup.marked_block import apply_marked_block

HOOK_MARKER_BEGIN = "# orbitkb:begin (managed by `orbitkb setup` — do not edit by hand)"
HOOK_MARKER_END = "# orbitkb:end"
_HOOK_NAMES = ("post-commit", "post-merge")


def resolve_git_dir(repository_root: Path) -> Path | None:
    """The real git directory for `repository_root`, resolving worktrees where
    `.git` is a file pointing elsewhere. `None` when it isn't a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repository_root, capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    git_dir = result.stdout.strip()
    if not git_dir:
        return None
    path = Path(git_dir)
    return path if path.is_absolute() else (repository_root / path).resolve()


def hook_block(repository_name: str, db_path: Path, log_path: Path) -> str:
    return (
        f'nohup orbitkb update --repository "{repository_name}" --db "{db_path}" '
        f'>> "{log_path}" 2>&1 &'
    )


def install_hook(hook_path: Path, block_text: str, *, dry_run: bool = False) -> SetupAction:
    action = apply_marked_block(
        hook_path, block_text, HOOK_MARKER_BEGIN, HOOK_MARKER_END,
        category="hook", client="git", header="#!/bin/sh\n\n", dry_run=dry_run,
    )
    if action.status in ("created", "updated") and not dry_run:
        hook_path.chmod(hook_path.stat().st_mode | 0o111)
    return action


def install_repository_hooks(
    repository_root: Path, repository_name: str, db_path: Path, *, dry_run: bool = False,
) -> list[SetupAction]:
    """Install the reindex hook into `post-commit` and `post-merge`. Returns one
    `SetupAction` per hook, or a single "skipped" action when `repository_root`
    isn't a git repository."""
    git_dir = resolve_git_dir(repository_root)
    if git_dir is None:
        return [
            SetupAction(
                category="hook", client="git", scope="", path=repository_root, status="skipped",
                detail="not a git repository; the reindex hook was not installed",
            )
        ]
    log_path = git_dir / "orbitkb-update.log"
    block = hook_block(repository_name, db_path, log_path)
    hooks_dir = git_dir / "hooks"
    return [install_hook(hooks_dir / name, block, dry_run=dry_run) for name in _HOOK_NAMES]
