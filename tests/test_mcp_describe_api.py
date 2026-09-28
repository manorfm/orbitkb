"""Coverage for describe_api's request_shape field — mirrors the existing
response_shape treatment (see db/repositories/apis.py upsert_api)."""
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.mcp import queries


def test_describe_api_includes_request_shape(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "python")
    request_shape = [{"field": "amount", "type_desc": "number, amount in cents", "required": True}]
    apis_repo.upsert_api(
        conn, service_id, "POST", "/charge", "charges a card", "d", [], [], request_shape=request_shape,
    )

    result = queries.describe_api(conn, "payments-service", "POST", "/charge")

    assert result["request_shape"] == request_shape


def test_describe_api_defaults_request_shape_to_empty_list(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "python")
    apis_repo.upsert_api(conn, service_id, "POST", "/charge", "charges a card", "d", [], [])

    result = queries.describe_api(conn, "payments-service", "POST", "/charge")

    assert result["request_shape"] == []


def test_describe_api_includes_a_compact_api_shape_block(tmp_path: Path):
    """`api_shape` is a lean, structured restatement of the same request/response
    fields already returned flat -- no new data, just a compact shape a consumer
    can render (a "simplified swagger") without reassembling it itself."""
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "python")
    request_shape = [{"field": "amount", "type_desc": "number, amount in cents", "required": True}]
    response_shape = [{"field": "id", "type_desc": "string, charge id"}]
    apis_repo.upsert_api(
        conn, service_id, "POST", "/charge", "charges a card", "d",
        response_shape, [], request_shape=request_shape,
    )

    result = queries.describe_api(conn, "payments-service", "POST", "/charge")

    assert result["api_shape"] == {
        "method": "POST",
        "path": "/charge",
        "endpoint_kind": "rest",
        "request": {"body": request_shape},
        "response": {"body": response_shape},
    }


def test_describe_api_classifies_a_health_check_path():
    from orbitkb.mcp.queries import classify_endpoint_kind

    assert classify_endpoint_kind("/health") == "health_check"
    assert classify_endpoint_kind("/actuator/health") == "health_check"
    assert classify_endpoint_kind("/internal/orders") == "internal"
    assert classify_endpoint_kind("/orders/{id}/cancel") == "rest"
