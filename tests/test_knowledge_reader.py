from orbitkb.generation.knowledge import KnowledgeReader, compose_endpoint_summaries


class FakeKnowledgeReader:
    def api_summaries(self, service_id: int) -> dict[tuple[str, str], str]:
        assert service_id == 7
        return {("GET", "/items"): "Lists items", ("POST", "/items"): "Creates items"}


def test_component_summary_can_be_composed_from_a_reader_without_sqlite():
    reader: KnowledgeReader = FakeKnowledgeReader()

    summary = compose_endpoint_summaries(
        [("GET", "/items"), ("GET", "/items"), ("POST", "/items"), ("DELETE", "/missing")],
        reader.api_summaries(7),
    )

    assert summary == "- GET /items: Lists items\n- POST /items: Creates items"


def test_component_summary_preserves_empty_fallback():
    assert compose_endpoint_summaries([("GET", "/missing")], {}) == "(no endpoint summaries available yet)"
