from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.knowledge import (
    ComponentSummary,
    EndpointSummary,
    KnowledgeReader,
    compose_component_summaries,
    compose_endpoint_summaries,
)
from orbitkb.generation.legacy_knowledge import LegacyKnowledgeAdapter


class FakeKnowledgeReader:
    def endpoint_keys(self, service_id: int) -> set[tuple[str, str]]:
        return {("GET", "/items"), ("POST", "/items")}

    def api_summaries(self, service_id: int) -> dict[tuple[str, str], EndpointSummary]:
        assert service_id == 7
        return {
            ("GET", "/items"): EndpointSummary("Lists items", []),
            ("POST", "/items"): EndpointSummary("Creates items", []),
        }


def test_component_summary_can_be_composed_from_a_reader_without_sqlite():
    reader: KnowledgeReader = FakeKnowledgeReader()

    summary = compose_endpoint_summaries(
        [("GET", "/items"), ("GET", "/items"), ("POST", "/items"), ("DELETE", "/missing")],
        reader.api_summaries(7),
    )

    assert summary == "- GET /items: Lists items\n- POST /items: Creates items"


def test_component_summary_preserves_empty_fallback():
    assert compose_endpoint_summaries([("GET", "/missing")], {}) == "(no endpoint summaries available yet)"


def test_component_context_uses_evidence_from_available_endpoint_summaries_only():
    from orbitkb.generation.knowledge import compose_endpoint_context

    first = {"file": "src/items.py", "start_line": 10, "end_line": 20}
    second = {"file": "src/items.py", "start_line": 30, "end_line": 40}
    context = compose_endpoint_context(
        [("GET", "/items"), ("POST", "/items"), ("GET", "/items"), ("DELETE", "/missing")],
        {
            ("GET", "/items"): EndpointSummary("Lists", [first]),
            ("POST", "/items"): EndpointSummary("Creates", [first, second]),
        },
    )

    assert context.text == "- GET /items: Lists\n- POST /items: Creates"
    assert context.evidence == [first, second]


def test_overview_composition_uses_typed_component_summaries_without_sqlite():
    components = [
        ComponentSummary("Controller", "src/controller.py", "Handles requests"),
        ComponentSummary("Worker", "src/worker.py", "Processes jobs"),
    ]

    assert compose_component_summaries(components) == (
        "- Controller (src/controller.py): Handles requests\n"
        "- Worker (src/worker.py): Processes jobs"
    )
    assert compose_component_summaries([]).startswith("(no classes/controllers detected")


def test_legacy_reader_lists_existing_endpoint_keys_once_per_service(tmp_path):
    conn = open_db(tmp_path / "routes.db")
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")
    apis_repo.upsert_api(conn, service_id, "GET", "/menus", "list", "d", [], [])
    apis_repo.upsert_api(conn, service_id, "POST", "/menus", "create", "d", [], [])

    assert LegacyKnowledgeAdapter(conn).endpoint_keys(service_id) == {
        ("GET", "/menus"), ("POST", "/menus"),
    }
