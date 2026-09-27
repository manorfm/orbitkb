from datetime import datetime, timezone

import pytest

from orbitkb import cli
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import (
    change_plans,
    context_telemetry,
    index_runs,
    services,
)
from orbitkb.mcp.activity import track_local_activity
from orbitkb.monitor import (
    collect_snapshot,
    render_snapshot,
    should_render_snapshot,
    snapshot_state_key,
)


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


def test_metrics_watch_rejects_a_nonpositive_refresh_interval(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "monitor.db"
    open_db(db_path)
    args = cli.build_parser().parse_args([
        "metrics", "--watch", "--interval", "0", "--db", str(db_path), "--no-color",
    ])
    monkeypatch.setattr(cli.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert cli._cmd_metrics(args) == 1
    assert "interval must be greater than zero" in capsys.readouterr().err


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
        "active_operations": [],
        "context_runs": 1,
        "context_tokens": 300,
        "truncated_contexts": 0,
        "change_plans": {"needs_decision": 1},
    }
    rendered = render_snapshot(snapshot, color=False)
    assert "Agent activity" in rendered
    assert "context briefings: 1" in rendered
    assert "change plans: needs_decision=1" in rendered


def test_local_monitor_reports_only_an_in_flight_agent_operation(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)

    with track_local_activity(db_path, "plan_change"):
        snapshot = collect_snapshot(conn)

        assert snapshot["agent"]["active_operations"] == [{
            "operation": "plan_change",
            "started_at": snapshot["agent"]["active_operations"][0]["started_at"],
        }]
        assert "running now: plan_change (" in render_snapshot(snapshot, color=False)

    assert collect_snapshot(conn)["agent"]["active_operations"] == []


def test_local_activity_is_removed_when_the_operation_fails(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)

    with pytest.raises(RuntimeError, match="failed operation"):
        with track_local_activity(db_path, "plan_change"):
            raise RuntimeError("failed operation")

    assert collect_snapshot(conn)["agent"]["active_operations"] == []


def test_local_monitor_treats_missing_ephemeral_activity_table_as_idle(tmp_path):
    conn = open_db(tmp_path / "legacy-monitor.db")
    conn.execute("DROP TABLE local_activity_runs")
    conn.commit()

    assert collect_snapshot(conn)["agent"]["active_operations"] == []


def test_monitor_redraws_only_for_state_changes_unless_work_is_active(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    idle = collect_snapshot(conn)

    assert should_render_snapshot(idle, previous_state=None) is True
    assert should_render_snapshot(idle, previous_state=snapshot_state_key(idle)) is False

    active = {**idle, "agent": {**idle["agent"], "active_operations": [{
        "operation": "plan_change", "started_at": "2026-09-27T00:00:00+00:00",
    }]}}
    assert should_render_snapshot(active, previous_state=snapshot_state_key(active)) is True


def test_local_monitor_renders_active_operation_duration(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    snapshot = collect_snapshot(conn)
    snapshot["agent"]["active_operations"] = [{
        "operation": "plan_change", "started_at": "2026-09-27T00:00:00+00:00",
    }]

    rendered = render_snapshot(
        snapshot, color=False, current_time=datetime(2026, 9, 27, 0, 1, 5, tzinfo=timezone.utc),
    )

    assert "running now: plan_change (1m 5s)" in rendered
