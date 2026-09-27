"""Read-only local metrics for the terminal monitor.

The monitor intentionally derives its state from existing, redacted records.
It owns no background worker and never writes to the knowledge base.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from orbitkb.db.repositories import local_activity


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
    return {
        "indexing": {
            "active": [
                {"service": row["service"] or "unknown service", "backend": row["backend"]}
                for row in active_rows
            ],
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
    }


def render_snapshot(
    snapshot: dict[str, Any], color: bool, current_time: datetime | None = None,
) -> str:
    """Render a stable, human-readable snapshot without terminal dependencies."""
    indexing = snapshot["indexing"]
    agent = snapshot["agent"]
    lines = [
        _paint("OrbitKB local monitor", "36", color),
        "─" * 40,
        "Indexing",
    ]
    if indexing["active"]:
        for active in indexing["active"]:
            status = _paint(f"running ({active['backend']})", "33", color)
            lines.append(
                f"  {active['service']}  {status}"
            )
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
    ])
    return "\n".join(lines)


def _paint(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _plan_statuses(statuses: dict[str, int]) -> str:
    return ", ".join(f"{status}={count}" for status, count in statuses.items()) or "none"


def snapshot_state_key(snapshot: dict[str, Any]) -> str:
    """Return a stable key for state changes, excluding render-only elapsed time."""
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def should_render_snapshot(snapshot: dict[str, Any], previous_state: str | None) -> bool:
    """Refresh on a state transition, or once per interval while duration advances."""
    return previous_state != snapshot_state_key(snapshot) or bool(snapshot["agent"]["active_operations"])


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
