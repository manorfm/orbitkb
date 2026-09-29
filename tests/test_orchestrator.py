"""Exercises the real indexing pipeline (discovery -> prompt rendering -> backend ->
schema validation -> SQLite writes -> incremental hash-based skip) against the
project's own verify/sample_project fixture, faking only the LLM call itself so the
suite stays deterministic and never shells out to a real `claude`/`codex` CLI.
"""
import logging
import shutil
from pathlib import Path

import pytest

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import apis as apis_repo
from orbitkb.db.repositories import ci_commands as ci_commands_repo
from orbitkb.db.repositories import components as components_repo
from orbitkb.db.repositories import embeddings as embeddings_repo
from orbitkb.db.repositories import (
    kubernetes_configuration as kubernetes_configuration_repo,
)
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import services as services_repo
from orbitkb.discovery.registry import detector_for
from orbitkb.discovery.walker import discover_services
from orbitkb.generation import orchestrator
from orbitkb.generation.backend_base import GenerationError, GenerationOutcome
from orbitkb.generation.mock_backend import kind_for_schema
from orbitkb.generation.orchestrator import DiscoveryError, index_path, index_service

SAMPLE_ROOT = Path(__file__).resolve().parent.parent / "verify" / "sample_project"


class FakeOrchestratorBackend:
    """Returns a canned, schema-shaped response per unit kind, reusing
    orbitkb.generation.mock_backend's schema-to-kind detection (the same one
    `orbitkb index --backend mock` uses) but with representative sample values
    instead of that backend's empty placeholders, since some tests assert on the
    persisted content itself. Optionally fails a chosen unit kind, to exercise
    failure isolation.
    """

    name = "fake"

    def __init__(self, fail_kind: str | None = None):
        self.fail_kind = fail_kind
        self.calls = 0

    def generate(self, prompt: str, schema: dict, cwd: Path) -> GenerationOutcome:
        self.calls += 1
        kind = kind_for_schema(schema)
        if kind == self.fail_kind:
            raise GenerationError("simulated failure")
        if kind == "persistence":
            structured = {
                "entities": [
                    {"name": "fake_table", "kind": "sql_table", "engine": "postgres", "fields": [{"field": "id", "type_desc": "string"}]}
                ]
            }
        elif kind == "messaging":
            structured = {"messages": [{"direction": "publishes", "channel": "fake_channel", "provider": "kafka", "shape": [], "description": "fake"}]}
        elif kind == "api_detail":
            structured = {
                "summary": "Fake summary.",
                "description": "Fake description.",
                "response_shape": [{"field": "id", "type_desc": "string"}],
                "request_shape": [{"field": "amount", "type_desc": "number", "required": True}],
                "calls": [{
                    "to_service_name": "payments-service", "call_kind": "http", "reason": "fake reason",
                    "data_needed": ["amount"], "purpose_kind": "data_fetch", "confidence": 0.8, "target_kind": "internal",
                    "resource_type": "not_applicable",
                }],
                "validations": [{"kind": "authorization", "description": "fake auth rule"}],
            }
        elif kind == "service_overview":
            structured = {"short_desc": "Fake short description.", "long_desc": "Fake long description."}
        elif kind == "component":
            structured = {"summary": "Fake component summary."}
        return GenerationOutcome(structured=structured)


class RecordingOrchestratorBackend(FakeOrchestratorBackend):
    """Same canned responses as FakeOrchestratorBackend, but also keeps every prompt it
    was given, keyed by unit kind — lets a test assert not just what got persisted but
    what the LLM actually saw for a given unit (e.g. that the overview prompt really
    includes the component summaries composed below it, not just a coincidence)."""

    def __init__(self, fail_kind: str | None = None):
        super().__init__(fail_kind=fail_kind)
        self.prompts_by_kind: dict[str, list[str]] = {}

    def generate(self, prompt: str, schema: dict, cwd: Path) -> dict:
        self.prompts_by_kind.setdefault(kind_for_schema(schema), []).append(prompt)
        return super().generate(prompt, schema, cwd)


def test_message_provider_is_persisted_from_the_llm_result(tmp_path: Path):
    from orbitkb.db.repositories import messages as messages_repo

    conn = open_db(tmp_path / "test.db")
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    orders = services_repo.get_service_by_name(conn, "orders-service")
    messages = messages_repo.list_messages(conn, orders["id"])
    assert messages[0]["provider"] == "kafka"


