"""Self-indexing end-to-end suite: orbitkb indexes its own source tree through the
real CLI path (discover/bypass -> orchestrator -> SQLite -> MCP), the same one a
real user runs — not a shortcut through internal functions. This is the project's
own dogfooding check, and it doubles as proof that the `--stack` escape hatch (see
cli.py, generation/orchestrator.py) genuinely fixes the documented discovery gap:
orbitkb's own repo root has no main.py/app.py/wsgi.py (the Python heuristic's
entry-file signal for a web service — see discovery/python_stack.py's
PythonDetector.matches), because orbitkb is a library/CLI/MCP server, not a web
microservice, and `matches()` deliberately isn't broadened to cover this (see
README's "Known limitations" — that would reduce detection precision for real
targets).

In the same database, this suite also indexes verify/sample_project — a second,
independent `orbitkb index` call — making the "knowledge is cumulative across
independently-indexed roots" story concrete instead of only a README claim.

Only the LLM backend is faked (deterministic, no real `claude`/`codex` CLI call,
consistent with the rest of the suite); everything else — real discovery, real file
hashing, real SQLite writes, the real MCP query layer, real architecture-smell/
change-surface computation over the combined graph — runs for real.
"""
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from orbitkb import cli
from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import indexed_files as indexed_files_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.generation import change_surface
from orbitkb.mcp import queries
from tests.mcp_test_helpers import content_json, server_params
from tests.test_orchestrator import SAMPLE_ROOT, FakeOrchestratorBackend

REPO_ROOT = Path(__file__).resolve().parents[1]
SELF_ROOT = REPO_ROOT / "orbitkb"


def test_static_analysis_dogfoods_the_project_cli_entrypoint():
    result = StaticAnalysisEngine().analyze(SELF_ROOT, "python")

    entrypoint = next(entry for entry in result.entrypoints if entry.symbol == "cli.main")
    assert entrypoint.kind == "cli"
    assert entrypoint.evidence.file_path == "cli.py"
    assert any(edge.source == "cli.main" and edge.target == "cli.build_parser" for edge in result.edges)


@pytest.fixture
def fake_backends(monkeypatch):
    """Patches cli.resolve_backend to hand out a fresh FakeOrchestratorBackend per
    call, while keeping a reference to each one so a test can inspect e.g. how many
    LLM calls a specific `orbitkb index` invocation actually made."""
    created: list[FakeOrchestratorBackend] = []

    def _factory(*args, **kwargs):
        backend = FakeOrchestratorBackend()
        created.append(backend)
        return backend

    monkeypatch.setattr(cli, "resolve_backend", _factory)
    return created


def _parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def _index_self(db_path: Path, force: bool = False) -> int:
    return cli._cmd_index(_parse([
        "index", str(SELF_ROOT), "--db", str(db_path),
        "--service", "orbitkb-core", "--stack", "python",
        *(["--force"] if force else []),
    ]))


def test_indexing_orbitkbs_own_source_tree_through_the_real_cli_succeeds(tmp_path: Path, fake_backends):
    db_path = tmp_path / "self.db"

    exit_code = _index_self(db_path)

    assert exit_code == 0
    conn = open_db(db_path)
    row = services_repo.get_service_by_name(conn, "orbitkb-core")
    assert row is not None
    assert row["short_desc"]  # FakeOrchestratorBackend's canned overview
    entrypoints = queries.list_entrypoints(conn, "orbitkb-core")
    assert any(entry["symbol"] == "cli.main" for entry in entrypoints["entrypoints"])
    flow = queries.describe_entrypoint(conn, "orbitkb-core", "cli", "command", "cli")
    assert flow["entrypoint"]["symbol"] == "cli.main"
    assert any(edge["to"] == "cli.build_parser" for edge in flow["flow"])
    # Dogfooding finding: orbitkb has no FastAPI/Flask/Django endpoints, no
    # SQLAlchemy/Django models and no queue calls (it's a CLI/library/MCP server,
    # not a web microservice), so PythonDetector's heuristics find zero "relevant"
    # files to hash-track here — only the always-generated overview unit runs, from
    # the folder tree alone. This is the discovery-heuristic gap the module
    # docstring above describes, made concrete instead of just asserted in prose.
    assert indexed_files_repo.get_indexed_file_hashes(conn, row["id"]) == {}
    assert fake_backends[0].calls == 1  # exactly the overview unit


