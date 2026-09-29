import json
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation.knowledge import EndpointDocumentation
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
