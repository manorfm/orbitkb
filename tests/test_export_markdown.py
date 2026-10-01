import hashlib
import json
from pathlib import Path

from orbitkb.analysis.canonical_projection import project_analysis
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.analysis.models import (
    AnalysisResult,
    ApiHeader,
    CloudFact,
    EntryPoint,
    Evidence,
    FlowEdge,
    Injection,
    SecurityRequirement,
    StaticServiceCall,
)
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import canonical_snapshots as snapshots_repo
from orbitkb.db.repositories import flows as flows_repo
from orbitkb.db.repositories import messages as messages_repo
from orbitkb.db.repositories import persistence as persistence_repo
from orbitkb.db.repositories import service_calls as service_calls_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.domain.canonical import ServiceKey
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import generate_topology_diagram

SAMPLE_ORDER = Path(__file__).resolve().parents[1] / "verify/flow_corpus/sample-order-kotlin-service"


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
    assert "dependency analysis unavailable" in index_text.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "POST /orders" in index_text

    api_files = list((out_dir / "orders-service" / "apis").glob("*.md"))
    assert len(api_files) == 1
    api_text = api_files[0].read_text(encoding="utf-8")
    assert "order_id" in api_text
    assert "authorization" in api_text


def test_service_exports_keep_http_target_when_indexed_call_uses_other_protocol(tmp_path: Path):
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

    export_markdown(conn, tmp_path / "docs")
    markdown = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    diagram = generate_topology_diagram(conn)

    assert "**catalog-service** (http (unresolved), declared target)" in markdown
    assert 'ext_catalog_service_declared_target(("catalog-service (declared target)"))' in diagram


def test_export_markdown_keeps_distinct_routes_with_the_same_filename_slug(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "python")
    routes = ("/items/id", "/items/{id}")
    for path in routes:
        apis_repo.upsert_api(conn, service_id, "GET", path, path, f"Details for {path}", [], [])

    out_dir = tmp_path / "docs"
    written = export_markdown(conn, out_dir)
    api_dir = out_dir / "catalog-service" / "apis"
    pages = list(api_dir.glob("*.md"))
    index = (out_dir / "catalog-service" / "index.md").read_text(encoding="utf-8")

    assert len(pages) == len(routes)
    assert set(pages).issubset(set(written))
    for path in routes:
        page = next(page for page in pages if page.read_text(encoding="utf-8").startswith(f"# GET {path}\n"))
        assert f"Details for {path}" in page.read_text(encoding="utf-8")
        route_line = next(line for line in index.splitlines() if line.startswith(f"- `GET {path}`"))
        assert f"[detail](apis/{page.name})" in route_line


def test_reexport_removes_only_unchanged_generated_api_pages(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "python")
    for path in ("/old", "/edited", "/current"):
        apis_repo.upsert_api(conn, service_id, "GET", path, path, path, [], [])
    out_dir = tmp_path / "docs"
    export_markdown(conn, out_dir)
    api_dir = out_dir / "catalog-service" / "apis"
    custom = api_dir / "notes.md"
    custom.write_text("my notes", encoding="utf-8")
    edited = api_dir / "get-edited.md"
    edited.write_text("my edited page", encoding="utf-8")

    apis_repo.prune_apis_not_in(conn, service_id, {("GET", "/current")})
    export_markdown(conn, out_dir)

    assert not (api_dir / "get-old.md").exists()
    assert edited.read_text(encoding="utf-8") == "my edited page"
    assert custom.read_text(encoding="utf-8") == "my notes"
    assert (api_dir / "get-current.md").exists()
    index = (out_dir / "catalog-service" / "index.md").read_text(encoding="utf-8")
    assert "GET /old" not in index
    assert "GET /current" in index


