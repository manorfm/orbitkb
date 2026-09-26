"""orbitkb.setup.agent_instructions: teaches any MCP-connected coding agent to
check `freshness` before trusting `find_change_surface`/`plan_change`, and how
to reindex when it's stale.

Written to both AGENTS.md and CLAUDE.md (not just one) because Claude Code
only falls back to AGENTS.md when a repository has no CLAUDE.md of its own —
see `orbitkb-plans/agent-connection-setup/plan.md` WP1. Writing both guarantees
Claude Code, Codex CLI and Cursor all see the block regardless of which
instruction file the team already maintains.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.setup.agent_instructions import (
    remove_agent_instructions,
    write_agent_instructions,
)


def test_writes_block_to_both_agents_md_and_claude_md(tmp_path: Path):
    actions = write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert {a.status for a in actions} == {"created"}
    assert {a.path.name for a in actions} == {"AGENTS.md", "CLAUDE.md"}
    for filename in ("AGENTS.md", "CLAUDE.md"):
        content = (tmp_path / filename).read_text()
        assert 'orbitkb update --repository "shop"' in content
        assert str(tmp_path / "orbitkb.db") in content
        assert "freshness" in content


def test_updating_preserves_surrounding_team_content(tmp_path: Path):
    write_agent_instructions(tmp_path, "shop", tmp_path / "old.db")
    for filename, note in (("AGENTS.md", "Always run lint first."), ("CLAUDE.md", "Use TDD.")):
        path = tmp_path / filename
        path.write_text(f"# Team notes\n\n{note}\n\n{path.read_text()}")

    actions = write_agent_instructions(tmp_path, "shop", tmp_path / "new.db")

    assert {a.status for a in actions} == {"updated"}
    assert "Always run lint first." in (tmp_path / "AGENTS.md").read_text()
    assert "Use TDD." in (tmp_path / "CLAUDE.md").read_text()
    assert str(tmp_path / "new.db") in (tmp_path / "AGENTS.md").read_text()
    assert str(tmp_path / "old.db") not in (tmp_path / "AGENTS.md").read_text()


def test_is_idempotent(tmp_path: Path):
    write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")

    actions = write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert {a.status for a in actions} == {"unchanged"}


def test_never_overwrites_a_file_with_unrelated_content_and_no_marker(tmp_path: Path):
    foreign = "# Team notes\n\nNo orbitkb block here.\n"
    (tmp_path / "AGENTS.md").write_text(foreign)

    actions = write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert (tmp_path / "AGENTS.md").read_text() == foreign
    agents_action = next(a for a in actions if a.path.name == "AGENTS.md")
    assert agents_action.status == "conflict"
    claude_action = next(a for a in actions if a.path.name == "CLAUDE.md")
    assert claude_action.status == "created"


def test_dry_run_writes_nothing(tmp_path: Path):
    actions = write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db", dry_run=True)

    assert {a.status for a in actions} == {"created"}
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_remove_deletes_files_that_had_only_the_orbitkb_block(tmp_path: Path):
    write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")

    actions = remove_agent_instructions(tmp_path)

    assert {a.status for a in actions} == {"removed"}
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_remove_preserves_surrounding_team_content(tmp_path: Path):
    write_agent_instructions(tmp_path, "shop", tmp_path / "orbitkb.db")
    for filename, note in (("AGENTS.md", "Always run lint first."), ("CLAUDE.md", "Use TDD.")):
        path = tmp_path / filename
        path.write_text(f"# Team notes\n\n{note}\n\n{path.read_text()}")

    remove_agent_instructions(tmp_path)

    assert "Always run lint first." in (tmp_path / "AGENTS.md").read_text()
    assert "Use TDD." in (tmp_path / "CLAUDE.md").read_text()
    assert "ORBITKB" not in (tmp_path / "AGENTS.md").read_text()


def test_remove_never_touches_a_file_without_the_marker(tmp_path: Path):
    foreign = "# Team notes\n\nNo orbitkb block here.\n"
    (tmp_path / "AGENTS.md").write_text(foreign)

    actions = remove_agent_instructions(tmp_path)

    assert (tmp_path / "AGENTS.md").read_text() == foreign
    agents_action = next(a for a in actions if a.path.name == "AGENTS.md")
    assert agents_action.status == "skipped"
