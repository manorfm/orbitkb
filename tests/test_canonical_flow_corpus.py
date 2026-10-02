import shutil
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
    assert ("kafka.publish", "producer.send", None, "unknown") in [
        (operation["source"], operation["target"], operation["channel"], operation["status"])
        for operation in detail["message_operations"]
    ]


def test_node_route_resolves_only_its_literal_kafka_topics(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-topics.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    charge = queries.describe_entrypoint(conn, "payments-service", "http", "POST", "/charge")
    refund = queries.describe_entrypoint(conn, "payments-service", "http", "POST", "/payments/:id/refund")

    assert [item["channel"] for item in charge["message_operations"]
            if item["status"] == "confirmed"] == [
        "payment.failed", "payment.failed", "payment.completed",
    ]
    assert refund["message_operations"] == []


def test_node_route_reports_only_reachable_external_http_destinations(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-http.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    charge = queries.describe_entrypoint(conn, "payments-service", "http", "POST", "/charge")
    refund = queries.describe_entrypoint(conn, "payments-service", "http", "POST", "/payments/:id/refund")

    assert {(item["source"], item["method"], item["host"], item["path"])
            for item in charge["external_http_calls"]} == {
        ("card_gateway.client.charge", "POST", "card-gateway.vendor.io", "/v1/charge"),
        ("notify_hub.client.send", "POST", "notify-hub.vendor.io", "/v1/send"),
    }
    assert {(item["source"], item["method"], item["host"], item["path"])
            for item in refund["external_http_calls"]} == {
        ("card_gateway.client.refund", "POST", "card-gateway.vendor.io", "/v1/refund"),
    }
    assert charge["service_calls"] == []


def test_node_service_topology_shows_proven_http_hosts_as_external_nodes(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-topology.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    diagram = queries.describe_service_topology(conn, "payments-service", hops=0)["mermaid"]

    assert 'ext_card_gateway_vendor_io(("card-gateway.vendor.io"))' in diagram
    assert 'ext_notify_hub_vendor_io(("notify-hub.vendor.io"))' in diagram
    assert 'svc_payments_service -.->|HTTPS POST /v1/charge| ext_card_gateway_vendor_io' in diagram
    assert 'svc_payments_service -.->|HTTPS POST /v1/refund| ext_card_gateway_vendor_io' in diagram
    assert 'svc_payments_service -.->|HTTPS POST /v1/send| ext_notify_hub_vendor_io' in diagram
    assert 'svc_card_gateway_vendor_io' not in diagram


def test_node_service_topology_shows_only_proven_message_channels(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-message-topology.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    diagram = queries.describe_service_topology(conn, "payments-service", hops=0)["mermaid"]

    assert 'channel_payments_service_payment_failed(("channel: payment.failed"))' in diagram
    assert 'channel_payments_service_payment_completed(("channel: payment.completed"))' in diagram
    assert diagram.count('svc_payments_service -.->|publish| channel_payments_service_payment_failed') == 1
    assert diagram.count('svc_payments_service -.->|publish| channel_payments_service_payment_completed') == 1
    assert 'channel_payments_service_order_cancelled(("channel: order.cancelled"))' in diagram
    assert 'channel_payments_service_order_cancelled -.->|consume| svc_payments_service' in diagram
    assert 'Kafka' not in diagram
    messaging = queries.describe_messages(conn, "payments-service")
    assert ("consumes", "order.cancelled") in {
        (contract["direction"], contract["exchange"])
        for contract in messaging["static_contracts"]
    }
    consumption = queries.describe_entrypoint(
        conn, "payments-service", "message", "CONSUME", "order.cancelled",
    )
    assert [(contract["direction"], contract["channel"], contract["evidence"]["start_line"])
            for contract in consumption["message_contracts"]] == [
        ("consumes", "order.cancelled", 21),
    ]
    assert consumption["message_operations"] == []


def test_node_consumer_reaches_refund_from_proven_bootstrap_argument(tmp_path):
    analysis = StaticAnalysisEngine().analyze(NODE_CORPUS, "node-js")
    conn = open_db(tmp_path / "payments-consumer-flow.db")
    service_id = services.ensure_service(conn, "payments-service", str(NODE_CORPUS), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    detail = queries.describe_entrypoint(conn, "payments-service", "message", "CONSUME", "order.cancelled")

    assert ("message.consume:order.cancelled", "PaymentService.refundCustomer") in {
        (edge["from"], edge["to"]) for edge in detail["flow"]
    }
    assert [(call["source"], call["host"], call["path"]) for call in detail["external_http_calls"]] == [
        ("card_gateway.client.refund", "card-gateway.vendor.io", "/v1/refund"),
    ]
    assert {(operation["target"], operation["operation"])
            for operation in detail["persistence_operations"]} >= {
        ("Transaction.findOne", "reads"),
        ("LedgerEntry.create", "writes"),
        ("transaction.save", "writes"),
    }


def test_node_consumer_keeps_receiver_unresolved_when_bootstrap_arguments_conflict(tmp_path):
    project = tmp_path / "payments-service"
    shutil.copytree(NODE_CORPUS, project)
    server = project / "server.js"
    source = server.read_text()
    source = source.replace(
        "const paymentService = require('./services/payment.service');",
        "const paymentService = require('./services/payment.service');\n"
        "const otherService = require('./services/other.service');",
    ).replace(
        "await startOrderCancelledConsumer(paymentService);",
        "await startOrderCancelledConsumer(paymentService);\n"
        "  await startOrderCancelledConsumer(otherService);",
    )
    server.write_text(source)
    (project / "services/other.service.js").write_text(
        "class OtherService { refundCustomer() { return null; } }\n"
        "module.exports = new OtherService();\n",
    )
    analysis = StaticAnalysisEngine().analyze(project, "node-js")
    conn = open_db(tmp_path / "ambiguous-consumer.db")
    service_id = services.ensure_service(conn, "payments-service", str(project), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    detail = queries.describe_entrypoint(conn, "payments-service", "message", "CONSUME", "order.cancelled")

    assert ("message.consume:order.cancelled", "paymentService.refundCustomer") in {
        (edge["from"], edge["to"]) for edge in detail["flow"]
    }
    assert detail["external_http_calls"] == []


def test_node_consumer_keeps_receiver_unresolved_when_another_caller_has_unknown_argument(tmp_path):
    project = tmp_path / "payments-service"
    shutil.copytree(NODE_CORPUS, project)
    (project / "extra.js").write_text(
        "import { startOrderCancelledConsumer } from './events/kafka';\n"
        "startOrderCancelledConsumer(otherService);\n",
    )
    analysis = StaticAnalysisEngine().analyze(project, "node-js")
    conn = open_db(tmp_path / "unknown-consumer.db")
    service_id = services.ensure_service(conn, "payments-service", str(project), "node-js")
    flows.replace_analysis(conn, service_id, analysis)

    detail = queries.describe_entrypoint(conn, "payments-service", "message", "CONSUME", "order.cancelled")

    assert ("message.consume:order.cancelled", "paymentService.refundCustomer") in {
        (edge["from"], edge["to"]) for edge in detail["flow"]
    }
