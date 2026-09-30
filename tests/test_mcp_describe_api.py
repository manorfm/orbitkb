"""Coverage for describe_api's request_shape field — mirrors the existing
response_shape treatment (see db/repositories/apis.py upsert_api)."""
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    EntryPoint,
    Evidence,
    FlowEdge,
    SecurityRequirement,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.route_patterns import route_pattern_covers
from orbitkb.export.markdown import export_markdown
from orbitkb.mcp import queries

SAMPLE_ORDER = Path(__file__).resolve().parents[1] / "verify/flow_corpus/sample-order-kotlin-service"


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
        "request": {"body": request_shape, "headers": []},
        "response": {"body": response_shape, "headers": []},
        "security": None,
    }


def test_describe_api_classifies_a_health_check_path():
    from orbitkb.mcp.queries import classify_endpoint_kind

    assert classify_endpoint_kind("/health") == "health_check"
    assert classify_endpoint_kind("/actuator/health") == "health_check"
    assert classify_endpoint_kind("/internal/orders") == "internal"
    assert classify_endpoint_kind("/orders/{id}/cancel") == "rest"


def test_route_pattern_covers_matches_a_trailing_double_star():
    assert route_pattern_covers("/restaurants/{id}/**", "/restaurants/{restaurantId}/destinations")
    assert route_pattern_covers("/restaurants/{id}/**", "/restaurants/{id}")
    assert not route_pattern_covers("/restaurants/{id}/**", "/clusters/{id}")


def test_route_pattern_covers_requires_equal_length_without_a_double_star():
    assert route_pattern_covers("/orders/{id}/cancel", "/orders/{orderId}/cancel")
    assert not route_pattern_covers("/orders/{id}/cancel", "/orders/{id}/cancel/confirm")
    assert not route_pattern_covers("/orders/{id}", "/orders/{id}/cancel")


def test_route_pattern_covers_any_request_wildcard():
    assert route_pattern_covers("**", "/anything/at/all")
    assert route_pattern_covers("**", "/")


def test_describe_api_includes_the_first_matching_security_requirement(tmp_path: Path):
    """Spring Security evaluates authorizeHttpRequests rules in declaration order
    and stops at the first match -- a specific POST rule declared before a
    broader catch-all must win over the catch-all for the route it covers.
    """
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-manager", "/tmp/menu-manager", "jvm-spring")
    apis_repo.upsert_api(conn, service_id, "POST", "/restaurants/{id}/destinations", "s", "d", [], [])
    evidence = Evidence("SecurityConfig.kt", 1, 1)
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(security_requirements=[
        SecurityRequirement(
            "/restaurants/{id}/destinations", "POST", None,
            "custom:RestaurantAccessAuthorizationManager", ("MANAGER",), evidence,
        ),
        SecurityRequirement("**", None, None, "authenticated", (), evidence),
    ]))

    result = queries.describe_api(conn, "menu-manager", "POST", "/restaurants/{id}/destinations")

    assert result["api_shape"]["security"] == {
        "requirement": "custom:RestaurantAccessAuthorizationManager", "roles": ["MANAGER"],
    }


def test_describe_api_security_is_none_when_no_requirement_covers_the_route(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    apis_repo.upsert_api(conn, service_id, "GET", "/orders", "s", "d", [], [])

    result = queries.describe_api(conn, "orders-service", "GET", "/orders")

    assert result["api_shape"]["security"] is None


def test_describe_api_includes_request_and_response_headers_in_api_shape(tmp_path: Path):
    from orbitkb.analysis.models import ApiHeader

    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-manager", "/tmp/menu-manager", "jvm-spring")
    apis_repo.upsert_api(conn, service_id, "GET", "/menus/active", "s", "d", [], [])
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(api_headers=[
        ApiHeader("GET", "/menus/active", "request", "Accept-Language", Evidence("MenuController.kt", 1, 1)),
        ApiHeader("GET", "/menus/active", "response", "ETag", Evidence("MenuController.kt", 1, 1)),
        ApiHeader("GET", "/menus/active", "response", "Cache-Control", Evidence("MenuController.kt", 1, 1)),
    ]))

    result = queries.describe_api(conn, "menu-manager", "GET", "/menus/active")

    assert result["api_shape"]["request"]["headers"] == ["Accept-Language"]
    assert result["api_shape"]["response"]["headers"] == ["Cache-Control", "ETag"]


