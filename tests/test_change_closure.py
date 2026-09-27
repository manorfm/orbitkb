from jsonschema import validate

from orbitkb.db.repositories import change_closure_summaries as closure_summaries_repo
from orbitkb.db.repositories import ci_commands as ci_commands_repo
from orbitkb.db.repositories import repositories as repositories_repo
from orbitkb.generation.llm_harness import load_schema
from orbitkb.mcp import queries
from tests.test_change_assessment import _plan_with_a_resolved_http_unit


def test_review_change_closure_reports_ready_only_for_covered_plan_with_reported_validation(tmp_path):
    conn, plan, since_commit = _plan_with_a_resolved_http_unit(tmp_path / "repository")
    repository_id = repositories_repo.get_repository_by_name(conn, "commerce")["id"]
    ci_commands_repo.replace_ci_commands(conn, repository_id, [{
        "workflow_path": ".github/workflows/ci.yml", "kind": "test", "command": "pytest -q",
        "evidence": {"file": ".github/workflows/ci.yml", "start_line": 5, "end_line": 5},
    }])
    assert queries.record_ci_validation_result(
        conn, plan["plan_id"], "commerce", ".github/workflows/ci.yml", 5, "passed", 450,
    ) == {"ok": True, "status": "passed", "duration_ms": 450}
    assert queries.record_change_unit_validation_result(
        conn, plan["plan_id"], "http-contract:checkout-service:payments-service:POST:/authorizations", 0, "passed",
    ) == {"ok": True, "status": "passed"}
    (tmp_path / "repository" / "checkout-service" / "client.py").write_text("changed\n")

    result = queries.review_change_closure(conn, plan["plan_id"], "commerce", since_commit)

    assert result == {
        "plan_id": plan["plan_id"],
        "repository": "commerce",
        "since_commit": since_commit,
        "status": "ready_for_manual_review",
        "coverage": {"planned_units": 1, "covered_units": 1, "omitted_units": 0, "unassessable_units": 0},
        "risks": {
            "files_outside_planned_surface": 0,
            "public_error_contracts_at_risk": 0,
            "public_error_contract_breaks": 0,
        },
        "ci_validation": {
            "status": "reported_passed",
            "summary": {"total": 1, "passed": 1, "failed": 0, "pending": 0},
        },
        "manual_validation": {
            "status": "reported_passed",
            "summary": {"total": 1, "passed": 1, "failed": 0, "pending": 0},
        },
        "outstanding_ci_validation": [],
        "outstanding_change_units": {
            "omitted": [], "unassessable": [], "manual_pending": [], "manual_failed": [],
        },
    }
    validate(result, load_schema("change_closure"))
    assert closure_summaries_repo.get_summary(conn, int(plan["plan_id"].removeprefix("cp_")), repository_id) == {
        "status": "ready_for_manual_review",
        "planned_units": 1,
        "covered_units": 1,
        "omitted_units": 0,
        "unassessable_units": 0,
        "files_outside_planned_surface": 0,
        "public_error_contracts_at_risk": 0,
        "public_error_contract_breaks": 0,
    }


def test_review_change_closure_keeps_an_omitted_change_unit_as_needing_attention(tmp_path):
    conn, plan, since_commit = _plan_with_a_resolved_http_unit(tmp_path / "repository")
    (tmp_path / "repository" / "README.md").write_text("changed outside the plan\n")

    result = queries.review_change_closure(conn, plan["plan_id"], "commerce", since_commit)

    assert result["status"] == "needs_attention"
    assert result["coverage"] == {
        "planned_units": 1, "covered_units": 0, "omitted_units": 1, "unassessable_units": 0,
    }
    assert result["ci_validation"]["status"] == "no_indexed_commands"
    assert result["outstanding_change_units"] == {
        "omitted": ["http-contract:checkout-service:payments-service:POST:/authorizations"],
        "unassessable": [],
        "manual_pending": ["http-contract:checkout-service:payments-service:POST:/authorizations"],
        "manual_failed": [],
    }


def test_review_change_closure_keeps_pending_manual_checks_in_review(tmp_path):
    conn, plan, since_commit = _plan_with_a_resolved_http_unit(tmp_path / "repository")
    repository_id = repositories_repo.get_repository_by_name(conn, "commerce")["id"]
    ci_commands_repo.replace_ci_commands(conn, repository_id, [{
        "workflow_path": ".github/workflows/ci.yml", "kind": "test", "command": "pytest -q",
        "evidence": {"file": ".github/workflows/ci.yml", "start_line": 5, "end_line": 5},
    }])
    assert queries.record_ci_validation_result(
        conn, plan["plan_id"], "commerce", ".github/workflows/ci.yml", 5, "passed",
    ) == {"ok": True, "status": "passed", "duration_ms": None}
    (tmp_path / "repository" / "checkout-service" / "client.py").write_text("changed\n")

    result = queries.review_change_closure(conn, plan["plan_id"], "commerce", since_commit)

    assert result["status"] == "needs_review"
    assert result["manual_validation"] == {
        "status": "pending",
        "summary": {"total": 1, "passed": 0, "failed": 0, "pending": 1},
    }
    assert result["outstanding_change_units"]["manual_pending"] == [
        "http-contract:checkout-service:payments-service:POST:/authorizations",
    ]
    assert result["outstanding_change_units"]["manual_failed"] == []


def test_review_change_closure_identifies_pending_indexed_ci_validation(tmp_path):
    conn, plan, since_commit = _plan_with_a_resolved_http_unit(tmp_path / "repository")
    repository_id = repositories_repo.get_repository_by_name(conn, "commerce")["id"]
    ci_commands_repo.replace_ci_commands(conn, repository_id, [{
        "workflow_path": ".github/workflows/ci.yml", "kind": "test", "command": "pytest -q",
        "evidence": {"file": ".github/workflows/ci.yml", "start_line": 5, "end_line": 5},
    }])
    (tmp_path / "repository" / "checkout-service" / "client.py").write_text("changed\n")

    result = queries.review_change_closure(conn, plan["plan_id"], "commerce", since_commit)

    assert result["outstanding_ci_validation"] == [{
        "workflow_path": ".github/workflows/ci.yml", "start_line": 5, "kind": "test", "status": "pending",
    }]
