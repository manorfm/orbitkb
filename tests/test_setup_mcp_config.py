"""orbitkb.setup.mcp_config: idempotent, non-destructive MCP client registration.

Schemas asserted here were confirmed against real local config files during
implementation (`~/.claude.json`'s top-level `mcpServers`, `~/.cursor/mcp.json`),
not assumed from memory — see `orbitkb-plans/agent-connection-setup/plan.md` WP3.
"""
from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from orbitkb.setup.mcp_config import (
    mcp_command_line,
    remove_claude_code_config,
    remove_codex_config,
    remove_cursor_config,
    write_claude_code_config,
    write_codex_config,
    write_cursor_config,
)


def test_mcp_command_line_builds_minimal_serve_invocation(tmp_path: Path):
    db = tmp_path / "orbitkb.db"

    command, args = mcp_command_line(None, None, False, False, db)

    assert command  # resolved to something runnable, absolute path or bare name
    assert args == ["serve", "--db", str(db)]


def test_mcp_command_line_includes_optional_flags_only_when_set(tmp_path: Path):
    db = tmp_path / "orbitkb.db"

    _, args = mcp_command_line("codex", "gpt-5", True, True, db)

    assert args == [
        "serve", "--backend", "codex", "--model", "gpt-5",
        "--claude-bare", "--codex-api-key", "--db", str(db),
    ]


def test_write_claude_code_config_creates_project_file(tmp_path: Path):
    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    assert action.status == "created"
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["orbitkb"] == {"type": "stdio", "command": "orbitkb", "args": ["serve"]}


def test_write_claude_code_config_preserves_other_servers_in_existing_file(tmp_path: Path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"other-tool": {"command": "other-mcp"}}}))

    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    assert action.status == "updated"
    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["other-tool"] == {"command": "other-mcp"}
    assert data["mcpServers"]["orbitkb"]["command"] == "orbitkb"


def test_write_claude_code_config_is_idempotent(tmp_path: Path):
    write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    assert action.status == "unchanged"


def test_write_claude_code_config_reports_conflict_without_overwriting(tmp_path: Path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"orbitkb": {"command": "some-other-command"}}}))
    original = mcp_path.read_text()

    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    assert action.status == "conflict"
    assert '"command": "orbitkb"' in action.detail  # paste-able snippet with the intended entry
    assert mcp_path.read_text() == original


def test_write_claude_code_config_force_overwrites_conflict(tmp_path: Path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"orbitkb": {"command": "some-other-command"}}}))

    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"], force=True)

    assert action.status == "updated"
    data = json.loads(mcp_path.read_text())
    assert data["mcpServers"]["orbitkb"]["command"] == "orbitkb"


def test_write_claude_code_config_dry_run_writes_nothing(tmp_path: Path):
    action = write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"], dry_run=True)

    assert action.status == "created"
    assert not (tmp_path / ".mcp.json").exists()


def test_write_claude_code_config_user_scope_uses_top_level_mcp_servers(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"userID": "abc", "mcpServers": {}}))

    action = write_claude_code_config(tmp_path, "user", "orbitkb", ["serve"], home=home)

    assert action.status == "updated"
    data = json.loads((home / ".claude.json").read_text())
    assert data["userID"] == "abc"
    assert data["mcpServers"]["orbitkb"]["command"] == "orbitkb"


def test_write_claude_code_config_rejects_unknown_scope(tmp_path: Path):
    try:
        write_claude_code_config(tmp_path, "bogus", "orbitkb", ["serve"])
    except ValueError as exc:
        assert "scope" in str(exc)
    else:
        raise AssertionError("expected ValueError for an unknown scope")


def test_write_cursor_config_creates_project_file_without_type_field(tmp_path: Path):
    action = write_cursor_config(tmp_path, "project", "orbitkb", ["serve"])

    assert action.status == "created"
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert data["mcpServers"]["orbitkb"] == {"command": "orbitkb", "args": ["serve"]}


def test_write_cursor_config_preserves_other_servers(tmp_path: Path):
    cursor_dir = tmp_path / ".cursor"
    cursor_dir.mkdir()
    (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"design-graph": {"command": "design-mcp"}}}))

    write_cursor_config(tmp_path, "project", "orbitkb", ["serve"])

    data = json.loads((cursor_dir / "mcp.json").read_text())
    assert data["mcpServers"]["design-graph"] == {"command": "design-mcp"}
    assert data["mcpServers"]["orbitkb"]["command"] == "orbitkb"


