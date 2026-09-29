"""Adapter from current SQLite API rows to the generation read contract."""

import sqlite3

from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.knowledge import (
    ComponentDocumentation,
    ComponentSummary,
    EndpointDocumentation,
    MessagingDocumentation,
    OverviewDocumentation,
    PersistenceDocumentation,
    RouteKey,
)


class LegacyKnowledgeAdapter:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def endpoint_keys(self, service_id: int) -> set[RouteKey]:
        return apis_repo.list_api_keys(self._conn, service_id)

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
        self._conn.execute("SAVEPOINT endpoint_documentation")
        try:
            api_id = apis_repo.upsert_api(
                self._conn, service_id, documentation.method, documentation.path,
                documentation.summary, documentation.description, documentation.response_shape,
                documentation.evidence, request_shape=documentation.request_shape, commit=False,
            )
            apis_repo.replace_api_validations(
                self._conn, api_id, documentation.validations, commit=False,
            )
            service_calls_repo.replace_calls_for_api(
                self._conn, service_id, api_id, documentation.calls, documentation.evidence,
                commit=False,
            )
        except Exception:
            self._conn.execute("ROLLBACK TO SAVEPOINT endpoint_documentation")
            self._conn.execute("RELEASE SAVEPOINT endpoint_documentation")
            raise
        self._conn.execute("RELEASE SAVEPOINT endpoint_documentation")

    def prune_endpoints(self, service_id: int, keep_keys: set[RouteKey]) -> None:
        apis_repo.prune_apis_not_in(self._conn, service_id, keep_keys)

    def save_component(self, service_id: int, documentation: ComponentDocumentation) -> None:
        components_repo.upsert_component(
            self._conn, service_id, documentation.name, documentation.file_path,
            documentation.summary, documentation.evidence,
        )

    def prune_components(self, service_id: int, keep_keys: set[tuple[str, str]]) -> None:
        components_repo.prune_components_not_in(self._conn, service_id, keep_keys)

    def save_overview(self, service_id: int, documentation: OverviewDocumentation) -> None:
        services_repo.update_service_overview(
            self._conn, service_id, documentation.short_desc, documentation.long_desc,
        )

    def replace_persistence(self, service_id: int, documentation: PersistenceDocumentation) -> None:
        persistence_repo.replace_persistence_entities(
            self._conn, service_id,
            [
                {"name": entity.name, "kind": entity.kind, "engine": entity.engine, "schema_json": entity.fields}
                for entity in documentation.entities
            ],
            documentation.evidence,
        )

    def replace_messaging(self, service_id: int, documentation: MessagingDocumentation) -> None:
        messages_repo.replace_messages(
            self._conn, service_id,
            [
                {
                    "direction": message.direction,
                    "channel": message.channel,
                    "provider": message.provider,
                    "shape_json": message.shape,
                    "description": message.description,
                }
                for message in documentation.messages
            ],
            documentation.evidence,
        )
