"""`orbitkb setup`: ties MCP client registration (WP3), the reindex git hook
(WP4) and the agent instruction block (WP5) into one command. Never touches the
real machine's home directory — every test points `Path.home()` at an isolated
`tmp_path` via monkeypatch.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import tomlkit

from orbitkb import cli
from orbitkb.db.connection import open_db
from tests.test_orchestrator import SAMPLE_ROOT, FakeOrchestratorBackend


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    monkeypatch.setattr(cli, "resolve_backend", lambda *a, **kw: FakeOrchestratorBackend())


def _parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def _indexed_repo(tmp_path: Path, db_path: Path, *, git: bool = True) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(SAMPLE_ROOT, root)
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    cli._cmd_index(_parse(["index", str(root), "--db", str(db_path), "--repository-name", "test-repo"]))
    return root


def _isolated_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_setup_registers_all_clients_and_installs_hook_and_instructions(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    home = _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 0
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["orbitkb"]["command"]
    assert json.loads((root / ".cursor" / "mcp.json").read_text())["mcpServers"]["orbitkb"]["command"]
    doc = tomlkit.parse((home / ".codex" / "config.toml").read_text())
    assert doc["mcp_servers"]["orbitkb"]["command"]
    for hook_name in ("post-commit", "post-merge"):
        content = (root / ".git" / "hooks" / hook_name).read_text()
        assert 'orbitkb update --repository "test-repo"' in content
    for filename in ("AGENTS.md", "CLAUDE.md"):
        assert "freshness" in (root / filename).read_text()


def test_setup_is_idempotent(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)
    _indexed_repo(tmp_path, db_path)
    cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path)]))

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 0


def test_setup_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    home = _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path), "--dry-run"]))

    assert exit_code == 0
    assert not (root / ".mcp.json").exists()
    assert not (root / ".cursor" / "mcp.json").exists()
    assert not (home / ".codex" / "config.toml").exists()
    assert not (root / ".git" / "hooks" / "post-commit").exists()
    assert not (root / "AGENTS.md").exists()


def test_setup_client_filter_only_registers_requested_client(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    home = _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)

    exit_code = cli._cmd_setup(_parse([
        "setup", "--repository", "test-repo", "--db", str(db_path), "--client", "codex",
    ]))

    assert exit_code == 0
    assert (home / ".codex" / "config.toml").exists()
    assert not (root / ".mcp.json").exists()
    assert not (root / ".cursor" / "mcp.json").exists()


def test_setup_without_repository_only_registers_user_scope_clients(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    home = _isolated_home(tmp_path, monkeypatch)

    exit_code = cli._cmd_setup(_parse(["setup", "--db", str(db_path)]))

    assert exit_code == 0
    assert json.loads((home / ".claude.json").read_text())["mcpServers"]["orbitkb"]["command"]
    assert json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["orbitkb"]["command"]
    doc = tomlkit.parse((home / ".codex" / "config.toml").read_text())
    assert doc["mcp_servers"]["orbitkb"]["command"]


def test_setup_rejects_project_scope_without_repository(tmp_path: Path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)

    exit_code = cli._cmd_setup(_parse(["setup", "--scope", "project", "--db", str(db_path)]))

    assert exit_code == 1
    assert "--repository" in capsys.readouterr().err


def test_setup_errors_on_unknown_repository(tmp_path: Path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)
    open_db(db_path)

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "does-not-exist", "--db", str(db_path)]))

    assert exit_code == 1
    assert "unknown repository" in capsys.readouterr().err


def test_setup_reports_conflict_without_force_and_keeps_existing_file(tmp_path: Path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"orbitkb": {"command": "something-else"}}}))
    original = (root / ".mcp.json").read_text()

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 1
    assert (root / ".mcp.json").read_text() == original
    assert "conflict" in capsys.readouterr().out.lower()


def test_setup_force_overwrites_conflicting_mcp_config(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"orbitkb": {"command": "something-else"}}}))

    exit_code = cli._cmd_setup(_parse([
        "setup", "--repository", "test-repo", "--db", str(db_path), "--force",
    ]))

    assert exit_code == 0
    assert json.loads((root / ".mcp.json").read_text())["mcpServers"]["orbitkb"]["command"] != "something-else"


def test_setup_never_overwrites_a_foreign_git_hook(tmp_path: Path, monkeypatch, capsys):
    db_path = tmp_path / "test.db"
    _isolated_home(tmp_path, monkeypatch)
    root = _indexed_repo(tmp_path, db_path)
    hooks_dir = root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    foreign = "#!/bin/sh\nnpx husky-run post-commit\n"
    (hooks_dir / "post-commit").write_text(foreign)

    exit_code = cli._cmd_setup(_parse(["setup", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 1
    assert (hooks_dir / "post-commit").read_text() == foreign
