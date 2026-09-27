from datetime import datetime, timezone

import pytest

from orbitkb import cli
from orbitkb.cli_progress import LocalIndexProgressReporter
from orbitkb.db.connection import open_db
from orbitkb.db.repositories import (
    change_closure_summaries,
    change_plans,
    ci_validation_results,
    context_telemetry,
    index_runs,
    local_activity,
    manual_validation_results,
    repositories,
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
    assert "Action needed:" not in rendered


def test_local_monitor_reports_granular_active_index_progress(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)
    progress = LocalIndexProgressReporter(db_path)

    progress.service_started("payments", total_units=3)
    progress.unit_started("payments", "POST /payments")
    progress.unit_finished("payments", "POST /payments", "ok")

    snapshot = collect_snapshot(conn)
    assert snapshot["indexing"]["active"] == [{
        "service": "payments",
        "backend": "unknown",
        "stage": "endpoint analysis",
        "completed_units": 1,
        "total_units": 3,
    }]
    assert "endpoint analysis 1/3" in render_snapshot(snapshot, color=False)

    progress.service_finished("payments")

    assert collect_snapshot(conn)["indexing"]["active"] == []


def test_metrics_command_prints_a_read_only_local_snapshot(tmp_path, capsys):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)
    service_id = services.ensure_service(conn, "payments", "/workspace/payments", "node-ts")
    index_runs.start_index_run(conn, service_id, "fake")

    args = cli.build_parser().parse_args(["metrics", "--db", str(db_path), "--no-color"])

    assert cli._cmd_metrics(args) == 0
    assert "OrbitKB local monitor" in capsys.readouterr().out


def test_metrics_alerts_only_hides_healthy_operational_details(tmp_path, capsys):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)
    service_id = services.ensure_service(conn, "payments", "/workspace/payments", "node-ts")
    index_runs.start_index_run(conn, service_id, "fake")

    args = cli.build_parser().parse_args([
        "metrics", "--alerts-only", "--db", str(db_path), "--no-color",
    ])

    assert cli._cmd_metrics(args) == 0
    rendered = capsys.readouterr().out
    assert "No alerts" in rendered
    assert "updated:" in rendered
    assert "Indexing" not in rendered
    assert "Totals" not in rendered