def test_export_preserves_an_existing_user_page_with_a_generated_filename(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "python")
    apis_repo.upsert_api(conn, service_id, "GET", "/items", "s", "d", [], [])
    api_dir = tmp_path / "docs/catalog-service/apis"
    api_dir.mkdir(parents=True)
    user_page = api_dir / "get-items.md"
    user_page.write_text("user content", encoding="utf-8")

    export_markdown(conn, tmp_path / "docs")

    pages = list(api_dir.glob("*.md"))
    generated = next(page for page in pages if page != user_page)
    assert user_page.read_text(encoding="utf-8") == "user content"
    assert generated.read_text(encoding="utf-8").startswith("# GET /items\n")
    index = (tmp_path / "docs/catalog-service/index.md").read_text(encoding="utf-8")
    assert f"[detail](apis/{generated.name})" in index


def test_reexport_ignores_manifest_paths_outside_the_api_directory(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "catalog-service", "/tmp/catalog", "python")
    apis_repo.upsert_api(conn, service_id, "GET", "/items", "s", "d", [], [])
    out_dir = tmp_path / "docs"
    export_markdown(conn, out_dir)
    service_dir = out_dir / "catalog-service"
    outside = service_dir / "private.md"
    outside.write_text("private", encoding="utf-8")
    manifest = service_dir / "apis/.orbitkb-pages.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["pages"]["../private.md"] = hashlib.sha256(outside.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    apis_repo.prune_apis_not_in(conn, service_id, set())
    export_markdown(conn, out_dir)

    assert outside.read_text(encoding="utf-8") == "private"
    assert not (service_dir / "apis/get-items.md").exists()


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


def test_export_markdown_marks_messaging_unassessed_without_snapshot(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    services_repo.ensure_service(conn, "empty-service", "/tmp/empty", "python")

    export_markdown(conn, tmp_path / "docs")

    page = (tmp_path / "docs/empty-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "dependency analysis unavailable" in dependencies
    assert "no dependency detected" not in dependencies
    messaging = page.split("## Messaging", 1)[1].split("## Cloud", 1)[0]
    assert "Static analysis:** unknown" in messaging
    assert messaging.count("- (not assessed)") == 2
    assert "none detected" not in messaging


def test_service_index_does_not_claim_no_dependencies_when_flow_is_unresolved(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    source = Evidence("MenuController.kt", 1, 1)
    entrypoint = EntryPoint("http", "GET", "/menus", "MenuController.list", source)
    service = ServiceKey("menu-service")
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge("MenuController.list", "dynamicCall", "invokes", source)],
    )))

    export_markdown(conn, tmp_path / "docs")
    page = (tmp_path / "docs/menu-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]

    assert "static flow limited; other dependencies may exist" in dependencies
    assert "no dependency detected" not in dependencies

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
    )))
    export_markdown(conn, tmp_path / "docs")
    complete = (tmp_path / "docs/menu-service/index.md").read_text(encoding="utf-8")
    assert "no dependency detected" in complete.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]


def test_service_index_marks_http_flow_unassessed_without_canonical_routes(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    service = ServiceKey("orders-service")
    out_dir = tmp_path / "docs"
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult()))

    export_markdown(conn, out_dir)
    page = (out_dir / "orders-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "HTTP route flow unassessed; other dependencies may exist" in dependencies
    assert "no dependency detected" not in dependencies

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        static_service_calls=[StaticServiceCall(
            "Orders.fetch", "catalog-service", "http", "GET", "/catalog",
            Evidence("CatalogClient.kt", 8, 8),
        )],
    )))
    export_markdown(conn, out_dir)
    page = (out_dir / "orders-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "**catalog-service** (http (unresolved), declared target)" in dependencies
    assert "HTTP route flow unassessed; other dependencies may exist" in dependencies


def test_service_index_keeps_known_dependency_and_warns_about_limited_flow(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = _seed(conn)
    source = Evidence("OrdersController.kt", 1, 1)
    entrypoint = EntryPoint("http", "POST", "/orders", "OrdersController.create", source)
    service = ServiceKey("orders-service")
    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
        edges=[FlowEdge("OrdersController.create", "dynamicCall", "invokes", source)],
    )))

    export_markdown(conn, tmp_path / "docs")
    page = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]

    assert dependencies.count("payments-service") == 1
    assert "charge the customer" in dependencies
    assert "static flow limited; other dependencies may exist" in dependencies

    snapshots_repo.replace_snapshot(conn, service_id, project_analysis(service, AnalysisResult(
        entrypoints=[entrypoint],
    )))
    export_markdown(conn, tmp_path / "docs")
    complete = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    assert "static flow limited" not in complete.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]