def test_index_path_persists_kubernetes_runtime_configuration_bindings(tmp_path: Path):
    root = tmp_path / "sample-project"
    shutil.copytree(SAMPLE_ROOT, root)
    (root / "orders-service" / "deployment.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: orders\nspec:\n"
        "  template:\n    spec:\n      containers:\n        - name: api\n          env:\n"
        "            - name: ORDERS_TOPIC\n              valueFrom:\n                configMapKeyRef:\n"
        "                  name: orders-config\n                  key: orders-topic\n"
    )
    (root / "orders-service" / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: orders-config\ndata:\n  other-key: value\n"
    )
    conn = open_db(tmp_path / "test.db")

    index_path(conn, root, FakeOrchestratorBackend())

    orders = services_repo.get_service_by_name(conn, "orders-service")
    bindings = kubernetes_configuration_repo.list_kubernetes_configuration_bindings_for_service(conn, orders["id"])
    assert [(binding["environment_key"], binding["source_kind"], binding["source_name"], binding["source_key"])
            for binding in bindings] == [("ORDERS_TOPIC", "config_map", "orders-config", "orders-topic")]
    mismatches = kubernetes_configuration_repo.list_kubernetes_configuration_key_mismatches_for_service(conn, orders["id"])
    assert [(mismatch["source_name"], mismatch["source_key"]) for mismatch in mismatches] == [
        ("orders-config", "orders-topic"),
    ]


def test_messaging_prompt_includes_provider_hints_and_config_evidence(tmp_path: Path):
    backend = RecordingOrchestratorBackend()
    conn = open_db(tmp_path / "test.db")
    index_path(conn, SAMPLE_ROOT, backend)

    messaging_prompts = backend.prompts_by_kind["messaging"]
    assert any("Best-effort provider guesses" in p for p in messaging_prompts)
    assert any("kafka" in p for p in messaging_prompts)


