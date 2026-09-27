"""Read-only local metrics for the terminal monitor.

The monitor intentionally derives its state from existing, redacted records.
It owns no background worker and never writes to the knowledge base.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from orbitkb.db.repositories import local_activity, local_index_progress


def collect_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the compact current state needed by the local terminal view."""
    active_rows = conn.execute(
        """SELECT services.name AS service, index_runs.backend
           FROM index_runs
           LEFT JOIN services ON services.id = index_runs.service_id
           WHERE index_runs.finished_at IS NULL
           ORDER BY index_runs.started_at, index_runs.id"""
    ).fetchall()
    totals = conn.execute(
        """SELECT COUNT(*) AS runs, COALESCE(SUM(files_changed), 0) AS files_changed,
                  COALESCE(SUM(llm_calls), 0) AS llm_calls,
                  COALESCE(SUM(input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(cost_usd), 0) AS cost_usd
           FROM index_runs"""
    ).fetchone()
    context = conn.execute(
        """SELECT COUNT(*) AS runs, COALESCE(SUM(estimated_tokens), 0) AS tokens,
                  COALESCE(SUM(truncated), 0) AS truncated
           FROM context_budget_runs"""
    ).fetchone()
    plan_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM change_plan_runs GROUP BY status ORDER BY status"
    ).fetchall()
    progress_by_service = {
        progress["service"]: progress for progress in local_index_progress.list_active(conn)
    }
    active_indexing: list[dict[str, Any]] = []
    indexed_services: set[str] = set()
    for row in active_rows:
        service = row["service"] or "unknown service"
        indexed_services.add(service)
        entry: dict[str, Any] = {"service": service, "backend": row["backend"] or "unknown"}
        if progress := progress_by_service.get(service):
            entry.update({
                "stage": progress["stage"].replace("_", " "),
                "completed_units": progress["completed_units"],
                "total_units": progress["total_units"],
            })
        active_indexing.append(entry)
    for service, progress in progress_by_service.items():
        if service in indexed_services:
            continue
        active_indexing.append({
            "service": service,
            "backend": "unknown",
            "stage": progress["stage"].replace("_", " "),
            "completed_units": progress["completed_units"],
            "total_units": progress["total_units"],
        })
    validation = _validation_summary(conn)
    plan_quality = _plan_quality_summary(conn)
    return {
        "indexing": {
            "active": active_indexing,
            "runs": totals["runs"],
            "files_changed": totals["files_changed"],
            "llm_calls": totals["llm_calls"],
            "input_tokens": totals["input_tokens"],
            "output_tokens": totals["output_tokens"],
            "cost_usd": totals["cost_usd"],
        },
        "agent": {
            "active_operations": local_activity.list_active(conn),
            "context_runs": context["runs"],
            "context_tokens": context["tokens"],
            "truncated_contexts": context["truncated"],
            "change_plans": {row["status"]: row["count"] for row in plan_rows},
        },
        "validation": validation,
        "plan_quality": plan_quality,
    }