def test_markdown_reports_source_proven_http_target_without_model_call(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(static_service_calls=[
        StaticServiceCall(
            "Orders.fetch", "catalog-service", "http", "GET", "/catalog/{id}",
            Evidence("CatalogClient.kt", 8, 8),
        ),
    ]))
    conn.execute("DELETE FROM canonical_snapshots WHERE service_id = ?", (service_id,))

    export_markdown(conn, tmp_path / "docs")

    text = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    dependencies = text.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
    assert "catalog-service" in dependencies
    assert "http (unresolved)" in dependencies
    assert "dependency analysis unavailable" in dependencies
    assert "no dependency detected" not in dependencies


def test_service_index_reads_http_target_from_canonical_snapshot_without_legacy_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "jvm-spring")
    snapshot = project_analysis(ServiceKey("orders-service"), AnalysisResult(static_service_calls=[
        StaticServiceCall("Orders.fetch", "catalog-service", "http", "GET", "/catalog/{id}",
                          Evidence("CatalogClient.kt", 8, 8)),
    ]))
    snapshots_repo.replace_snapshot(conn, service_id, snapshot)

    export_markdown(conn, tmp_path / "docs")

    page = (tmp_path / "docs/orders-service/index.md").read_text(encoding="utf-8")
    dependencies = page.split("## Depends on\n", 1)[1].split("\n## APIs", 1)[0]
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


def test_api_markdown_uses_first_source_proven_route_security_rule(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    for method, path in (("GET", "/health"), ("POST", "/orders"), ("GET", "/orders")):
        apis_repo.upsert_api(conn, service_id, method, path, "s", "d", [], [])
    source = Evidence("SecurityConfig.kt", 1, 1)
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(security_requirements=[
        SecurityRequirement("/health", "GET", None, "permitAll", (), source),
        SecurityRequirement("/orders", "POST", None, "hasRole", ("ADMIN",), source),
        SecurityRequirement("**", None, None, "authenticated", (), source),
        SecurityRequirement(None, None, "OrderController.create", "denyAll", (), source),
    ]))

    export_markdown(conn, tmp_path / "docs")

    api_dir = tmp_path / "docs/menu-service/apis"
    get_health = (api_dir / "get-health.md").read_text(encoding="utf-8")
    post_orders = (api_dir / "post-orders.md").read_text(encoding="utf-8")
    get_orders = (api_dir / "get-orders.md").read_text(encoding="utf-8")
    assert "## Declared route security\n- permitAll" in get_health
    assert "## Declared route security\n- hasRole (roles: ADMIN)" in post_orders
    assert "## Declared route security\n- authenticated" in get_orders
    assert "denyAll" not in get_health + post_orders + get_orders


