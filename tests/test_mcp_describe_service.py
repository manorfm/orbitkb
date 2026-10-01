from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.models import AnalysisResult, Evidence, StaticServiceCall
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import components as components_repo
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
