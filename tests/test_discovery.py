from pathlib import Path

from orbitkb.discovery.jvm_stack import JvmSpringDetector
from orbitkb.discovery.node_ts import NodeTsDetector
from orbitkb.discovery.python_stack import PythonDetector
from orbitkb.discovery.walker import discover_services

SAMPLE_ROOT = Path(__file__).resolve().parent.parent / "verify" / "sample_project"


def test_discover_services_finds_all_three():
    candidates = discover_services(SAMPLE_ROOT)
    names = sorted(c.name for c in candidates)
    assert names == ["inventory-service", "orders-service", "payments-service"]


def test_python_detector_matches_and_finds_hints():
    """orders-service is a hexagonal (ports & adapters) FastAPI service: three
    endpoints (create/get/cancel an order) live on a single OrdersController
    class in adapters/http/orders_controller.py, plus a plain function-based
    /health check in main.py (see the next test)."""
    folder = SAMPLE_ROOT / "orders-service"
    detector = PythonDetector()
    assert detector.matches(folder)

    hints = detector.collect_hints(folder)
    assert hints.entry_excerpt is not None
    assert hints.entry_excerpt.file_path == "main.py"

    assert len(hints.endpoints) == 4
    create_order = next(e for e in hints.endpoints if e.path == "/orders")
    assert create_order.method == "POST"
    assert create_order.component_hint == "OrdersController"
    assert any(e.path == "/orders/{order_id}" and e.method == "GET" for e in hints.endpoints)
    assert any(e.path == "/orders/{order_id}/cancel" and e.method == "POST" for e in hints.endpoints)

    outbound_kinds = {c.call_kind for c in hints.outbound_calls}
    assert "http" in outbound_kinds

    assert any(m.direction == "publishes" and m.provider_hint == "kafka" for m in hints.messaging)
    assert any(m.direction == "consumes" and m.provider_hint == "kafka" for m in hints.messaging)
    assert any(p.name_hint == "Order" and p.engine_hint == "postgres" for p in hints.persistence)


def test_python_endpoint_falls_back_to_file_stem_with_no_enclosing_class():
    """main.py's own /health check is the one endpoint in orders-service with no
    enclosing class (it's a bare `@app.get` in the composition root) — a natural
    case of the function-based-routing fallback, distinct from OrdersController."""
    folder = SAMPLE_ROOT / "orders-service"
    hints = PythonDetector().collect_hints(folder)
    endpoint = next(e for e in hints.endpoints if e.path == "/health")
    assert endpoint.component_hint == "main"  # function-based routing, no class wraps it
    assert endpoint.extra_excerpts == []  # every call in the handler is a library call


COMPONENT_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_python"


def test_python_endpoint_inside_a_class_uses_the_class_as_its_component():
    hints = PythonDetector().collect_hints(COMPONENT_FIXTURE_ROOT)
    endpoint = next(e for e in hints.endpoints if e.path == "/orders")
    assert endpoint.component_hint == "OrdersController"


def test_python_endpoint_resolves_a_locally_defined_helper_into_extra_excerpts():
    hints = PythonDetector().collect_hints(COMPONENT_FIXTURE_ROOT)
    endpoint = next(e for e in hints.endpoints if e.path == "/orders")
    assert len(endpoint.extra_excerpts) == 1
    assert endpoint.extra_excerpts[0].file_path == "routes/orders.py"
    assert "def format_total" in endpoint.extra_excerpts[0].text


COMPONENT_KOTLIN_ROOT = Path(__file__).resolve().parent / "fixtures" / "component_kotlin"


def test_kotlin_endpoint_resolves_a_statically_imported_extension_function_into_extra_excerpts():
    """RestaurantController.get() builds its response via `.out()`, a Kotlin
    extension function statically imported from a separate mapper file
    (out.kt) — the exact same-package-different-file mapper idiom that a
    same-file-only regex heuristic can't follow."""
    hints = JvmSpringDetector().collect_hints(COMPONENT_KOTLIN_ROOT)
    endpoint = next(e for e in hints.endpoints if e.path == "/{id}")
    assert len(endpoint.extra_excerpts) == 1
    assert endpoint.extra_excerpts[0].file_path == "out.kt"
    assert "fun Restaurant.out()" in endpoint.extra_excerpts[0].text


