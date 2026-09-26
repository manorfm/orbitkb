"""Which MCP clients (Claude Code, Cursor, Codex CLI) are plausibly installed
on this machine, and a simple terminal menu to confirm the selection.

The heuristic is deliberately loose (an executable on PATH, or that client's
own config directory existing) — it exists only to narrow `orbitkb setup`'s
default from "always write all three" to "write the ones actually in use",
never to gate a feature behind a hard requirement. `orbitkb setup` still
accepts `--client` to override it explicitly.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

_PathWhich = Callable[[str], str | None]


def detect_available_clients(
    *, path_which: _PathWhich | None = None, home: Path | None = None,
) -> list[str]:
    """`path_which` defaults to `shutil.which`, resolved at call time (not
    bound as a parameter default) so patching `shutil.which` in a test takes
    effect — otherwise a real `claude`/`codex` on the test runner's own PATH
    would leak into every test that doesn't override it explicitly.
    """
    path_which = path_which or shutil.which
    home = home or Path.home()
    detected = []
    if path_which("claude") is not None or (home / ".claude.json").exists():
        detected.append("claude")
    if (home / ".cursor").exists():
        detected.append("cursor")
    if path_which("codex") is not None or (home / ".codex").exists():
        detected.append("codex")
    return detected


def choose_clients_interactively(
    candidates: list[str], *, input_func: Callable[..., str] | None = None,
) -> list[str]:
    """Render a numbered menu over `candidates` and return the picks. Empty
    input, or input that resolves to no valid pick, returns every candidate —
    a mistyped answer should never silently register nothing. `input_func`
    defaults to the builtin `input`, resolved at call time (not bound as a
    parameter default) so patching `builtins.input` in a test takes effect.
    """
    input_func = input_func or input
    print("Detected possible MCP clients on this machine:")
    for index, name in enumerate(candidates, start=1):
        print(f"  {index}. {name}")
    raw = input_func("Register which ones? (comma-separated numbers, Enter for all): ").strip()
    if not raw:
        return list(candidates)

    chosen = []
    for token in raw.split(","):
        token = token.strip()
        if not token.isdigit():
            continue
        index = int(token) - 1
        if 0 <= index < len(candidates):
            chosen.append(candidates[index])
    return chosen or list(candidates)
