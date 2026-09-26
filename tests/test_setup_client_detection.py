"""orbitkb.setup.client_detection: a light heuristic for which MCP clients are
plausibly installed, plus a simple terminal menu — used by `orbitkb setup` only
when `--client` was omitted, and the menu only when running in a real
interactive terminal (never blocking CI or an agent's own shell call)."""
from __future__ import annotations

from pathlib import Path

from orbitkb.setup.client_detection import (
    choose_clients_interactively,
    detect_available_clients,
)


def test_detects_nothing_on_a_bare_machine(tmp_path: Path):
    detected = detect_available_clients(path_which=lambda name: None, home=tmp_path)

    assert detected == []


def test_detects_claude_code_by_executable_on_path(tmp_path: Path):
    detected = detect_available_clients(path_which=lambda name: "/usr/bin/claude" if name == "claude" else None, home=tmp_path)

    assert detected == ["claude"]


def test_detects_claude_code_by_config_file_without_executable(tmp_path: Path):
    (tmp_path / ".claude.json").write_text("{}")

    detected = detect_available_clients(path_which=lambda name: None, home=tmp_path)

    assert detected == ["claude"]


def test_detects_codex_by_executable_or_config_dir(tmp_path: Path):
    (tmp_path / ".codex").mkdir()

    detected = detect_available_clients(path_which=lambda name: None, home=tmp_path)

    assert detected == ["codex"]


def test_detects_cursor_by_config_dir(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()

    detected = detect_available_clients(path_which=lambda name: None, home=tmp_path)

    assert detected == ["cursor"]


def test_detects_all_three_when_all_present(tmp_path: Path):
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".claude.json").write_text("{}")

    detected = detect_available_clients(path_which=lambda name: None, home=tmp_path)

    assert set(detected) == {"claude", "cursor", "codex"}


def test_menu_returns_all_candidates_on_empty_input(tmp_path: Path):
    chosen = choose_clients_interactively(["claude", "cursor"], input_func=lambda prompt="": "")

    assert chosen == ["claude", "cursor"]


def test_menu_returns_selected_candidates(tmp_path: Path):
    chosen = choose_clients_interactively(["claude", "cursor", "codex"], input_func=lambda prompt="": "1,3")

    assert chosen == ["claude", "codex"]


def test_menu_falls_back_to_all_when_input_has_no_valid_selection(tmp_path: Path):
    chosen = choose_clients_interactively(["claude", "cursor"], input_func=lambda prompt="": "bogus")

    assert chosen == ["claude", "cursor"]


def test_menu_ignores_out_of_range_numbers(tmp_path: Path):
    chosen = choose_clients_interactively(["claude", "cursor"], input_func=lambda prompt="": "1,9")

    assert chosen == ["claude"]
