"""Shared helper for every repository module."""
from __future__ import annotations

import os
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def process_exists(process_id: int) -> bool:
    """Return whether a local PID still exists without inspecting its command."""
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
