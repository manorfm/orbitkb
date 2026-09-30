from pathlib import Path

from orbitkb.analysis.models import (
    AnalysisResult,
    CloudFact,
    Evidence,
    FlowEdge,
    Injection,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import generate_topology_diagram


def _seed(conn):
    orders_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    services_repo.update_service_overview(conn, orders_id, "Handles orders.", "Longer description of orders-service.")
    api_id = apis_repo.upsert_api(
        conn, orders_id, "POST", "/orders", "creates an order", "Creates a new order.",
        [{"field": "order_id", "type_desc": "string"}], [],
    )
    apis_repo.replace_api_validations(conn, api_id, [{"kind": "authorization", "description": "needs a bearer token"}])
    service_calls_repo.replace_calls_for_api(
        conn, orders_id, api_id,
        [{
            "to_service_name": "payments-service", "call_kind": "http",
            "reason": "charge the customer", "data_needed": ["amount"],
            "purpose_kind": "data_fetch", "confidence": 0.9,
        }],
        [],
    )
    persistence_repo.replace_persistence_entities(conn, orders_id, [{"name": "orders", "kind": "sql_table", "schema_json": []}], [])
    messages_repo.replace_messages(
        conn, orders_id, [{"direction": "publishes", "channel": "order_created", "shape_json": [], "description": "order created"}], [],
    )
    return orders_id


def test_export_markdown_writes_service_index_and_api_detail(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    _seed(conn)
    out_dir = tmp_path / "docs"

    written = export_markdown(conn, out_dir)

    index_path = out_dir / "orders-service" / "index.md"
    assert index_path in written
    index_text = index_path.read_text(encoding="utf-8")
    assert "orders-service" in index_text
    assert "Handles orders." in index_text
    assert "payments-service" in index_text  # dependency line
    assert "charge the customer" in index_text
    assert "POST /orders" in index_text

    api_files = list((out_dir / "orders-service" / "apis").glob("*.md"))
    assert len(api_files) == 1
    api_text = api_files[0].read_text(encoding="utf-8")
    assert "order_id" in api_text
    assert "authorization" in api_text


def test_export_markdown_includes_a_cloud_section(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = _seed(conn)
    flows_repo.replace_analysis(conn, orders_id, AnalysisResult(cloud_facts=[
        CloudFact("aws", "queue", "sqs", "SendMessage", "publish", "aws-sdk-js-v3", "orders-queue", Evidence("a.ts", 1, 1)),
    ]))
    out_dir = tmp_path / "docs"

    export_markdown(conn, out_dir)

    index_text = (out_dir / "orders-service" / "index.md").read_text(encoding="utf-8")
    assert "## Cloud" in index_text
    assert "sqs" in index_text
    assert "orders-queue" in index_text


def test_export_markdown_service_filter_only_writes_matching_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    _seed(conn)
    services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "node-ts")
    out_dir = tmp_path / "docs"

    written = export_markdown(conn, out_dir, service_filter="orders-service")

    assert all("orders-service" in str(p) for p in written)
    assert not (out_dir / "payments-service").exists()


def test_export_markdown_splits_messaging_into_publishes_and_consumes(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    orders_id = _seed(conn)  # already publishes "order_created"
    messages_repo.replace_messages(
        conn, orders_id,
        [
            {"direction": "publishes", "channel": "order_created", "shape_json": [], "description": "order created"},
            {"direction": "consumes", "channel": "payment_confirmed", "shape_json": [], "description": "payment confirmed"},
        ],
        [],
    )
    out_dir = tmp_path / "docs"

    export_markdown(conn, out_dir)

    index_text = (out_dir / "orders-service" / "index.md").read_text(encoding="utf-8")
    publishes_section = index_text.split("### Publishes")[1].split("### Consumes")[0]
    consumes_section = index_text.split("### Consumes")[1]
    assert "order_created" in publishes_section
    assert "payment_confirmed" not in publishes_section
    assert "payment_confirmed" in consumes_section
    assert "order_created" not in consumes_section


def test_export_markdown_handles_service_with_no_apis(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "empty-service", "/tmp/empty", "python")
    out_dir = tmp_path / "docs"

    written = export_markdown(conn, out_dir)

    index_text = (out_dir / "empty-service" / "index.md").read_text(encoding="utf-8")
    assert "no API detected" in index_text
    assert not (out_dir / "empty-service" / "apis").exists()
    assert len(written) == 1


def test_markdown_reports_source_proven_http_target_without_model_call(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(static_service_calls=[
        StaticServiceCall(
            "Orders.fetch", "catalog-service", "http", "GET", "/catalog/{id}",
            Evidence("CatalogClient.kt", 8, 8),
        ),
    ]))

    export_markdown(conn, tmp_path / "docs")

    text = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    dependencies = text.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "catalog-service" in dependencies
    assert "http (unresolved)" in dependencies
    assert "no dependency detected" not in dependencies


def test_markdown_does_not_repeat_a_source_target_already_in_indexed_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = _seed(conn)
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(static_service_calls=[
        StaticServiceCall(
            "Orders.charge", "payments-service", "http", "POST", "/charges",
            Evidence("PaymentsClient.kt", 8, 8),
        ),
    ]))

    export_markdown(conn, tmp_path / "docs")

    text = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    dependencies = text.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert dependencies.count("payments-service") == 1
    assert "charge the customer" in dependencies


def test_markdown_and_mermaid_agree_on_confirmed_redis_publication(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    publication = FlowEdge(
        "Events.publish", "redis.convertAndSend", "publishes",
        Evidence("Events.kt", 12, 12), boundary_kind="redis_pubsub",
    )
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=[publication]))

    def publications() -> tuple[str, str]:
        export_markdown(conn, tmp_path / "docs")
        markdown = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
        return markdown.split("### Publishes\n", 1)[1].split("\n### Consumes", 1)[0], generate_topology_diagram(conn)

    markdown, mermaid = publications()
    assert "Redis Pub/Sub" in markdown
    assert "channel unresolved" in markdown
    assert "none detected" not in markdown
    assert "Redis Pub/Sub" in mermaid
    assert "events:table" not in markdown + mermaid

    flows_repo.replace_analysis(conn, service_id, AnalysisResult(edges=[FlowEdge(
        "Events.publish", "redis.convertAndSend", "publishes",
        Evidence("Events.kt", 12, 12), confidence="medium", boundary_kind="redis_pubsub",
    )]))
    markdown, mermaid = publications()
    assert "Redis Pub/Sub" not in markdown + mermaid

    flows_repo.replace_analysis(conn, service_id, AnalysisResult())
    markdown, mermaid = publications()
    assert "Redis Pub/Sub" not in markdown + mermaid


def test_markdown_and_mermaid_agree_on_source_proven_mongo_access(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    mongo = Injection("Store.mongo", "MongoTemplate", None, Evidence("Store.kt", 2, 2))
    call = FlowEdge(
        "Store.save", "mongo.execute", "invokes", Evidence("Store.kt", 4, 4),
        boundary_kind="persistence",
    )
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(injections=[mongo], edges=[call]))

    def persistence() -> tuple[str, str]:
        export_markdown(conn, tmp_path / "docs")
        markdown = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
        return markdown.split("## Persistence\n", 1)[1].split("\n## Messaging", 1)[0], generate_topology_diagram(conn)

    markdown, mermaid = persistence()
    assert "MongoDB" in markdown
    assert "collection unresolved" in markdown
    assert "none detected" not in markdown
    assert 'db_orders_service_mongodb[("MongoDB")]' in mermaid

    flows_repo.replace_analysis(conn, service_id, AnalysisResult(injections=[mongo], edges=[FlowEdge(
        "Store.save", "mongo.execute", "invokes", Evidence("Store.kt", 4, 4),
        confidence="medium", boundary_kind="persistence",
    )]))
    markdown, mermaid = persistence()
    assert "MongoDB" not in markdown + mermaid

    flows_repo.replace_analysis(conn, service_id, AnalysisResult())
    markdown, mermaid = persistence()
    assert "MongoDB" not in markdown + mermaid


def test_markdown_does_not_repeat_mongo_access_when_entity_engine_is_known(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(
        injections=[Injection("Store.mongo", "MongoTemplate", None, Evidence("Store.kt", 2, 2))],
        edges=[FlowEdge(
            "Store.save", "mongo.save", "writes", Evidence("Store.kt", 4, 4),
            boundary_kind="persistence",
        )],
    ))
    persistence_repo.replace_persistence_entities(
        conn, service_id,
        [{"name": "orders", "kind": "document", "engine": "mongodb", "schema_json": []}], [],
    )

    export_markdown(conn, tmp_path / "docs")

    markdown = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    section = markdown.split("## Persistence\n", 1)[1].split("\n## Messaging", 1)[0]
    assert "**orders** (document)" in section
    assert "collection unresolved" not in section


def test_markdown_marks_indexed_call_unresolved_in_service_and_api_docs(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    api_id = apis_repo.upsert_api(conn, service_id, "GET", "/menus", "s", "d", [], [])
    service_calls_repo.replace_calls_for_api(
        conn, service_id, api_id,
        [{"to_service_name": "RestaurantClient", "call_kind": "http", "reason": "lookup",
          "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.6,
          "target_kind": "unknown"}], [],
    )

    export_markdown(conn, tmp_path / "docs")

    service_doc = (tmp_path / "docs/menu-service/index.md").read_text(encoding="utf-8")
    api_doc = next((tmp_path / "docs/menu-service/apis").glob("*.md")).read_text(encoding="utf-8")
    mermaid = generate_topology_diagram(conn)
    assert "**RestaurantClient** (http (unresolved), data_fetch)" in service_doc
    assert "**RestaurantClient** (http (unresolved), data_fetch)" in api_doc
    assert 'svc_menu_service -.->|http (unresolved)| ext_restaurantclient' in mermaid

    services_repo.ensure_service(conn, "RestaurantClient", "/tmp/restaurant", "jvm-spring")
    service_calls_repo.reconcile_service_call_targets(conn)
    export_markdown(conn, tmp_path / "docs")
    service_doc = (tmp_path / "docs/menu-service/index.md").read_text(encoding="utf-8")
    api_doc = next((tmp_path / "docs/menu-service/apis").glob("*.md")).read_text(encoding="utf-8")
    assert "**RestaurantClient** (http, data_fetch)" in service_doc
    assert "**RestaurantClient** (http, data_fetch)" in api_doc
    assert "RestaurantClient** (http (unresolved)" not in service_doc + api_doc