def test_metrics_watch_rejects_a_nonpositive_refresh_interval(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "monitor.db"
    open_db(db_path)
    args = cli.build_parser().parse_args([
        "metrics", "--watch", "--interval", "0", "--db", str(db_path), "--no-color",
    ])
    monkeypatch.setattr(cli.time, "sleep", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert cli._cmd_metrics(args) == 1
    assert "interval must be greater than zero" in capsys.readouterr().err


def test_metrics_watch_reads_interval_from_environment_and_cli_overrides_it(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    open_db(db_path)
    monkeypatch.setenv("ORBITKB_METRICS_INTERVAL", "0.25")
    observed_intervals: list[float] = []

    def stop_after_sleep(interval: float) -> None:
        observed_intervals.append(interval)
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", stop_after_sleep)
    environment_args = cli.build_parser().parse_args([
        "metrics", "--watch", "--db", str(db_path), "--no-color",
    ])
    with pytest.raises(KeyboardInterrupt):
        cli._cmd_metrics(environment_args)

    explicit_args = cli.build_parser().parse_args([
        "metrics", "--watch", "--interval", "0.5", "--db", str(db_path), "--no-color",
    ])
    with pytest.raises(KeyboardInterrupt):
        cli._cmd_metrics(explicit_args)

    assert observed_intervals == [0.25, 0.5]


def test_metrics_watch_rejects_an_invalid_environment_interval(tmp_path, capsys, monkeypatch):
    db_path = tmp_path / "monitor.db"
    open_db(db_path)
    monkeypatch.setenv("ORBITKB_METRICS_INTERVAL", "not-a-number")
    args = cli.build_parser().parse_args([
        "metrics", "--watch", "--db", str(db_path), "--no-color",
    ])
    monkeypatch.setattr(
        cli.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(AssertionError("invalid interval must not start watch")),
    )

    assert cli._cmd_metrics(args) == 1
    assert "ORBITKB_METRICS_INTERVAL must be a number" in capsys.readouterr().err


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


def test_local_monitor_summarizes_manual_and_reported_ci_validation(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    plan_id = change_plans.record_plan(conn, None, "ready", 2200, [], [{
        "id": "payments.validate", "validation": ["unit", "contract"],
    }])
    manual_validation_results.record_result(conn, plan_id, "payments.validate", 0, "passed")
    repository_id = repositories.ensure_repository(conn, "commerce", "/workspace/commerce")
    ci_validation_results.record_result(conn, plan_id, repository_id, {
        "workflow_path": ".github/workflows/ci.yml",
        "kind": "test",
        "command": "pytest",
        "start_line": 8,
        "status": "failed",
        "duration_ms": 1200,
    })

    snapshot = collect_snapshot(conn)

    assert snapshot["validation"] == {
        "manual": {"total": 2, "passed": 1, "failed": 0, "pending": 1},
        "ci_reported": {"total": 1, "passed": 0, "failed": 1},
    }
    rendered = render_snapshot(snapshot, color=False)
    assert "manual checks: passed=1 pending=1 failed=0" in rendered
    assert "CI reported: passed=0 failed=1" in rendered
    assert rendered.index("Action needed: validation=2") < rendered.index("Indexing")
    alerts_only = render_snapshot(snapshot, color=False, alerts_only=True)
    assert "Action needed: validation=2" in alerts_only
    assert "Validation" in alerts_only
    assert "Indexing" not in alerts_only


def test_local_monitor_summarizes_latest_plan_closure_quality(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    plan_id = change_plans.record_plan(conn, None, "ready", 2200, [], [])
    repository_id = repositories.ensure_repository(conn, "commerce", "/workspace/commerce")
    change_closure_summaries.record_summary(conn, plan_id, repository_id, {
        "status": "needs_attention",
        "coverage": {
            "planned_units": 4,
            "covered_units": 2,
            "omitted_units": 1,
            "unassessable_units": 1,
        },
        "risks": {
            "files_outside_planned_surface": 1,
            "public_error_contracts_at_risk": 2,
            "public_error_contract_breaks": 1,
        },
    })

    snapshot = collect_snapshot(conn)

    assert snapshot["plan_quality"] == {
        "ready_plans": 1,
        "reviewed_ready_plans": 1,
        "unreviewed_ready_plans": 0,
        "potentially_stale_ready_plans": 0,
        "closures": {"needs_attention": 1, "needs_review": 0, "ready_for_manual_review": 0},
        "coverage": {"planned_units": 4, "covered_units": 2, "omitted_units": 1, "unassessable_units": 1},
        "risks": {
            "files_outside_planned_surface": 1,
            "public_error_contracts_at_risk": 2,
            "public_error_contract_breaks": 1,
        },
    }
    rendered = render_snapshot(snapshot, color=False)
    assert "Action needed: closures=1" in rendered
    assert "quality findings=6" in rendered
    assert "coverage: 2/4 covered, 1 omitted, 1 unassessable" in rendered
    assert "contracts: 2 at risk, 1 break" in rendered
    alerts_only = render_snapshot(snapshot, color=False, alerts_only=True)
    assert "Plan quality" in alerts_only
    assert "coverage: 2/4 covered, 1 omitted, 1 unassessable" in alerts_only
    assert "contracts: 2 at risk, 1 break" in alerts_only
    assert "Totals" not in alerts_only


def test_local_monitor_separates_ready_plans_without_a_closure_review(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    reviewed_plan_id = change_plans.record_plan(conn, None, "ready", 2200, [], [])
    change_plans.record_plan(conn, None, "ready", 2200, [], [])
    change_plans.record_plan(conn, None, "needs_decision", 2200, [], [])
    repository_id = repositories.ensure_repository(conn, "commerce", "/workspace/commerce")
    change_closure_summaries.record_summary(conn, reviewed_plan_id, repository_id, {
        "status": "ready_for_manual_review",
        "coverage": {
            "planned_units": 1,
            "covered_units": 1,
            "omitted_units": 0,
            "unassessable_units": 0,
        },
        "risks": {
            "files_outside_planned_surface": 0,
            "public_error_contracts_at_risk": 0,
            "public_error_contract_breaks": 0,
        },
    })

    snapshot = collect_snapshot(conn)

    assert {
        key: snapshot["plan_quality"][key]
        for key in (
            "ready_plans",
            "reviewed_ready_plans",
            "unreviewed_ready_plans",
            "potentially_stale_ready_plans",
        )
    } == {
        "ready_plans": 2,
        "reviewed_ready_plans": 1,
        "unreviewed_ready_plans": 1,
        "potentially_stale_ready_plans": 0,
    }
    assert (
        "review coverage: ready=2 reviewed=1 awaiting=1 potentially stale=0"
        in render_snapshot(snapshot, color=False)
    )


def test_local_monitor_colors_only_actionable_plan_quality_counts(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    reviewed_plan_id = change_plans.record_plan(conn, None, "ready", 2200, [], [])
    repository_id = repositories.ensure_repository(conn, "commerce", "/workspace/commerce")
    change_closure_summaries.record_summary(conn, reviewed_plan_id, repository_id, {
        "status": "ready_for_manual_review",
        "coverage": {
            "planned_units": 1,
            "covered_units": 1,
            "omitted_units": 0,
            "unassessable_units": 0,
        },
        "risks": {
            "files_outside_planned_surface": 0,
            "public_error_contracts_at_risk": 0,
            "public_error_contract_breaks": 0,
        },
    })

    healthy_quality = render_snapshot(collect_snapshot(conn), color=True).split("Plan quality\n", 1)[1]

    assert "\033[" not in healthy_quality
    assert "quality risks: none" in healthy_quality
    assert "\n  coverage:" not in healthy_quality
    assert "\n  contracts:" not in healthy_quality

    change_plans.record_plan(conn, None, "ready", 2200, [], [])

    actionable_quality = render_snapshot(collect_snapshot(conn), color=True).split("Plan quality\n", 1)[1]

    assert "\033[33mawaiting=1\033[0m" in actionable_quality


def test_local_monitor_marks_closure_reviews_preceding_repository_indexing_as_stale(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    plan_id = change_plans.record_plan(conn, None, "ready", 2200, [], [])
    repository_id = repositories.ensure_repository(conn, "commerce", "/workspace/commerce")
    change_closure_summaries.record_summary(conn, plan_id, repository_id, {
        "status": "ready_for_manual_review",
        "coverage": {
            "planned_units": 1,
            "covered_units": 1,
            "omitted_units": 0,
            "unassessable_units": 0,
        },
        "risks": {
            "files_outside_planned_surface": 0,
            "public_error_contracts_at_risk": 0,
            "public_error_contract_breaks": 0,
        },
    })
    conn.execute(
        "UPDATE repositories SET updated_at = ? WHERE id = ?",
        ("2099-01-01T00:00:00+00:00", repository_id),
    )
    conn.commit()

    snapshot = collect_snapshot(conn)

    assert snapshot["plan_quality"]["potentially_stale_ready_plans"] == 1


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


def test_local_monitor_treats_missing_closure_summary_table_as_no_reviews(tmp_path):
    conn = open_db(tmp_path / "legacy-monitor.db")
    conn.execute("DROP TABLE change_plan_closure_summaries")
    conn.commit()

    assert collect_snapshot(conn)["plan_quality"]["closures"] == {
        "needs_attention": 0,
        "needs_review": 0,
        "ready_for_manual_review": 0,
    }


def test_local_monitor_hides_activity_left_by_a_terminated_process(tmp_path, monkeypatch):
    conn = open_db(tmp_path / "monitor.db")
    conn.execute(
        "INSERT INTO local_activity_runs (operation, process_id, started_at) VALUES (?, ?, ?)",
        ("plan_change", 999_999, "2026-09-27T00:00:00+00:00"),
    )
    conn.commit()
    monkeypatch.setattr(local_activity, "_process_exists", lambda _: False)

    assert collect_snapshot(conn)["agent"]["active_operations"] == []


def test_new_local_activity_prunes_abandoned_rows(tmp_path, monkeypatch):
    db_path = tmp_path / "monitor.db"
    conn = open_db(db_path)
    conn.execute(
        "INSERT INTO local_activity_runs (operation, process_id, started_at) VALUES (?, ?, ?)",
        ("plan_change", 999_999, "2026-09-27T00:00:00+00:00"),
    )
    conn.commit()
    monkeypatch.setattr(local_activity, "_process_exists", lambda _: False)

    with track_local_activity(db_path, "get_change_context"):
        rows = conn.execute("SELECT operation FROM local_activity_runs").fetchall()

        assert [row["operation"] for row in rows] == ["get_change_context"]


def test_monitor_redraws_only_for_state_changes_unless_work_is_active(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    idle = collect_snapshot(conn)

    assert should_render_snapshot(idle, previous_state=None) is True
    assert should_render_snapshot(idle, previous_state=snapshot_state_key(idle)) is False

    active = {**idle, "agent": {**idle["agent"], "active_operations": [{
        "operation": "plan_change", "started_at": "2026-09-27T00:00:00+00:00",
    }]}}
    assert should_render_snapshot(active, previous_state=snapshot_state_key(active)) is True


def test_alerts_only_monitor_ignores_healthy_updates_and_hidden_activity(tmp_path):
    conn = open_db(tmp_path / "monitor.db")
    idle = collect_snapshot(conn)
    previous_state = snapshot_state_key(idle, alerts_only=True)
    healthy_update = {
        **idle,
        "indexing": {**idle["indexing"], "runs": 1},
        "agent": {**idle["agent"], "active_operations": [{
            "operation": "plan_change", "started_at": "2026-09-27T00:00:00+00:00",
        }]},
    }

    assert should_render_snapshot(healthy_update, previous_state, alerts_only=True) is False

    actionable_update = {
        **healthy_update,
        "validation": {
            **healthy_update["validation"],
            "manual": {"total": 1, "passed": 0, "pending": 1, "failed": 0},
        },
    }

    assert should_render_snapshot(actionable_update, previous_state, alerts_only=True) is True


def test_alerts_only_monitor_shows_its_last_render_time(tmp_path):
    conn = open_db(tmp_path / "monitor.db")

    rendered = render_snapshot(
        collect_snapshot(conn),
        color=False,
        current_time=datetime(2026, 9, 27, 3, 45, 6, tzinfo=timezone.utc),
        alerts_only=True,
        include_updated_at=True,
    )

    assert "updated: 2026-09-27 03:45:06 UTC" in rendered


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
