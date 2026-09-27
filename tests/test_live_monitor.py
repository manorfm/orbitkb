from orbitkb import cli
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import (
    change_plans,
    context_telemetry,
    index_runs,
    services,
)
from orbitkb.monitor import collect_snapshot, render_snapshot


def test_local_monitor_reports_an_active_index_run_and_aggregate_usage(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    service_id = services.ensure_service(conn, "payments", "/workspace/payments", "node-ts")
    index_runs.start_index_run(conn, service_id, "fake")

    snapshot = collect_snapshot(conn)

    assert snapshot["indexing"] == {
        "active": [{"service": "payments", "backend": "fake"}],
        "runs": 1,
        "files_changed": 0,
        "llm_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
    }
    rendered = render_snapshot(snapshot, color=False)
    assert "Indexing" in rendered
    assert "payments  running (fake)" in rendered
    assert "runs: 1" in rendered


def test_metrics_command_prints_a_read_only_local_snapshot(tmp_path, capsys):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)
    service_id = services.ensure_service(conn, "payments", "/workspace/payments", "node-ts")
    index_runs.start_index_run(conn, service_id, "fake")

    args = cli.build_parser().parse_args(["metrics", "--db", str(db_path), "--no-color"])

    assert cli._cmd_metrics(args) == 0
    assert "OrbitKB local monitor" in capsys.readouterr().out


def test_local_monitor_reports_redacted_agent_context_and_change_plans(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    context_telemetry.record_run(conn, {
        "change_surface_run_id": None,
        "epic_type": "feature",
        "requested_budget": 3,
        "returned_cards": 2,
        "candidate_count": 4,
        "truncated": False,
        "response_bytes": 1200,
        "estimated_tokens": 300,
        "token_measurement": "tiktoken:o200k_base",
        "included_service_ids": [],
        "omitted_service_ids": [],
        "candidate_ranking": [],
        "recommended_queries": [],
    })
    change_plans.record_plan(conn, None, "needs_decision", 2200, [{}], [])

    snapshot = collect_snapshot(conn)

    assert snapshot["agent"] == {
        "context_runs": 1,
        "context_tokens": 300,
        "truncated_contexts": 0,
        "change_plans": {"needs_decision": 1},
    }
    rendered = render_snapshot(snapshot, color=False)
    assert "Agent activity" in rendered
    assert "context briefings: 1" in rendered
    assert "change plans: needs_decision=1" in rendered
