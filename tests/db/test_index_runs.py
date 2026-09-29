"""Unit tests for db.repositories.index_runs: run lifecycle plus token/cost usage
accounting (finish_index_run's new columns and usage_totals)."""
from pathlib import Path

from orbitkb.db.connection import open_db
from orbitkb.db.repositories import index_runs as repository
from orbitkb.db.repositories import services as services_repo


def test_finish_index_run_persists_usage_columns(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    run_id = repository.start_index_run(conn, service_id, "claude")

    repository.finish_index_run(
        conn, run_id, "ok", 3, 2, None,
        input_tokens=1000, output_tokens=200, cost_usd=0.05, llm_invocations=3,
    )

    run = repository.recent_index_runs(conn, service_id, limit=1)[0]
    assert run["input_tokens"] == 1000
    assert run["output_tokens"] == 200
    assert run["cost_usd"] == 0.05
    assert run["llm_invocations"] == 3


def test_finish_index_run_defaults_usage_to_none(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    run_id = repository.start_index_run(conn, service_id, "claude")

    repository.finish_index_run(conn, run_id, "ok", 0, 0, None)

    run = repository.recent_index_runs(conn, service_id, limit=1)[0]
    assert run["input_tokens"] is None
    assert run["cost_usd"] is None


def test_unit_usage_is_scoped_to_one_run(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    first = repository.start_index_run(conn, service_id, "claude")
    second = repository.start_index_run(conn, service_id, "claude")

    repository.record_unit_usage(conn, first, "endpoint", 2, 3, False, 100, 20, 0.1)
    repository.record_unit_usage(conn, second, "endpoint", 0, 0, False, None, None, None)

    first_usage = repository.list_unit_usage(conn, first)
    second_usage = repository.list_unit_usage(conn, second)
    assert [(row["generated_units"], row["llm_invocations"], row["cost_usd"]) for row in first_usage] == [
        (2, 3, 0.1),
    ]
    assert [(row["generated_units"], row["llm_invocations"], row["cost_usd"]) for row in second_usage] == [
        (0, 0, None),
    ]


def test_usage_totals_sums_across_runs_for_one_service(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    other_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "node-ts")

    run1 = repository.start_index_run(conn, service_id, "claude")
    repository.finish_index_run(conn, run1, "ok", 1, 1, None, input_tokens=100, output_tokens=10, cost_usd=0.01)
    run2 = repository.start_index_run(conn, service_id, "claude")
    repository.finish_index_run(conn, run2, "ok", 1, 1, None, input_tokens=200, output_tokens=20, cost_usd=0.02)
    other_run = repository.start_index_run(conn, other_id, "claude")
    repository.finish_index_run(conn, other_run, "ok", 1, 1, None, input_tokens=9999, output_tokens=999, cost_usd=9.99)

    totals = repository.usage_totals(conn, service_id)

    assert totals["input_tokens"] == 300
    assert totals["output_tokens"] == 30
    assert totals["cost_usd"] == 0.03


def test_usage_totals_sums_across_the_whole_db_when_no_service_given(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    other_id = services_repo.ensure_service(conn, "payments-service", "/tmp/payments", "node-ts")
    run1 = repository.start_index_run(conn, service_id, "claude")
    repository.finish_index_run(conn, run1, "ok", 1, 1, None, input_tokens=100, output_tokens=10, cost_usd=0.01)
    run2 = repository.start_index_run(conn, other_id, "claude")
    repository.finish_index_run(conn, run2, "ok", 1, 1, None, input_tokens=200, output_tokens=20, cost_usd=0.02)

    totals = repository.usage_totals(conn)

    assert totals["input_tokens"] == 300
    assert totals["cost_usd"] == 0.03


def test_usage_totals_are_none_when_no_runs_recorded_usage(tmp_path: Path):
    conn = open_db(tmp_path / "test.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    run_id = repository.start_index_run(conn, service_id, "claude")
    repository.finish_index_run(conn, run_id, "ok", 1, 1, None)

    totals = repository.usage_totals(conn, service_id)

    assert totals["input_tokens"] is None
    assert totals["cost_usd"] is None


def test_recover_unfinished_runs_marks_abandoned_attempts_failed(tmp_path: Path):
    conn = open_db(tmp_path / "runs.db")
    service_id = services_repo.ensure_service(conn, "orders-service", "/tmp/orders", "python")
    abandoned = repository.start_index_run(conn, service_id, "claude")

    assert repository.recover_unfinished_runs(conn, service_id) == 1
    row = conn.execute("SELECT status, finished_at, notes FROM index_runs WHERE id = ?", (abandoned,)).fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert "superseded" in row["notes"]


def test_service_lock_is_exclusive_and_released(tmp_path: Path):
    conn = open_db(tmp_path / "locks.db")

    assert repository.acquire_service_lock(conn, "repo:orders") is True
    assert repository.acquire_service_lock(conn, "repo:orders") is False
    repository.release_service_lock(conn, "repo:orders")
    assert repository.acquire_service_lock(conn, "repo:orders") is True


def test_service_lock_recovers_a_dead_local_process(tmp_path: Path, monkeypatch):
    conn = open_db(tmp_path / "dead-lock.db")
    conn.execute("INSERT INTO service_index_locks (lock_key, acquired_at, process_id) VALUES ('repo:orders', 'now', 999999)")
    conn.commit()
    monkeypatch.setattr(repository, "_process_exists", lambda _: False)

    assert repository.acquire_service_lock(conn, "repo:orders") is True
