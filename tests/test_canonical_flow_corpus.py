from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import canonical_snapshots, flows, services
from orbitkb.domain.navigation import KnowledgeNavigator, TraversalPolicy
from orbitkb.mcp import queries

CORPUS = Path(__file__).resolve().parents[1] / "verify/flow_corpus/menu-kotlin-service"
NODE_CORPUS = Path(__file__).resolve().parents[1] / "verify/sample_project/payments-service"


def test_kotlin_spring_route_reaches_feign_client_through_use_case_and_gateway(tmp_path):
    analysis = StaticAnalysisEngine().analyze(CORPUS, "jvm-spring")
    conn = open_db(tmp_path / "menu-flow.db")
    service_id = services.ensure_service(conn, "menu-manager", str(CORPUS), "jvm-spring")
    flows.replace_analysis(conn, service_id, analysis)
    snapshot = canonical_snapshots.read_snapshot(conn, service_id)
    entrypoint = next(fact.subject for fact in snapshot.facts
                      if fact.kind == "entrypoint" and fact.subject.name == "/menus/{id}")

    result = KnowledgeNavigator(snapshot).reachable(entrypoint, TraversalPolicy())

    calls = [fact for fact in result.facts if fact.kind == "service_call"]
    assert len(calls) == 1
    assert calls[0].attributes == {
        "target_service": "restaurant-service", "protocol": "http",
        "target_method": "GET", "target_path": "/restaurants/{id}",
    }
    assert result.path_to(calls[0].id) == (
        "MenuController.get", "MenuUseCase.get", "MenuGateway.fetch",
    )
    assert calls[0].sources[0].file_path.endswith("MenuGateway.kt")
    assert not result.truncated

    detail = queries.describe_entrypoint(conn, "menu-manager", "http", "GET", "/menus/{id}")
    path_symbols = set(result.path_to(calls[0].id))
    expected_edges = {(edge.source, edge.target, edge.kind) for edge in analysis.edges
                      if edge.source in path_symbols}
    actual_edges = {(edge["from"], edge["to"], edge["kind"]) for edge in detail["flow"]}
    assert actual_edges == expected_edges
    assert detail["service_calls"][0]["target_service"] == "restaurant-service"
    assert detail["service_calls"][0]["path"] == "/restaurants/{id}"
    feign_boundary = next(boundary for boundary in detail["boundaries"]
                          if boundary["target"] == "restaurantClient.getRestaurant")
    assert (feign_boundary["kind"], feign_boundary["source"]) == ("external_call", "MenuGateway.fetch")
    assert feign_boundary["evidence"]["file"].endswith("MenuGateway.kt")


def test_node_route_public_flow_reaches_service_client_and_message_publisher(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-flow.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    detail = queries.describe_entrypoint(conn, "payments-service", "http", "POST", "/charge")

    pairs = {(edge["from"], edge["to"]) for edge in detail["flow"]}
    assert ("payments.routes.http.post:/charge", "PaymentService.chargeCustomer") in pairs
    assert ("PaymentService.chargeCustomer", "card_gateway.client.charge") in pairs
    assert ("card_gateway.client.charge", "axios.post") in pairs
    assert ("PaymentService.chargeCustomer", "PaymentService.publishPaymentEvent") in pairs
    assert ("PaymentService.publishPaymentEvent", "kafka.publish") in pairs
    assert not any(source == "PaymentService.refundCustomer" for source, _ in pairs)