def render_snapshot(
    snapshot: dict[str, Any], color: bool, current_time: datetime | None = None,
    alerts_only: bool = False, include_updated_at: bool = False, watch_interval: float | None = None,
    watch_interval_source: str | None = None,
) -> str:
    """Render a stable, human-readable snapshot without terminal dependencies."""
    if alerts_only:
        return _render_alert_snapshot(snapshot, color, current_time, include_updated_at)
    indexing = snapshot["indexing"]
    agent = snapshot["agent"]
    validation = snapshot["validation"]
    plan_quality = snapshot["plan_quality"]
    manual = validation["manual"]
    ci_reported = validation["ci_reported"]
    closure_counts = plan_quality["closures"]
    coverage = plan_quality["coverage"]
    risks = plan_quality["risks"]
    lines = [
        _paint("OrbitKB local monitor", "36", color),
        "─" * 40,
    ]
    if watch_interval is not None:
        source = f" ({watch_interval_source})" if watch_interval_source else ""
        lines.append(f"refresh: every {watch_interval:g}s{source}")
    if action_summary := _action_summary(validation, plan_quality, color):
        lines.append(action_summary)
    lines.append("Indexing")
    if indexing["active"]:
        for active in indexing["active"]:
            status = _paint(f"running ({active['backend']})", "33", color)
            progress = _index_progress(active)
            lines.append(f"  {active['service']}  {status}{progress}")
    else:
        lines.append(f"  {_paint('idle', '32', color)}")
    lines.extend([
        "Totals",
        f"  runs: {indexing['runs']}",
        f"  files changed: {indexing['files_changed']}",
        f"  LLM calls: {indexing['llm_calls']}",
        f"  tokens: {indexing['input_tokens']} in / {indexing['output_tokens']} out",
        f"  indexed cost: ${indexing['cost_usd']:.4f}",
        "Agent activity",
        f"  running now: {_active_operations(agent['active_operations'], current_time) or 'none'}",
        f"  context briefings: {agent['context_runs']}",
        f"  context tokens: {agent['context_tokens']}",
        f"  truncated briefings: {agent['truncated_contexts']}",
        f"  change plans: {_plan_statuses(agent['change_plans'])}",
        "Validation",
        "  manual checks: " + " ".join([
            f"passed={manual['passed']}",
            _paint_if_positive(f"pending={manual['pending']}", manual["pending"], "33", color),
            _paint_if_positive(f"failed={manual['failed']}", manual["failed"], "31", color),
        ]),
        "  CI reported: " + " ".join([
            f"passed={ci_reported['passed']}",
            _paint_if_positive(f"failed={ci_reported['failed']}", ci_reported["failed"], "31", color),
        ]),
        "Plan quality",
        "  " + _review_coverage_line(plan_quality, color),
        "  " + _closure_line(closure_counts, color),
    ])
    lines.extend(_plan_quality_detail_lines(coverage, risks, color))
    return "\n".join(lines)