def test_components_are_synthesized_from_endpoint_summaries_not_raw_code(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    orders = services_repo.get_service_by_name(conn, "orders-service")
    components = components_repo.list_components(conn, orders["id"])
    # Two components: OrdersController (the three /orders endpoints) and main
    # (the bare /health check, no class wraps it).
    names = {c["name"] for c in components}
    assert names == {"OrdersController", "main"}
    assert all(c["summary"] == "Fake component summary." for c in components)


def test_overview_prompt_is_composed_from_the_components_summary(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    backend = RecordingOrchestratorBackend()
    index_path(conn, SAMPLE_ROOT, backend)

    overview_prompts = backend.prompts_by_kind["service_overview"]
    assert any("Fake component summary." in p for p in overview_prompts)


def test_index_path_indexes_all_three_sample_services(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    backend = FakeOrchestratorBackend()

    results = index_path(conn, SAMPLE_ROOT, backend)

    names = {r.service_name for r in results}
    assert names == {"orders-service", "payments-service", "inventory-service"}
    assert all(r.status == "ok" for r in results)
    assert all(r.llm_calls > 0 for r in results)
    assert sum(r.llm_invocations for r in results) == backend.calls
    assert all(r.llm_invocations == r.llm_calls for r in results)

    services = {s["name"] for s in services_repo.list_services(conn)}
    assert services == names

    orders = services_repo.get_service_by_name(conn, "orders-service")
    assert orders["short_desc"] == "Fake short description."
    apis = apis_repo.list_apis(conn, orders["id"])
    assert any(a["method"] == "POST" and a["path"] == "/orders" for a in apis)


def test_repository_is_created_and_linked_to_all_services(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    repos = repositories_repo.list_repositories(conn)
    assert len(repos) == 1
    assert repos[0]["root_path"] == str(SAMPLE_ROOT.resolve())

    for svc in services_repo.list_services(conn):
        row = services_repo.get_service_by_name(conn, svc["name"])
        assert row["repository_id"] == repos[0]["id"]


def test_index_path_persists_safe_github_actions_commands(tmp_path: Path):
    root = tmp_path / "sample-project"
    shutil.copytree(SAMPLE_ROOT, root)
    workflow = root / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("jobs:\n  verify:\n    steps:\n      - run: npm test\n", encoding="utf-8")
    conn = open_db(tmp_path / "ci-commands.db")

    index_path(conn, root, FakeOrchestratorBackend())

    repository = repositories_repo.get_repository_by_name(conn, root.name)
    assert [dict(command) for command in ci_commands_repo.list_ci_commands(conn, repository["id"])] == [{
        "workflow_path": ".github/workflows/ci.yml",
        "kind": "test",
        "command": "npm test",
        "file_path": ".github/workflows/ci.yml",
        "start_line": 4,
        "end_line": 4,
    }]


def test_reindexing_unchanged_files_skips_generation(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    backend = FakeOrchestratorBackend()
    index_path(conn, SAMPLE_ROOT, backend)
    calls_after_first_run = backend.calls

    second_backend = FakeOrchestratorBackend()
    results = index_path(conn, SAMPLE_ROOT, second_backend)

    assert second_backend.calls == 0  # nothing changed, every unit skipped
    assert all(r.status == "ok" for r in results)
    assert all(r.llm_calls == 0 for r in results)
    assert all(r.llm_invocations == 0 for r in results)
    assert calls_after_first_run > 0  # sanity: the first run did do real work


def test_reindexing_unchanged_static_inputs_skips_ast_analysis(tmp_path: Path, monkeypatch):
    conn = open_db(tmp_path / "static-incremental.db")
    original_analyze = orchestrator.StaticAnalysisEngine.analyze
    analyzed: list[tuple[Path, str]] = []

    def record_analyze(self, root: Path, stack: str):
        analyzed.append((root, stack))
        return original_analyze(self, root, stack)

    monkeypatch.setattr(orchestrator.StaticAnalysisEngine, "analyze", record_analyze)
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())
    first_run_count = len(analyzed)

    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    assert first_run_count > 0
    assert len(analyzed) == first_run_count


def test_reindexing_changed_static_inputs_reanalyzes_only_the_affected_service(tmp_path: Path, monkeypatch):
    root = tmp_path / "sample-project"
    shutil.copytree(SAMPLE_ROOT, root)
    conn = open_db(tmp_path / "changed-static-input.db")
    original_analyze = orchestrator.StaticAnalysisEngine.analyze
    analyzed: list[Path] = []

    def record_analyze(self, service_root: Path, stack: str):
        analyzed.append(service_root)
        return original_analyze(self, service_root, stack)

    monkeypatch.setattr(orchestrator.StaticAnalysisEngine, "analyze", record_analyze)
    index_path(conn, root, FakeOrchestratorBackend())
    analyzed.clear()
    (root / "payments-service" / "static-only.ts").write_text("export const retries = 3;\n", encoding="utf-8")

    index_path(conn, root, FakeOrchestratorBackend())

    assert analyzed == [root / "payments-service"]


def test_reindexing_with_an_external_depth_provider_does_not_reuse_static_snapshot(tmp_path: Path, monkeypatch):
    class ExternalDepthProvider:
        def enrich(self, root: Path, analysis):
            return []

    conn = open_db(tmp_path / "external-depth-static-analysis.db")
    original_analyze = orchestrator.StaticAnalysisEngine.analyze
    analyzed: list[Path] = []

    def record_analyze(self, root: Path, stack: str):
        analyzed.append(root)
        return original_analyze(self, root, stack)

    monkeypatch.setattr(orchestrator.StaticAnalysisEngine, "analyze", record_analyze)
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), depth_provider=ExternalDepthProvider())
    first_run_count = len(analyzed)

    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), depth_provider=ExternalDepthProvider())

    assert first_run_count > 0
    assert len(analyzed) == first_run_count * 2