def test_api_markdown_shows_source_proven_headers_for_the_matching_route(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    for method in ("GET", "POST"):
        apis_repo.upsert_api(conn, service_id, method, "/menus", "s", "d", [], [])
    source = Evidence("MenuController.kt", 4, 4)
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(api_headers=[
        ApiHeader("GET", "/menus", "request", "Accept-Language", source),
        ApiHeader("GET", "/menus", "response", "ETag", source),
        ApiHeader("POST", "/menus", "request", "Idempotency-Key", source),
    ]))

    export_markdown(conn, tmp_path / "docs")

    api_dir = tmp_path / "docs/menu-service/apis"
    get_doc = (api_dir / "get-menus.md").read_text(encoding="utf-8")
    post_doc = (api_dir / "post-menus.md").read_text(encoding="utf-8")
    assert "## Request headers\n- `Accept-Language`" in get_doc
    assert "## Response headers\n- `ETag`" in get_doc
    assert "Idempotency-Key" not in get_doc
    assert "## Request headers\n- `Idempotency-Key`" in post_doc
    assert "Accept-Language" not in post_doc
    assert "ETag" not in post_doc

    flows_repo.replace_analysis(conn, service_id, AnalysisResult())
    export_markdown(conn, tmp_path / "docs")
    get_doc = (api_dir / "get-menus.md").read_text(encoding="utf-8")
    assert "## Request headers" not in get_doc
    assert "## Response headers" not in get_doc


def test_api_markdown_scopes_source_proven_http_calls_to_reachable_route(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "sample-order", str(SAMPLE_ORDER), "jvm-spring")
    analysis = StaticAnalysisEngine().analyze(SAMPLE_ORDER, "jvm-spring")
    flows_repo.replace_analysis(conn, service_id, analysis)
    route = "/venues/{restaurantId}/spots/{tableId}/checks/{billId}/items"
    apis_repo.upsert_api(conn, service_id, "POST", route, "s", "d", [], [])
    apis_repo.upsert_api(conn, service_id, "GET", "/health", "s", "d", [], [])

    export_markdown(conn, tmp_path / "docs")

    api_dir = tmp_path / "docs/sample-order/apis"
    order_doc = next(path.read_text(encoding="utf-8") for path in api_dir.glob("post-*.md"))
    health_doc = (api_dir / "get-health.md").read_text(encoding="utf-8")
    calls = order_doc.split("## Calls\n", 1)[1].split("\n##", 1)[0]
    assert calls.count("**catalog-service**") == 2
    assert "/catalogs/{menuId}/products/{itemId}/summary" in calls
    assert "/ingredients/{ingredientId}" in calls
    assert "source-proven" in calls
    assert "catalog-service" not in health_doc

    api_id = apis_repo.get_api_by_key(conn, service_id, "POST", route)["id"]
    service_calls_repo.replace_calls_for_api(conn, service_id, api_id, [
        {"to_service_name": "catalog-service", "call_kind": "http", "reason": "load catalog",
         "data_needed": [], "purpose_kind": "data_fetch", "confidence": 0.8,
         "target_kind": "unknown"},
    ], [])
    export_markdown(conn, tmp_path / "docs")
    order_doc = next(path.read_text(encoding="utf-8") for path in api_dir.glob("post-*.md"))
    calls = order_doc.split("## Calls\n", 1)[1].split("\n##", 1)[0]
    assert calls.count("**catalog-service**") == 1
    assert "load catalog" in calls
    assert "/catalogs/{menuId}/products/{itemId}/summary" in calls
    assert "/ingredients/{ingredientId}" in calls
    assert "source-proven" in calls


def test_api_markdown_reports_bounded_flow_instead_of_claiming_no_calls(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "menu-service", "/tmp/menu", "jvm-spring")
    apis_repo.upsert_api(conn, service_id, "GET", "/slow", "s", "d", [], [])
    source = Evidence("Flow.kt", 1, 1)
    symbols = ["Controller.get", *(f"Worker{index}.run" for index in range(10))]
    flows_repo.replace_analysis(conn, service_id, AnalysisResult(
        entrypoints=[EntryPoint("http", "GET", "/slow", symbols[0], source)],
        edges=[FlowEdge(current, following, "invokes", source)
               for current, following in zip(symbols, symbols[1:])],
    ))

    export_markdown(conn, tmp_path / "docs")

    api_doc = (tmp_path / "docs/menu-service/apis/get-slow.md").read_text(encoding="utf-8")
    calls = api_doc.split("## Calls\n", 1)[1].split("\n##", 1)[0]
    assert "static flow limited; other calls may exist" in calls
    assert "no dependency detected" not in calls