def test_python_celery_task_is_tagged_as_an_abstracted_provider(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("celery\n")
    (tmp_path / "main.py").write_text("app = None\n")
    (tmp_path / "tasks.py").write_text(
        "from celery import Celery\n\napp = Celery()\n\n\n@app.task\ndef process_order(order_id):\n    pass\n"
    )

    hints = PythonDetector().collect_hints(tmp_path)

    celery_hints = [m for m in hints.messaging if m.provider_hint == "abstracted"]
    assert len(celery_hints) == 1
    assert celery_hints[0].direction == "consumes"


def test_python_persistence_engine_hint_comes_from_the_manifest_driver(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("fastapi\nsqlalchemy\npsycopg2-binary\n")
    (tmp_path / "main.py").write_text(
        "from sqlalchemy.orm import declarative_base\nBase = declarative_base()\n\n"
        "class Order(Base):\n    __tablename__ = 'orders'\n"
    )

    hints = PythonDetector().collect_hints(tmp_path)

    assert hints.persistence[0].engine_hint == "postgres"


def test_node_ts_detector_matches_and_finds_hints():
    """payments-service is deliberately class-free at the routing layer (plain
    Express handlers in routes/payments.routes.js, delegating to the fat
    PaymentService) — component_hint falls back to the file stem."""
    folder = SAMPLE_ROOT / "payments-service"
    detector = NodeTsDetector()
    assert detector.matches(folder)

    hints = detector.collect_hints(folder)
    assert len(hints.endpoints) == 3
    charge = next(e for e in hints.endpoints if e.path == "/charge")
    assert charge.method == "POST"
    assert charge.component_hint == "payments.routes"  # no enclosing class, file-stem fallback
    assert any(e.path == "/payments/:id" and e.method == "GET" for e in hints.endpoints)
    assert any(e.path == "/payments/:id/refund" and e.method == "POST" for e in hints.endpoints)

    assert any(m.channel_hint for m in hints.messaging)
    assert any(m.provider_hint == "kafka" for m in hints.messaging)  # kafkajs producer.send
    # Mongoose's `new Schema(...)` constructor never names its own model (that
    # happens in a separate `mongoose.model(name, schema)` call this heuristic
    # doesn't chase) — engine/kind is still unambiguous, name_hint stays "?".
    assert any(p.kind == "document" and p.engine_hint == "mongodb" for p in hints.persistence)


def test_jvm_spring_detector_matches_and_finds_hints():
    """inventory-service is Clean Architecture over Cassandra: StockController
    (conforming) exposes two endpoints; legacy.QuickStockPatchController is a
    deliberate architecture-smell endpoint that bypasses every layer — see its
    own component below. The service makes zero outbound calls (a pure leaf in
    the synchronous graph), only reacting to/publishing Kafka events."""
    folder = SAMPLE_ROOT / "inventory-service"
    detector = JvmSpringDetector()
    assert detector.matches(folder)

    hints = detector.collect_hints(folder)
    assert hints.entry_excerpt is not None

    assert len(hints.endpoints) == 3
    stock_check = next(e for e in hints.endpoints if e.path == "/stock/{sku}")
    assert stock_check.method == "GET"
    assert stock_check.component_hint == "StockController"
    assert any(e.path == "/stock/{sku}/reserve" and e.method == "POST" for e in hints.endpoints)
    smell_endpoint = next(e for e in hints.endpoints if e.path == "/internal/stock/{sku}/adjust")
    assert smell_endpoint.component_hint == "QuickStockPatchController"  # its own, separate component

    assert not hints.outbound_calls  # leaf service, no outbound calls

    assert any(m.direction == "consumes" and m.channel_hint == "order.created" for m in hints.messaging)
    assert any(m.direction == "consumes" and m.channel_hint == "order.cancelled" for m in hints.messaging)
    assert any(m.direction == "publishes" and m.provider_hint == "kafka" for m in hints.messaging)

    assert any(p.name_hint == "SpringDataStockRepository" and p.engine_hint == "cassandra" for p in hints.persistence)