def test_force_reindex_regenerates_everything(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    backend = FakeOrchestratorBackend()
    results = index_path(conn, SAMPLE_ROOT, backend, force=True)

    assert backend.calls > 0
    assert all(r.llm_calls > 0 for r in results)


def test_reindexing_a_monorepo_prunes_a_service_removed_from_disk(tmp_path: Path):
    root = tmp_path / "shop"
    shutil.copytree(SAMPLE_ROOT, root)
    conn = open_db(tmp_path / "test.db")
    index_path(conn, root, FakeOrchestratorBackend(), repository_name="shop")
    removed_id = services_repo.get_service_by_name(conn, "inventory-service", repository_id=1)["id"]

    shutil.rmtree(root / "inventory-service")
    index_path(conn, root, FakeOrchestratorBackend(), repository_name="shop")

    repository = repositories_repo.get_repository_by_name(conn, "shop")
    assert services_repo.get_service_by_name(conn, "inventory-service", repository_id=repository["id"]) is None
    assert {row["name"] for row in services_repo.list_services(conn, repository["id"])} == {
        "orders-service", "payments-service",
    }
    assert conn.execute("SELECT 1 FROM search_fts WHERE service_id = ?", (removed_id,)).fetchone() is None


def test_reindexing_with_a_new_service_name_reuses_the_same_root_identity(tmp_path: Path):
    root = tmp_path / "service"
    root.mkdir()
    (root / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")

    index_path(
        conn, root, FakeOrchestratorBackend(), service_override="orders", stack_override="python", repository_name="shop",
    )
    repository = repositories_repo.get_repository_by_name(conn, "shop")
    original_id = services_repo.get_service_by_name(conn, "orders", repository_id=repository["id"])["id"]
    index_path(
        conn, root, FakeOrchestratorBackend(), service_override="checkout", stack_override="python", repository_name="shop",
    )

    assert services_repo.get_service_by_name(conn, "orders", repository_id=repository["id"]) is None
    renamed = services_repo.get_service_by_name(conn, "checkout", repository_id=repository["id"])
    assert renamed["id"] == original_id
    assert renamed["root_path"] == str(root.resolve())


def test_reindexing_a_moved_checkout_with_a_stable_repository_name_reuses_its_repository(tmp_path: Path):
    old_root = tmp_path / "old-checkout"
    old_root.mkdir()
    (old_root / "main.py").write_text("def main(): pass\n", encoding="utf-8")
    conn = open_db(tmp_path / "test.db")
    index_path(
        conn, old_root, FakeOrchestratorBackend(), service_override="orders", stack_override="python", repository_name="shop",
    )
    repository = repositories_repo.get_repository_by_name(conn, "shop")
    original_repository_id = repository["id"]
    service_id = services_repo.get_service_by_name(conn, "orders", repository_id=original_repository_id)["id"]

    new_root = tmp_path / "new-checkout"
    old_root.rename(new_root)
    index_path(
        conn, new_root, FakeOrchestratorBackend(), service_override="orders", stack_override="python", repository_name="shop",
    )

    moved_repository = repositories_repo.get_repository_by_name(conn, "shop")
    moved_service = services_repo.get_service_by_name(conn, "orders", repository_id=moved_repository["id"])
    assert moved_repository["id"] == original_repository_id
    assert moved_repository["root_path"] == str(new_root.resolve())
    assert moved_service["id"] == service_id
    assert moved_service["root_path"] == str(new_root.resolve())


def test_generation_failure_is_isolated_per_unit(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    backend = FakeOrchestratorBackend(fail_kind="service_overview")
    failures_root = tmp_path / "failures"

    results = index_path(conn, SAMPLE_ROOT, backend, progress=None)
    # can't pass failures_root through index_path; call index_service directly instead
    conn2 = open_db(tmp_path / "test2.db")
    candidates = discover_services(SAMPLE_ROOT)
    orders = next(c for c in candidates if c.name == "orders-service")
    calls_before = backend.calls
    result = index_service(conn2, orders.name, orders.path, orders.detector, backend, failures_root=failures_root)

    assert result.status == "partial"
    assert result.llm_invocations == backend.calls - calls_before
    assert result.llm_invocations == result.llm_calls + 2
    run = conn2.execute("SELECT llm_invocations FROM index_runs WHERE service_id = ?", (result.service_id,)).fetchone()
    assert run["llm_invocations"] == result.llm_invocations
    # the API unit still succeeded even though overview failed for this service
    orders_row = services_repo.get_service_by_name(conn2, "orders-service")
    assert orders_row["short_desc"] is None  # overview failed, never written
    apis = apis_repo.list_apis(conn2, orders_row["id"])
    assert len(apis) == 4  # api_detail units succeeded independently (4 endpoints)
    assert failures_root.exists()
    assert list(failures_root.glob("*.txt"))

    # results from the first index_path call are still meaningful: overview failed
    # for every service (since fail_kind applies globally), so every one is partial
    assert all(r.status == "partial" for r in results)


def test_index_path_raises_discovery_error_on_empty_directory(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    empty_dir = tmp_path / "nothing_here"
    empty_dir.mkdir()

    with pytest.raises(DiscoveryError):
        index_path(conn, empty_dir, FakeOrchestratorBackend())


def test_index_path_service_override_requires_single_candidate(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    with pytest.raises(DiscoveryError):
        index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), service_override="custom-name")


def test_index_path_with_service_and_stack_override_bypasses_discovery(tmp_path: Path):
    """A folder discover_services() would never recognize on its own (no manifest,
    no entry-point file at all — the exact shape of a library/CLI package like
    orbitkb itself) still indexes successfully when the caller explicitly names both
    --service and --stack, per generation/orchestrator.py's documented escape hatch."""
    conn = open_db(tmp_path / "test.db")
    unrecognizable_dir = tmp_path / "some-library"
    unrecognizable_dir.mkdir()
    (unrecognizable_dir / "lib.py").write_text("# just a module, no manifest, no entrypoint\n")

    results = index_path(
        conn, unrecognizable_dir, FakeOrchestratorBackend(),
        service_override="some-library-core", stack_override="python",
    )

    assert len(results) == 1
    assert results[0].service_name == "some-library-core"
    assert results[0].status == "ok"
    assert services_repo.get_service_by_name(conn, "some-library-core") is not None


def test_index_path_stack_override_without_service_override_is_rejected(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    unrecognizable_dir = tmp_path / "some-library"
    unrecognizable_dir.mkdir()

    with pytest.raises(DiscoveryError):
        index_path(conn, unrecognizable_dir, FakeOrchestratorBackend(), stack_override="python")


def test_index_path_stack_override_rejects_an_unknown_stack_id(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    unrecognizable_dir = tmp_path / "some-library"
    unrecognizable_dir.mkdir()

    with pytest.raises(DiscoveryError):
        index_path(
            conn, unrecognizable_dir, FakeOrchestratorBackend(),
            service_override="x", stack_override="rust",
        )


class FakeEmbeddingBackend:
    model_name = "fake-embedding-model"

    def __init__(self):
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(t))] for t in texts]


def test_indexing_with_an_embedding_backend_stores_a_vector_for_each_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    embedding_backend = FakeEmbeddingBackend()

    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), embedding_backend=embedding_backend)

    orders = services_repo.get_service_by_name(conn, "orders-service")
    rows = embeddings_repo.get_all_service_embeddings(conn)
    assert {r["service_name"] for r in rows} == {"orders-service", "payments-service", "inventory-service"}
    assert embedding_backend.calls > 0
    assert orders["id"] in {r["service_id"] for r in rows}


