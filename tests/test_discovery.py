import logging
from pathlib import Path

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.discovery.go_stack import GoDetector
from orbitkb.discovery.jvm_stack import JvmSpringDetector
from orbitkb.discovery.node_ts import NodeTsDetector
from orbitkb.discovery.python_stack import PythonDetector
from orbitkb.discovery.walker import discover_services

SAMPLE_ROOT = Path(__file__).resolve().parent.parent / "verify" / "sample_project"


def test_go_handlefunc_hints_require_net_http_import_in_the_same_file(tmp_path: Path):
    (tmp_path / "fake.go").write_text('''package main
import "example.org/http"
func fake() { http.HandleFunc("/fake", fakeHandler) }
''', encoding="utf-8")
    (tmp_path / "missing.go").write_text('''package main
func missing() { http.HandleFunc("/missing", missingHandler) }
''', encoding="utf-8")
    (tmp_path / "valid.go").write_text('''package main
import "net/http"
func valid() { http.HandleFunc("/valid", validHandler) }
func validHandler() {}
''', encoding="utf-8")
    (tmp_path / "grouped.go").write_text('''package main
import (
    http "net/http"
)
func grouped() {
    http.HandleFunc(
        "/grouped",
        groupedHandler,
    )
}
func groupedHandler() {}
''', encoding="utf-8")

    hints = GoDetector().collect_hints(tmp_path)

    assert {(hint.method, hint.path) for hint in hints.endpoints} == {
        ("ANY", "/valid"), ("ANY", "/grouped"),
    }


def test_go_handlefunc_hints_require_a_named_handler_declared_in_the_same_file(tmp_path: Path):
    (tmp_path / "main.go").write_text('''package main
import "net/http"
func main() {
    http.HandleFunc("/known", known)
    http.HandleFunc("/unknown", unknown)
    http.HandleFunc("/method", handler.Serve)
    http.HandleFunc("/dynamic", func() {})
}
func known() {}
''', encoding="utf-8")
    (tmp_path / "other.go").write_text('''package main
func unknown() {}
''', encoding="utf-8")

    hints = GoDetector().collect_hints(tmp_path)

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("ANY", "/known")]


def test_go_discovery_does_not_assign_get_to_generic_router_handle(tmp_path: Path):
    (tmp_path / "main.go").write_text('''package main
func register() {
    router.Handle("/orders", orderHandler)
    router.GET("/health", healthHandler)
}
''', encoding="utf-8")

    hints = GoDetector().collect_hints(tmp_path)

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("GET", "/health")]


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
    endpoint = next(e for e in hints.endpoints if e.path == "/restaurants/{id}")
    assert len(endpoint.extra_excerpts) == 1
    assert endpoint.extra_excerpts[0].file_path == "out.kt"
    assert "fun Restaurant.out()" in endpoint.extra_excerpts[0].text


def test_spring_bare_method_mapping_uses_class_prefix(tmp_path: Path):
    (tmp_path / "MenuController.kt").write_text(
        '@RestController\n@RequestMapping("/menus")\nclass MenuController {\n'
        '    @PostMapping\n    fun create() = Unit\n}\n', encoding="utf-8"
    )

    hints = JvmSpringDetector().collect_hints(tmp_path)

    assert [(endpoint.method, endpoint.path) for endpoint in hints.endpoints] == [("POST", "/menus")]


def test_spring_bare_class_request_mapping_is_not_an_endpoint(tmp_path: Path):
    (tmp_path / "MenuController.kt").write_text(
        '@RestController\n@RequestMapping\nclass MenuController {\n'
        '    @GetMapping("/health")\n    fun health() = "ok"\n}\n', encoding="utf-8"
    )

    hints = JvmSpringDetector().collect_hints(tmp_path)

    assert [(endpoint.method, endpoint.path) for endpoint in hints.endpoints] == [("GET", "/health")]


def test_spring_class_request_mapping_is_not_an_endpoint(tmp_path: Path):
    (tmp_path / "MenuController.kt").write_text(
        '@RestController\n'
        '@RequestMapping(produces = [APPLICATION_JSON_VALUE], consumes = [APPLICATION_JSON_VALUE])\n'
        'class MenuController {\n'
        '    @GetMapping("/menus")\n'
        '    fun list() = emptyList<String>()\n'
        '}\n',
        encoding="utf-8",
    )

    endpoints = JvmSpringDetector().collect_hints(tmp_path).endpoints

    assert [(endpoint.method, endpoint.path) for endpoint in endpoints] == [("GET", "/menus")]


