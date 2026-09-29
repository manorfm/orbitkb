"""Exercises the CLI commands directly (not via subprocess), faking only backend
resolution so `index`/`update` never shell out to a real `claude`/`codex` CLI.
"""
import argparse
import faulthandler
import gc
import logging
import re
from pathlib import Path

import pytest

from orbitkb import cli
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.db.repositories import services as services_repo
from tests.test_orchestrator import SAMPLE_ROOT, FakeOrchestratorBackend


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    monkeypatch.setattr(cli, "resolve_backend", lambda *a, **kw: FakeOrchestratorBackend())


def _parse(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def test_index_command_indexes_sample_project(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    args = _parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)])

    exit_code = cli._cmd_index(args)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "orders-service" in out
    assert "status=ok" in out
    conn = open_db(db_path)
    assert len(services_repo.list_services(conn)) == 3


def test_index_command_accepts_repository_name(tmp_path: Path):
    db_path = tmp_path / "test.db"
    args = _parse(["index", str(SAMPLE_ROOT), "--db", str(db_path), "--repository-name", "custom-repo"])

    cli._cmd_index(args)

    conn = open_db(db_path)
    repos = repositories_repo.list_repositories(conn)
    assert repos[0]["name"] == "custom-repo"


def test_index_command_reports_error_on_empty_directory(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    args = _parse(["index", str(empty_dir), "--db", str(db_path)])

    exit_code = cli._cmd_index(args)

    assert exit_code == 1
    assert "error" in capsys.readouterr().err


def test_index_command_with_stack_override_bypasses_discovery(tmp_path: Path):
    db_path = tmp_path / "test.db"
    unrecognizable_dir = tmp_path / "some-library"
    unrecognizable_dir.mkdir()
    (unrecognizable_dir / "lib.py").write_text("# just a module\n")
    args = _parse([
        "index", str(unrecognizable_dir), "--db", str(db_path),
        "--service", "some-library-core", "--stack", "python",
    ])

    exit_code = cli._cmd_index(args)

    assert exit_code == 0
    conn = open_db(db_path)
    assert services_repo.get_service_by_name(conn, "some-library-core") is not None


def test_index_command_accepts_verbose_flag(tmp_path: Path):
    args = _parse(["index", str(SAMPLE_ROOT), "--db", str(tmp_path / "t.db"), "--verbose"])

    assert args.verbose is True


def test_index_command_can_show_route_sufficiency(tmp_path: Path, capsys):
    args = _parse([
        "index", str(SAMPLE_ROOT), "--db", str(tmp_path / "t.db"),
        "--sufficiency-details",
    ])

    assert cli._cmd_index(args) == 0
    output = capsys.readouterr().out
    assert "sufficiency=" in output
    assert '"render_status": "ineligible"' in output
    assert '"dimension": "business_behavior"' in output
    assert '"reason": "canonical static facts do not establish a business description"' in output


def test_update_command_accepts_sufficiency_details_flag(tmp_path: Path):
    args = _parse(["update", "orders-service", "--db", str(tmp_path / "t.db"), "--sufficiency-details"])

    assert args.sufficiency_details is True


def test_index_command_verbose_defaults_to_false(tmp_path: Path):
    args = _parse(["index", str(SAMPLE_ROOT), "--db", str(tmp_path / "t.db")])

    assert args.verbose is False


def test_update_command_accepts_verbose_flag(tmp_path: Path):
    args = _parse(["update", "--repository", "shop", "--db", str(tmp_path / "t.db"), "--verbose"])

    assert args.verbose is True


def test_configure_verbose_logging_raises_root_level_to_debug():
    logging.getLogger().setLevel(logging.WARNING)
    try:
        cli._configure_verbose_logging(True)
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG
    finally:
        logging.getLogger().setLevel(logging.WARNING)


def test_configure_verbose_logging_does_nothing_when_false():
    logging.getLogger().setLevel(logging.WARNING)

    cli._configure_verbose_logging(False)

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_update_command_reindexes_known_service(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))

    exit_code = cli._cmd_update(_parse(["update", "orders-service", "--db", str(db_path)]))

    assert exit_code == 0
    assert "orders-service" in capsys.readouterr().out


