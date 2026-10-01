from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.canonical import ServiceKey
from orbitkb.mcp import queries


def test_describe_service_includes_its_components(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    components_repo.upsert_component(
        conn, service_id, "OrdersController", "orders/controller.py",
        "Handles order creation and lookup.", [],
    )

    result = queries.describe_service(conn, "orders-service")

    assert result["components"] == [
        {"name": "OrdersController", "file_path": "orders/controller.py", "summary": "Handles order creation and lookup."}
    ]


def test_describe_service_keeps_canonical_http_target_separate_from_indexed_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(
        ServiceKey("orders-service"), AnalysisResult(static_service_calls=[
            StaticServiceCall("Orders.fetch", "catalog-service", "http", "GET", "/catalog/{id}",
                              Evidence("CatalogClient.kt", 8, 8)),
        ]),
    ))

    result = queries.describe_service(conn, "orders-service")

    assert result["calls"] == []
    assert result["source_targets"] == [
        {"target_service": "catalog-service", "destination_status": "unresolved"},
    ]
    assert result["pagination"]["source_targets"] == {"total": 1, "truncated": False}


def test_describe_service_paginates_source_targets_independently(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(
        ServiceKey("orders-service"), AnalysisResult(static_service_calls=[
            StaticServiceCall("Orders.fetch", target, "http", "GET", "/items",
                              Evidence("Clients.kt", line, line))
            for line, target in enumerate(("catalog-service", "pricing-service"), start=1)
        ]),
    ))

    first = queries.describe_service(conn, "orders-service", limit=1)
    second = queries.describe_service(conn, "orders-service", limit=1, offset=1)

    assert [item["target_service"] for item in first["source_targets"]] == ["catalog-service"]
    assert [item["target_service"] for item in second["source_targets"]] == ["pricing-service"]
    assert first["pagination"]["source_targets"] == {"total": 2, "truncated": True}
    assert second["pagination"]["source_targets"] == {"total": 2, "truncated": False}


def test_describe_service_reports_source_target_coverage(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    source = Evidence("OrdersController.kt", 1, 1)
    entrypoint = EntryPoint("http", "GET", "/orders", "OrdersController.list", source)
    service = ServiceKey("orders-service")

    absent = queries.describe_service(conn, "orders-service")
    assert absent["source_targets"] == []
    assert absent["source_targets_status"] == "unassessed"

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge("OrdersController.list", "dynamicCall", "invokes", source)],
    )))
    limited = queries.describe_service(conn, "orders-service")
    assert limited["source_targets"] == []
    assert limited["source_targets_status"] == "limited"

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
    )))
    assessed = queries.describe_service(conn, "orders-service")
    assert assessed["source_targets"] == []
    assert assessed["source_targets_status"] == "assessed"


def test_describe_service_limits_coverage_when_an_indexed_api_is_missing_from_snapshot(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    apis_repo.upsert_api(conn, service_id, "GET", "/orders", "s", "d", [], [])
    apis_repo.upsert_api(conn, service_id, "POST", "/orders", "s", "d", [], [])
    source = Evidence("OrdersController.kt", 1, 1)
    service = ServiceKey("orders-service")
    get_route = EntryPoint("http", "GET", "/orders", "OrdersController.list", source)
    post_route = EntryPoint("http", "POST", "/orders", "OrdersController.create", source)
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[get_route],
    )))

    partial = queries.describe_service(conn, "orders-service")
    assert partial["source_targets_status"] == "limited"
    assert queries.describe_service(conn, "orders-service", limit=1, offset=1)[
        "source_targets_status"
    ] == "limited"

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[get_route, post_route],
    )))
    complete = queries.describe_service(conn, "orders-service")
    assert complete["source_targets_status"] == "assessed"


def test_describe_service_deduplicates_http_targets_only_against_http_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    api_id = apis_repo.upsert_api(conn, service_id, "GET", "/orders", "s", "d", [], [])
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(
        ServiceKey("orders-service"), AnalysisResult(static_service_calls=[
            StaticServiceCall("Orders.fetch", "catalog-service", "http", "GET", "/catalog",
                              Evidence("CatalogClient.kt", 8, 8)),
        ]),
    ))
    service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
        {"to_service_name": "catalog-service", "call_kind": "queue_publish"},
    ], [])

    with_queue = queries.describe_service(conn, "orders-service")
    assert with_queue["source_targets"] == [
        {"target_service": "catalog-service", "destination_status": "unresolved"},
    ]

    service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
        {"to_service_name": "catalog-service", "call_kind": "http"},
    ], [])
    with_http = queries.describe_service(conn, "orders-service")
    assert with_http["source_targets"] == []
