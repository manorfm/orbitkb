"""Adapter from current SQLite API rows to the generation read contract."""

import sqlite3

from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.generation.knowledge import (
    ComponentSummary,
    EndpointDocumentation,
    RouteKey,
)


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

    def save_endpoint(self, service_id: int, documentation: EndpointDocumentation) -> None:
        api_id = apis_repo.upsert_api(
            self._conn, service_id, documentation.method, documentation.path,
            documentation.summary, documentation.description, documentation.response_shape,
            documentation.evidence, request_shape=documentation.request_shape,
        )
        apis_repo.replace_api_validations(self._conn, api_id, documentation.validations)
        service_calls_repo.replace_calls_for_api(
            self._conn, service_id, api_id, documentation.calls, documentation.evidence,
        )

    def prune_endpoints(self, service_id: int, keep_keys: set[RouteKey]) -> None:
        apis_repo.prune_apis_not_in(self._conn, service_id, keep_keys)