def test_update_command_errors_on_unknown_service(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_update(_parse(["update", "does-not-exist", "--db", str(db_path)]))

    assert exit_code == 1
    assert "unknown service" in capsys.readouterr().err


def test_update_command_with_repository_reindexes_every_service(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse([
        "index", str(SAMPLE_ROOT), "--db", str(db_path), "--repository-name", "test-repo",
    ]))

    exit_code = cli._cmd_update(_parse(["update", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "orders-service" in out
    assert "payments-service" in out
    assert "inventory-service" in out


def test_update_command_with_repository_continues_after_one_service_fails(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse([
        "index", str(SAMPLE_ROOT), "--db", str(db_path), "--repository-name", "test-repo",
    ]))
    conn = open_db(db_path)
    conn.execute(
        "UPDATE services SET root_path = ? WHERE name = ?",
        (str(tmp_path / "does-not-exist-anymore"), "orders-service"),
    )
    conn.commit()

    exit_code = cli._cmd_update(_parse(["update", "--repository", "test-repo", "--db", str(db_path)]))

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "root path for 'orders-service' no longer exists" in captured.err
    assert "payments-service" in captured.out
    assert "inventory-service" in captured.out


def test_update_command_errors_on_unknown_repository(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_update(_parse(["update", "--repository", "does-not-exist", "--db", str(db_path)]))

    assert exit_code == 1
    assert "unknown repository" in capsys.readouterr().err


def test_update_command_rejects_service_and_repository_together(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_update(_parse([
        "update", "orders-service", "--repository", "test-repo", "--db", str(db_path),
    ]))

    assert exit_code == 1
    assert "either a service name or --repository" in capsys.readouterr().err


def test_update_command_requires_service_or_repository(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_update(_parse(["update", "--db", str(db_path)]))

    assert exit_code == 1
    assert "a service name or --repository" in capsys.readouterr().err


def test_list_command_prints_indexed_services(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))

    exit_code = cli._cmd_list(_parse(["list", "--db", str(db_path)]))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "orders-service" in out
    assert "payments-service" in out


def test_context_command_returns_a_bounded_empty_briefing_without_a_match(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_context(_parse(["context", "unrelated xyz", "--db", str(db_path)]))

    assert exit_code == 0
    assert '"services": []' in capsys.readouterr().out


def test_list_command_handles_empty_database(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli._cmd_list(_parse(["list", "--db", str(db_path)]))

    assert exit_code == 0
    assert "nenhum serviço" in capsys.readouterr().out


def test_status_command_for_one_service(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))

    exit_code = cli._cmd_status(_parse(["status", "orders-service", "--db", str(db_path)]))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "orders-service" in out
    assert "run#" in out
    assert "cumulative usage:" in out
    assert "endpoint: generated=" in out
    assert "overview: generated=" in out
    assert "backend_duration_ms=" in out


def test_status_units_shows_opaque_per_unit_results_on_request(tmp_path: Path, capsys):
    db_path = tmp_path / "units.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))
    capsys.readouterr()

    exit_code = cli._cmd_status(_parse(["status", "orders-service", "--units", "--db", str(db_path)]))

    assert exit_code == 0
    unit_lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("      unit ")]
    assert unit_lines
    assert any(re.search(r"unit endpoint key=[0-9a-f]{64} status=success attempts=1", line) for line in unit_lines)
    assert all("/orders" not in line and "prompt" not in line for line in unit_lines)


def test_status_command_global(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))

    exit_code = cli._cmd_status(_parse(["status", "--db", str(db_path)]))

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "services indexed: 3" in out
    assert "cumulative usage:" in out


def test_index_command_prints_cost_per_service(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    args = _parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)])

    cli._cmd_index(args)

    out = capsys.readouterr().out
    assert "cost_usd=" in out


def test_status_command_global_shows_recent_verifications(tmp_path: Path, capsys):
    from orbitkb.db.repositories import change_surface as change_surface_repo
    from orbitkb.db.repositories import verification as verification_repo

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    run_id = change_surface_repo.record_change_surface_run(
        conn, "task", "claude",
        {"primary": [], "secondary": [], "no_change_hint": [], "external_integrations": [], "unmapped_internal_hint": []},
    )
    verification_repo.record_verification(
        conn, run_id, repository="checkout-repo", since_commit="abc123",
        precision=0.5, recall=1.0, true_positives=["a"], false_positives=["b"], false_negatives=[],
    )

    exit_code = cli._cmd_status(_parse(["status", "--db", str(db_path)]))

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "checkout-repo" in out
    assert "precision=0.5" in out


def test_export_command_writes_markdown(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))
    out_dir = tmp_path / "docs"

    exit_code = cli._cmd_export(_parse(["export", "md", "--out", str(out_dir), "--db", str(db_path)]))

    assert exit_code == 0
    assert (out_dir / "orders-service" / "index.md").exists()
    assert "wrote" in capsys.readouterr().out


def test_export_command_writes_mermaid_diagrams(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    cli._cmd_index(_parse(["index", str(SAMPLE_ROOT), "--db", str(db_path)]))
    out_dir = tmp_path / "docs"

    exit_code = cli._cmd_export(_parse(["export", "mermaid", "--out", str(out_dir), "--db", str(db_path)]))

    assert exit_code == 0
    assert (out_dir / "topology.mmd").exists()
    assert "graph TD" in (out_dir / "topology.mmd").read_text()
    assert "wrote" in capsys.readouterr().out


def test_every_subcommand_argument_documents_itself():
    parser = cli.build_parser()
    subparsers_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for command, subparser in subparsers_action.choices.items():
        for action in subparser._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            assert action.help, f"{command}'s {action.dest!r} argument has no --help text"


def test_help_leads_with_the_index_ask_verify_mental_model(capsys):
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "index" in out and "ask" in out.lower() and "verify" in out.lower()


def test_index_help_includes_a_runnable_example(capsys):
    try:
        cli.main(["index", "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert "orbitkb index" in out


def test_index_command_exposes_external_depth_budgets():
    args = cli.build_parser().parse_args([
        "index", "/repo", "--depth-mode", "augment", "--depth-command", "codegraph",
        "--depth-timeout", "7", "--depth-max-edges", "42",
    ])

    assert (args.depth_timeout, args.depth_max_edges) == (7.0, 42)


def test_top_level_help_groups_commands_into_labeled_sections(capsys):
    try:
        cli.main(["--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out

    for title, names in cli._COMMAND_GROUPS:
        assert f"{title}:" in out
        for name in names:
            assert name in out

    global_options_start = out.index("Global Options:")
    core_workflow_start = out.index("Core Workflow:")
    assert global_options_start < core_workflow_start
    assert "-h, --help" in out[global_options_start:core_workflow_start]


def test_bare_invocation_prints_grouped_help_instead_of_erroring(capsys):
    exit_code = cli.main([])

    assert exit_code == 0
    assert capsys.readouterr().out.startswith("usage: orbitkb")


def test_version_flag_prints_the_installed_version(capsys):
    import orbitkb

    try:
        cli.main(["--version"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert orbitkb.__version__ in out


def test_analyze_command_prints_change_surface_json(tmp_path: Path, capsys, monkeypatch):
    import json

    from tests.test_change_surface import FakeBackend

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    service_id = services_repo.ensure_service(conn, "checkout-service", "/tmp/checkout", "python")
    services_repo.update_service_overview(conn, service_id, "Owns the checkout entry point.", "L")
    from orbitkb.db.repositories import search as search_repo

    search_repo.rebuild_search_index(conn)

    fake = FakeBackend({
        "primary": [{"service": "checkout-service", "reason": "owns checkout", "confidence": 0.9}],
        "secondary": [], "no_change": [],
    })
    monkeypatch.setattr(cli, "resolve_backend", lambda *a, **kw: fake)

    exit_code = cli.main(["analyze", "checkout task", "--db", str(db_path)])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["primary"][0]["service"] == "checkout-service"


def test_analyze_command_accepts_hint_services(tmp_path: Path, capsys, monkeypatch):
    import json

    from tests.test_change_surface import FakeBackend

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    services_repo.ensure_service(conn, "notification-service", "/tmp/notif", "python")

    fake = FakeBackend({
        "primary": [{"service": "notification-service", "reason": "explicitly hinted", "confidence": 0.6}],
        "secondary": [], "no_change": [],
    })
    monkeypatch.setattr(cli, "resolve_backend", lambda *a, **kw: fake)

    exit_code = cli.main([
        "analyze", "xyz unrelated", "--hint-services", "notification-service", "--db", str(db_path),
    ])

    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["primary"][0]["service"] == "notification-service"
    assert fake.calls == 1


def test_analyze_requires_repository_scope_for_duplicate_service_names(tmp_path: Path, capsys, monkeypatch):
    import json

    from tests.test_change_surface import FakeBackend

    db_path = tmp_path / "duplicate-services.db"
    conn = open_db(db_path)
    checkout_id = repositories_repo.ensure_repository(conn, "checkout-repo", "/tmp/checkout-repo")
    fulfillment_id = repositories_repo.ensure_repository(conn, "fulfillment-repo", "/tmp/fulfillment-repo")
    services_repo.ensure_service(conn, "orders", "/tmp/checkout-repo/orders", "go", repository_id=checkout_id)
    services_repo.ensure_service(conn, "orders", "/tmp/fulfillment-repo/orders", "jvm-spring", repository_id=fulfillment_id)
    fake = FakeBackend({
        "primary": [{"service": "orders", "reason": "scoped", "confidence": 0.8}],
        "secondary": [], "no_change": [],
    })
    monkeypatch.setattr(cli, "resolve_backend", lambda *a, **kw: fake)

    ambiguous_exit = cli.main(["analyze", "orders change", "--db", str(db_path)])
    scoped_exit = cli.main([
        "analyze", "orders change", "--repository", "checkout-repo", "--hint-services", "orders", "--db", str(db_path),
    ])

    captured = capsys.readouterr()
    assert ambiguous_exit == 1
    assert "ambiguous service identities; specify repository" in captured.err
    assert scoped_exit == 0
    assert json.loads(captured.out)["scope"] == {"repository": "checkout-repo"}


def test_main_dispatches_to_list_command(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    open_db(db_path)

    exit_code = cli.main(["list", "--db", str(db_path)])

    assert exit_code == 0


def test_remove_command_deletes_a_repository_and_its_services(tmp_path: Path, capsys):
    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    repository_id = repositories_repo.ensure_repository(conn, "retired-shop", "/tmp/retired-shop")
    services_repo.ensure_service(conn, "orders", "/tmp/retired-shop/orders", "python", repository_id=repository_id)

    exit_code = cli.main(["remove", "--repository", "retired-shop", "--db", str(db_path)])

    assert exit_code == 0
    assert "removed repository retired-shop and 1 service(s)" in capsys.readouterr().out
    assert repositories_repo.get_repository_by_name(conn, "retired-shop") is None
    assert services_repo.list_services(conn) == []


def test_verify_command_reports_precision_and_recall(tmp_path: Path, capsys):
    import subprocess

    from orbitkb.db.repositories import change_surface as change_surface_repo
    from orbitkb.db.repositories import repositories as repositories_repo

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo_root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo_root, check=True)
    (repo_root / "checkout-service").mkdir()
    (repo_root / "checkout-service" / "main.py").write_text("1")
    subprocess.run(["git", "add", "."], cwd=repo_root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=repo_root, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, check=True
    ).stdout.strip()

    db_path = tmp_path / "test.db"
    conn = open_db(db_path)
    repo_id = repositories_repo.ensure_repository(conn, "checkout-repo", str(repo_root))
    services_repo.ensure_service(conn, "checkout-service", str(repo_root / "checkout-service"), "python", repository_id=repo_id)
    run_id = change_surface_repo.record_change_surface_run(
        conn, "task", "claude",
        {"primary": [{"service": "checkout-service", "reason": "r", "confidence": 0.9, "evidence": []}],
         "secondary": [], "no_change_hint": [], "external_integrations": [], "unmapped_internal_hint": []},
    )

    exit_code = cli.main([
        "verify", str(run_id), "--repository", "checkout-repo", "--since", commit, "--db", str(db_path),
    ])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "precision" in out
    assert "recall" in out


def test_main_enables_faulthandler_for_native_crash_diagnostics(tmp_path: Path):
    """`index`/`update` shell out into tree-sitter, a native extension; a use-after-free
    there raises SIGBUS/SIGSEGV, which Python itself can't catch or report on -- the OS
    crash reporter only shows C frames inside the interpreter, never the Python source
    line that was executing. `faulthandler` is the one thing that can print that Python
    frame at the moment of the signal, so it has to be armed before any such command runs,
    not opt-in after the fact (a crash doesn't leave a second chance to enable it).
    """
    faulthandler.disable()
    db_path = tmp_path / "test.db"
    open_db(db_path)

    cli.main(["list", "--db", str(db_path)])

    assert faulthandler.is_enabled()


def test_main_disables_cyclic_gc_around_the_command_and_restores_it_after(tmp_path: Path, monkeypatch):
    """A real SIGBUS was found happening *inside* the cyclic garbage collector's own
    traversal (gc_collect_main -> subtype_traverse) of a long-lived, cached tree-sitter
    `Tree` -- triggered automatically by the interpreter's allocation-count threshold,
    not by any use-after-free on our end. `Tree`/`Node` objects hold no reference
    cycles, so the cyclic collector buys nothing walking them; disabling it for the
    command's duration sidesteps that call path entirely. Regular refcounting
    (tp_dealloc, not gc_collect_main) still frees every object normally the instant its
    refcount hits zero, so nothing leaks.
    """
    db_path = tmp_path / "test.db"
    open_db(db_path)
    calls = []
    monkeypatch.setattr(gc, "disable", lambda: calls.append("disable"))
    monkeypatch.setattr(gc, "enable", lambda: calls.append("enable"))

    cli.main(["list", "--db", str(db_path)])

    assert calls == ["disable", "enable"]