def test_multiline_spring_class_request_mapping_is_not_an_endpoint(tmp_path: Path):
    (tmp_path / "MenuController.kt").write_text(
        '@RestController\n'
        '@RequestMapping(\n'
        '    "/clusters/{clusterId}",\n'
        '    produces = [APPLICATION_JSON_VALUE],\n'
        '    consumes = [APPLICATION_JSON_VALUE]\n'
        ')\n'
        'class MenuController {\n'
        '    @GetMapping("/menus")\n'
        '    fun list() = emptyList<String>()\n'
        '}\n',
        encoding="utf-8",
    )

    endpoints = JvmSpringDetector().collect_hints(tmp_path).endpoints

    assert [(endpoint.method, endpoint.path) for endpoint in endpoints] == [("GET", "/clusters/{clusterId}/menus")]


def test_spring_handler_path_includes_class_route_prefix(tmp_path: Path):
    (tmp_path / "RestaurantController.kt").write_text(
        '@RestController\n'
        '@RequestMapping("/restaurants")\n'
        'class RestaurantController {\n'
        '    @GetMapping("/{id}")\n'
        '    fun get(id: String) = id\n'
        '}\n',
        encoding="utf-8",
    )

    endpoints = JvmSpringDetector().collect_hints(tmp_path).endpoints

    assert [(endpoint.method, endpoint.path) for endpoint in endpoints] == [("GET", "/restaurants/{id}")]


def test_kotlin_endpoint_with_a_return_typed_let_chain_resolves_the_static_import(tmp_path: Path):
    """A real controller shape from the repository this rewrite was validated
    against: a return-type-annotated expression body (`: MenuOut =`) whose value is
    a multi-line `.let { ... }` chain ending in a statically-imported extension
    function call (`.out()`) -- distinct from the single-line case already covered
    by `component_kotlin`'s fixture, and the exact shape a real bug in
    `_end_of_expression_body` (fixed alongside this test) truncated to nothing.
    """
    (tmp_path / "out.kt").write_text(
        "package com.example.out\n\nfun Menu.out() = MenuOut(id)\n\ndata class MenuOut(val id: String)\n",
        encoding="utf-8",
    )
    (tmp_path / "MenuController.kt").write_text(
        "package com.example\n\n"
        "import com.example.out.out\n\n"
        "@RestController\n"
        "class MenuController(private val menuService: MenuService) {\n"
        "    @GetMapping(\"/menus/{id}\")\n"
        "    fun get(@PathVariable id: String): MenuOut =\n"
        "        logger.info(\"get menu $id\")\n"
        "            .let { menuService.get(id).out() }\n"
        "}\n",
        encoding="utf-8",
    )

    hints = JvmSpringDetector().collect_hints(tmp_path)

    endpoint = next(e for e in hints.endpoints if e.path == "/menus/{id}")
    assert len(endpoint.extra_excerpts) == 1
    assert endpoint.extra_excerpts[0].file_path == "out.kt"
    assert "fun Menu.out()" in endpoint.extra_excerpts[0].text


def test_jvm_collect_hints_logs_before_and_during_call_resolution(caplog):
    """A real crash used to happen inside this exact call chain
    (JvmSpringDetector.collect_hints -> _endpoint_hint -> resolve_kotlin_java_calls),
    entirely before StaticAnalysisEngine.analyze()'s own per-file DEBUG line ever
    runs -- so `--verbose` produced no output at all. These three log lines (scan
    start, endpoint hint, and the jvm_ast.py source scan itself) give a future crash
    anywhere on this path something to end on.
    """
    with caplog.at_level(logging.DEBUG):
        JvmSpringDetector().collect_hints(COMPONENT_KOTLIN_ROOT)

    messages = [record.message for record in caplog.records]
    assert any("scanning JVM/Spring hints" in m for m in messages)
    assert any("building endpoint hint" in m for m in messages)
    assert any("scanning JVM/Kotlin source" in m for m in messages)