def test_write_codex_config_creates_toml_table(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    action = write_codex_config("orbitkb", ["serve"], home=home)

    assert action.status == "created"
    doc = tomlkit.parse((home / ".codex" / "config.toml").read_text())
    assert doc["mcp_servers"]["orbitkb"]["command"] == "orbitkb"
    assert list(doc["mcp_servers"]["orbitkb"]["args"]) == ["serve"]


def test_write_codex_config_preserves_other_toml_content(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        "# a comment the user wrote\n"
        "model = \"gpt-5\"\n\n"
        "[mcp_servers.other]\n"
        "command = \"other-mcp\"\n"
    )

    write_codex_config("orbitkb", ["serve"], home=home)

    text = (codex_dir / "config.toml").read_text()
    assert "# a comment the user wrote" in text
    assert 'model = "gpt-5"' in text
    doc = tomlkit.parse(text)
    assert doc["mcp_servers"]["other"]["command"] == "other-mcp"
    assert doc["mcp_servers"]["orbitkb"]["command"] == "orbitkb"


def test_write_codex_config_is_idempotent(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    write_codex_config("orbitkb", ["serve"], home=home)
    action = write_codex_config("orbitkb", ["serve"], home=home)

    assert action.status == "unchanged"


def test_write_codex_config_reports_conflict_without_overwriting(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    write_codex_config("orbitkb", ["serve", "--backend", "claude"], home=home)
    original = (home / ".codex" / "config.toml").read_text()

    action = write_codex_config("orbitkb", ["serve", "--backend", "codex"], home=home)

    assert action.status == "conflict"
    assert (home / ".codex" / "config.toml").read_text() == original


def test_write_codex_config_dry_run_writes_nothing(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    action = write_codex_config("orbitkb", ["serve"], home=home, dry_run=True)

    assert action.status == "created"
    assert not (home / ".codex" / "config.toml").exists()


def test_remove_claude_code_config_skips_when_nothing_registered(tmp_path: Path):
    action = remove_claude_code_config(tmp_path, "project")

    assert action.status == "skipped"


def test_remove_claude_code_config_deletes_file_left_with_only_our_entry(tmp_path: Path):
    write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])

    action = remove_claude_code_config(tmp_path, "project")

    assert action.status == "removed"
    assert not (tmp_path / ".mcp.json").exists()


def test_remove_claude_code_config_preserves_other_servers(tmp_path: Path):
    write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])
    mcp_path = tmp_path / ".mcp.json"
    data = json.loads(mcp_path.read_text())
    data["mcpServers"]["other-tool"] = {"command": "other-mcp"}
    mcp_path.write_text(json.dumps(data))

    action = remove_claude_code_config(tmp_path, "project")

    assert action.status == "removed"
    data = json.loads(mcp_path.read_text())
    assert "orbitkb" not in data["mcpServers"]
    assert data["mcpServers"]["other-tool"] == {"command": "other-mcp"}


def test_remove_claude_code_config_declines_an_entry_that_was_repurposed(tmp_path: Path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"orbitkb": {"command": "some-other-tool"}}}))

    action = remove_claude_code_config(tmp_path, "project")

    assert action.status == "declined"
    assert json.loads(mcp_path.read_text())["mcpServers"]["orbitkb"]["command"] == "some-other-tool"


def test_remove_claude_code_config_accepts_any_orbitkb_serve_invocation(tmp_path: Path):
    write_claude_code_config(tmp_path, "project", "/opt/other/path/orbitkb", ["serve", "--backend", "claude"])

    action = remove_claude_code_config(tmp_path, "project")

    assert action.status == "removed"


def test_remove_claude_code_config_dry_run_touches_nothing(tmp_path: Path):
    write_claude_code_config(tmp_path, "project", "orbitkb", ["serve"])
    mcp_path = tmp_path / ".mcp.json"
    original = mcp_path.read_text()

    action = remove_claude_code_config(tmp_path, "project", dry_run=True)

    assert action.status == "removed"
    assert mcp_path.read_text() == original


def test_remove_cursor_config_removes_our_entry(tmp_path: Path):
    write_cursor_config(tmp_path, "project", "orbitkb", ["serve"])

    action = remove_cursor_config(tmp_path, "project")

    assert action.status == "removed"
    assert not (tmp_path / ".cursor" / "mcp.json").exists()


def test_remove_codex_config_removes_our_entry_and_deletes_empty_file(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    write_codex_config("orbitkb", ["serve"], home=home)

    action = remove_codex_config(home=home)

    assert action.status == "removed"
    assert not (home / ".codex" / "config.toml").exists()


def test_remove_codex_config_preserves_other_toml_content(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    write_codex_config("orbitkb", ["serve"], home=home)
    codex_path = home / ".codex" / "config.toml"
    doc = tomlkit.parse(codex_path.read_text())
    doc["model"] = "gpt-5"
    codex_path.write_text(tomlkit.dumps(doc))

    action = remove_codex_config(home=home)

    assert action.status == "removed"
    remaining = tomlkit.parse(codex_path.read_text())
    assert remaining["model"] == "gpt-5"
    assert "mcp_servers" not in remaining or "orbitkb" not in remaining.get("mcp_servers", {})


def test_remove_codex_config_declines_an_entry_that_was_repurposed(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text('[mcp_servers.orbitkb]\ncommand = "some-other-tool"\n')

    action = remove_codex_config(home=home)

    assert action.status == "declined"


def test_remove_codex_config_skips_when_nothing_registered(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()

    action = remove_codex_config(home=home)

    assert action.status == "skipped"
