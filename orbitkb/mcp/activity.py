"""Lifecycle tracking for expensive local MCP operations."""
from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import local_activity


@contextmanager
def track_local_activity(db_path: Path | None, operation: str) -> Iterator[None]:
    """Expose an operation to a separate local monitor only while it executes."""
    with closing(open_db(db_path)) as conn:
        activity_id = local_activity.start(conn, operation)
    try:
        yield
    finally:
        with closing(open_db(db_path)) as conn:
            local_activity.finish(conn, activity_id)