def test_collect_hints_shares_a_parse_cache_across_endpoints(tmp_path: Path, caplog):
    """`resolve_kotlin_java_calls()` used to re-scan every candidate file in the
    package from scratch for every endpoint that couldn't resolve a call locally --
    for N endpoints all calling the same helper, that's N re-scans of the same file,
    and N fresh `rglob()` walks of the whole service. A cache shared for the whole
    `collect_hints()` pass scans each file at most once, which matters for
    throughput on a real service with dozens of endpoints all reaching a shared
    helper.
    """
    (tmp_path / "Helpers.kt").write_text("package com.acme\n\nfun helper() {}\n", encoding="utf-8")
    (tmp_path / "Controller.kt").write_text(
        "package com.acme\nimport com.acme.helper\n\nclass Controller {\n"
        '  @GetMapping("/a")\n  fun a() { helper() }\n\n'
        '  @PostMapping("/b")\n  fun b() { helper() }\n}\n',
        encoding="utf-8",
    )

    with caplog.at_level(logging.DEBUG, logger="orbitkb.discovery.jvm_ast"):
        hints = JvmSpringDetector().collect_hints(tmp_path)

    assert len(hints.endpoints) == 2
    assert all(e.extra_excerpts and e.extra_excerpts[0].file_path == "Helpers.kt" for e in hints.endpoints)
    helper_scans = [
        r for r in caplog.records if "scanning JVM/Kotlin source" in r.message and "Helpers.kt" in r.message
    ]
    assert len(helper_scans) == 1


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


