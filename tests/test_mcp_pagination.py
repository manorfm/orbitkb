"""Pagination tests for the MCP description tools (describe_service, list_apis,
describe_persistence, describe_messages): each returns a bounded page plus
total/truncated so a real service with dozens of endpoints can't blow an
agent's context budget by default (see README's context-efficiency notes)."""
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.mcp import queries


def _seed_many_apis(conn, service_id: int, count: int) -> None:
    for i in range(count):
        apis_repo.upsert_api(conn, service_id, "GET", f"/resource/{i}", f"summary {i}", "d", [], [])


def test_list_apis_defaults_to_a_bounded_page(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    _seed_many_apis(conn, service_id, 60)

    result = queries.list_apis(conn, "big-service")

    assert len(result["apis"]) == queries.DEFAULT_LIST_LIMIT
    assert result["total"] == 60
    assert result["truncated"] is True


def test_list_apis_pages_through_with_offset(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    _seed_many_apis(conn, service_id, 60)

    first_page = queries.list_apis(conn, "big-service", limit=50, offset=0)
    second_page = queries.list_apis(conn, "big-service", limit=50, offset=50)

    assert len(first_page["apis"]) == 50
    assert first_page["truncated"] is True
    assert len(second_page["apis"]) == 10
    assert second_page["truncated"] is False
    first_paths = {a["path"] for a in first_page["apis"]}
    second_paths = {a["path"] for a in second_page["apis"]}
    assert first_paths.isdisjoint(second_paths)


def test_list_apis_rejects_negative_limit_or_offset(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "svc", "/tmp/svc", "python")

    assert "error" in queries.list_apis(conn, "svc", limit=-1)
    assert "error" in queries.list_apis(conn, "svc", offset=-1)
    assert "error" in queries.list_apis(conn, "svc", limit=0)


def test_list_apis_caps_an_oversized_limit_instead_of_erroring(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    _seed_many_apis(conn, service_id, 10)

    result = queries.list_apis(conn, "big-service", limit=999999)

    assert len(result["apis"]) == 10  # never errors, just bounded by what's real
    assert result["truncated"] is False


def test_describe_service_paginates_every_list_independently(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    _seed_many_apis(conn, service_id, 60)

    result = queries.describe_service(conn, "big-service", limit=10, offset=0)

    assert len(result["apis"]) == 10
    assert result["pagination"]["apis"]["total"] == 60
    assert result["pagination"]["apis"]["truncated"] is True
    assert result["pagination"]["limit"] == 10
    assert result["pagination"]["offset"] == 0


def test_describe_persistence_paginates_entities(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    entities = [
        {"name": f"table_{i}", "kind": "sql_table", "engine": "postgres", "schema_json": []} for i in range(55)
    ]
    persistence_repo.replace_persistence_entities(conn, service_id, entities, [])

    result = queries.describe_persistence(conn, "big-service")

    assert len(result["entities"]) == queries.DEFAULT_LIST_LIMIT
    assert result["total"] == 55
    assert result["truncated"] is True


def test_describe_messages_paginates_messages(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "big-service", "/tmp/big", "python")
    messages = [
        {"direction": "publishes", "channel": f"chan_{i}", "shape_json": [], "description": "d", "provider": "kafka"}
        for i in range(55)
    ]
    messages_repo.replace_messages(conn, service_id, messages, [])

    result = queries.describe_messages(conn, "big-service")

    assert len(result["messages"]) == queries.DEFAULT_LIST_LIMIT
    assert result["total"] == 55
    assert result["truncated"] is True
    assert result["static_analysis_status"] == "unknown"