def test_indexing_without_an_embedding_backend_stores_no_vectors(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")

    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend())

    assert embeddings_repo.get_all_service_embeddings(conn) == []


def test_reindexing_unchanged_files_does_not_recompute_the_embedding(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    embedding_backend = FakeEmbeddingBackend()
    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), embedding_backend=embedding_backend)
    calls_after_first_run = embedding_backend.calls

    index_path(conn, SAMPLE_ROOT, FakeOrchestratorBackend(), embedding_backend=embedding_backend)

    assert embedding_backend.calls == calls_after_first_run  # overview was skipped, so was the embedding


def test_index_service_directly_with_service_override_style_path(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    single_service_root = SAMPLE_ROOT / "orders-service"
    detector = detector_for(single_service_root)
    assert detector is not None

    result = index_service(conn, "custom-orders-name", single_service_root, detector, FakeOrchestratorBackend())

    assert result.status == "ok"
    assert services_repo.get_service_by_name(conn, "custom-orders-name") is not None


def test_index_service_logs_hint_collection_before_static_analysis(tmp_path: Path, caplog):
    """Hint collection (`detector.collect_hints`) runs before `StaticAnalysisEngine.analyze()`'s
    own per-file DEBUG line (test_engine_verbose_logging.py) -- and for a JVM/Spring service it
    shells into tree-sitter (jvm_ast.py) before that per-file logging exists to name anything.
    A real crash there produced zero `--verbose` output for exactly that reason; this line is
    the first thing on the timeline, so the next crash's log always has *something* to end on.
    """
    conn = open_db(tmp_path / "test.db")
    single_service_root = SAMPLE_ROOT / "orders-service"
    detector = detector_for(single_service_root)
    assert detector is not None

    with caplog.at_level(logging.DEBUG, logger="orbitkb.generation.orchestrator"):
        index_service(conn, "custom-orders-name", single_service_root, detector, FakeOrchestratorBackend())

    assert any(
        "collecting hints" in record.message and detector.id in record.message
        for record in caplog.records
    )


def test_index_service_succeeds_on_a_real_jvm_spring_service(tmp_path: Path):
    """jvm-spring's whole pipeline (collect_hints/analyze/enrich) is pure regex now
    (see jvm_scanner.py/jvm_spring_analyzer.py/jvm_ast.py/jvm_grpc_analyzer.py) --
    no longer isolated in a subprocess (there's no native crash left to contain),
    so this just confirms indexing this stack still works end to end in-process,
    same as every other stack.
    """
    conn = open_db(tmp_path / "test.db")
    single_service_root = SAMPLE_ROOT / "inventory-service"
    detector = detector_for(single_service_root)
    assert detector is not None
    assert detector.id == "jvm-spring"

    result = index_service(conn, "inventory-service", single_service_root, detector, FakeOrchestratorBackend())

    assert result.status == "ok"
    apis = apis_repo.list_apis(conn, result.service_id)
    assert len(apis) > 0
