from __future__ import annotations

import os
from pathlib import Path

from orbitkb.generation.backend_base import LLMBackend
from orbitkb.generation.claude_backend import ClaudeBackend
from orbitkb.generation.codex_backend import CodexBackend
from orbitkb.generation.mock_backend import MockBackend

DEFAULT_BACKEND = "claude"
DEFAULT_DB_PATH = Path.home() / ".orbitkb" / "orbitkb.db"


def resolve_backend(
    name: str | None, model: str | None = None, claude_bare: bool = False, codex_api_key: bool = False
) -> LLMBackend:
    chosen = name or os.environ.get("ORBITKB_BACKEND", DEFAULT_BACKEND)
    if chosen == "claude":
        return ClaudeBackend(model=model, bare=claude_bare)
    if chosen == "codex":
        return CodexBackend(model=model, api_key=codex_api_key)
    if chosen == "mock":
        return MockBackend()
    raise ValueError(f"Unknown backend: {chosen!r} (expected 'claude', 'codex' or 'mock')")