def _paint(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _paint_if_positive(text: str, value: int, code: str, enabled: bool) -> str:
    return _paint(text, code, enabled and value > 0)


def _render_alert_snapshot(
    snapshot: dict[str, Any], color: bool, current_time: datetime | None, include_updated_at: bool,
) -> str:
    validation = snapshot["validation"]
    plan_quality = snapshot["plan_quality"]
    action_summary = _action_summary(validation, plan_quality, color)
    lines = [_paint("OrbitKB local monitor", "36", color), "─" * 40]
    if action_summary is None:
        lines.append("No alerts")
        if include_updated_at:
            lines.append(_updated_at_line(current_time))
        return "\n".join(lines)
    lines.append(action_summary)
    manual = validation["manual"]
    ci_reported = validation["ci_reported"]
    if _validation_action_count(validation):
        lines.append("Validation")
        if manual["pending"] or manual["failed"]:
            lines.append("  manual checks: " + " ".join([
                _paint_if_positive(f"pending={manual['pending']}", manual["pending"], "33", color),
                _paint_if_positive(f"failed={manual['failed']}", manual["failed"], "31", color),
            ]))
        if ci_reported["failed"]:
            lines.append(
                "  CI reported: "
                + _paint_if_positive(f"failed={ci_reported['failed']}", ci_reported["failed"], "31", color)
            )
    if (
        _review_action_count(plan_quality)
        or _closure_action_count(plan_quality["closures"])
        or _has_quality_risks(plan_quality["coverage"], plan_quality["risks"])
    ):
        lines.append("Plan quality")
        if _review_action_count(plan_quality):
            lines.append("  " + _review_coverage_line(plan_quality, color))
        closures = plan_quality["closures"]
        if _closure_action_count(closures):
            lines.append("  " + _closure_alert_line(closures, color))
        lines.extend(_plan_quality_detail_lines(
            plan_quality["coverage"], plan_quality["risks"], color, alerts_only=True,
        ))
    if include_updated_at:
        lines.append(_updated_at_line(current_time))
    return "\n".join(lines)


def _review_coverage_line(plan_quality: dict[str, Any], color: bool) -> str:
    return "review coverage: " + " ".join([
        f"ready={plan_quality['ready_plans']}",
        f"reviewed={plan_quality['reviewed_ready_plans']}",
        _paint_if_positive(
            f"awaiting={plan_quality['unreviewed_ready_plans']}",
            plan_quality["unreviewed_ready_plans"], "33", color,
        ),
        _paint_if_positive(
            f"potentially stale={plan_quality['potentially_stale_ready_plans']}",
            plan_quality["potentially_stale_ready_plans"], "33", color,
        ),
    ])


def _closure_line(closures: dict[str, int], color: bool) -> str:
    return "closures: " + " ".join([
        _paint_if_positive(f"attention={closures['needs_attention']}", closures["needs_attention"], "31", color),
        _paint_if_positive(f"review={closures['needs_review']}", closures["needs_review"], "33", color),
        f"ready={closures['ready_for_manual_review']}",
    ])


def _closure_alert_line(closures: dict[str, int], color: bool) -> str:
    values: list[str] = []
    if closures["needs_attention"]:
        values.append(_paint(f"attention={closures['needs_attention']}", "31", color))
    if closures["needs_review"]:
        values.append(_paint(f"review={closures['needs_review']}", "33", color))
    return "closures: " + " ".join(values)


def _updated_at_line(current_time: datetime | None) -> str:
    timestamp = current_time or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    else:
        timestamp = timestamp.astimezone(timezone.utc)
    return f"updated: {timestamp:%Y-%m-%d %H:%M:%S UTC}"


def _action_summary(
    validation: dict[str, dict[str, int]], plan_quality: dict[str, Any], color: bool,
) -> str | None:
    manual = validation["manual"]
    ci_reported = validation["ci_reported"]
    validation_count = _validation_action_count(validation)
    review_count = _review_action_count(plan_quality)
    closures = plan_quality["closures"]
    closure_count = _closure_action_count(closures)
    quality_count = _quality_risk_count(plan_quality["coverage"], plan_quality["risks"])
    parts: list[str] = []
    if validation_count:
        code = "31" if manual["failed"] or ci_reported["failed"] else "33"
        parts.append(_paint_if_positive(f"validation={validation_count}", validation_count, code, color))
    if review_count:
        parts.append(_paint_if_positive(f"plan reviews={review_count}", review_count, "33", color))
    if closure_count:
        code = "31" if closures["needs_attention"] else "33"
        parts.append(_paint_if_positive(f"closures={closure_count}", closure_count, code, color))
    if quality_count:
        code = "31" if plan_quality["risks"]["public_error_contract_breaks"] else "33"
        parts.append(_paint_if_positive(f"quality findings={quality_count}", quality_count, code, color))
    return f"Action needed: {' '.join(parts)}" if parts else None


def _validation_action_count(validation: dict[str, dict[str, int]]) -> int:
    manual = validation["manual"]
    return manual["pending"] + manual["failed"] + validation["ci_reported"]["failed"]


def _review_action_count(plan_quality: dict[str, Any]) -> int:
    return plan_quality["unreviewed_ready_plans"] + plan_quality["potentially_stale_ready_plans"]


def _closure_action_count(closures: dict[str, int]) -> int:
    return closures["needs_attention"] + closures["needs_review"]


def _has_quality_risks(coverage: dict[str, int], risks: dict[str, int]) -> bool:
    return _quality_risk_count(coverage, risks) > 0


def _quality_risk_count(coverage: dict[str, int], risks: dict[str, int]) -> int:
    return sum((
        coverage["omitted_units"],
        coverage["unassessable_units"],
        risks["files_outside_planned_surface"],
        risks["public_error_contracts_at_risk"],
        risks["public_error_contract_breaks"],
    ))


def _plan_quality_detail_lines(
    coverage: dict[str, int], risks: dict[str, int], color: bool, alerts_only: bool = False,
) -> list[str]:
    if not _has_quality_risks(coverage, risks):
        return [] if alerts_only else ["  quality risks: none"]
    if alerts_only:
        lines: list[str] = []
        if coverage["omitted_units"] or coverage["unassessable_units"]:
            values = [f"{coverage['covered_units']}/{coverage['planned_units']} covered"]
            if coverage["omitted_units"]:
                values.append(_paint(f"{coverage['omitted_units']} omitted", "33", color))
            if coverage["unassessable_units"]:
                values.append(_paint(f"{coverage['unassessable_units']} unassessable", "33", color))
            lines.append("  coverage: " + ", ".join(values))
        if risks["public_error_contracts_at_risk"] or risks["public_error_contract_breaks"]:
            values = []
            if risks["public_error_contracts_at_risk"]:
                values.append(_paint(f"{risks['public_error_contracts_at_risk']} at risk", "33", color))
            if risks["public_error_contract_breaks"]:
                values.append(_paint(f"{risks['public_error_contract_breaks']} break", "31", color))
            lines.append("  contracts: " + ", ".join(values))
        if risks["files_outside_planned_surface"]:
            lines.append("  " + _paint(
                f"outside planned surface: {risks['files_outside_planned_surface']}", "33", color,
            ))
        return lines
    return [
        "  coverage: " + " ".join([
            f"{coverage['covered_units']}/{coverage['planned_units']} covered,",
            _paint_if_positive(f"{coverage['omitted_units']} omitted,", coverage["omitted_units"], "33", color),
            _paint_if_positive(
                f"{coverage['unassessable_units']} unassessable", coverage["unassessable_units"], "33", color,
            ),
        ]),
        "  contracts: " + " ".join([
            _paint_if_positive(
                f"{risks['public_error_contracts_at_risk']} at risk,",
                risks["public_error_contracts_at_risk"], "33", color,
            ),
            _paint_if_positive(
                f"{risks['public_error_contract_breaks']} break",
                risks["public_error_contract_breaks"], "31", color,
            ),
        ]),
        "  " + _paint_if_positive(
            f"outside planned surface: {risks['files_outside_planned_surface']}",
            risks["files_outside_planned_surface"], "33", color,
        ),
    ]


def _plan_statuses(statuses: dict[str, int]) -> str:
    return ", ".join(f"{status}={count}" for status, count in statuses.items()) or "none"


def _index_progress(active: dict[str, Any]) -> str:
    if not {"stage", "completed_units", "total_units"} <= active.keys():
        return ""
    stage = active["stage"]
    return f" · {stage} {active['completed_units']}/{active['total_units']}"


def _validation_summary(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    ready_plans = conn.execute(
        "SELECT id, change_units_json FROM change_plan_runs WHERE status = 'ready'"
    ).fetchall()
    expected_manual_checks: set[tuple[int, str, int]] = set()
    for plan in ready_plans:
        try:
            change_units = json.loads(plan["change_units_json"])
        except json.JSONDecodeError:
            continue
        if not isinstance(change_units, list):
            continue
        for unit in change_units:
            if not isinstance(unit, dict) or not isinstance(unit.get("id"), str):
                continue
            checks = unit.get("validation")
            if isinstance(checks, list):
                expected_manual_checks.update(
                    (plan["id"], unit["id"], index) for index, _check in enumerate(checks)
                )
    manual_results = {
        (row["plan_id"], row["change_unit_id"], row["check_index"]): row["status"]
        for row in conn.execute(
            """SELECT result.plan_id, result.change_unit_id, result.check_index, result.status
               FROM change_plan_manual_validation_results AS result
               JOIN change_plan_runs AS plan ON plan.id = result.plan_id
               WHERE plan.status = 'ready'"""
        ).fetchall()
    }
    manual_statuses = [manual_results.get(check, "pending") for check in expected_manual_checks]
    ci = conn.execute(
        """SELECT result.status, COUNT(*) AS count
           FROM change_plan_ci_validation_results AS result
           JOIN change_plan_runs AS plan ON plan.id = result.plan_id
           WHERE plan.status = 'ready'
           GROUP BY result.status"""
    ).fetchall()
    ci_counts = {row["status"]: row["count"] for row in ci}
    return {
        "manual": {
            "total": len(manual_statuses),
            "passed": sum(status == "passed" for status in manual_statuses),
            "failed": sum(status == "failed" for status in manual_statuses),
            "pending": sum(status == "pending" for status in manual_statuses),
        },
        "ci_reported": {
            "total": sum(ci_counts.values()),
            "passed": ci_counts.get("passed", 0),
            "failed": ci_counts.get("failed", 0),
        },
    }


def _plan_quality_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    ready_plan_count = conn.execute(
        "SELECT COUNT(*) AS count FROM change_plan_runs WHERE status = 'ready'"
    ).fetchone()["count"]
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS count FROM change_plan_closure_summaries GROUP BY status"
        ).fetchall()
        totals = conn.execute(
            """SELECT COALESCE(SUM(planned_units), 0) AS planned_units,
                      COALESCE(SUM(covered_units), 0) AS covered_units,
                      COALESCE(SUM(omitted_units), 0) AS omitted_units,
                      COALESCE(SUM(unassessable_units), 0) AS unassessable_units,
                      COALESCE(SUM(files_outside_planned_surface), 0) AS files_outside_planned_surface,
                      COALESCE(SUM(public_error_contracts_at_risk), 0) AS public_error_contracts_at_risk,
                      COALESCE(SUM(public_error_contract_breaks), 0) AS public_error_contract_breaks
               FROM change_plan_closure_summaries"""
        ).fetchone()
        reviewed_ready_plans = conn.execute(
            """SELECT COUNT(DISTINCT plan.id) AS count
               FROM change_plan_runs AS plan
               JOIN change_plan_closure_summaries AS summary ON summary.plan_id = plan.id
               WHERE plan.status = 'ready'"""
        ).fetchone()["count"]
        potentially_stale_ready_plans = conn.execute(
            """SELECT COUNT(DISTINCT plan.id) AS count
               FROM change_plan_runs AS plan
               JOIN change_plan_closure_summaries AS summary ON summary.plan_id = plan.id
               JOIN repositories AS repository ON repository.id = summary.repository_id
               WHERE plan.status = 'ready'
                 AND summary.recorded_at < repository.updated_at"""
        ).fetchone()["count"]
    except sqlite3.OperationalError as exc:
        if "no such table: change_plan_closure_summaries" not in str(exc):
            raise
        return _empty_plan_quality(ready_plan_count)
    closures = {row["status"]: row["count"] for row in rows}
    return {
        "ready_plans": ready_plan_count,
        "reviewed_ready_plans": reviewed_ready_plans,
        "unreviewed_ready_plans": ready_plan_count - reviewed_ready_plans,
        "potentially_stale_ready_plans": potentially_stale_ready_plans,
        "closures": {
            status: closures.get(status, 0)
            for status in ("needs_attention", "needs_review", "ready_for_manual_review")
        },
        "coverage": {
            key: totals[key]
            for key in ("planned_units", "covered_units", "omitted_units", "unassessable_units")
        },
        "risks": {
            key: totals[key]
            for key in (
                "files_outside_planned_surface",
                "public_error_contracts_at_risk",
                "public_error_contract_breaks",
            )
        },
    }


def _empty_plan_quality(ready_plan_count: int = 0) -> dict[str, Any]:
    return {
        "ready_plans": ready_plan_count,
        "reviewed_ready_plans": 0,
        "unreviewed_ready_plans": ready_plan_count,
        "potentially_stale_ready_plans": 0,
        "closures": {
            "needs_attention": 0,
            "needs_review": 0,
            "ready_for_manual_review": 0,
        },
        "coverage": {
            "planned_units": 0,
            "covered_units": 0,
            "omitted_units": 0,
            "unassessable_units": 0,
        },
        "risks": {
            "files_outside_planned_surface": 0,
            "public_error_contracts_at_risk": 0,
            "public_error_contract_breaks": 0,
        },
    }


def snapshot_state_key(snapshot: dict[str, Any], alerts_only: bool = False) -> str:
    """Return a stable key for visible state changes, excluding elapsed time."""
    if alerts_only:
        return render_snapshot(snapshot, color=False, alerts_only=True)
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def should_render_snapshot(
    snapshot: dict[str, Any], previous_state: str | None, alerts_only: bool = False,
) -> bool:
    """Refresh on a state transition, or once per interval while duration advances."""
    return (
        previous_state != snapshot_state_key(snapshot, alerts_only=alerts_only)
        or (not alerts_only and bool(snapshot["agent"]["active_operations"]))
    )


def _active_operations(operations: list[dict[str, str]], current_time: datetime | None) -> str:
    now = current_time or datetime.now(timezone.utc)
    return ", ".join(
        f"{operation['operation']} ({_elapsed(operation['started_at'], now)})"
        for operation in operations
    )


def _elapsed(started_at: str, current_time: datetime) -> str:
    try:
        elapsed_seconds = max(0, int((current_time - datetime.fromisoformat(started_at)).total_seconds()))
    except (TypeError, ValueError):
        return "unknown duration"
    minutes, seconds = divmod(elapsed_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"
