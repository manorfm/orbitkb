"""Writes an idempotent instruction block teaching any MCP-connected coding
agent (Claude Code, Codex CLI, Cursor, ...) to check `freshness` before
trusting `find_change_surface`/`plan_change`/`get_change_context` for a
repository, and how to reindex it when stale.

Written to both `AGENTS.md` and `CLAUDE.md`, not just one: Claude Code only
reads `AGENTS.md` as a fallback when a repository has no `CLAUDE.md` of its
own, so relying on `AGENTS.md` alone would silently stop working the moment a
team added its own `CLAUDE.md`. Writing both guarantees every client sees the
block regardless of which instruction file the team already maintains — see
`orbitkb-plans/agent-connection-setup/plan.md` (WP1) for the sourced findings
behind this decision.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.setup.actions import SetupAction
from orbitkb.setup.marked_block import apply_marked_block, remove_marked_block

INSTRUCTIONS_MARKER_BEGIN = "<!-- ORBITKB:START (managed by `orbitkb setup` — do not edit by hand) -->"
INSTRUCTIONS_MARKER_END = "<!-- ORBITKB:END -->"
_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")


def instruction_block(repository_name: str, db_path: Path) -> str:
    update_command = f'orbitkb update --repository "{repository_name}" --db "{db_path}"'
    return (
        "## OrbitKB — keep this repository's knowledge fresh\n\n"
        f"This repository is indexed by OrbitKB (repository `{repository_name}`), an MCP server "
        "that provides bounded, evidence-backed context before you plan a code change.\n\n"
        "Before trusting the result of `find_change_surface`, `plan_change` or `get_change_context` "
        "for this repository, check the `freshness` field of every relevant service in the response.\n\n"
        "If any relevant service reports `freshness.stale == true`, its indexed knowledge may be out "
        f"of date. Run `{update_command}` before relying on the plan — or tell the user the index is "
        "stale if you don't have shell access."
    )


def write_agent_instructions(
    repository_root: Path, repository_name: str, db_path: Path, *, dry_run: bool = False,
) -> list[SetupAction]:
    block = instruction_block(repository_name, db_path)
    return [
        apply_marked_block(
            repository_root / filename, block, INSTRUCTIONS_MARKER_BEGIN, INSTRUCTIONS_MARKER_END,
            category="instructions", client=filename.lower(), dry_run=dry_run,
        )
        for filename in _INSTRUCTION_FILES
    ]


def remove_agent_instructions(repository_root: Path, *, dry_run: bool = False) -> list[SetupAction]:
    """Remove the orbitkb block from `AGENTS.md`/`CLAUDE.md`, installed by
    `write_agent_instructions`. Never touches a file without the marker: that
    means it wasn't written by this tool."""
    return [
        remove_marked_block(
            repository_root / filename, INSTRUCTIONS_MARKER_BEGIN, INSTRUCTIONS_MARKER_END,
            category="instructions", client=filename.lower(), dry_run=dry_run,
        )
        for filename in _INSTRUCTION_FILES
    ]
