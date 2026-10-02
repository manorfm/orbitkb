import pytest
from jsonschema import validate
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from orbitkb.analysis.engine import StaticAnalysisEngine
from orbitkb.db.connection import open_db
from orbitkb.generation.llm_harness import load_schema
from orbitkb.mcp import queries
from tests.mcp_test_helpers import content_json, server_params


def test_describe_indexing_capabilities_exposes_the_initial_stack_contract():
    result = queries.describe_indexing_capabilities()

    assert result == {
        "capabilities": [
            {
                "stack": "node-ts",
                "languages": ["javascript", "typescript"],
                "entrypoint_kinds": ["http", "graphql", "message"],
                "error_contract_protocols": ["http", "graphql"],
                "known_unknowns": ["dynamic_routes", "global_error_middleware", "dynamic_message_channels"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "node-js",
                "languages": ["javascript"],
                "entrypoint_kinds": ["http", "graphql", "message"],
                "error_contract_protocols": ["http", "graphql"],
                "known_unknowns": ["dynamic_routes", "global_error_middleware", "dynamic_message_channels"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "jvm-spring",
                "languages": ["java", "kotlin"],
                "entrypoint_kinds": ["http", "grpc", "message", "job"],
                "error_contract_protocols": ["http"],
                "known_unknowns": [
                    "dynamic_configuration", "framework_global_error_boundaries",
                    "dynamic_message_channels", "dynamic_schedules",
                ],
                "messaging_analysis": "supported",
            },
            {
                "stack": "go",
                "languages": ["go"],
                "entrypoint_kinds": ["http", "grpc", "message"],
                "error_contract_protocols": ["http"],
                "known_unknowns": ["dynamic_statuses", "custom_response_writers", "dynamic_message_channels"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "python",
                "languages": ["python"],
                "entrypoint_kinds": ["http", "cli"],
                "error_contract_protocols": [],
                "known_unknowns": ["dynamic_routes", "indirect_router_exports", "flask_django_routes"],
                "messaging_analysis": "unsupported",
            },
        ],
        "guarantee": "listed facts are deterministic; unlisted behavior remains unknown",
    }
    validate(result, load_schema("indexing_capabilities"))


def test_advertised_messaging_support_matches_builtin_frontends(tmp_path):
    engine = StaticAnalysisEngine()
    for capability in queries.describe_indexing_capabilities()["capabilities"]:
        analysis = engine.analyze(tmp_path, capability["stack"])
        expected = "supported" if analysis.capabilities["messaging"] else "unsupported"
        assert capability["messaging_analysis"] == expected


def test_source_proven_consumers_and_scheduled_job_are_advertised(tmp_path):
    (tmp_path / "consumer.go").write_text(
        "package orders\n"
        "func Consume() {\n"
        "  reader := kafka.NewReader(kafka.ReaderConfig{Topic: \"orders.created\"})\n"
        "  message, err := reader.ReadMessage(ctx)\n"
        "  orderService.Process(message)\n"
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "Consumer.java").write_text(
        "class Consumer {\n"
        "  @KafkaListener(topics = \"orders.created\")\n"
        "  void consume(Event event) { service.handle(event); }\n"
        "  @Scheduled(cron = \"0 */5 * * * *\")\n"
        "  void reconcile() { ledger.sync(); }\n"
        "}\n",
        encoding="utf-8",
    )
    consumer = (
        'channel.consume("orders.created", async (message) => {\n'
        '  await service.handle(message);\n'
        '});\n'
    )
    (tmp_path / "consumer.js").write_text(consumer, encoding="utf-8")
    (tmp_path / "consumer.ts").write_text(consumer, encoding="utf-8")
    advertised = {item["stack"]: set(item["entrypoint_kinds"])
                  for item in queries.describe_indexing_capabilities()["capabilities"]}

    for stack, expected in (
        ("go", {"message"}),
        ("jvm-spring", {"message", "job"}),
        ("node-js", {"message"}),
        ("node-ts", {"message"}),
    ):
        observed = {entry.kind for entry in StaticAnalysisEngine().analyze(tmp_path, stack).entrypoints}
        assert expected <= observed
        assert observed <= advertised[stack]


@pytest.mark.anyio
async def test_indexing_capabilities_are_available_over_mcp(tmp_path):
    db_path = tmp_path / "capabilities.db"
    open_db(db_path).close()

    async with stdio_client(server_params(db_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = content_json(await session.call_tool("describe_indexing_capabilities", {}))

    assert [capability["stack"] for capability in result["capabilities"]] == [
        "node-ts", "node-js", "jvm-spring", "go", "python",
    ]
