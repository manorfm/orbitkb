"""Privacy-safe, latest-only summaries of completed advisory plan closure reviews."""
from __future__ import annotations

import sqlite3

from ._util import now


def record_summary(
    conn: sqlite3.Connection, plan_id: int, repository_id: int, closure: dict,
) -> None:
    """Replace one plan/repository's compact closure metrics without retaining a diff."""
    coverage = closure["coverage"]
    risks = closure["risks"]
    conn.execute(
        """INSERT INTO change_plan_closure_summaries
           (plan_id, repository_id, status, planned_units, covered_units, omitted_units,
            unassessable_units, files_outside_planned_surface,
            public_error_contracts_at_risk, public_error_contract_breaks, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(plan_id, repository_id) DO UPDATE SET
               status = excluded.status,
               planned_units = excluded.planned_units,
               covered_units = excluded.covered_units,
               omitted_units = excluded.omitted_units,
               unassessable_units = excluded.unassessable_units,
               files_outside_planned_surface = excluded.files_outside_planned_surface,
               public_error_contracts_at_risk = excluded.public_error_contracts_at_risk,
               public_error_contract_breaks = excluded.public_error_contract_breaks,
               recorded_at = excluded.recorded_at""",
        (
            plan_id, repository_id, closure["status"], coverage["planned_units"],
            coverage["covered_units"], coverage["omitted_units"], coverage["unassessable_units"],
            risks["files_outside_planned_surface"], risks["public_error_contracts_at_risk"],
            risks["public_error_contract_breaks"], now(),
        ),
    )
    conn.commit()


def get_summary(conn: sqlite3.Connection, plan_id: int, repository_id: int) -> dict | None:
    row = conn.execute(
        """SELECT status, planned_units, covered_units, omitted_units, unassessable_units,
                  files_outside_planned_surface, public_error_contracts_at_risk,
                  public_error_contract_breaks
           FROM change_plan_closure_summaries
           WHERE plan_id = ? AND repository_id = ?""",
        (plan_id, repository_id),
    ).fetchone()
    return dict(row) if row is not None else None
