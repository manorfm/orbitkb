"""A frozen, public-output baseline over the repository's synthetic fixture."""
import json
from pathlib import Path

from benchmark.fixtures import build_pix_fixture
from benchmark.indexing_baseline import collect_baseline, compare_baseline
from orbitkb.db.connection import open_db
from orbitkb.discovery.walker import discover_services
from orbitkb.export.markdown import export_markdown
from orbitkb.export.mermaid import export_mermaid, generate_topology_diagram
from orbitkb.generation.mock_backend import MockBackend
from orbitkb.generation.orchestrator import index_path
from scripts.check_indexing_baseline import main as check_baseline_main

SAMPLE_ROOT = Path(__file__).resolve().parents[1] / "verify" / "sample_project"
LANGUAGE_ROOT = Path(__file__).resolve().parents[1] / "verify" / "language_corpus"
GOLDEN = Path(__file__).resolve().parent / "golden" / "indexing_baseline.json"


def test_language_corpus_discovers_kotlin_go_and_typescript_routes():
    candidates = discover_services(LANGUAGE_ROOT)
    routes = {
        candidate.name: {(endpoint.method, endpoint.path)
                         for endpoint in candidate.detector.collect_hints(candidate.path).endpoints}
        for candidate in candidates
    }

    assert routes == {
        "menu-kotlin-service": {("GET", "/menus/{id}"), ("POST", "/menus")},
        "catalog-go-service": {("ANY", "/catalog"), ("ANY", "/health")},
        "inventory-typescript-service": {("GET", "/items/:id"), ("POST", "/items")},
    }


def test_mock_indexing_baseline_is_reproducible_and_matches_golden(tmp_path: Path):
    first = collect_baseline((SAMPLE_ROOT, LANGUAGE_ROOT), tmp_path / "first")
    second = collect_baseline((SAMPLE_ROOT, LANGUAGE_ROOT), tmp_path / "second")

    assert first == second
    assert compare_baseline(first, GOLDEN) == []
    assert first["stacks"] == ["go", "jvm-spring", "node-ts", "python"]
    assert len(first["services"]) == 6
    assert {(api["method"], api["path"]) for service in first["services"]
            if service["name"] == "menu-kotlin-service" for api in service["apis"]} == {
                ("GET", "/menus/{id}"), ("POST", "/menus")}
    assert {(api["method"], api["path"]) for service in first["services"]
            if service["name"] == "inventory-typescript-service" for api in service["apis"]} == {
                ("GET", "/items/:id"), ("POST", "/items")}
    assert all(service["api_count"] > 0 for service in first["services"])
    assert all("mermaid" in service for service in first["services"])
    assert all("root_path" not in service and "file_path" not in service for service in first["services"])
    assert all("/Users/" not in digest for digest in first["exports"].values())
    assert "svc_checkout_service -->|http| svc_payments_service" in first["integration_fixture"]["topology"]
    assert "payment_authorized" in first["integration_fixture"]["topology"]
    assert first["integration_fixture"]["exports"]
    assert all(metrics == {"false_positives": None, "omissions": None, "cost_usd": None}
               for metrics in first["quality_by_stack"].values())


def test_baseline_comparison_reports_changed_public_fields(tmp_path: Path):
    current = collect_baseline((SAMPLE_ROOT, LANGUAGE_ROOT), tmp_path / "current")
    current["services"][0]["api_count"] += 1

    assert "services[0].api_count" in compare_baseline(current, GOLDEN)


def test_baseline_check_command_reports_drift(tmp_path: Path, capsys):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden["services"][0]["api_count"] += 1
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(golden), encoding="utf-8")

    assert check_baseline_main(["--baseline", str(changed)]) == 1
    assert "services[0].api_count" in capsys.readouterr().out


def test_pre_v3_database_keeps_mermaid_and_markdown_after_migration(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = build_pix_fixture(db_path)

    def public_outputs(connection, output_dir: Path):
        written = export_markdown(connection, output_dir) + export_mermaid(connection, output_dir)
        return generate_topology_diagram(connection), {
            path.relative_to(output_dir).as_posix(): path.read_bytes() for path in written
        }

    before = public_outputs(conn, tmp_path / "before")
    conn.execute("DROP TABLE index_run_unit_usage")
    conn.execute("ALTER TABLE index_runs DROP COLUMN llm_invocations")
    conn.execute("UPDATE schema_meta SET value = '41' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()

    upgraded = open_db(db_path)

    assert public_outputs(upgraded, tmp_path / "after") == before


def test_unchanged_six_service_corpus_needs_no_backend_invocations(tmp_path: Path):
    conn = open_db(tmp_path / "incremental.db")
    sources = (SAMPLE_ROOT, LANGUAGE_ROOT)

    first = [result for source in sources for result in index_path(conn, source, MockBackend())]
    second = [result for source in sources for result in index_path(conn, source, MockBackend())]

    assert sum(result.llm_invocations for result in first) > 0
    assert len(second) == 6
    assert all(result.llm_invocations == 0 for result in second)