def test_describe_api_headers_are_empty_lists_when_none_were_extracted(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    apis_repo.upsert_api(conn, service_id, "GET", "/orders", "s", "d", [], [])

    result = queries.describe_api(conn, "orders-service", "GET", "/orders")

    assert result["api_shape"]["request"]["headers"] == []
    assert result["api_shape"]["response"]["headers"] == []


def test_describe_api_exposes_route_reachable_source_calls_separately(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "sample-order", str(SAMPLE_ORDER), "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, StaticAnalysisEngine().analyze(SAMPLE_ORDER, "jvm-spring"))
    route = "/venues/{restaurantId}/spots/{tableId}/checks/{billId}/items"
    apis_repo.upsert_api(conn, service_id, "POST", route, "s", "d", [], [])
    apis_repo.upsert_api(conn, service_id, "GET", "/health", "s", "d", [], [])

    order = queries.describe_api(conn, "sample-order", "POST", route)
    health = queries.describe_api(conn, "sample-order", "GET", "/health")

    assert order["calls"] == []
    assert {(call["target_service"], call["target_method"], call["target_path"])
            for call in order["source_calls"]} == {
        ("catalog-service", "GET", "/venues/{restaurantId}/catalogs/{menuId}/products/{itemId}/summary"),
        ("catalog-service", "GET", "/venues/{restaurantId}/ingredients/{ingredientId}"),
    }
    assert all(call["destination_status"] == "unresolved" for call in order["source_calls"])
    assert order["source_calls_status"] == "assessed"
    assert health["source_calls"] == []
    assert health["source_calls_status"] == "unassessed"

    api_id = apis_repo.get_api_by_key(conn, service_id, "POST", route)["id"]
    service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
        {"to_service_name": "catalog-service", "call_kind": "http", "reason": "load catalog",
         "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.8,
         "target_kind": "unknown"},
    ], [])
    represented = queries.describe_api(conn, "sample-order", "POST", route)
    assert len(represented["calls"]) == 1
    assert represented["source_calls"] == order["source_calls"]

    flows_repo.replace_analysis(conn, service_id, AnalysisResult())
    removed = queries.describe_api(conn, "sample-order", "POST", route)
    assert removed["source_calls"] == []
    assert removed["source_calls_status"] == "unassessed"


def test_api_call_status_distinguishes_absent_complete_and_limited_flow(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    apis_repo.upsert_api(conn, service_id, "GET", "/slow", "s", "d", [], [])

    absent = queries.describe_api(conn, "menu-service", "GET", "/slow")
    export_markdown(conn, tmp_path / "docs")
    markdown = (tmp_path / "docs/menu-service/apis/get-slow.md").read_text(encoding="utf-8")
    assert absent["source_calls_status"] == "unassessed"
    assert "route source flow unavailable; additional calls unknown" in markdown
    assert "no dependency detected" not in markdown

    source = Evidence("Flow.kt", 1, 1)
    entrypoint = EntryPoint("http", "GET", "/slow", "Controller.get", source)
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(entrypoints=[entrypoint]))
    complete = queries.describe_api(conn, "menu-service", "GET", "/slow")
    assert complete["source_calls_status"] == "assessed"
    assert complete["source_calls"] == []

    symbols = ["Controller.get", *(f"Worker{index}.run" for index in range(10))]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge(current, following, "invokes", source)
               for current, following in zip(symbols, symbols[1:])],
    ))
    limited = queries.describe_api(conn, "menu-service", "GET", "/slow")
    assert limited["source_calls_status"] == "limited"
