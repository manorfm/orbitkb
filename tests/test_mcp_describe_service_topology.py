from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.mcp import queries

EVIDENCE = [{"file": "main.py", "start_line": 1, "end_line": 5}]


def test_describe_service_topology_scopes_the_diagram_to_one_hop(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "node-ts")
    services_repo.ensure_service(conn, "unrelated-service", "/tmp/unrelated", "python")
    api_id = apis_repo.upsert_api(conn, orders_id, "POST", "/orders", "s", "d", [], EVIDENCE)
    service_calls_repo.replace_calls_for_api(
        conn, orders_id, api_id,
        [{"to_service_name": "payments-service", "call_kind": "http", "reason": "charge",
          "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.9, "target_kind": "unknown"}],
        EVIDENCE,
    )
    service_calls_repo.reconcile_service_call_targets(conn)

    result = queries.describe_service_topology(conn, "orders-service")

    assert result["service"] == "orders-service"
    assert result["hops"] == 1
    assert result["mermaid"].startswith("graph TD")
    assert "payments-service" in result["mermaid"]
    assert "unrelated-service" not in result["mermaid"]
    assert result["legend"]


def test_describe_service_topology_reports_an_error_for_an_unknown_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")

    result = queries.describe_service_topology(conn, "does-not-exist")

    assert "error" in result


def test_describe_service_topology_scopes_same_named_services_by_repository(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    for repository in ("alpha", "beta"):
        repo_id = repositories_repo.ensure_repository(conn, repository, f"/tmp/{repository}")
        orders_id = services_repo.ensure_service(
            conn, "orders", f"/tmp/{repository}/orders", "python", repository_id=repo_id,
        )
        api_id = apis_repo.upsert_api(conn, orders_id, "POST", "/orders", "s", "d", [], EVIDENCE)
        service_calls_repo.replace_calls_for_api(conn, orders_id, api_id, [
            {"to_service_name": f"vendor-{repository}", "call_kind": "http", "reason": "notify",
             "data_needed": [], "purpose_kind": "other", "confidence": 0.9,
             "target_kind": "external", "resource_type": "saas"},
        ], EVIDENCE)

    diagram = queries.describe_service_topology(conn, "orders", repository="alpha", hops=0)["mermaid"]

    assert '["orders (alpha)"]' in diagram
    assert "orders (beta)" not in diagram
    assert "vendor-alpha" in diagram
    assert "vendor-beta" not in diagram
