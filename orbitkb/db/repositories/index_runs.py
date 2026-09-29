"""The `index_runs` table: one row per `index`/`update` invocation for one
service, used by `orbitkb status` to show indexing history and token/cost usage."""
from __future__ import annotations

import os
import sqlite3

from ._util import now


def acquire_service_lock(conn: sqlite3.Connection, lock_key: str) -> bool:
    try:
        conn.execute("INSERT INTO service_index_locks (lock_key, acquired_at, process_id) VALUES (?, ?, ?)", (lock_key, now(), os.getpid()))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT process_id FROM service_index_locks WHERE lock_key = ?", (lock_key,)).fetchone()
        if row and row["process_id"] and not _process_exists(row["process_id"]):
            conn.execute("DELETE FROM service_index_locks WHERE lock_key = ?", (lock_key,))
            conn.commit()
            return acquire_service_lock(conn, lock_key)
        return False


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def release_service_lock(conn: sqlite3.Connection, lock_key: str) -> None:
    conn.execute("DELETE FROM service_index_locks WHERE lock_key = ?", (lock_key,))
    conn.commit()


def start_index_run(conn: sqlite3.Connection, service_id: int | None, backend: str) -> int:
    cur = conn.execute(
        "INSERT INTO index_runs (service_id, started_at, backend, status) VALUES (?, ?, ?, 'partial')",
        (service_id, now(), backend),
    )
    conn.commit()
    return cur.lastrowid


def finish_index_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    files_changed: int,
    llm_calls: int,
    notes: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    llm_invocations: int | None = None,
) -> None:
    conn.execute(
        """UPDATE index_runs
           SET finished_at = ?, status = ?, files_changed = ?, llm_calls = ?, notes = ?,
               input_tokens = ?, output_tokens = ?, cost_usd = ?, llm_invocations = ?
           WHERE id = ?""",
        (now(), status, files_changed, llm_calls, notes, input_tokens, output_tokens, cost_usd, llm_invocations, run_id),
    )
    conn.commit()


def recover_unfinished_runs(conn: sqlite3.Connection, service_id: int) -> int:
    """Close attempts abandoned by a terminated process before a fresh retry."""
    cur = conn.execute(
        """UPDATE index_runs SET finished_at = ?, status = 'failed',
           notes = 'index process ended before completion; superseded by a new attempt'
           WHERE service_id = ? AND finished_at IS NULL""",
        (now(), service_id),
    )
    conn.commit()
    return cur.rowcount


def recent_index_runs(conn: sqlite3.Connection, service_id: int | None = None, limit: int = 10) -> list[sqlite3.Row]:
    if service_id is not None:
        return conn.execute(
            "SELECT * FROM index_runs WHERE service_id = ? ORDER BY id DESC LIMIT ?", (service_id, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM index_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def usage_totals(conn: sqlite3.Connection, service_id: int | None = None) -> dict:
    """Cumulative token/cost usage across every index_runs row (optionally scoped to
    one service) — the number `orbitkb status` prints alongside the recent-runs list,
    since that list is capped and shouldn't be mistaken for the full total."""
    if service_id is not None:
        row = conn.execute(
            """SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                      SUM(cost_usd) AS cost_usd
               FROM index_runs WHERE service_id = ?""",
            (service_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, SUM(cost_usd) AS cost_usd "
            "FROM index_runs"
        ).fetchone()
    return {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"], "cost_usd": row["cost_usd"]}