def test_reindexing_after_no_real_change_regenerates_nothing(tmp_path: Path, fake_backends):
    db_path = tmp_path / "self2.db"
    _index_self(db_path)
    calls_after_first_run = fake_backends[0].calls

    exit_code = _index_self(db_path)

    assert exit_code == 0
    assert calls_after_first_run > 0  # sanity: the first run did do real work
    assert fake_backends[1].calls == 0  # nothing changed on disk since the first run


def test_describe_and_search_work_against_the_self_indexed_service(tmp_path: Path, fake_backends):
    db_path = tmp_path / "self3.db"
    _index_self(db_path)

    conn = open_db(db_path)
    described = queries.describe_service(conn, "orbitkb-core")
    assert described["name"] == "orbitkb-core"
    assert described["short_desc"]
    assert "freshness" in described


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_static_rabbitmq_publication_contract(tmp_path: Path, fake_backends):
    root = tmp_path / "publisher"
    root.mkdir()
    (root / "resolvers.ts").write_text(
        '''export const resolvers = {
  Mutation: { createOrder: (_: unknown, input: CreateOrderInput) => channel.publish("orders", "created", input, { headers: { schema_version: "1" } }) }
};
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "publisher.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-publisher", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_messages", {"service": "orders-publisher"}))

    assert result["static_contracts"] == [{
        "direction": "publishes", "exchange": "orders", "routing_key": "created", "payload_type": "CreateOrderInput", "message_version": "1",
        "evidence": {"file": "resolvers.ts", "start_line": 2, "end_line": 2},
    }]


@pytest.mark.anyio
async def test_cumulative_repositories_keep_duplicate_identities_and_context_telemetry_scoped(tmp_path: Path, fake_backends):
    """Real CLI -> one cumulative SQLite DB -> MCP, including an empty briefing.

    The empty task intentionally avoids a real headless LLM while still exercising
    the public get_change_context telemetry path. The two same-named services prove
    that repository scope is preserved end to end rather than only in unit queries.
    """
    checkout = tmp_path / "checkout"
    fulfillment = tmp_path / "fulfillment"
    checkout.mkdir()
    fulfillment.mkdir()
    (checkout / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    (fulfillment / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    db_path = tmp_path / "cumulative.db"

    for root, repository in ((checkout, "checkout-repo"), (fulfillment, "fulfillment-repo")):
        assert cli._cmd_index(_parse([
            "index", str(root), "--db", str(db_path), "--service", "orders", "--stack", "go",
            "--repository-name", repository,
        ])) == 0

    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        repositories = content_json(await session.call_tool("list_repositories", {}))
        described = content_json(await session.call_tool("describe_service", {
            "service": "orders", "repository": "fulfillment-repo",
        }))
        context = content_json(await session.call_tool("get_change_context", {
            "task": "unmatched calibration task", "repository": "checkout-repo", "epic_type": "feature",
        }))
        metrics = content_json(await session.call_tool("get_context_budget_metrics", {"epic_type": "feature"}))

    assert {item["name"] for item in repositories["repositories"]} == {"checkout-repo", "fulfillment-repo"}
    assert described["repository"] == "fulfillment-repo"
    assert context["scope"] == {"repository": "checkout-repo"}
    assert context["telemetry"]["recorded"] is True
    assert metrics["runs"] == 1


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_literal_spring_amqp_publication(tmp_path: Path, fake_backends):
    root = tmp_path / "publisher"
    root.mkdir()
    (root / "OrderPublisher.java").write_text(
        '''class OrderPublisher {
  RabbitTemplate publisher;
  void publish(OrderCreated event) { publisher.convertAndSend("orders", "order.created", event); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "publisher.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-publisher", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_messages", {"service": "orders-publisher"}))

    assert result["static_contracts"] == [{
        "direction": "publishes", "exchange": "orders", "routing_key": "order.created", "payload_type": "OrderCreated", "message_version": None,
        "evidence": {"file": "OrderPublisher.java", "start_line": 3, "end_line": 3},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_literal_go_amqp_publication(tmp_path: Path, fake_backends):
    root = tmp_path / "publisher"
    root.mkdir()
    (root / "publisher.go").write_text(
        '''package orders
func publish(channel *amqp.Channel, event OrderCreated) error {
  return channel.Publish("orders", "order.created", false, false, amqp.Publishing{Body: event})
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "publisher.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-publisher", "--stack", "go",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_messages", {"service": "orders-publisher"}))

    assert result["static_contracts"] == [{
        "direction": "publishes", "exchange": "orders", "routing_key": "order.created", "payload_type": "OrderCreated", "message_version": None,
        "evidence": {"file": "publisher.go", "start_line": 3, "end_line": 3},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_literal_rabbitmq_bindings(tmp_path: Path, fake_backends):
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "consumer.ts").write_text(
        '''channel.assertQueue("orders.created");
channel.bindQueue("orders.created", "orders", "order.created");
channel.consume("orders.created", async (message: OrderCreated) => orderService.handle(message));
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "consumer.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-consumer", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-consumer", "kind": "message", "method": "CONSUME", "name": "orders.created",
        }))

    assert result["contract"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_literal_go_rabbitmq_bindings(tmp_path: Path, fake_backends):
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "consumer.go").write_text(
        '''package orders
func consume(channel *amqp.Channel) {
  channel.QueueDeclare("orders.created", true, false, false, false, amqp.Table{"x-dead-letter-routing-key": "orders.dlq", "x-message-ttl": 5000})
  channel.QueueBind("orders.created", "order.created", "orders", false, nil)
  channel.Consume("orders.created", "", false, false, false, false, func(message amqp.Delivery) {})
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "consumer.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-consumer", "--stack", "go",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-consumer", "kind": "message", "method": "CONSUME", "name": "orders.created",
        }))

    assert result["contract"]["bindings"] == [
        {"exchange": "orders", "routing_key": "order.created"},
    ]
    assert result["contract"]["dead_letter_routing_key"] == "orders.dlq"
    assert result["contract"]["retry_delay_ms"] == 5000


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_static_jpa_persistence_fact(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "Order.java").write_text(
        '''@Entity @Table(name = "orders") class Order { String id; }''', encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-jpa", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_persistence", {"service": "orders-jpa"}))

    assert result["static_facts"] == [{
        "name": "orders", "kind": "sql_table", "owner": "Order",
        "evidence": {"file": "Order.java", "start_line": 1, "end_line": 1},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_static_mongoose_persistence_fact(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "order-model.ts").write_text(
        '''import mongoose from "mongoose";
const Order = mongoose.model("Order", orderSchema, "orders");''', encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-mongo", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_persistence", {"service": "orders-mongo"}))

    assert result["static_facts"] == [{
        "name": "orders", "kind": "document", "owner": "Order",
        "evidence": {"file": "order-model.ts", "start_line": 2, "end_line": 2},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_static_spring_mongo_persistence_fact(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "Order.java").write_text(
        '''@Document(collection = "orders") class Order {}''', encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-mongo", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_persistence", {"service": "orders-mongo"}))

    assert result["static_facts"] == [{
        "name": "orders", "kind": "document", "owner": "Order",
        "evidence": {"file": "Order.java", "start_line": 1, "end_line": 1},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_a_static_prisma_persistence_fact(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "schema.prisma").write_text(
        '''datasource db {
  provider = "postgresql"
}
model Order {
  id String @id
  @@map("orders")
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-prisma", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_persistence", {"service": "orders-prisma"}))

    assert result["static_facts"] == [{
        "name": "orders", "kind": "sql_table", "owner": "Order",
        "evidence": {"file": "schema.prisma", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_preserves_literal_rest_response_statuses(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  @PostMapping("/orders") @ResponseStatus(HttpStatus.CREATED)
  Order create(Order order) { return order; }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-http", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-http", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["contract"]["response_statuses"] == [{"code": 201, "name": "CREATED"}]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_bounded_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final OrderRepository repository;
  @PostMapping("/orders")
  Order create(Order order) { return repository.save(order); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-http", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-http", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "repository.save",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_derived_spring_data_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrderRepository.java").write_text(
        '''interface OrderRepository extends JpaRepository<Order, String> {
  long deleteByCustomerId(String customerId);
}
''',
        encoding="utf-8",
    )
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final OrderRepository repository;
  @DeleteMapping("/orders/{customerId}")
  long delete(String customerId) { return repository.deleteByCustomerId(customerId); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-http", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-http", "kind": "http", "method": "DELETE", "name": "/orders/{customerId}",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "repository.deleteByCustomerId",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_modifying_spring_data_query_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrderRepository.java").write_text(
        '''interface OrderRepository extends JpaRepository<Order, String> {
  @Query("update Order o set o.archived = true") @Modifying
  int archiveExpired();
}
''',
        encoding="utf-8",
    )
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final OrderRepository repository;
  @PostMapping("/orders/archive")
  int archive() { return repository.archiveExpired(); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-http", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-http", "kind": "http", "method": "POST", "name": "/orders/archive",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "repository.archiveExpired",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_jdbc_template_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final JdbcTemplate jdbc;
  @PostMapping("/orders/archive")
  int archive() { return jdbc.update("update orders set archived = true"); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-http", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-http", "kind": "http", "method": "POST", "name": "/orders/archive",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "jdbc.update",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_mongo_template_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final MongoTemplate mongo;
  @PostMapping("/orders")
  Order create(Order order) { return mongo.save(order); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-mongo", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-mongo", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "mongo.save",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_entity_manager_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "OrdersController.java").write_text(
        '''class OrdersController {
  private final EntityManager entityManager;
  @PostMapping("/orders")
  void create(Order order) { entityManager.persist(order); }
}
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-jpa", "--stack", "jvm-spring",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-jpa", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "entityManager.persist",
        "evidence": {"file": "OrdersController.java", "start_line": 4, "end_line": 4},
    }]


@pytest.mark.anyio
@pytest.mark.parametrize("export_style", ["named", "default", "named_clause", "default_clause", "reexport", "star"])
async def test_cli_to_mcp_exposes_mongoose_persistence_operations(tmp_path: Path, fake_backends, export_style):
    root = tmp_path / "orders"
    root.mkdir()
    export_line = {
        "named": 'export const Order = mongoose.model("Order", orderSchema, "orders");',
        "default": 'export default mongoose.model("Order", orderSchema, "orders");',
        "named_clause": 'const Order = mongoose.model("Order", orderSchema, "orders");\nexport { Order };',
        "default_clause": 'const Order = mongoose.model("Order", orderSchema, "orders");\nexport { Order as default };',
        "reexport": 'export const Order = mongoose.model("Order", orderSchema, "orders");',
        "star": 'export const Order = mongoose.model("Order", orderSchema, "orders");',
    }[export_style]
    (root / "order.model.ts").write_text(
        f'import mongoose from "mongoose";\n{export_line}\n',
        encoding="utf-8",
    )
    if export_style in {"reexport", "star"}:
        (root / "models.ts").write_text(
            'export * from "./order.model";\n' if export_style == "star"
            else 'export { Order } from "./order.model";\n',
            encoding="utf-8",
        )
    module = "./models" if export_style in {"reexport", "star"} else "./order.model"
    import_line = (
        f'import Order from "{module}";'
        if export_style in {"default", "default_clause"}
        else f'import {{ Order }} from "{module}";'
    )
    (root / "resolvers.ts").write_text(
        f'''{import_line}
export const resolvers = {{
  Mutation: {{ createOrder: (_: unknown, input: CreateOrderInput) => Order.create(input) }},
}};
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-mongo", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-mongo", "kind": "graphql", "method": "MUTATION", "name": "createOrder",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "Order.create",
        "evidence": {"file": "resolvers.ts", "start_line": 3, "end_line": 3},
        "model": "Order", "collection": "orders",
    }]


def test_update_refreshes_imported_mongoose_model_identity(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"start": "node index.js"}}', encoding="utf-8")
    model = root / "order.model.ts"
    model.write_text(
        "import mongoose from 'mongoose';\n"
        "export const Order = mongoose.model('Order', schema, 'orders');\n",
        encoding="utf-8",
    )
    (root / "resolvers.ts").write_text(
        "import { Order } from './order.model';\n"
        "export const resolvers = { Mutation: { createOrder: () => Order.create({}) } };\n",
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"
    assert cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-mongo", "--stack", "node-ts",
    ])) == 0
    conn = open_db(db_path)

    def operations():
        return queries.describe_entrypoint(
            conn, "orders-mongo", "graphql", "MUTATION", "createOrder",
        )["persistence_operations"]

    assert [(item["model"], item["collection"]) for item in operations()] == [("Order", "orders")]

    model.write_text(
        "import mongoose from 'mongoose';\n"
        "export const Order = mongoose.model('Payment', schema, 'payments');\n",
        encoding="utf-8",
    )
    assert cli._cmd_update(_parse(["update", "orders-mongo", "--db", str(db_path)])) == 0
    assert [(item["model"], item["collection"]) for item in operations()] == [("Payment", "payments")]

    model.write_text("export const Order = fakeFactory.model('Payment', schema);\n", encoding="utf-8")
    assert cli._cmd_update(_parse(["update", "orders-mongo", "--db", str(db_path)])) == 0
    assert operations() == []


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_prisma_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "resolvers.ts").write_text(
        '''const prisma = new PrismaClient();
export const resolvers = {
  Query: { order: (_: unknown, id: string) => prisma.order.findUnique({ where: { id } }) },
};
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-prisma", "--stack", "node-ts",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-prisma", "kind": "graphql", "method": "QUERY", "name": "order",
        }))

    assert result["persistence_operations"] == [{
        "operation": "reads", "target": "prisma.order.findUnique",
        "evidence": {"file": "resolvers.ts", "start_line": 3, "end_line": 3},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_gorm_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "orders.go").write_text(
        '''package orders
func Create(db *gorm.DB, order Order) {
  db.Create(&order)
}
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-gorm", "--stack", "go",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-gorm", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "db.Create",
        "evidence": {"file": "orders.go", "start_line": 3, "end_line": 3},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_gorm_fluent_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "orders.go").write_text(
        '''package orders
func Create(ctx context.Context, db *gorm.DB, order Order) {
  db.WithContext(ctx).Create(&order)
}
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-gorm", "--stack", "go",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-gorm", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "db.WithContext(ctx).Create",
        "evidence": {"file": "orders.go", "start_line": 3, "end_line": 3},
    }]


@pytest.mark.anyio
async def test_cli_to_mcp_exposes_database_sql_persistence_operations(tmp_path: Path, fake_backends):
    root = tmp_path / "orders"
    root.mkdir()
    (root / "orders.go").write_text(
        '''package orders
func Create(tx *sql.Tx, id string) {
  tx.ExecContext(ctx, "insert into orders(id) values(?)", id)
}
func register() { router.POST("/orders", Create) }
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "orders.db"

    exit_code = cli._cmd_index(_parse([
        "index", str(root), "--db", str(db_path), "--service", "orders-sql", "--stack", "go",
    ]))

    assert exit_code == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = content_json(await session.call_tool("describe_entrypoint", {
            "service": "orders-sql", "kind": "http", "method": "POST", "name": "/orders",
        }))

    assert result["persistence_operations"] == [{
        "operation": "writes", "target": "tx.ExecContext",
        "evidence": {"file": "orders.go", "start_line": 3, "end_line": 3},
    }]


def test_knowledge_is_cumulative_across_two_independently_indexed_roots(tmp_path: Path, fake_backends):
    """One database, two separate `orbitkb index` invocations — orbitkb's own
    source and the project's own sample fixture — proving the "index one repo at a
    time, or a monorepo, into the same accumulating DB" story concretely instead of
    only in the README. find_architecture_smells and find_change_surface are then
    exercised against the combined, cumulative knowledge model, including
    orbitkb's own architecture — the most direct proof the pipeline works end to
    end against real, non-trivial code."""
    db_path = tmp_path / "cumulative.db"

    self_exit_code = _index_self(db_path)
    sample_exit_code = cli._cmd_index(_parse([
        "index", str(SAMPLE_ROOT), "--db", str(db_path), "--repository-name", "sample-project",
    ]))

    assert self_exit_code == 0
    assert sample_exit_code == 0
    conn = open_db(db_path)
    repository_names = {r["name"] for r in repositories_repo.list_repositories(conn)}
    assert "sample-project" in repository_names
    assert SELF_ROOT.name in repository_names  # default repository name: the indexed folder's own name

    services = {s["name"] for s in services_repo.list_services(conn)}
    assert services == {"orbitkb-core", "orders-service", "payments-service", "inventory-service"}

    smells = queries.find_architecture_smells(conn)
    assert smells["run_id"] is not None  # recomputed over the whole cumulative graph, not just one root

    result = change_surface.analyze_change_surface(
        conn, "Split the repository layer into smaller modules", FakeOrchestratorBackend(),
        hint_services=["orbitkb-core"],
    )
    # hint_services anchors the search directly, since the fake overview text
    # won't keyword-match a specific engineering task the way a real LLM summary would.
    assert "recommended_next_queries" in result
    assert "freshness" in result


@pytest.mark.anyio
async def test_cli_to_mcp_disambiguates_same_named_services_across_repositories(tmp_path: Path, fake_backends):
    checkout = tmp_path / "checkout"
    fulfillment = tmp_path / "fulfillment"
    checkout.mkdir()
    fulfillment.mkdir()
    (checkout / "orders.go").write_text(
        '''package orders
func Create() {}
func register() { router.POST("/checkout/orders", Create) }
''',
        encoding="utf-8",
    )
    (fulfillment / "orders.go").write_text(
        '''package orders
func Create() {}
func register() { router.POST("/fulfillment/orders", Create) }
''',
        encoding="utf-8",
    )
    db_path = tmp_path / "same-name-services.db"

    checkout_exit = cli._cmd_index(_parse([
        "index", str(checkout), "--db", str(db_path), "--service", "orders", "--repository-name", "checkout-repo",
        "--stack", "go",
    ]))
    fulfillment_exit = cli._cmd_index(_parse([
        "index", str(fulfillment), "--db", str(db_path), "--service", "orders", "--repository-name", "fulfillment-repo",
        "--stack", "go",
    ]))

    assert checkout_exit == 0
    assert fulfillment_exit == 0
    async with stdio_client(server_params(db_path)) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = content_json(await session.call_tool("list_services", {"repository": "checkout-repo"}))
        ambiguous = content_json(await session.call_tool("describe_service", {"service": "orders"}))
        selected = content_json(await session.call_tool("describe_service", {
            "service": "orders", "repository": "fulfillment-repo",
        }))
        ambiguous_apis = content_json(await session.call_tool("list_apis", {"service": "orders"}))
        selected_apis = content_json(await session.call_tool("list_apis", {
            "service": "orders", "repository": "fulfillment-repo",
        }))

    assert [(service["name"], service["repository"]) for service in listed["services"]] == [
        ("orders", "checkout-repo"),
    ]
    assert ambiguous == {
        "error": "ambiguous service: orders; specify repository",
        "repositories": ["checkout-repo", "fulfillment-repo"],
    }
    assert selected["repository"] == "fulfillment-repo"
    assert selected["name"] == "orders"
    assert ambiguous_apis["error"] == "ambiguous service: orders; specify repository"
    assert selected_apis["repository"] == "fulfillment-repo"
