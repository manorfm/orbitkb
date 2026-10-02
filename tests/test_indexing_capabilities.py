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
                "entrypoint_kinds": ["http", "graphql"],
                "error_contract_protocols": ["http", "graphql"],
                "known_unknowns": ["dynamic_routes", "global_error_middleware"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "node-js",
                "languages": ["javascript"],
                "entrypoint_kinds": ["http", "graphql"],
                "error_contract_protocols": ["http", "graphql"],
                "known_unknowns": ["dynamic_routes", "global_error_middleware"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "jvm-spring",
                "languages": ["java", "kotlin"],
                "entrypoint_kinds": ["http", "grpc"],
                "error_contract_protocols": ["http"],
                "known_unknowns": ["dynamic_configuration", "framework_global_error_boundaries"],
                "messaging_analysis": "supported",
            },
            {
                "stack": "go",
                "languages": ["go"],
                "entrypoint_kinds": ["http", "grpc"],
                "error_contract_protocols": ["http"],
                "known_unknowns": ["dynamic_statuses", "custom_response_writers"],
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
