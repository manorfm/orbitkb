"""Adapter from current SQLite API rows to the generation read contract."""

import sqlite3

from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.generation.knowledge import ComponentSummary, RouteKey


class LegacyKnowledgeAdapter:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def api_summaries(self, service_id: int) -> dict[RouteKey, str]:
        return {
            (row["method"], row["path"]): row["summary"]
            for row in apis_repo.list_apis(self._conn, service_id)
        }

    def component_summaries(self, service_id: int) -> list[ComponentSummary]:
        return [
            ComponentSummary(row["name"], row["file_path"], row["summary"])
            for row in components_repo.list_components(self._conn, service_id)
        ]