def test_node_route_hints_require_a_locally_created_http_receiver(tmp_path: Path):
    (tmp_path / "main.ts").write_text('''import express from "express";
import Fastify from "fastify";
const api = express();
const router = express.Router();
const fastify = Fastify();
const app = unrelatedClient();
function handler(req, res) { res.sendStatus(200); }
api.get("/express", handler);
router.post("/router", handler);
fastify.put("/fastify", handler);
app.get("/fake", handler);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)

    assert {(hint.method, hint.path) for hint in hints.endpoints} == {
        ("GET", "/express"), ("PUT", "/fastify"),
    }


def test_node_direct_route_hints_require_a_local_handler(tmp_path: Path):
    (tmp_path / "main.ts").write_text('''import express from "express";
import Fastify from "fastify";
const app = express();
const server = Fastify();
function known(req, res) { res.sendStatus(200); }
app.get("/known", known);
app.post("/inline", (req, res) => res.sendStatus(201));
app.get("/ghost", missingHandler);
app.GET("/uppercase", known);
server.put("/fastify", known);
server.delete("/missing", missingHandler);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    expected = {("GET", "/known"), ("POST", "/inline"), ("PUT", "/fastify")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_route_hints_include_a_literal_local_express_mount_prefix(tmp_path: Path):
    (tmp_path / "main.ts").write_text('''import express from "express";
const app = express();
const router = express.Router();
app.use("/api", router);
router.post("/orders", createOrder);
app.get("/health", health);
function createOrder(req, res) { res.sendStatus(201); }
function health(req, res) { res.sendStatus(200); }
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    expected = {("POST", "/api/orders"), ("GET", "/health")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_route_hints_include_a_proven_cross_file_express_mount_prefix(tmp_path: Path):
    (tmp_path / "orders.routes.js").write_text('''const express = require("express");
const router = express.Router();
router.post("/orders", createOrder);
function createOrder(req, res) { res.sendStatus(201); }
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("POST", "/api/orders")]


def test_node_chained_route_resolves_a_proven_cross_file_mount(tmp_path: Path):
    (tmp_path / "orders.routes.js").write_text('''const express = require("express");
const router = express.Router();
function createOrder(req, res) { res.sendStatus(201); }
router.route("/orders").post(createOrder);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("POST", "/api/orders")]
    assert [(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"] == [
        ("POST", "/api/orders"),
    ]


def test_node_route_chain_resolves_multiple_methods_on_a_cross_file_mount(tmp_path: Path):
    (tmp_path / "orders.routes.js").write_text('''const express = require("express");
const router = express.Router();
function readOrder(req, res) { res.sendStatus(200); }
function createOrder(req, res) { res.sendStatus(201); }
router.route("/orders").get(readOrder).post(createOrder);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    expected = {("GET", "/api/orders"), ("POST", "/api/orders")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_route_chain_applies_all_middleware_to_each_method(tmp_path: Path):
    (tmp_path / "orders.routes.js").write_text('''const express = require("express");
const router = express.Router();
function requireAuthentication(req, res, next) { next(); }
function traceRequest(req, res, next) { next(); }
function validateOrder(req, res, next) { next(); }
function readOrder(req, res) { res.sendStatus(200); }
function createOrder(req, res) { res.sendStatus(201); }
router.route("/orders").all(requireAuthentication).all(traceRequest)
    .get(validateOrder, readOrder).post(createOrder);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    expected = {("GET", "/api/orders"), ("POST", "/api/orders")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected
    contracts = {entry.method: entry.contract for entry in analysis.entrypoints if entry.kind == "http"}
    assert contracts == {
        "GET": {"route_middlewares": [
            {"symbol": "requireAuthentication"}, {"symbol": "traceRequest"}, {"symbol": "validateOrder"},
        ]},
        "POST": {"route_middlewares": [
            {"symbol": "requireAuthentication"}, {"symbol": "traceRequest"},
        ]},
    }


def test_node_route_chain_applies_all_only_to_later_methods(tmp_path: Path):
    (tmp_path / "orders.js").write_text('''const express = require("express");
const app = express();
function requireAuthentication(req, res, next) { next(); }
function readOrder(req, res) { res.sendStatus(200); }
function createOrder(req, res) { res.sendStatus(201); }
app.route("/orders").get(readOrder).all(requireAuthentication).post(createOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    expected = {("GET", "/orders"), ("POST", "/orders")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    contracts = {entry.method: entry.contract for entry in analysis.entrypoints if entry.kind == "http"}
    assert contracts == {
        "GET": None,
        "POST": {"route_middlewares": [{"symbol": "requireAuthentication"}]},
    }


def test_node_routes_accept_local_function_expression_handlers(tmp_path: Path):
    (tmp_path / "orders.js").write_text('''const express = require("express");
const app = express();
const readOrder = function(req, res) { res.sendStatus(200); };
const createOrder = function(req, res) { res.sendStatus(201); };
app.route("/orders").get(readOrder);
app.post("/orders", createOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    expected = {("GET", "/orders"), ("POST", "/orders")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_route_resolves_an_exported_local_imported_handler(tmp_path: Path):
    (tmp_path / "handlers.ts").write_text('''export function createOrder(req, res) {
  return orderService.create(req.body);
}
export const readOrder = (req, res) => res.sendStatus(200);
''', encoding="utf-8")
    (tmp_path / "server.ts").write_text('''import express from "express";
import { createOrder as addOrder, readOrder } from "./handlers";
const app = express();
app.post("/orders", addOrder);
app.get("/orders", readOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {(hint.method, hint.path) for hint in hints.endpoints} == {("POST", "/orders"), ("GET", "/orders")}
    assert {(entry.method, entry.name, entry.symbol) for entry in analysis.entrypoints if entry.kind == "http"} == {
        ("POST", "/orders", "handlers.createOrder"), ("GET", "/orders", "handlers.readOrder"),
    }
    assert any(edge.source == "handlers.createOrder" and edge.target == "orderService.create" for edge in analysis.edges)


def test_node_route_resolves_a_function_exported_after_its_declaration(tmp_path: Path):
    (tmp_path / "handlers.ts").write_text('''function createOrder(req, res) {
  return orderService.create(req.body);
}
const readOrder = (req, res) => res.sendStatus(200);
export { createOrder as submitOrder, readOrder };
''', encoding="utf-8")
    (tmp_path / "server.ts").write_text('''import express from "express";
import { submitOrder as addOrder, readOrder } from "./handlers";
const app = express();
app.post("/orders", addOrder);
app.get("/orders", readOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {(hint.method, hint.path) for hint in hints.endpoints} == {("POST", "/orders"), ("GET", "/orders")}
    assert {(entry.method, entry.name, entry.symbol) for entry in analysis.entrypoints if entry.kind == "http"} == {
        ("POST", "/orders", "handlers.createOrder"), ("GET", "/orders", "handlers.readOrder"),
    }
    assert any(edge.source == "handlers.createOrder" and edge.target == "orderService.create" for edge in analysis.edges)


def test_node_route_resolves_a_local_default_function_import(tmp_path: Path):
    (tmp_path / "createHandler.ts").write_text('''export default function createOrder(req, res) {
  return orderService.create(req.body);
}
''', encoding="utf-8")
    (tmp_path / "readHandler.ts").write_text('''const readOrder = (req, res) => res.sendStatus(200);
export default readOrder;
''', encoding="utf-8")
    (tmp_path / "server.ts").write_text('''import express from "express";
import addOrder from "./createHandler";
import readOrder from "./readHandler";
const app = express();
app.post("/orders", addOrder);
app.get("/orders", readOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert {(hint.method, hint.path) for hint in hints.endpoints} == {("POST", "/orders"), ("GET", "/orders")}
    assert {(entry.method, entry.name, entry.symbol) for entry in analysis.entrypoints if entry.kind == "http"} == {
        ("POST", "/orders", "createHandler.createOrder"), ("GET", "/orders", "readHandler.readOrder"),
    }
    assert any(edge.source == "createHandler.createOrder" and edge.target == "orderService.create" for edge in analysis.edges)


def test_node_route_ignores_imports_without_a_named_local_function_export(tmp_path: Path):
    (tmp_path / "hidden.ts").write_text('''function hidden(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "defaults.ts").write_text('''export default function defaultHandler(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "typed.ts").write_text('''export function typeOnly(req, res) { res.sendStatus(200); }
export function inlineType(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "type-export.ts").write_text('''function typeExport(req, res) { res.sendStatus(200); }
export type { typeExport };
''', encoding="utf-8")
    (tmp_path / "value.ts").write_text('''const value = 123;
export { value };
''', encoding="utf-8")
    (tmp_path / "default-value.ts").write_text('''export default 123;
''', encoding="utf-8")
    (tmp_path / "anonymous-default.ts").write_text('''export default function(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "default-function.ts").write_text('''export default function onlyType(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "reexport.ts").write_text('''export { remote } from "./remote";
''', encoding="utf-8")
    (tmp_path / "remote.ts").write_text('''export function remote(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "ambiguous.ts").write_text('''export function duplicate(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "ambiguous.js").write_text('''export function duplicate(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (tmp_path / "server.ts").write_text('''import express from "express";
import { hidden } from "./hidden";
import { defaultHandler } from "./defaults";
import { externalHandler } from "external-package";
import type
{ typeOnly } from "./typed";
import { type inlineType } from "./typed";
import { typeExport } from "./type-export";
import { value } from "./value";
import { remote } from "./reexport";
import notCallable from "./default-value";
import anonymous from "./anonymous-default";
import type onlyType from "./default-function";
import externalDefault from "external-package";
import { duplicate } from "./ambiguous";
const app = express();
app.get("/hidden", hidden);
app.get("/default", defaultHandler);
app.get("/external", externalHandler);
app.get("/type", typeOnly);
app.get("/inlineType", inlineType);
app.get("/typeExport", typeExport);
app.get("/value", value);
app.get("/reexport", remote);
app.get("/defaultValue", notCallable);
app.get("/anonymous", anonymous);
app.get("/defaultType", onlyType);
app.get("/externalDefault", externalDefault);
app.get("/ambiguous", duplicate);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert hints.endpoints == []
    assert [entry for entry in analysis.entrypoints if entry.kind == "http"] == []


def test_node_route_does_not_follow_an_import_outside_the_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "outside.ts").write_text('''export function leaked(req, res) { res.sendStatus(200); }
''', encoding="utf-8")
    (project / "server.ts").write_text('''import express from "express";
import { leaked } from "../outside";
const app = express();
app.get("/leaked", leaked);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(project)
    analysis = StaticAnalysisEngine().analyze(project, "node-ts")

    assert hints.endpoints == []
    assert [entry for entry in analysis.entrypoints if entry.kind == "http"] == []


def test_node_route_resolves_a_local_imported_handler_on_a_mounted_router(tmp_path: Path):
    (tmp_path / "handlers.ts").write_text('''export function createOrder(req, res) { res.sendStatus(201); }
''', encoding="utf-8")
    (tmp_path / "orders.routes.ts").write_text('''import { createOrder } from "./handlers";
const express = require("express");
const router = express.Router();
router.route("/orders").post(createOrder);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.ts").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("POST", "/api/orders")]
    assert [(entry.method, entry.name, entry.symbol) for entry in analysis.entrypoints if entry.kind == "http"] == [
        ("POST", "/api/orders", "handlers.createOrder"),
    ]


def test_node_chained_route_ignores_ambiguous_cross_file_mount(tmp_path: Path):
    (tmp_path / "orders.routes.js").write_text('''const express = require("express");
const router = express.Router();
function createOrder(req, res) { res.sendStatus(201); }
router.route("/orders").post(createOrder);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./orders.routes");
const app = express();
app.use("/api", orders);
app.use("/internal", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-js")

    assert hints.endpoints == []
    assert [entry for entry in analysis.entrypoints if entry.kind == "http"] == []


def test_node_route_hints_do_not_guess_an_ambiguous_cross_file_mount(tmp_path: Path):
    (tmp_path / "routes.js").write_text('''const express = require("express");
const router = express.Router();
router.get("/orders", handler);
module.exports = router;
''', encoding="utf-8")
    (tmp_path / "server.js").write_text('''const express = require("express");
const orders = require("./routes");
const app = express();
app.use("/api", orders);
app.use("/internal", orders);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)

    assert hints.endpoints == []


def test_node_route_hints_include_proven_express_route_chains(tmp_path: Path):
    (tmp_path / "main.ts").write_text('''import express from "express";
const app = express();
const client = unrelatedClient();
function createOrder(req, res) { res.sendStatus(201); }
function readOrder(req, res) { res.sendStatus(200); }
app.route("/orders").post(createOrder);
app.route("/orders").get(readOrder);
app.route("/multi").get(readOrder).post(createOrder);
app.route("/inline").delete((req, res) => res.sendStatus(204));
app.route("/ghost").get(missingHandler);
app.route("/invalid").unknown(readOrder).post(createOrder);
app.route("/empty").get().post(createOrder);
app.route("/emptyAll").all().get(readOrder);
client.route("/fake").get(readOrder);
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    expected = {
        ("POST", "/orders"), ("GET", "/orders"),
        ("GET", "/multi"), ("POST", "/multi"), ("DELETE", "/inline"),
    }
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_route_hints_include_literal_fastify_route_objects(tmp_path: Path):
    (tmp_path / "main.ts").write_text('''import Fastify from "fastify";
const app = Fastify();
const client = unrelatedClient();
const dynamicPath = "/dynamic";
function createOrder(request, reply) { reply.code(201).send(); }
function readOrder(request, reply) { reply.send(); }
app.route({ method: "POST", url: "/orders", handler: createOrder });
app.route({ method: ["GET", "HEAD"], url: "/orders/:id", handler: readOrder });
app.route({ method: "GET", url: dynamicPath, handler: readOrder });
client.route({ method: "DELETE", url: "/fake", handler: readOrder });
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    expected = {("POST", "/orders"), ("GET", "/orders/:id"), ("HEAD", "/orders/:id")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_nest_hints_require_imported_decorators_and_include_controller_prefix(tmp_path: Path):
    (tmp_path / "orders.controller.ts").write_text('''import { Controller, Get as Read, Post } from "@nestjs/common";
@Controller("/orders")
export class OrdersController {
  @Read(":id")
  find() { return "ok"; }
  @Post()
  create() { return "ok"; }
}
''', encoding="utf-8")
    (tmp_path / "fake.ts").write_text('''@Controller("/fake")
class FakeController {
  @Get("/item")
  find() { return "fake"; }
}
''', encoding="utf-8")
    (tmp_path / "misleading.ts").write_text('''import { Injectable } from "@nestjs/common";
import { Controller, Get } from "unrelated/common";
@Controller("/wrong")
export class WrongController {
  @Get("/item")
  find() { return "wrong"; }
}
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    expected = {("GET", "/orders/:id"), ("POST", "/orders")}
    assert {(hint.method, hint.path) for hint in hints.endpoints} == expected
    assert {(entry.method, entry.name) for entry in analysis.entrypoints if entry.kind == "http"} == expected


def test_node_nest_hints_include_a_non_exported_controller(tmp_path: Path):
    (tmp_path / "orders.ts").write_text('''import { Controller, Get } from "@nestjs/common";
@Controller("/orders")
class OrdersController {
  @Get(":id")
  find() { return "ok"; }
}
''', encoding="utf-8")

    hints = NodeTsDetector().collect_hints(tmp_path)
    analysis = StaticAnalysisEngine().analyze(tmp_path, "node-ts")

    assert [(hint.method, hint.path) for hint in hints.endpoints] == [("GET", "/orders/:id")]
    assert [(entry.method, entry.name, entry.symbol) for entry in analysis.entrypoints if entry.kind == "http"] == [
        ("GET", "/orders/:id", "OrdersController.find"),
    ]


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
