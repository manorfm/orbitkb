"""orbitkb.setup.marked_block: idempotent, non-destructive "replace only what's
between two marker lines" — shared by the git hook (WP4) and the agent
instruction block (WP5), so both follow one rule: never touch a file's content
outside the orbitkb-managed markers.
"""
from __future__ import annotations

from pathlib import Path

from orbitkb.setup.marked_block import apply_marked_block

BEGIN = "# orbitkb:begin"
END = "# orbitkb:end"


def test_creates_file_with_header_and_block_when_missing(tmp_path: Path):
    path = tmp_path / "hook.sh"

    action = apply_marked_block(
        path, "echo hello", BEGIN, END, category="hook", client="git", header="#!/bin/sh\n\n",
    )

    assert action.status == "created"
    content = path.read_text()
    assert content.startswith("#!/bin/sh\n\n")
    assert BEGIN in content and END in content
    assert "echo hello" in content


def test_is_idempotent_when_block_already_matches(tmp_path: Path):
    path = tmp_path / "hook.sh"
    apply_marked_block(path, "echo hello", BEGIN, END, category="hook", client="git")

    action = apply_marked_block(path, "echo hello", BEGIN, END, category="hook", client="git")

    assert action.status == "unchanged"


def test_updates_only_the_marked_block_preserving_surrounding_content(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    path.write_text(f"# Team notes\n\nDo not break prod.\n\n{BEGIN}\nold block\n{END}\n\nMore team notes.\n")

    action = apply_marked_block(path, "new block", BEGIN, END, category="instructions", client="agents.md")

    assert action.status == "updated"
    content = path.read_text()
    assert "Do not break prod." in content
    assert "More team notes." in content
    assert "old block" not in content
    assert "new block" in content


def test_never_touches_a_file_with_foreign_content_and_no_markers(tmp_path: Path):
    path = tmp_path / "post-commit"
    foreign_content = "#!/bin/sh\nnpx husky-run post-commit\n"
    path.write_text(foreign_content)

    action = apply_marked_block(path, "nohup orbitkb update ...", BEGIN, END, category="hook", client="git")

    assert action.status == "conflict"
    assert path.read_text() == foreign_content
    assert "nohup orbitkb update" in action.detail  # paste-able snippet offered instead


def test_dry_run_creates_nothing(tmp_path: Path):
    path = tmp_path / "hook.sh"

    action = apply_marked_block(path, "echo hello", BEGIN, END, category="hook", client="git", dry_run=True)

    assert action.status == "created"
    assert not path.exists()


def test_dry_run_does_not_modify_existing_file(tmp_path: Path):
    path = tmp_path / "AGENTS.md"
    original = f"{BEGIN}\nold block\n{END}\n"
    path.write_text(original)

    action = apply_marked_block(path, "new block", BEGIN, END, category="instructions", client="agents.md", dry_run=True)

    assert action.status == "updated"
    assert path.read_text() == original
