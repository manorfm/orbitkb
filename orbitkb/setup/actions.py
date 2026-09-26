"""`SetupAction`: the outcome of one idempotent setup step (MCP registration,
git hook, or agent instruction block), shared across `orbitkb/setup/*` so the
`orbitkb setup` CLI command can render one consistent summary regardless of
which module produced the action.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# "created": the target file did not exist and was written (or would be, in dry-run).
# "updated": the target file existed and the orbitkb entry/block was added or changed.
# "unchanged": the target already had exactly this entry/block; nothing to do.
# "conflict": something else is already there and differs; nothing was touched (write path).
# "skipped": this action does not apply here (not a git repository, nothing to remove, ...).
# "removed": a previous orbitkb-managed entry/block was removed (or would be, in dry-run).
# "declined": something is there, but it doesn't look like ours to remove; left untouched.
Status = str


@dataclass(frozen=True)
class SetupAction:
    category: str  # "mcp" | "hook" | "instructions"
    client: str  # e.g. "claude-code", "cursor", "codex", "git", "agents.md", "claude.md"
    scope: str  # "project" | "user" | "" when scope does not apply
    path: Path
    status: Status
    detail: str = ""
