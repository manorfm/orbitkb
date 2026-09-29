import json
from pathlib import Path

import pytest

from orbitkb.db.connection import open_db, open_readonly_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.knowledge import (
    ComponentDocumentation,
    EndpointDocumentation,
    OverviewDocumentation,
)
from orbitkb.generation.legacy_knowledge import LegacyKnowledgeAdapter


def test_legacy_writer_replaces_endpoint_children_and_prunes_absent_routes(tmp_path: Path):
    conn = open_db(tmp_path / "writer.db")
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")
    writer = LegacyKnowledgeAdapter(conn)
    evidence = [{"file": "menu.py", "start_line": 4, "end_line": 9}]
    first = EndpointDocumentation(
        method="GET", path="/menus", summary="Lists menus", description="Returns menus",
        response_shape=[{"field": "id", "type_desc": "string"}], request_shape=[],
        validations=[{"kind": "authorization", "description": "Requires JWT"}],
        calls=[{"to_service_name": "users", "call_kind": "http", "reason": "load owner"}],
        evidence=evidence,
    )

    writer.save_endpoint(service_id, first)
    api = apis_repo.get_api_by_key(conn, service_id, "GET", "/menus")
    assert json.loads(api["evidence_json"]) == evidence
    assert len(apis_repo.list_validations_for_api(conn, api["id"])) == 1
    assert [call["to_service_name"] for call in service_calls_repo.list_calls_for_api(conn, api["id"])] == ["users"]

    writer.save_endpoint(service_id, EndpointDocumentation(
        method="GET", path="/menus", summary="Lists active menus", description="Returns active menus",
        response_shape=[], request_shape=[], validations=[], calls=[], evidence=evidence,
    ))
    assert apis_repo.get_api_by_key(conn, service_id, "GET", "/menus")["id"] == api["id"]
    assert apis_repo.list_validations_for_api(conn, api["id"]) == []
    assert service_calls_repo.list_calls_for_api(conn, api["id"]) == []

    writer.prune_endpoints(service_id, set())
    assert apis_repo.get_api_by_key(conn, service_id, "GET", "/menus") is None


@pytest.mark.parametrize("existing", [False, True])
def test_legacy_writer_rolls_back_complete_endpoint_when_calls_fail(tmp_path: Path, existing: bool):
    path = tmp_path / "atomic-writer.db"
    conn = open_db(path)
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")
    writer = LegacyKnowledgeAdapter(conn)
    evidence = [{"file": "menu.py", "start_line": 4, "end_line": 9}]
    if existing:
        writer.save_endpoint(service_id, EndpointDocumentation(
            method="GET", path="/menus", summary="Original", description="Original description",
            response_shape=[], request_shape=[],
            validations=[{"kind": "authorization", "description": "Original rule"}],
            calls=[{"to_service_name": "users", "call_kind": "http"}], evidence=evidence,
        ))

    with pytest.raises(KeyError, match="to_service_name"):
        writer.save_endpoint(service_id, EndpointDocumentation(
            method="GET", path="/menus", summary="Partial", description="Partial description",
            response_shape=[], request_shape=[],
            validations=[{"kind": "input_validation", "description": "Partial rule"}],
            calls=[{"call_kind": "http"}], evidence=evidence,
        ))

    for checked in (conn, open_db(path)):
        api = apis_repo.get_api_by_key(checked, service_id, "GET", "/menus")
        if not existing:
            assert api is None
            continue
        assert api["summary"] == "Original"
        assert [row["description"] for row in apis_repo.list_validations_for_api(checked, api["id"])] == ["Original rule"]
        assert [row["to_service_name"] for row in service_calls_repo.list_calls_for_api(checked, api["id"])] == ["users"]


def test_legacy_writer_preserves_an_outer_transaction(tmp_path: Path):
    path = tmp_path / "outer-transaction.db"
    conn = open_db(path)
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")
    conn.execute("INSERT INTO schema_meta (key, value) VALUES ('unrelated', 'pending')")

    LegacyKnowledgeAdapter(conn).save_endpoint(service_id, EndpointDocumentation(
        method="GET", path="/menus", summary="Menus", description="Lists menus",
        response_shape=[], request_shape=[], validations=[], calls=[], evidence=[],
    ))

    assert conn.in_transaction
    assert apis_repo.get_api_by_key(open_readonly_db(path), service_id, "GET", "/menus") is None
    conn.rollback()
    assert apis_repo.get_api_by_key(conn, service_id, "GET", "/menus") is None
    assert conn.execute("SELECT value FROM schema_meta WHERE key = 'unrelated'").fetchone() is None


def test_legacy_writer_updates_component_evidence_and_prunes_missing_components(tmp_path: Path):
    conn = open_db(tmp_path / "components.db")
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")
    writer = LegacyKnowledgeAdapter(conn)
    evidence = [{"file": "menu.py", "start_line": 5, "end_line": 10}]

    writer.save_component(service_id, ComponentDocumentation("MenuController", "menu.py", "Lists menus", evidence))
    first = components_repo.list_components(conn, service_id)[0]
    assert json.loads(first["evidence_json"]) == evidence

    writer.save_component(service_id, ComponentDocumentation("MenuController", "menu.py", "Lists active menus", evidence))
    updated = components_repo.list_components(conn, service_id)
    assert [(row["name"], row["summary"]) for row in updated] == [("MenuController", "Lists active menus")]

    writer.prune_components(service_id, set())
    assert components_repo.list_components(conn, service_id) == []


def test_legacy_writer_saves_overview_descriptions(tmp_path: Path):
    conn = open_db(tmp_path / "overview.db")
    service_id = services_repo.ensure_service(conn, "menus", "/tmp/menus", "python")

    LegacyKnowledgeAdapter(conn).save_overview(service_id, OverviewDocumentation("Menu API", "Manages menus"))

    service = services_repo.get_service_by_name(conn, "menus")
    assert (service["short_desc"], service["long_desc"]) == ("Menu API", "Manages menus")
