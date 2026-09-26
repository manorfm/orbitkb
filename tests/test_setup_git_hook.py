"""orbitkb.setup.git_hook: installs a non-blocking post-commit/post-merge hook
that runs `orbitkb update --repository <name>` in the background, without ever
overwriting a hook a team already has (husky, pre-commit framework, etc.)."""
from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from orbitkb.setup.git_hook import (
    install_repository_hooks,
    resolve_git_dir,
    uninstall_repository_hooks,
)


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)


def test_resolve_git_dir_finds_dot_git_directory(tmp_path: Path):
    _init_git_repo(tmp_path)

    git_dir = resolve_git_dir(tmp_path)

    assert git_dir == (tmp_path / ".git").resolve()


def test_resolve_git_dir_returns_none_outside_a_repository(tmp_path: Path):
    assert resolve_git_dir(tmp_path) is None


def test_install_repository_hooks_creates_executable_post_commit_and_post_merge(tmp_path: Path):
    _init_git_repo(tmp_path)
    db_path = tmp_path / "orbitkb.db"

    actions = install_repository_hooks(tmp_path, "shop", db_path)

    hooks_dir = tmp_path / ".git" / "hooks"
    post_commit = hooks_dir / "post-commit"
    post_merge = hooks_dir / "post-merge"
    assert {a.status for a in actions} == {"created"}
    for hook_path in (post_commit, post_merge):
        content = hook_path.read_text()
        assert 'orbitkb update --repository "shop"' in content
        assert str(db_path) in content
        assert content.strip().startswith("#!/bin/sh")
        assert "&" in content  # runs detached, never blocks the commit
        assert hook_path.stat().st_mode & stat.S_IXUSR


def test_install_repository_hooks_is_idempotent(tmp_path: Path):
    _init_git_repo(tmp_path)
    install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    actions = install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert {a.status for a in actions} == {"unchanged"}


def test_install_repository_hooks_never_overwrites_a_foreign_hook(tmp_path: Path):
    _init_git_repo(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    foreign = "#!/bin/sh\nnpx husky-run post-commit\n"
    (hooks_dir / "post-commit").write_text(foreign)

    actions = install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert (hooks_dir / "post-commit").read_text() == foreign
    post_commit_action = next(a for a in actions if a.path.name == "post-commit")
    assert post_commit_action.status == "conflict"
    assert "orbitkb update" in post_commit_action.detail


def test_install_repository_hooks_skips_outside_a_git_repository(tmp_path: Path):
    actions = install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    assert len(actions) == 1
    assert actions[0].status == "skipped"


def test_install_repository_hooks_dry_run_writes_nothing(tmp_path: Path):
    _init_git_repo(tmp_path)

    actions = install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db", dry_run=True)

    assert {a.status for a in actions} == {"created"}
    assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()
    assert not (tmp_path / ".git" / "hooks" / "post-merge").exists()


def test_uninstall_repository_hooks_removes_files_it_created(tmp_path: Path):
    _init_git_repo(tmp_path)
    install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    actions = uninstall_repository_hooks(tmp_path)

    assert {a.status for a in actions} == {"removed"}
    assert not (tmp_path / ".git" / "hooks" / "post-commit").exists()
    assert not (tmp_path / ".git" / "hooks" / "post-merge").exists()


def test_uninstall_repository_hooks_never_touches_a_foreign_hook(tmp_path: Path):
    _init_git_repo(tmp_path)
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    foreign = "#!/bin/sh\nnpx husky-run post-commit\n"
    (hooks_dir / "post-commit").write_text(foreign)

    actions = uninstall_repository_hooks(tmp_path)

    assert (hooks_dir / "post-commit").read_text() == foreign
    post_commit_action = next(a for a in actions if a.path.name == "post-commit")
    assert post_commit_action.status == "skipped"


def test_uninstall_repository_hooks_dry_run_touches_nothing(tmp_path: Path):
    _init_git_repo(tmp_path)
    install_repository_hooks(tmp_path, "shop", tmp_path / "orbitkb.db")

    actions = uninstall_repository_hooks(tmp_path, dry_run=True)

    assert {a.status for a in actions} == {"removed"}
    assert (tmp_path / ".git" / "hooks" / "post-commit").exists()
